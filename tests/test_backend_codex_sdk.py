"""Codex `sdk` backend tests (fake-module based; no real SDK, no model call).

The Codex SDK drives the local app-server and surfaces a turn as a stream of
thread items. These are exercised with FAKE item objects (``.type`` +
type-specific fields, ``.thread_id``) so the event mapper, option mapping, probe,
and provider-session capture are covered without installing ``openai-codex`` or
calling a model. It stays message-only for anything it cannot classify (it does
not fake ``codex exec --json`` parity).
"""

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from agent_collab import backends
from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.codex_sdk import (
    CodexSdkBackend,
    CodexSdkRunner,
    _default_item_stream,
    _map_sdk_options,
    iter_codex_events,
    sandbox_member_name,
)
from agent_collab.config import AgentConfig

AGENT = AgentConfig(id="codex", type="codex", backend="sdk")


class _Item:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def _aiter(items):
    for item in items:
        yield item


def _stream_factory(items, *, error=None):
    def factory(agent, options, workdir, prompt):
        if error is not None:
            raise error
        return _aiter(items)

    return factory


def _run(items, *, verbose=False, options=None, error=None):
    runner = CodexSdkRunner(
        AGENT, verbose, options or {}, item_stream=_stream_factory(items, error=error)
    )

    async def collect():
        return [event async for event in runner.run("do a thing", Path("."))]

    return asyncio.run(collect())


class CodexEventMappingTests(unittest.TestCase):
    def test_message_command_and_file_change_items_map_to_standard_events(self):
        items = [
            _Item(type="command_execution", command="pytest -q"),
            _Item(type="file_change", path="hello.py", changes=[{"path": "hello.py"}]),
            _Item(type="agent_message", text="Ran the tests and edited hello.py."),
        ]
        events = _run(items)
        by_type = {}
        for event in events:
            by_type.setdefault(event.type, []).append(event)

        self.assertEqual(by_type["command"][0].source, "tool")
        self.assertIn("pytest", by_type["command"][0].text)
        self.assertEqual(by_type["file_change"][0].source, "tool")
        self.assertIn("hello.py", by_type["file_change"][0].text)
        self.assertTrue(any(e.source == "codex" and "Ran the tests" in e.text for e in by_type["message"]))

    def test_reasoning_hidden_unless_verbose(self):
        item = _Item(type="reasoning", text="thinking about the plan")
        quiet = _run([item], verbose=False)
        self.assertFalse(any("thinking about the plan" in (e.text or "") for e in quiet))
        loud = _run([item], verbose=True)
        self.assertTrue(any(e.type == "status" and "thinking about the plan" in (e.text or "") for e in loud))

    def test_degrades_to_message_only_for_final_response(self):
        events = _run([_Item(type="agent_message", text="Here is the summary.")])
        self.assertFalse(any(e.source == "tool" for e in events))
        self.assertTrue(any(e.type == "message" and e.source == "codex" for e in events))

    def test_error_item_maps_to_error_event(self):
        events = _run([_Item(type="error", message="app-server crashed")])
        self.assertTrue(any(e.type == "error" and "app-server crashed" in e.text for e in events))

    def test_failed_command_item_stays_a_command_not_a_bare_error(self):
        # A command that ran but exited non-zero carries is_error; it must still
        # surface as its command event (with the command string), not be swallowed
        # into a generic "codex sdk error".
        events = _run([_Item(type="command_execution", command="pytest -q", is_error=True)])
        commands = [e for e in events if e.type == "command"]
        self.assertEqual(len(commands), 1)
        self.assertIn("pytest", commands[0].text)
        self.assertFalse(any(e.type == "error" for e in events))

    def test_unclassified_error_flag_still_becomes_error(self):
        events = _run([_Item(type="unknown_thing", is_error=True)])
        self.assertTrue(any(e.type == "error" for e in events))

    def test_missing_import_at_stream_open_surfaces_error_event(self):
        events = _run([], error=BackendUnavailable("codex", "sdk", "openai_codex is not importable", "hint"))
        self.assertTrue(any(e.type == "error" and "not importable" in e.text for e in events))


class CodexSessionCaptureTests(unittest.TestCase):
    def test_thread_id_captured_uniformly_regardless_of_verbose(self):
        items = [
            _Item(type="agent_message", text="working", thread_id="thread-9"),
            _Item(type="agent_message", text="done", thread_id="thread-9"),
        ]
        for verbose in (False, True):
            events = _run(items, verbose=verbose)
            captured = [e for e in events if (e.raw or {}).get("provider_session_id") == "thread-9"]
            self.assertEqual(len(captured), 1, f"verbose={verbose}")  # once, not per item
            raw = captured[0].raw
            self.assertEqual(raw["provider_session_kind"], "thread")
            self.assertEqual(raw["agent_id"], AGENT.id)
            self.assertEqual(captured[0].source, "codex")


class CodexOptionMappingTests(unittest.TestCase):
    def test_map_sdk_options_keeps_only_supported_keys(self):
        mapped = _map_sdk_options(
            {"model": "gpt-5-codex", "sandbox": "workspace-write", "profile": "p", "approval_policy": "never"}
        )
        self.assertEqual(mapped, {"model": "gpt-5-codex", "sandbox": "workspace-write"})

    def test_sandbox_member_name_maps_cli_values_to_enum_members(self):
        self.assertEqual(sandbox_member_name("read-only"), "read_only")
        self.assertEqual(sandbox_member_name("workspace-write"), "workspace_write")
        self.assertEqual(sandbox_member_name("danger-full-access"), "full_access")
        self.assertIsNone(sandbox_member_name("nonesuch"))


class CodexBackendSurfaceTests(unittest.TestCase):
    def test_registered_pair_and_honest_capabilities(self):
        self.assertTrue(backends.is_registered("codex", "sdk"))
        caps = backends.capabilities_for("codex", "sdk")
        self.assertEqual(caps.to_dict(), {"resume": False, "interrupt": False, "tool_gate": False})

    def test_probe_reports_unavailable_with_install_hint(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            health = CodexSdkBackend().probe()
        self.assertEqual(health.status, "unavailable")
        self.assertIn("openai-codex", health.reason)

    def test_settings_summary_has_package_and_options(self):
        summary = CodexSdkBackend().settings_summary(AGENT, {"model": "gpt-5-codex", "sandbox": "read-only"})
        self.assertEqual(summary["backend"], "sdk")
        self.assertEqual(summary["package"], "openai-codex")
        self.assertEqual(summary["options"], {"model": "gpt-5-codex", "sandbox": "read-only"})

    def test_default_stream_raises_backend_unavailable_when_module_absent(self):
        import sys

        with mock.patch.dict(sys.modules, {"openai_codex": None}):
            with self.assertRaises(BackendUnavailable) as ctx:
                _default_item_stream(AGENT, {}, Path("."), "hi")
        self.assertIn("openai-codex", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
