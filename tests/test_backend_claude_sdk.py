"""Claude `sdk` backend tests (fake-module based; no real SDK, no model call).

The Claude Agent SDK's ``query(...)`` yields typed messages whose blocks are
``TextBlock``/``ToolUseBlock``/``ThinkingBlock`` and whose terminal message
carries ``session_id``/``is_error``. These are exercised with FAKE message
objects with the pinned constructors' real fields so the event mapper, option
mapping, probe, and provider-session capture are all covered without installing
``claude-agent-sdk`` or calling a model.
"""

import asyncio
import unittest
from pathlib import Path
from types import ModuleType
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


class TextBlock:
    def __init__(self, text):
        self.text = text


class ThinkingBlock:
    def __init__(self, thinking, signature):
        self.thinking = thinking
        self.signature = signature


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content=None, is_error=None):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(
        self,
        content,
        model,
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id=None,
        stop_reason=None,
        session_id=None,
        uuid=None,
    ):
        self.content = content
        self.model = model
        self.parent_tool_use_id = parent_tool_use_id
        self.error = error
        self.usage = usage
        self.message_id = message_id
        self.stop_reason = stop_reason
        self.session_id = session_id
        self.uuid = uuid


class ResultMessage:
    def __init__(
        self,
        subtype,
        duration_ms,
        duration_api_ms,
        is_error,
        num_turns,
        session_id,
        stop_reason=None,
        total_cost_usd=None,
        usage=None,
        result=None,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        deferred_tool_use=None,
        errors=None,
        api_error_status=None,
        uuid=None,
    ):
        self.subtype = subtype
        self.duration_ms = duration_ms
        self.duration_api_ms = duration_api_ms
        self.is_error = is_error
        self.num_turns = num_turns
        self.session_id = session_id
        self.stop_reason = stop_reason
        self.total_cost_usd = total_cost_usd
        self.usage = usage
        self.result = result
        self.structured_output = structured_output
        self.model_usage = model_usage
        self.permission_denials = permission_denials
        self.deferred_tool_use = deferred_tool_use
        self.errors = errors
        self.api_error_status = api_error_status
        self.uuid = uuid


class SystemMessage:
    def __init__(self, subtype, data):
        self.subtype = subtype
        self.data = data


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
    return AssistantMessage(content=list(blocks), model="claude-test")


def _result(**overrides):
    values = {
        "subtype": "success",
        "duration_ms": 120,
        "duration_api_ms": 100,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sess-1",
    }
    values.update(overrides)
    return ResultMessage(**values)


class ClaudeEventMappingTests(unittest.TestCase):
    def test_message_and_typed_tool_uses_map_to_standard_events(self):
        message = _assistant(
            [
                TextBlock("Here is the plan."),
                ToolUseBlock(id="t1", name="Read", input={"path": "a.py"}),
                ToolUseBlock(id="t2", name="Bash", input={"command": "pytest -q"}),
                ToolUseBlock(id="t3", name="Edit", input={"path": "a.py"}),
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
        message = _assistant([ThinkingBlock("secret plan text", "OPAQUE_SIG"), TextBlock("Done.")])
        quiet = _run([message], verbose=False)
        self.assertFalse(any(e.type == "status" and "secret plan text" in (e.text or "") for e in quiet))
        loud = _run([message], verbose=True)
        self.assertTrue(any(e.type == "status" and "secret plan text" in (e.text or "") for e in loud))
        # The opaque verification signature must never appear anywhere.
        for event in quiet + loud:
            self.assertNotIn("OPAQUE_SIG", event.text or "")
            self.assertNotIn("signature", event.raw or {})

    def test_degrades_to_message_only(self):
        events = _run([_assistant([TextBlock("Just prose.")])])
        self.assertFalse(any(e.source == "tool" for e in events))
        self.assertTrue(any(e.type == "message" and e.source == "claude" for e in events))

    def test_tool_result_success_and_error_keep_correlation_fields(self):
        message = _assistant(
            [
                ToolResultBlock("tool-1", "file contents", is_error=False),
                ToolResultBlock(
                    "tool-2",
                    [{"type": "text", "text": "permission denied"}],
                    is_error=True,
                ),
            ]
        )
        events = _run([message])
        success = next(event for event in events if event.source == "tool")
        failure = next(event for event in events if event.source == "error")

        self.assertEqual(success.type, "tool_call")
        self.assertEqual(
            success.raw,
            {"tool_use_id": "tool-1", "content": "file contents", "is_error": False},
        )
        self.assertEqual(failure.type, "error")
        self.assertEqual(failure.raw["tool_use_id"], "tool-2")
        self.assertEqual(failure.raw["content"][0]["text"], "permission denied")
        self.assertTrue(failure.raw["is_error"])

    def test_result_is_error_maps_to_error_event(self):
        result = _result(is_error=True, result="the model refused", subtype="error")
        events = _run([result])
        self.assertTrue(any(e.type == "error" and "the model refused" in e.text for e in events))

    def test_assistant_error_maps_to_error_after_content(self):
        message = AssistantMessage(
            content=[TextBlock("Partial response")],
            model="claude-test",
            error="rate_limit",
            usage={"input_tokens": 3},
        )
        events = _run([message])
        self.assertEqual([event.type for event in events], ["message", "error"])
        self.assertEqual(events[1].raw["model"], "claude-test")
        self.assertEqual(events[1].raw["usage"], {"input_tokens": 3})

    def test_result_usage_and_cost_are_verbose_status_only(self):
        result = _result(
            total_cost_usd=0.012,
            usage={"input_tokens": 11, "output_tokens": 7},
            model_usage={"claude-test": {"costUSD": 0.012}},
        )
        quiet = list(iter_claude_events(result, verbose=False))
        loud = list(iter_claude_events(result, verbose=True))

        self.assertEqual(quiet, [])
        self.assertEqual(len(loud), 1)
        self.assertEqual(loud[0].source, "claude")
        self.assertEqual(loud[0].type, "status")
        self.assertIn("cost_usd=0.012", loud[0].text)
        self.assertIn("input_tokens", loud[0].text)
        self.assertEqual(loud[0].raw["usage"]["output_tokens"], 7)
        self.assertEqual(loud[0].raw["model_usage"]["claude-test"]["costUSD"], 0.012)

    def test_missing_import_at_stream_open_surfaces_error_event(self):
        events = _run([], error=BackendUnavailable("claude", "sdk", "claude_agent_sdk is not importable", "hint"))
        self.assertTrue(any(e.type == "error" and "not importable" in e.text for e in events))

    def test_options_constructor_drift_surfaces_actionable_error_event(self):
        events = _run([], error=TypeError("unexpected keyword 'system_prompt'"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "error")
        self.assertIn("unexpected keyword 'system_prompt'", events[0].text)
        self.assertEqual(events[0].raw["exception"], "TypeError")


class ClaudeSessionCaptureTests(unittest.TestCase):
    def test_session_id_captured_uniformly_regardless_of_verbose(self):
        messages = [
            SystemMessage(subtype="init", data={"session_id": "sess-1"}),
            _assistant([TextBlock("hello")]),
            _result(total_cost_usd=0.01),
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
        events = list(
            iter_claude_events(SystemMessage(subtype="init", data={"session_id": "x"}), verbose=False)
        )
        self.assertFalse(any((e.raw or {}).get("provider_session_id") for e in events))


class ClaudeOptionMappingTests(unittest.TestCase):
    def test_map_sdk_options_keeps_only_supported_keys(self):
        mapped = _map_sdk_options(
            {
                "model": "opus",
                "permission_mode": "acceptEdits",
                "thinking_level": "high",
                "thinking_budget_tokens": 8192,
                "unknown": True,
            }
        )
        self.assertEqual(
            mapped,
            {
                "model": "opus",
                "permission_mode": "acceptEdits",
                "effort": "high",
                "max_thinking_tokens": 8192,
            },
        )

    def test_build_agent_options_passes_coding_presets_and_predictable_settings(self):
        captured = {}

        class FakeOptions:
            def __init__(
                self,
                tools=None,
                system_prompt=None,
                permission_mode=None,
                model=None,
                cwd=None,
                setting_sources=None,
                max_thinking_tokens=None,
                effort=None,
            ):
                captured.update(
                    {
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "permission_mode": permission_mode,
                        "model": model,
                        "cwd": cwd,
                        "setting_sources": setting_sources,
                        "max_thinking_tokens": max_thinking_tokens,
                        "effort": effort,
                    }
                )

        build_claude_agent_options(
            FakeOptions,
            {"model": "sonnet", "permission_mode": "acceptEdits", "thinking_level": "high"},
            Path("/w"),
        )
        self.assertEqual(captured["model"], "sonnet")
        self.assertEqual(captured["permission_mode"], "acceptEdits")
        self.assertEqual(captured["effort"], "high")
        self.assertEqual(captured["setting_sources"], [])  # runs do not implicitly load fs settings
        self.assertEqual(captured["cwd"], "/w")
        self.assertEqual(captured["system_prompt"], {"type": "preset", "preset": "claude_code"})
        self.assertEqual(captured["tools"], {"type": "preset", "preset": "claude_code"})

    def test_build_agent_options_passes_raw_thinking_budget(self):
        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        build_claude_agent_options(FakeOptions, {"thinking_budget_tokens": 16384}, Path("/w"))
        self.assertEqual(captured["max_thinking_tokens"], 16384)
        self.assertNotIn("effort", captured)

    def test_build_agent_options_does_not_retry_after_constructor_type_error(self):
        options_cls = mock.Mock(side_effect=TypeError("unexpected kwarg"))
        with self.assertRaisesRegex(TypeError, "unexpected kwarg"):
            build_claude_agent_options(options_cls, {"model": "opus"}, Path("/w"))
        options_cls.assert_called_once()
        kwargs = options_cls.call_args.kwargs
        self.assertEqual(kwargs["cwd"], "/w")
        self.assertEqual(kwargs["setting_sources"], [])
        self.assertIn("system_prompt", kwargs)
        self.assertIn("tools", kwargs)


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
        summary = ClaudeSdkBackend().settings_summary(
            AGENT,
            {"model": "opus", "permission_mode": "default", "thinking_level": "high"},
        )
        self.assertEqual(summary["backend"], "sdk")
        self.assertEqual(summary["package"], "claude-agent-sdk")
        self.assertEqual(
            summary["options"],
            {"model": "opus", "permission_mode": "default", "effort": "high"},
        )
        self.assertEqual(summary["setting_sources"], "none")
        self.assertEqual(summary["system_prompt"], "claude_code")
        self.assertEqual(summary["tools"], "claude_code")

    def test_default_stream_calls_query_and_returns_its_async_iterator(self):
        captured = {}
        fake_module = ModuleType("claude_agent_sdk")

        class FakeOptions:
            def __init__(self, **kwargs):
                captured["options"] = kwargs

        async def fake_query(*, prompt, options):
            captured["prompt"] = prompt
            captured["instance"] = options
            yield _assistant([TextBlock("fake response")])

        fake_module.ClaudeAgentOptions = FakeOptions
        fake_module.query = fake_query
        with mock.patch.dict("sys.modules", {"claude_agent_sdk": fake_module}):
            stream = _default_message_stream(AGENT, {"model": "sonnet"}, Path("/w"), "hi")

        async def collect():
            return [message async for message in stream]

        messages = asyncio.run(collect())
        self.assertEqual(messages[0].content[0].text, "fake response")
        self.assertEqual(captured["prompt"], "hi")
        self.assertEqual(captured["options"]["model"], "sonnet")
        self.assertEqual(captured["options"]["tools"]["preset"], "claude_code")

    def test_default_stream_raises_backend_unavailable_when_module_absent(self):
        import sys

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            with self.assertRaises(BackendUnavailable) as ctx:
                _default_message_stream(AGENT, {}, Path("."), "hi")
        self.assertIn("claude-agent-sdk", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
