"""Antigravity `sdk` backend tests.

The real SDK could not be installed in the spike environment, so these tests
drive the runner with a FAKE agent factory shaped like the hypothesis in
tests/fixtures/antigravity/sdk-hypothesis.json. The only fully-real path is the
missing-extra behaviour (the module genuinely is not importable here).
"""

import asyncio
import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.backends.antigravity_sdk import (
    AntigravitySdkBackend,
    AntigravitySdkRunner,
    _default_agent_factory,
    map_sdk_response,
)
from agent_collab.backends.base import BackendUnavailable
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import SessionState
from agent_collab.options import (
    StartOptionsError,
    build_session_settings,
    validate_start_backends,
    validate_start_options,
)

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
AGENT = AgentConfig(id="antigravity", type="antigravity", backend="sdk")


def _hypothesis():
    return json.loads((FIXTURES / "sdk-hypothesis.json").read_text(encoding="utf-8"))


class _FakeToolCall:
    def __init__(self, name, input):
        self.name = name
        self.input = input


class _FakeResponse:
    def __init__(self, blob):
        self.text = blob.get("text")
        self.thoughts = blob.get("thoughts")
        self.tool_calls = [_FakeToolCall(**tc) for tc in blob.get("tool_calls", [])]


class _FakeAgent:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, prompt):
        return self._response


def _factory_for(response):
    def factory(agent, options, workdir):
        return _FakeAgent(response)

    return factory


async def _collect(runner, prompt="do a thing"):
    return [event async for event in runner.run(prompt, Path("."))]


def _run(response, *, verbose=False, options=None):
    runner = AntigravitySdkRunner(AGENT, verbose, options or {}, agent_factory=_factory_for(response))
    return asyncio.run(_collect(runner))


class SdkEventMappingTests(unittest.TestCase):
    def test_text_and_typed_tool_calls_map_to_standard_events(self):
        events = _run(_FakeResponse(_hypothesis()["chat_response"]))
        by_type = {}
        for event in events:
            by_type.setdefault(event.type, []).append(event)

        # final text -> antigravity message
        self.assertTrue(any(e.source == "antigravity" and "print statement" in e.text for e in by_type["message"]))
        # write_file -> file_change, run_command -> command, read_file -> tool_call
        self.assertTrue(by_type.get("file_change"))
        self.assertTrue(by_type.get("command"))
        self.assertTrue(by_type.get("tool_call"))
        self.assertTrue(all(e.source == "tool" for e in by_type["file_change"] + by_type["command"] + by_type["tool_call"]))

    def test_reasoning_hidden_unless_verbose_and_never_leaks_signature(self):
        quiet = _run(_FakeResponse(_hypothesis()["chat_response"]), verbose=False)
        self.assertFalse(any(e.type == "status" and "quick change" in (e.text or "") for e in quiet))

        loud = _run(_FakeResponse(_hypothesis()["chat_response"]), verbose=True)
        self.assertTrue(any(e.type == "status" and "quick change" in (e.text or "") for e in loud))
        # thoughts carry reasoning text only, never an opaque signature.
        for event in loud:
            self.assertNotIn("signature", event.raw or {})

    def test_degrades_to_message_only_without_tool_calls(self):
        events = _run(_FakeResponse(_hypothesis()["text_only_response"]))
        self.assertFalse(any(e.source == "tool" for e in events))
        self.assertTrue(any(e.type == "message" and e.source == "antigravity" for e in events))

    def test_no_conversation_id_is_captured(self):
        events = _run(_FakeResponse(_hypothesis()["chat_response"]))
        for event in events:
            self.assertNotIn("conversation_id", event.raw or {})
        # agent_sessions is not shipped this stage (spike could not confirm an id).
        field_names = {f.name for f in dataclasses.fields(SessionState)}
        self.assertNotIn("agent_sessions", field_names)

    def test_map_sdk_response_is_message_only_when_tool_calls_attribute_absent(self):
        class _MinimalResponse:
            text = "just text"

        events = list(map_sdk_response(_MinimalResponse(), verbose=False))
        self.assertEqual([e.type for e in events], ["message"])


class SdkMissingExtraTests(unittest.TestCase):
    """Hermetic regardless of whether the real extra happens to be installed:
    force the module absent so the missing-extra path is exercised without
    reading real ~/.gemini credentials."""

    def test_probe_reports_unavailable_with_install_hint(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            health = AntigravitySdkBackend().probe()
        self.assertEqual(health.status, "unavailable")
        self.assertIn("antigravity-sdk", health.reason)

    def test_default_factory_raises_backend_unavailable(self):
        with mock.patch.dict(sys.modules, {"google.antigravity": None}):
            with self.assertRaises(BackendUnavailable) as ctx:
                _default_agent_factory(AGENT, {}, Path("."))
        self.assertIn("antigravity-sdk", str(ctx.exception))

    def test_runner_with_default_factory_emits_actionable_error_event(self):
        runner = AntigravitySdkBackend().create_runner(AGENT, False, {})
        with mock.patch.dict(sys.modules, {"google.antigravity": None}):
            events = asyncio.run(_collect(runner))
        self.assertTrue(any(e.type == "error" and "antigravity-sdk" in e.text for e in events))


class SdkSelectionTests(unittest.TestCase):
    def _config(self):
        return CollaborationConfig(
            agents={"ag": AgentConfig(id="ag", type="antigravity", backend="sdk")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["ag"])},
        )

    def test_sdk_resolves_for_antigravity_without_health_gating(self):
        selection = validate_start_backends(self._config(), "solo")
        self.assertEqual(selection.agent_backends, {"ag": "sdk"})

    def test_explicit_mode_option_is_rejected_on_sdk_backend(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(self._config(), "solo", antigravity_options={"mode": "plan"})
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "antigravity_options.mode")
        self.assertIn("sdk", detail["message"])

    def test_inferred_cli_mode_does_not_block_sdk_selection(self):
        # The built-in antigravity agent carries `-p --mode accept-edits` (cli
        # posture). Selecting the sdk backend must NOT be blocked by that inferred
        # mode — only an *explicit* antigravity_options.mode is rejected on sdk.
        config = CollaborationConfig(
            agents={
                "ag": AgentConfig(
                    id="ag",
                    type="antigravity",
                    command="agy",
                    args=["-p", "--mode", "accept-edits"],
                    backend="sdk",
                )
            },
            workflows={"solo": WorkflowConfig(id="solo", sequence=["ag"])},
        )
        # validate_start_options infers mode from the cli args...
        normalized = validate_start_options(config, "solo")
        self.assertEqual(normalized["antigravity_options"].get("mode"), "accept-edits")
        # ...but the backend validator keys off the explicit request (none here),
        # so sdk selection is not blocked.
        selection = validate_start_backends(config, "solo", request_backend=None, antigravity_options={})
        self.assertEqual(selection.agent_backends, {"ag": "sdk"})

    def test_sdk_settings_do_not_advertise_inferred_cli_mode(self):
        config = CollaborationConfig(
            agents={
                "ag": AgentConfig(
                    id="ag", type="antigravity", command="agy", args=["-p", "--mode", "accept-edits"], backend="sdk"
                )
            },
            workflows={"solo": WorkflowConfig(id="solo", sequence=["ag"])},
        )
        normalized = validate_start_options(config, "solo")
        settings = build_session_settings(config, "solo", normalized, agent_backends={"ag": "sdk"})
        entry = settings["agents"]["ag"]
        self.assertEqual(entry["backend"], "sdk")
        self.assertNotIn("mode", entry)  # mode is cli-only; not shown for sdk

    def test_settings_summary_replaces_command_preview_for_sdk(self):
        config = self._config()
        settings = build_session_settings(
            config, "solo", {"antigravity_options": {"model": "gemini-x"}}, agent_backends={"ag": "sdk"}
        )
        entry = settings["agents"]["ag"]
        self.assertEqual(entry["backend"], "sdk")
        self.assertNotIn("command_preview", entry)
        self.assertEqual(entry["backend_summary"]["package"], "google-antigravity")
        self.assertEqual(entry["backend_summary"]["options"], {"model": "gemini-x"})


if __name__ == "__main__":
    unittest.main()
