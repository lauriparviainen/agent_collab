"""Antigravity `sdk` backend tests.

The SDK's API shapes were CONFIRMED live against google-antigravity 0.1.5 (see
tests/fixtures/antigravity/sdk-introspection.json); only a live *chat* is blocked
(it needs a Gemini API key agent-collab does not manage). The event mapper is
driven by a fake agent built to the confirmed protocol: async ``resolve()``
returns typed Text/Thought/ToolCall/ToolResult values, thoughts/tool_calls are
independent async cursor properties, and usage/conversation ids are available
after resolution. No test calls a model.
"""

import asyncio
import dataclasses
import json
import os
import sys
import types
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


class Text:
    def __init__(self, step_index, text):
        self.step_index = step_index
        self.text = text


class Thought:
    def __init__(self, step_index, text, signature=None):
        self.step_index = step_index
        self.text = text
        self.signature = signature


class ToolCall:
    """Same public fields as google.antigravity.types.ToolCall."""

    def __init__(self, name, args=None, id=None, canonical_path=None):
        self.name = name
        self.args = args or {}
        self.id = id
        self.canonical_path = canonical_path


class ToolResult:
    """Same public fields as google.antigravity.types.ToolResult."""

    def __init__(self, name, id=None, result=None, error=None, exception=None):
        self.name = name
        self.id = id
        self.result = result
        self.error = error
        self.exception = exception


class UsageMetadata:
    def __init__(self, **values):
        for field in (
            "prompt_token_count",
            "cached_content_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "total_token_count",
        ):
            setattr(self, field, values.get(field))


def _typed_chunk(blob):
    chunk_type = blob["type"]
    values = {key: value for key, value in blob.items() if key != "type"}
    if chunk_type == "Text":
        return Text(**values)
    if chunk_type == "Thought":
        signature = values.get("signature")
        if isinstance(signature, str):
            values["signature"] = signature.encode("utf-8")
        return Thought(**values)
    if chunk_type == "ToolCall":
        return ToolCall(**values)
    if chunk_type == "ToolResult":
        return ToolResult(**values)
    raise AssertionError(f"unknown fake chunk type: {chunk_type}")


class _FakeResponse:
    """ChatResponse protocol with async cursors that must not be sync-iterated."""

    def __init__(self, blob):
        self._chunks = [_typed_chunk(chunk) for chunk in blob.get("chunks", [])]
        usage = blob.get("usage_metadata")
        self.usage_metadata = UsageMetadata(**usage) if usage else None
        self.resolve_calls = 0
        self.cursor_accesses = 0

    async def resolve(self):
        self.resolve_calls += 1
        return list(self._chunks)

    @property
    def thoughts(self):
        self.cursor_accesses += 1

        async def cursor():
            for chunk in self._chunks:
                if isinstance(chunk, Thought):
                    yield chunk.text

        return cursor()

    @property
    def tool_calls(self):
        self.cursor_accesses += 1

        async def cursor():
            for chunk in self._chunks:
                if isinstance(chunk, ToolCall):
                    yield chunk

        return cursor()


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
    def test_typed_buffer_maps_text_calls_results_and_errors(self):
        response = _FakeResponse(_sample()["chat_response"])
        events = _run(response)
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
        self.assertEqual(file_change.raw["id"], "call-create")
        self.assertIn("path", file_change.raw["args"])
        # Successful and failed ToolResult values retain the ToolCall id.
        successful_result = next(
            event for event in by_type["status"] if (event.raw or {}).get("id") == "call-create"
        )
        self.assertEqual(successful_result.source, "tool")
        self.assertEqual(successful_result.raw["result"], {"path": "hello.py"})
        failed_result = next(
            event for event in by_type["error"] if (event.raw or {}).get("id") == "call-view"
        )
        self.assertEqual(failed_result.raw["error"], "README.md was unavailable")

    def test_resolves_once_without_iterating_async_generator_properties(self):
        response = _FakeResponse(_sample()["chat_response"])
        events = _run(response)
        self.assertEqual(response.resolve_calls, 1)
        self.assertEqual(response.cursor_accesses, 0)
        self.assertTrue(any(event.type == "message" for event in events))
        self.assertFalse(any("async_generator" in event.text for event in events))

    def test_reasoning_hidden_unless_verbose_and_never_leaks_signature(self):
        quiet = _run(_FakeResponse(_sample()["chat_response"]), verbose=False)
        self.assertFalse(any(e.type == "status" and "create the file" in (e.text or "") for e in quiet))

        loud = _run(_FakeResponse(_sample()["chat_response"]), verbose=True)
        self.assertTrue(any(e.type == "status" and "create the file" in (e.text or "") for e in loud))
        # Thoughts carry reasoning text only, never their opaque bytes.
        for event in loud:
            self.assertNotIn("signature", event.raw or {})
            self.assertNotIn("never-emit-this-signature", event.to_json())

    def test_usage_is_a_verbose_status_when_available(self):
        quiet = _run(_FakeResponse(_sample()["chat_response"]), verbose=False)
        self.assertFalse(any((event.raw or {}).get("usage") for event in quiet))

        loud = _run(_FakeResponse(_sample()["chat_response"]), verbose=True)
        usage_events = [event for event in loud if (event.raw or {}).get("usage")]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].source, "antigravity")
        self.assertEqual(usage_events[0].raw["usage"]["total_token_count"], 19)

    def test_tool_result_exception_maps_to_correlated_error(self):
        events = list(
            map_antigravity_turn(
                [
                    ToolResult(
                        name="RUN_COMMAND",
                        id="call-exception",
                        exception=RuntimeError("process disappeared"),
                    )
                ],
                verbose=False,
            )
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "error")
        self.assertEqual(events[0].type, "error")
        self.assertEqual(events[0].raw["id"], "call-exception")
        self.assertEqual(events[0].raw["error"], "process disappeared")
        self.assertEqual(events[0].raw["exception"], "RuntimeError")

    def test_degrades_to_message_only_without_tool_calls(self):
        events = _run(_FakeResponse(_sample()["text_only_response"]))
        self.assertFalse(any(e.source == "tool" for e in events))
        self.assertTrue(any(e.type == "message" and e.source == "antigravity" for e in events))

    def test_conversation_id_captured_as_uniform_provider_session(self):
        # The SDK exposes Agent.conversation_id (confirmed). Stage 5.1 captures it
        # via a uniform provider-session status event (emitted regardless of
        # verbosity so the daemon can persist it), keyed to the agent id, using the
        # uniform schema (provider_session_id + provider_session_kind).
        for verbose in (False, True):
            events = _run(
                _FakeResponse(_sample()["text_only_response"]),
                verbose=verbose,
                conversation_id="conv-123",
            )
            captured = [e for e in events if (e.raw or {}).get("provider_session_id") == "conv-123"]
            self.assertEqual(len(captured), 1, f"verbose={verbose}")
            raw = captured[0].raw
            self.assertEqual(raw["provider_session_kind"], "conversation")
            self.assertEqual(raw["agent_id"], AGENT.id)
            self.assertEqual(captured[0].source, "antigravity")

        # SessionState now has a structured, persisted place for captured ids.
        field_names = {f.name for f in dataclasses.fields(SessionState)}
        self.assertIn("agent_sessions", field_names)

    def test_map_turn_is_message_only_when_no_tool_calls(self):
        events = list(map_antigravity_turn([Text(step_index=0, text="just text")], verbose=False))
        self.assertEqual([e.type for e in events], ["message"])

    def test_builtin_tool_enum_name_is_classified(self):
        # ToolCall.name may be a BuiltinTools enum, not a str.
        import enum

        class BuiltinTools(enum.Enum):
            EDIT_FILE = "edit_file"

        events = list(
            map_antigravity_turn(
                [ToolCall(BuiltinTools.EDIT_FILE, {"path": "x"}, id="edit-1")],
                verbose=False,
            )
        )
        self.assertEqual(events[0].type, "file_change")
        self.assertEqual(events[0].raw["name"], "EDIT_FILE")
        self.assertEqual(events[0].raw["id"], "edit-1")


class SdkMissingExtraTests(unittest.TestCase):
    """Hermetic regardless of whether the real extra happens to be installed:
    force the module absent so the missing-extra path is exercised without
    reading real ~/.gemini credentials."""

    def test_probe_reports_unavailable_with_install_hint(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            health = AntigravitySdkBackend().probe()
        self.assertEqual(health.status, "unavailable")
        self.assertIn("google-antigravity", health.reason)

    def test_default_factory_raises_backend_unavailable(self):
        with mock.patch.dict(sys.modules, {"google.antigravity": None}):
            with self.assertRaises(BackendUnavailable) as ctx:
                _default_agent_factory(AGENT, {}, Path("."))
        self.assertIn("google-antigravity", str(ctx.exception))

    def test_default_factory_constructs_confirmed_config_shape(self):
        captured = {}
        fake_module = types.ModuleType("google.antigravity")

        class LocalAgentConfig:
            def __init__(self, **kwargs):
                captured["config"] = kwargs

        class Agent:
            def __init__(self, config):
                captured["agent_config"] = config

        fake_module.LocalAgentConfig = LocalAgentConfig
        fake_module.Agent = Agent
        with mock.patch.dict(sys.modules, {"google.antigravity": fake_module}):
            result = _default_agent_factory(
                AGENT,
                {"model": "gemini-test"},
                Path("/tmp/antigravity-workspace"),
            )

        self.assertIsInstance(result, Agent)
        self.assertEqual(
            captured["config"],
            {"workspaces": ["/tmp/antigravity-workspace"], "model": "gemini-test"},
        )

    def test_runner_with_default_factory_emits_actionable_error_event(self):
        runner = AntigravitySdkBackend().create_runner(AGENT, False, {})
        with mock.patch.dict(sys.modules, {"google.antigravity": None}):
            events = asyncio.run(_collect(runner))
        self.assertTrue(any(e.type == "error" and "google-antigravity" in e.text for e in events))


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
        # The built-in antigravity agent carries `--mode accept-edits -p` (cli
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
