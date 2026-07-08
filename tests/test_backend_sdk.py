"""Antigravity `sdk` backend tests.

The SDK's API shapes were CONFIRMED live against google-antigravity 0.1.5 (see
tests/fixtures/antigravity/sdk-introspection.json); only a live *chat* is blocked
(it needs a Gemini API key agent-collab does not manage). So the event mapper is
driven by a FAKE agent factory built to the confirmed shapes (async
`response.text()`, `response.thoughts`/`response.tool_calls` properties,
`ToolCall.name`/`.args`, `Agent.conversation_id`), and the missing-extra path is
fully real.
"""

import asyncio
import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.backends.antigravity_sdk import (
    AntigravitySdkBackend,
    AntigravitySdkRunner,
    _default_agent_factory,
    map_antigravity_turn,
)
from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.health import gemini_api_key_credentials
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


def _sample():
    return json.loads((FIXTURES / "sdk-response-sample.json").read_text(encoding="utf-8"))


class _FakeToolCall:
    """Shaped like google.antigravity.types.ToolCall (name/args/canonical_path)."""

    def __init__(self, name, args, canonical_path=None):
        self.name = name
        self.args = args
        self.canonical_path = canonical_path


class _FakeResponse:
    """Shaped like ChatResponse: async text(), sync thoughts/tool_calls props."""

    def __init__(self, blob):
        self._text = blob.get("text")
        self.thoughts = blob.get("thoughts")
        self.tool_calls = [_FakeToolCall(**tc) for tc in blob.get("tool_calls", [])]

    async def text(self):
        return self._text


class _FakeAgent:
    def __init__(self, response, conversation_id=None):
        self._response = response
        self.conversation_id = conversation_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, prompt):
        return self._response


def _factory_for(response, conversation_id=None):
    def factory(agent, options, workdir):
        return _FakeAgent(response, conversation_id=conversation_id)

    return factory


async def _collect(runner, prompt="do a thing"):
    return [event async for event in runner.run(prompt, Path("."))]


def _run(response, *, verbose=False, options=None, conversation_id=None):
    runner = AntigravitySdkRunner(
        AGENT, verbose, options or {}, agent_factory=_factory_for(response, conversation_id)
    )
    return asyncio.run(_collect(runner))


class SdkEventMappingTests(unittest.TestCase):
    def test_text_and_typed_tool_calls_map_to_standard_events(self):
        events = _run(_FakeResponse(_sample()["chat_response"]))
        by_type = {}
        for event in events:
            by_type.setdefault(event.type, []).append(event)

        # final text -> antigravity message
        self.assertTrue(any(e.source == "antigravity" and "Created hello.py" in e.text for e in by_type["message"]))
        # CREATE_FILE -> file_change, RUN_COMMAND -> command, VIEW_FILE -> tool_call
        self.assertTrue(by_type.get("file_change"))
        self.assertTrue(by_type.get("command"))
        self.assertTrue(by_type.get("tool_call"))
        tool_events = by_type["file_change"] + by_type["command"] + by_type["tool_call"]
        self.assertTrue(all(e.source == "tool" for e in tool_events))
        # tool call text/raw carry the real BuiltinTools name + args (not `input`).
        file_change = by_type["file_change"][0]
        self.assertEqual(file_change.raw["name"], "CREATE_FILE")
        self.assertIn("path", file_change.raw["args"])

    def test_reasoning_hidden_unless_verbose_and_never_leaks_signature(self):
        quiet = _run(_FakeResponse(_sample()["chat_response"]), verbose=False)
        self.assertFalse(any(e.type == "status" and "create the file" in (e.text or "") for e in quiet))

        loud = _run(_FakeResponse(_sample()["chat_response"]), verbose=True)
        self.assertTrue(any(e.type == "status" and "create the file" in (e.text or "") for e in loud))
        # thoughts carry reasoning text only, never an opaque signature.
        for event in loud:
            self.assertNotIn("signature", event.raw or {})

    def test_degrades_to_message_only_without_tool_calls(self):
        events = _run(_FakeResponse(_sample()["text_only_response"]))
        self.assertFalse(any(e.source == "tool" for e in events))
        self.assertTrue(any(e.type == "message" and e.source == "antigravity" for e in events))

    def test_conversation_id_captured_in_verbose_transcript_only(self):
        # The SDK exposes Agent.conversation_id (confirmed). We capture it in the
        # transcript under verbose; nothing resumes it and there is no structured
        # agent_sessions field this stage.
        quiet = _run(_FakeResponse(_sample()["text_only_response"]), conversation_id="conv-123")
        self.assertFalse(any("conversation_id" in (e.raw or {}) for e in quiet))

        loud = _run(_FakeResponse(_sample()["text_only_response"]), verbose=True, conversation_id="conv-123")
        self.assertTrue(any((e.raw or {}).get("conversation_id") == "conv-123" for e in loud))

        field_names = {f.name for f in dataclasses.fields(SessionState)}
        self.assertNotIn("agent_sessions", field_names)

    def test_map_turn_is_message_only_when_no_tool_calls(self):
        events = list(map_antigravity_turn("just text", None, [], verbose=False))
        self.assertEqual([e.type for e in events], ["message"])

    def test_builtin_tool_enum_name_is_classified(self):
        # ToolCall.name may be a BuiltinTools enum, not a str.
        import enum

        class BuiltinTools(enum.Enum):
            EDIT_FILE = "edit_file"

        events = list(
            map_antigravity_turn("", None, [_FakeToolCall(BuiltinTools.EDIT_FILE, {"path": "x"})], verbose=False)
        )
        self.assertEqual(events[0].type, "file_change")
        self.assertEqual(events[0].raw["name"], "EDIT_FILE")


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


class SdkCredentialsTests(unittest.TestCase):
    """The sdk backend authenticates with GEMINI_API_KEY, not ~/.gemini OAuth.
    Absence must be `unknown` (warn), never `missing` (block) — other auth paths
    exist (config api_key, Vertex/ADC), so we must never block a working setup."""

    def test_gemini_api_key_present_is_ok(self):
        self.assertEqual(gemini_api_key_credentials({"GEMINI_API_KEY": "abc"}), "ok")

    def test_gemini_api_key_absent_is_unknown_not_missing(self):
        self.assertEqual(gemini_api_key_credentials({}), "unknown")

    def test_probe_credentials_track_the_env_and_never_report_missing(self):
        present = object()
        with mock.patch("importlib.util.find_spec", return_value=present):
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "abc"}):
                self.assertEqual(AntigravitySdkBackend().probe().credentials, "ok")
            env_no_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
            with mock.patch.dict(os.environ, env_no_key, clear=True):
                health = AntigravitySdkBackend().probe()
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.credentials, "unknown")  # never "missing" -> never blocks


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
