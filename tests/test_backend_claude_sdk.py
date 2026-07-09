"""Claude `sdk` backend tests (fake-module based; no real SDK, no model call).

The Claude Agent SDK's ``query(...)`` yields typed messages whose blocks are
``TextBlock``/``ToolUseBlock``/``ThinkingBlock`` and whose terminal message
carries ``session_id``/``is_error``. These are exercised with FAKE message
objects shaped to those attributes so the event mapper, option mapping, probe,
and provider-session capture are all covered without installing
``claude-agent-sdk`` or calling a model.
"""

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from agent_collab import backends
from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.claude_sdk import (
    ClaudeSdkBackend,
    ClaudeSdkRunner,
    _default_message_stream,
    _map_sdk_options,
    build_claude_agent_options,
    iter_claude_events,
)
from agent_collab.config import AgentConfig

AGENT = AgentConfig(id="claude", type="claude", backend="sdk")


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Message:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def _aiter(items):
    for item in items:
        yield item


def _stream_factory(messages, *, error=None):
    def factory(agent, options, workdir, prompt):
        if error is not None:
            raise error
        return _aiter(messages)

    return factory


def _run(messages, *, verbose=False, options=None, error=None):
    runner = ClaudeSdkRunner(
        AGENT, verbose, options or {}, message_stream=_stream_factory(messages, error=error)
    )

    async def collect():
        return [event async for event in runner.run("do a thing", Path("."))]

    return asyncio.run(collect())


def _assistant(blocks):
    return _Message(content=list(blocks))


class ClaudeEventMappingTests(unittest.TestCase):
    def test_message_and_typed_tool_uses_map_to_standard_events(self):
        message = _assistant(
            [
                _Block(text="Here is the plan."),
                _Block(name="Read", input={"path": "a.py"}, id="t1"),
                _Block(name="Bash", input={"command": "pytest -q"}, id="t2"),
                _Block(name="Edit", input={"path": "a.py"}, id="t3"),
            ]
        )
        events = _run([message])
        by_type = {}
        for event in events:
            by_type.setdefault(event.type, []).append(event)

        self.assertTrue(any(e.source == "claude" and "Here is the plan." in e.text for e in by_type["message"]))
        # Read -> tool_call, Bash -> command, Edit -> file_change.
        self.assertEqual(by_type["tool_call"][0].raw["name"], "Read")
        self.assertEqual(by_type["command"][0].raw["name"], "Bash")
        self.assertEqual(by_type["file_change"][0].raw["name"], "Edit")
        tool_events = by_type["tool_call"] + by_type["command"] + by_type["file_change"]
        self.assertTrue(all(e.source == "tool" for e in tool_events))
        # tool raw carries the SDK `input`, not a fabricated `args`.
        self.assertIn("path", by_type["file_change"][0].raw["input"])

    def test_thinking_hidden_unless_verbose_and_never_leaks_signature(self):
        message = _assistant([_Block(thinking="secret plan text", signature="OPAQUE_SIG"), _Block(text="Done.")])
        quiet = _run([message], verbose=False)
        self.assertFalse(any(e.type == "status" and "secret plan text" in (e.text or "") for e in quiet))
        loud = _run([message], verbose=True)
        self.assertTrue(any(e.type == "status" and "secret plan text" in (e.text or "") for e in loud))
        # The opaque verification signature must never appear anywhere.
        for event in quiet + loud:
            self.assertNotIn("OPAQUE_SIG", event.text or "")
            self.assertNotIn("signature", event.raw or {})

    def test_degrades_to_message_only(self):
        events = _run([_assistant([_Block(text="Just prose.")])])
        self.assertFalse(any(e.source == "tool" for e in events))
        self.assertTrue(any(e.type == "message" and e.source == "claude" for e in events))

    def test_result_is_error_maps_to_error_event(self):
        result = _Message(is_error=True, result="the model refused", subtype="error")
        events = _run([result])
        self.assertTrue(any(e.type == "error" and "the model refused" in e.text for e in events))

    def test_missing_import_at_stream_open_surfaces_error_event(self):
        events = _run([], error=BackendUnavailable("claude", "sdk", "claude_agent_sdk is not importable", "hint"))
        self.assertTrue(any(e.type == "error" and "not importable" in e.text for e in events))


class ClaudeSessionCaptureTests(unittest.TestCase):
    def test_session_id_captured_uniformly_regardless_of_verbose(self):
        messages = [
            _Message(subtype="init", data={"session_id": "sess-1"}),
            _assistant([_Block(text="hello")]),
            _Message(is_error=False, subtype="success", session_id="sess-1", total_cost_usd=0.01),
        ]
        for verbose in (False, True):
            events = _run(messages, verbose=verbose)
            captured = [e for e in events if (e.raw or {}).get("provider_session_id") == "sess-1"]
            self.assertEqual(len(captured), 1, f"verbose={verbose}")  # emitted once, not per message
            raw = captured[0].raw
            self.assertEqual(raw["provider_session_kind"], "session")
            self.assertEqual(raw["agent_id"], AGENT.id)
            self.assertEqual(captured[0].source, "claude")

    def test_iter_events_never_yields_session_capture(self):
        # Session capture is the runner's job (it attributes the agent id); the
        # pure mapper only emits transcript prose/tools.
        events = list(iter_claude_events(_Message(session_id="x", subtype="init"), verbose=False))
        self.assertFalse(any((e.raw or {}).get("provider_session_id") for e in events))


class ClaudeOptionMappingTests(unittest.TestCase):
    def test_map_sdk_options_keeps_only_supported_keys(self):
        mapped = _map_sdk_options(
            {"model": "opus", "permission_mode": "acceptEdits", "thinking_level": "high"}
        )
        self.assertEqual(mapped, {"model": "opus", "permission_mode": "acceptEdits"})

    def test_build_agent_options_passes_model_and_predictable_settings(self):
        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        build_claude_agent_options(FakeOptions, {"model": "sonnet", "thinking_level": "high"}, Path("/w"))
        self.assertEqual(captured["model"], "sonnet")
        self.assertNotIn("thinking_level", captured)  # cli-only; not mapped to the SDK
        self.assertEqual(captured["setting_sources"], [])  # runs do not implicitly load fs settings
        self.assertEqual(captured["cwd"], "/w")

    def test_build_agent_options_degrades_when_kwargs_unsupported(self):
        class StrictOptions:
            def __init__(self, **kwargs):
                if "setting_sources" in kwargs or "cwd" in kwargs:
                    raise TypeError("unexpected kwarg")
                self.kwargs = kwargs

        options = build_claude_agent_options(StrictOptions, {"model": "opus"}, Path("/w"))
        self.assertEqual(options.kwargs, {"model": "opus"})


class ClaudeBackendSurfaceTests(unittest.TestCase):
    def test_registered_pair_and_honest_capabilities(self):
        self.assertTrue(backends.is_registered("claude", "sdk"))
        caps = backends.capabilities_for("claude", "sdk")
        self.assertEqual(caps.to_dict(), {"resume": False, "interrupt": False, "tool_gate": False})

    def test_probe_reports_unavailable_with_install_hint(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            health = ClaudeSdkBackend().probe()
        self.assertEqual(health.status, "unavailable")
        self.assertIn("claude-agent-sdk", health.reason)

    def test_settings_summary_has_package_and_options(self):
        summary = ClaudeSdkBackend().settings_summary(AGENT, {"model": "opus", "permission_mode": "default"})
        self.assertEqual(summary["backend"], "sdk")
        self.assertEqual(summary["package"], "claude-agent-sdk")
        self.assertEqual(summary["options"], {"model": "opus", "permission_mode": "default"})
        self.assertEqual(summary["setting_sources"], "none")

    def test_default_stream_raises_backend_unavailable_when_module_absent(self):
        import sys

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            with self.assertRaises(BackendUnavailable) as ctx:
                _default_message_stream(AGENT, {}, Path("."), "hi")
        self.assertIn("claude-agent-sdk", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
