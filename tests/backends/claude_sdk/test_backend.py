"""Claude `sdk` backend tests (fake-module based; no real SDK, no model call).

The Claude Agent SDK's typed messages carry ``TextBlock``/``ToolUseBlock``/
``ThinkingBlock`` blocks and a terminal message with ``session_id``/``is_error``.
These are exercised with FAKE message objects with the pinned constructors' real
fields so the event mapper, option mapping, probe, and provider-session capture
are all covered without installing ``claude-agent-sdk`` or calling a model.
Production tests replace the lazy SDK import with a fake persistent
``ClaudeSDKClient``, including resume after reset and undelivered-prompt replay
(lifecycle verified on ``claude-agent-sdk`` 0.2.126).
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from agent_collab import backends
from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.claude_sdk.backend import (
    ClaudeSdkBackend,
    ClaudeSdkRunner,
    _default_conversation,
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


class _FakeConversation:
    """Runner-seam fake following the adapter contract; one turn per run()."""

    def __init__(self, turns, *, error=None):
        self.turns = [list(turn) for turn in turns]
        self.error = error
        self.prompts = []
        self.noted_ids = []
        self.reset_calls = 0
        self.close_calls = 0
        self.is_active = False
        self.is_closed = False

    def active(self):
        return not self.is_closed and (self.is_active or bool(self.noted_ids))

    async def run(self, prompt):
        self.prompts.append(prompt)
        self.is_active = True
        if self.error is not None:
            raise self.error
        if not self.turns:
            raise RuntimeError("no fake turn")
        for message in self.turns.pop(0):
            yield message

    def note_session_id(self, session_id):
        self.noted_ids.append(session_id)

    async def reset(self):
        self.reset_calls += 1
        self.is_active = False

    async def close(self):
        if self.is_closed:
            return
        self.is_closed = True
        self.close_calls += 1
        self.is_active = False


def _conversation_factory(conversation):
    return lambda _agent, _options, _workdir: conversation


def _factory_error(error):
    def factory(_agent, _options, _workdir):
        raise error

    return factory


def _runner_for(conversation=None, *, verbose=False, options=None, factory=None):
    factory = factory or _conversation_factory(conversation)
    return ClaudeSdkRunner(AGENT, verbose, options or {}, conversation_factory=factory)


def _run(messages, *, verbose=False, options=None, error=None, factory_error=None):
    if factory_error is not None:
        runner = _runner_for(
            verbose=verbose, options=options, factory=_factory_error(factory_error)
        )
    else:
        conversation = _FakeConversation([messages], error=error)
        runner = _runner_for(conversation, verbose=verbose, options=options)

    async def collect():
        events = []

        async def emit(event):
            events.append(event)

        await runner.run_turn("do a thing", Path("."), emit)
        return events

    return asyncio.run(collect())


def _outcome(messages, *, error=None):
    conversation = _FakeConversation([messages], error=error)
    runner = _runner_for(conversation)

    async def collect():
        async def emit(_event):
            return None

        return await runner.run_turn("do a thing", Path("."), emit)

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
    def test_terminal_result_controls_outcome(self):
        self.assertEqual(_outcome([_result()]).outcome, "completed")
        failed = _outcome([_result(is_error=True, subtype="error_during_execution")])
        self.assertEqual((failed.outcome, failed.code), ("failed", "provider_terminal_failure"))
        incomplete = _outcome([_assistant([TextBlock("partial")])])
        self.assertEqual(incomplete.code, "provider_output_incomplete")
        transport = _outcome([], error=RuntimeError("Bearer secret /home/private"))
        self.assertEqual(transport.code, "provider_transport_failed")
        self.assertNotIn("secret", str(transport.to_dict()))

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

        self.assertTrue(
            any(e.source == "claude" and "Here is the plan." in e.text for e in by_type["message"])
        )
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
        self.assertFalse(
            any(e.type == "status" and "secret plan text" in (e.text or "") for e in quiet)
        )
        loud = _run([message], verbose=True)
        self.assertTrue(
            any(e.type == "status" and "secret plan text" in (e.text or "") for e in loud)
        )
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

    def test_missing_import_at_conversation_creation_surfaces_error_event(self):
        events = _run(
            [],
            factory_error=BackendUnavailable(
                "claude", "sdk", "claude_agent_sdk is not importable", "hint"
            ),
        )
        self.assertTrue(any(e.type == "error" and "not importable" in e.text for e in events))

    def test_options_constructor_drift_surfaces_actionable_error_event(self):
        events = _run([], error=TypeError("unexpected keyword 'system_prompt'"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "error")
        self.assertIn("unexpected keyword 'system_prompt'", events[0].text)
        self.assertEqual(events[0].raw["exception"], "TypeError")


class ClaudeConversationLifecycleTests(unittest.TestCase):
    def test_conversation_active_transitions_after_materialized_turn(self):
        conversation = _FakeConversation([[_result()]])
        runner = _runner_for(conversation)

        async def scenario():
            self.assertFalse(runner.conversation_active())

            async def emit(_event):
                return None

            outcome = await runner.run_turn("one", Path("."), emit)
            self.assertTrue(runner.conversation_active())
            await runner.close()
            self.assertFalse(runner.conversation_active())
            return outcome

        outcome = asyncio.run(scenario())
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(conversation.noted_ids, ["sess-1"])
        self.assertEqual(conversation.reset_calls, 0)

    def test_two_turns_reuse_the_same_conversation_and_feed_back_the_id(self):
        conversation = _FakeConversation(
            [
                [SystemMessage("init", {"session_id": "sess-1"}), _result()],
                [_assistant([TextBlock("two")]), _result()],
            ]
        )
        runner = _runner_for(conversation)

        async def scenario():
            async def emit(_event):
                return None

            first = await runner.run_turn("one", Path("."), emit)
            second = await runner.run_turn("two", Path("."), emit)
            await runner.close()
            await runner.close()
            return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual((first.outcome, second.outcome), ("completed", "completed"))
        self.assertEqual(conversation.prompts, ["one", "two"])
        # The id is noted once per turn it is observed in, always the same value.
        self.assertEqual(conversation.noted_ids, ["sess-1", "sess-1"])
        self.assertEqual(conversation.close_calls, 1)

    def test_abnormal_result_resets_once_and_retains_continuation_identity(self):
        conversation = _FakeConversation([[_result(is_error=True, subtype="error")]])
        runner = _runner_for(conversation)

        async def scenario():
            async def emit(_event):
                return None

            outcome = await runner.run_turn("fail", Path("."), emit)
            return outcome, runner.conversation_active()

        outcome, active = asyncio.run(scenario())
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(conversation.reset_calls, 1)
        self.assertTrue(active)

    def test_missing_result_message_resets_once(self):
        conversation = _FakeConversation([[_assistant([TextBlock("partial")])]])
        runner = _runner_for(conversation)

        async def scenario():
            async def emit(_event):
                return None

            return await runner.run_turn("incomplete", Path("."), emit)

        outcome = asyncio.run(scenario())
        self.assertEqual(outcome.code, "provider_output_incomplete")
        self.assertEqual(conversation.reset_calls, 1)

    def test_cancellation_resets_once_and_closes_the_stream(self):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()

            class BlockingConversation(_FakeConversation):
                async def run(self, prompt):
                    self.prompts.append(prompt)
                    self.is_active = True
                    entered.set()
                    await release.wait()
                    yield _result()

            conversation = BlockingConversation([])
            runner = _runner_for(conversation)

            async def collect():
                async def emit(_event):
                    return None

                await runner.run_turn("cancel me", Path("."), emit)

            consumer = asyncio.create_task(collect())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assertEqual(conversation.reset_calls, 1)

        asyncio.run(scenario())

    def test_slow_reset_preserves_definitive_provider_outcome(self):
        class SlowResetConversation(_FakeConversation):
            async def reset(self):
                self.reset_calls += 1
                await asyncio.sleep(0.05)

        conversation = SlowResetConversation([[_result(is_error=True, subtype="error")]])
        runner = _runner_for(conversation)

        async def scenario():
            async def emit(_event):
                return None

            return await runner.run_turn("fail", Path("."), emit)

        with mock.patch(
            "agent_collab.backends.claude_sdk.backend.SDK_CLOSE_GRACE_SECONDS",
            0.001,
        ):
            outcome = asyncio.run(scenario())

        self.assertEqual(
            (outcome.outcome, outcome.code),
            ("failed", "provider_terminal_failure"),
        )
        self.assertEqual(conversation.reset_calls, 1)


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
            self.assertEqual(
                len(captured), 1, f"verbose={verbose}"
            )  # emitted once, not per message
            raw = captured[0].raw
            self.assertEqual(raw["provider_session_kind"], "session")
            self.assertEqual(raw["agent_id"], AGENT.id)
            self.assertEqual(captured[0].source, "claude")

    def test_iter_events_never_yields_session_capture(self):
        # Session capture is the runner's job (it attributes the agent id); the
        # pure mapper only emits transcript prose/tools.
        events = list(
            iter_claude_events(
                SystemMessage(subtype="init", data={"session_id": "x"}), verbose=False
            )
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

    def test_build_agent_options_omits_resume_fields_on_fresh_connections(self):
        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        build_claude_agent_options(FakeOptions, {"model": "sonnet"}, Path("/w"))
        self.assertNotIn("resume", captured)
        self.assertNotIn("fork_session", captured)

    def test_build_agent_options_continues_captured_session_on_reconnect(self):
        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        build_claude_agent_options(
            FakeOptions, {"model": "sonnet"}, Path("/w"), resume_session_id="sess-9"
        )
        self.assertEqual(captured["resume"], "sess-9")
        self.assertIs(captured["fork_session"], False)

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


class ClaudeProductionFactoryTests(unittest.TestCase):
    @staticmethod
    def _fake_module(state, turns):
        module = ModuleType("claude_agent_sdk")
        state.setdefault("clients", [])
        state.setdefault("connects", 0)
        state.setdefault("disconnects", 0)
        state.setdefault("queries", [])
        state.setdefault("open", 0)
        state["turns"] = list(turns)

        class FakeOptions:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeClient:
            def __init__(self, options):
                state["clients"].append(dict(options.kwargs))
                self.options = dict(options.kwargs)
                self.is_open = False

            async def connect(self):
                error = state.get("connect_error")
                if error is not None:
                    raise error
                if self.options.get("resume") is not None:
                    resume_error = state.get("resume_error")
                    if resume_error is not None:
                        raise resume_error
                state["connects"] += 1
                state["open"] += 1
                self.is_open = True

            async def query(self, prompt):
                if not self.is_open:
                    raise AssertionError("query on a disconnected client")
                error = state.get("query_error")
                if error is not None:
                    raise error
                gate = state.get("query_gate")
                if gate is not None:
                    state["query_entered"].set()
                    await gate.wait()
                state["queries"].append(prompt)

            async def receive_response(self):
                if not self.is_open:
                    raise AssertionError("receive on a disconnected client")
                turn = state["turns"].pop(0)
                if isinstance(turn, BaseException):
                    raise turn
                for message in turn:
                    if callable(message):
                        message = await message()
                    if not self.is_open:
                        raise AssertionError("receive on a disconnected client")
                    yield message

            async def disconnect(self):
                if not self.is_open:
                    return
                self.is_open = False
                state["open"] -= 1
                state["disconnects"] += 1

        module.ClaudeSDKClient = FakeClient
        module.ClaudeAgentOptions = FakeOptions
        return module

    @staticmethod
    def _runner(agent=AGENT, options=None):
        return ClaudeSdkRunner(
            agent,
            False,
            options or {},
            conversation_factory=_default_conversation,
        )

    @staticmethod
    async def _collect(runner, prompt):
        events = []

        async def emit(event):
            events.append(event)

        outcome = await runner.run_turn(prompt, Path("/workspace"), emit)
        return events, outcome

    def test_one_client_connect_spans_two_completed_turns(self):
        state = {}
        module = self._fake_module(
            state,
            [
                [
                    SystemMessage("init", {"session_id": "sess-live"}),
                    _result(session_id="sess-live"),
                ],
                [_assistant([TextBlock("two")]), _result(session_id="sess-live")],
            ],
        )
        runner = self._runner(
            options={"model": "sonnet", "permission_mode": "default", "thinking_level": "low"}
        )

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                self.assertFalse(runner.conversation_active())
                first = await self._collect(runner, "turn one")
                self.assertTrue(runner.conversation_active())
                second = await self._collect(runner, "turn two")
                self.assertTrue(runner.conversation_active())
                await runner.close()
                await runner.close()
                return first, second

            first, second = asyncio.run(scenario())

        self.assertEqual(first[1].outcome, "completed")
        self.assertEqual(second[1].outcome, "completed")
        # One client, one connect, two query/receive cycles on the live client.
        self.assertEqual(len(state["clients"]), 1)
        self.assertEqual(state["connects"], 1)
        self.assertEqual(state["queries"], ["turn one", "turn two"])
        self.assertEqual(state["disconnects"], 1)
        self.assertEqual(state["open"], 0)
        # Fresh first connection: presets and cwd present, resume/fork absent.
        fresh = state["clients"][0]
        self.assertEqual(fresh["cwd"], "/workspace")
        self.assertEqual(fresh["setting_sources"], [])
        self.assertEqual(fresh["system_prompt"], {"type": "preset", "preset": "claude_code"})
        self.assertEqual(fresh["tools"], {"type": "preset", "preset": "claude_code"})
        self.assertEqual(fresh["model"], "sonnet")
        self.assertEqual(fresh["effort"], "low")
        self.assertNotIn("resume", fresh)
        self.assertNotIn("fork_session", fresh)
        for events, _outcome in (first, second):
            self.assertTrue(
                any((event.raw or {}).get("provider_session_id") == "sess-live" for event in events)
            )
        self.assertFalse(runner.conversation_active())

    def test_abnormal_turn_resets_once_then_reconnects_with_captured_id(self):
        state = {}
        module = self._fake_module(
            state,
            [
                [_result(session_id="sess-live")],
                [_result(session_id="sess-live", is_error=True, subtype="error")],
                [_assistant([TextBlock("three")]), _result(session_id="sess-live")],
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                first = await self._collect(runner, "one")
                failed = await self._collect(runner, "two")
                self.assertTrue(runner.conversation_active())
                third = await self._collect(runner, "three")
                await runner.close()
                return first, failed, third

            first, failed, third = asyncio.run(scenario())

        self.assertEqual(
            [item[1].outcome for item in (first, failed, third)],
            ["completed", "failed", "completed"],
        )
        self.assertEqual(len(state["clients"]), 2)
        fresh, reconnect = state["clients"]
        self.assertNotIn("resume", fresh)
        self.assertEqual(reconnect["resume"], "sess-live")
        self.assertIs(reconnect["fork_session"], False)
        self.assertEqual(state["connects"], 2)
        # One reset disconnect after the failed turn plus the final close.
        self.assertEqual(state["disconnects"], 2)

    def test_resume_rejection_is_structured_and_never_starts_fresh(self):
        state = {}
        module = self._fake_module(
            state,
            [
                [_result(session_id="sess-live")],
                [_result(session_id="sess-live", is_error=True, subtype="error")],
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                await self._collect(runner, "one")
                await self._collect(runner, "fail and reset")
                state["resume_error"] = RuntimeError(
                    "No conversation found with session ID: sess-live"
                )
                rejected = await self._collect(runner, "must resume")
                rejected_again = await self._collect(runner, "must still resume")
                await runner.close()
                return rejected, rejected_again

            rejected, rejected_again = asyncio.run(scenario())

        self.assertEqual(rejected[1].code, "provider_transport_failed")
        self.assertTrue(
            any(
                event.type == "error" and "No conversation found" in event.text
                for event in rejected[0]
            )
        )
        self.assertEqual(rejected_again[1].code, "provider_transport_failed")
        # Only the first fresh connect ever happened; every reconnect carried
        # the captured resume id and none opened a fresh provider session.
        self.assertEqual(state["connects"], 1)
        self.assertEqual(
            [client.get("resume") for client in state["clients"]],
            [None, "sess-live", "sess-live"],
        )
        self.assertFalse(runner.conversation_active())

    def test_failed_resume_replays_undelivered_delta_after_recovery(self):
        state = {}
        module = self._fake_module(
            state,
            [
                [_result(session_id="sess-live")],
                [_result(session_id="sess-live", is_error=True, subtype="error")],
                [_assistant([TextBlock("recovered")]), _result(session_id="sess-live")],
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                await self._collect(runner, "one")
                await self._collect(runner, "fail and reset")
                state["resume_error"] = RuntimeError("temporary reconnect failure")
                rejected = await self._collect(runner, "SECRET_BLUE")
                del state["resume_error"]
                recovered = await self._collect(runner, "what was blue?")
                await runner.close()
                return rejected, recovered

            rejected, recovered = asyncio.run(scenario())

        self.assertEqual(rejected[1].code, "provider_transport_failed")
        self.assertEqual(recovered[1].outcome, "completed")
        self.assertEqual(
            state["queries"],
            ["one", "fail and reset", "SECRET_BLUE\n\nwhat was blue?"],
        )

    def test_connect_failure_retains_undelivered_prompt(self):
        state = {}
        module = self._fake_module(
            state,
            [[_assistant([TextBlock("late")]), _result(session_id="sess-live")]],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                state["connect_error"] = RuntimeError("spawn failed")
                failed = await self._collect(runner, "first delta")
                del state["connect_error"]
                recovered = await self._collect(runner, "second delta")
                await runner.close()
                return failed, recovered

            failed, recovered = asyncio.run(scenario())

        self.assertEqual(failed[1].code, "provider_transport_failed")
        self.assertEqual(recovered[1].outcome, "completed")
        self.assertEqual(state["queries"], ["first delta\n\nsecond delta"])

    def test_query_failure_is_structured_and_never_replays_the_handed_off_prompt(self):
        # Once the prompt is handed to the client's query() call, delivery is
        # uncertain and a replay would risk a duplicate provider turn — the
        # same hand-off boundary the codex adapter uses.
        state = {}
        module = self._fake_module(
            state,
            [[_assistant([TextBlock("ok")]), _result(session_id="sess-live")]],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                state["query_error"] = RuntimeError("transport write failed")
                failed = await self._collect(runner, "first")
                del state["query_error"]
                recovered = await self._collect(runner, "second")
                await runner.close()
                return failed, recovered

            failed, recovered = asyncio.run(scenario())

        self.assertEqual(failed[1].code, "provider_transport_failed")
        self.assertEqual(recovered[1].outcome, "completed")
        self.assertEqual(state["queries"], ["second"])
        # The failed turn's client was reset (disconnected) exactly once.
        self.assertEqual(state["disconnects"], 2)  # reset + final close

    def test_cancellation_inside_query_never_replays_the_prompt(self):
        # A cancel that lands inside query() after hand-off must not queue the
        # prompt for replay: the CLI may already have accepted the message and
        # a replay would duplicate the user turn on the resumed session.
        state = {}
        module = self._fake_module(
            state,
            [[_assistant([TextBlock("ok")]), _result(session_id="sess-live")]],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                state["query_gate"] = asyncio.Event()
                state["query_entered"] = asyncio.Event()
                turn = asyncio.create_task(self._collect(runner, "first"))
                await state["query_entered"].wait()
                turn.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await turn
                del state["query_gate"]
                recovered = await self._collect(runner, "second")
                await runner.close()
                return recovered

            recovered = asyncio.run(scenario())

        self.assertEqual(recovered[1].outcome, "completed")
        self.assertEqual(state["queries"], ["second"])

    def test_receive_failure_resets_then_recovers_on_the_captured_session(self):
        state = {}
        module = self._fake_module(
            state,
            [
                [_result(session_id="sess-live")],
                RuntimeError("stream broke"),
                [_assistant([TextBlock("three")]), _result(session_id="sess-live")],
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                first = await self._collect(runner, "one")
                broken = await self._collect(runner, "two")
                self.assertTrue(runner.conversation_active())
                third = await self._collect(runner, "three")
                await runner.close()
                return first, broken, third

            first, broken, third = asyncio.run(scenario())

        self.assertEqual(first[1].outcome, "completed")
        self.assertEqual(broken[1].code, "provider_transport_failed")
        self.assertTrue(
            any(event.type == "error" and "stream broke" in event.text for event in broken[0])
        )
        # The mid-receive failure dirties the live client: recovery must be a
        # reconnect that resumes the captured id, never a fresh session.
        self.assertEqual(third[1].outcome, "completed")
        self.assertEqual(len(state["clients"]), 2)
        self.assertEqual(state["clients"][1]["resume"], "sess-live")
        self.assertIs(state["clients"][1]["fork_session"], False)
        self.assertEqual(state["queries"], ["one", "two", "three"])
        # Reset after the broken turn, then the final close of the reconnect.
        self.assertEqual(state["disconnects"], 2)

    def test_missing_result_keeps_captured_id_and_resumes_never_fresh(self):
        # A session id can be observed before any ResultMessage (the CLI's
        # init message). If the turn then ends without a terminal result, the
        # delivered prompt already lives in provider context — the reconnect
        # must resume the captured id, never silently open a fresh session
        # (verified resumable on 0.2.126 even when turn 1 never finished).
        state = {}
        module = self._fake_module(
            state,
            [
                [SystemMessage("init", {"session_id": "sess-live"})],
                [_assistant([TextBlock("two")]), _result(session_id="sess-live")],
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                incomplete = await self._collect(runner, "one")
                self.assertTrue(runner.conversation_active())
                second = await self._collect(runner, "two")
                await runner.close()
                return incomplete, second

            incomplete, second = asyncio.run(scenario())

        self.assertEqual(incomplete[1].code, "provider_output_incomplete")
        self.assertEqual(second[1].outcome, "completed")
        self.assertEqual(len(state["clients"]), 2)
        self.assertNotIn("resume", state["clients"][0])
        self.assertEqual(state["clients"][1]["resume"], "sess-live")
        self.assertIs(state["clients"][1]["fork_session"], False)

    def test_close_serializes_behind_an_in_flight_turn(self):
        state = {}
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_message():
            started.set()
            await release.wait()
            return _result(session_id="sess-live")

        module = self._fake_module(state, [[blocking_message]])
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                turn = asyncio.create_task(self._collect(runner, "block"))
                await started.wait()
                close = asyncio.create_task(runner.close())
                await asyncio.sleep(0)
                self.assertFalse(close.done())
                self.assertEqual(state["disconnects"], 0)
                release.set()
                events, outcome = await turn
                await close
                return outcome

            outcome = asyncio.run(scenario())

        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state["disconnects"], 1)
        self.assertEqual(state["open"], 0)

    def test_close_serializes_behind_a_cancelled_turn(self):
        state = {}
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_message():
            started.set()
            await release.wait()
            return _result(session_id="sess-live")

        module = self._fake_module(state, [[blocking_message]])
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def scenario():
                turn = asyncio.create_task(self._collect(runner, "block"))
                await started.wait()
                turn.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await turn
                await runner.close()

            asyncio.run(scenario())

        self.assertEqual(state["disconnects"], 1)
        self.assertEqual(state["open"], 0)

    def test_default_conversation_reports_missing_or_incompatible_module(self):
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            with self.assertRaises(BackendUnavailable) as missing:
                _default_conversation(AGENT, {}, Path("."))
        self.assertIn("claude-agent-sdk", str(missing.exception))

        incompatible = ModuleType("claude_agent_sdk")
        incompatible.ClaudeSDKClient = object
        incompatible.ClaudeAgentOptions = object
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": incompatible}):
            with self.assertRaises(BackendUnavailable) as wrong_api:
                _default_conversation(AGENT, {}, Path("."))
        self.assertIn("connect", str(wrong_api.exception))


class ClaudeBackendSurfaceTests(unittest.TestCase):
    def test_registered_pair_and_honest_capabilities(self):
        self.assertTrue(backends.is_registered("claude", "sdk"))
        caps = backends.capabilities_for("claude", "sdk")
        self.assertEqual(
            caps.to_dict(),
            {"resume": False, "interrupt": False, "tool_gate": False, "continuity": True},
        )

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
        self.assertEqual(summary["conversation"], "persistent")


if __name__ == "__main__":
    unittest.main()
