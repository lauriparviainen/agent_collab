"""Antigravity `sdk` backend tests.

The SDK's API and native-runtime shapes were confirmed against
google-antigravity 0.1.8 (see
tests/fixtures/antigravity/sdk-introspection.json). The event mapper and
persistent conversation adapter are driven by fakes built to that protocol:
async ``resolve()`` returns typed Text/Thought/ToolCall/ToolResult values,
thoughts/tool_calls are independent async cursor properties, and strict reopen
uses ``conversation_id`` plus ``SessionContinuationMode.RESUME``. No hermetic
test imports the SDK or calls a model.
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

from agent_collab.backends.antigravity_sdk.backend import (
    AntigravitySdkBackend,
    AntigravitySdkRunner,
    _PersistentAntigravityConversation,
    _default_agent_factory,
    _default_conversation,
    assess_native_runtime,
    map_antigravity_turn,
)
from agent_collab.backends.base import BackendHealth, BackendOptionError, BackendUnavailable
from agent_collab.backends.common.health import gemini_api_key_credentials
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import SessionState
from agent_collab.options import (
    StartOptionsError,
    build_session_settings,
    validate_start_backends,
    validate_start_options,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "antigravity"
AGENT = AgentConfig(id="antigravity", type="antigravity", backend="sdk")
VERTEX_AGENT = AgentConfig(
    id="antigravity",
    type="antigravity",
    backend="sdk",
    backend_config={"vertex": True, "project": "test-project", "location": "us-central1"},
)


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
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def chat(self, prompt):
        return self._response


def _factory_for(response, conversation_id=None):
    def factory(agent, options, workdir):
        return _PersistentAntigravityConversation(
            lambda _resume_id: _FakeAgent(
                response,
                conversation_id=conversation_id,
            )
        )

    return factory


async def _collect(runner, prompt="do a thing"):
    events = []

    async def emit(event):
        events.append(event)

    await runner.run_turn(prompt, Path("."), emit)
    return events


def _run(response, *, verbose=False, options=None, conversation_id=None):
    runner = AntigravitySdkRunner(
        AGENT,
        verbose,
        options or {},
        conversation_factory=_factory_for(response, conversation_id),
    )
    return asyncio.run(_collect(runner))


def _outcome(response):
    runner = AntigravitySdkRunner(
        AGENT,
        False,
        {},
        conversation_factory=_factory_for(response),
    )

    async def collect():
        async def emit(_event):
            return None

        return await runner.run_turn("do a thing", Path("."), emit)

    return asyncio.run(collect())


class SdkEventMappingTests(unittest.TestCase):
    def test_resolved_message_is_required_for_success(self):
        completed = _outcome(_FakeResponse(_sample()["text_only_response"]))
        self.assertEqual(completed.outcome, "completed")
        empty = _outcome(_FakeResponse({"chunks": []}))
        self.assertEqual((empty.outcome, empty.code), ("failed", "provider_empty_response"))

    def test_typed_buffer_maps_text_calls_results_and_errors(self):
        response = _FakeResponse(_sample()["chat_response"])
        events = _run(response)
        by_type = {}
        for event in events:
            by_type.setdefault(event.type, []).append(event)

        # final text -> antigravity message
        self.assertTrue(
            any(
                e.source == "antigravity" and "Created hello.py" in e.text
                for e in by_type["message"]
            )
        )
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

    def test_cancellation_closes_response_before_agent_context_exits(self):
        async def scenario(close_error):
            entered = asyncio.Event()
            lifecycle = []

            class BlockingResponse:
                def __init__(self):
                    self.closed = False

                async def resolve(self):
                    entered.set()
                    await asyncio.Event().wait()

                async def aclose(self):
                    self.closed = True
                    lifecycle.append("response_closed")
                    if close_error:
                        raise RuntimeError("close failed")

            response = BlockingResponse()

            class TrackingAgent(_FakeAgent):
                async def __aexit__(self, *exc):
                    lifecycle.append("agent_exited")
                    return await super().__aexit__(*exc)

            sdk_agent = TrackingAgent(response)
            runner = AntigravitySdkRunner(
                AGENT,
                False,
                {},
                conversation_factory=lambda *_args: _PersistentAntigravityConversation(
                    lambda _resume_id: sdk_agent
                ),
            )
            consumer = asyncio.create_task(_collect(runner))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assertTrue(response.closed)
            self.assertTrue(sdk_agent.exited)
            self.assertEqual(lifecycle, ["response_closed", "agent_exited"])

        for close_error in (False, True):
            with self.subTest(close_error=close_error):
                asyncio.run(scenario(close_error))

    def test_reasoning_hidden_unless_verbose_and_never_leaks_signature(self):
        quiet = _run(_FakeResponse(_sample()["chat_response"]), verbose=False)
        self.assertFalse(
            any(e.type == "status" and "create the file" in (e.text or "") for e in quiet)
        )

        loud = _run(_FakeResponse(_sample()["chat_response"]), verbose=True)
        self.assertTrue(
            any(e.type == "status" and "create the file" in (e.text or "") for e in loud)
        )
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


class AntigravityConversationLifecycleTests(unittest.TestCase):
    @staticmethod
    async def _turn(runner, prompt):
        events = []

        async def emit(event):
            events.append(event)

        outcome = await runner.run_turn(prompt, Path("/workspace"), emit)
        return events, outcome

    @staticmethod
    def _runner(conversation):
        return AntigravitySdkRunner(
            AGENT,
            False,
            {},
            conversation_factory=lambda *_args: conversation,
        )

    def test_one_agent_context_spans_two_turns_and_captures_stable_id(self):
        conversation_id = "conv-" + ("a" * 32)

        class SequencedAgent:
            def __init__(self):
                self.responses = [
                    _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "one"}]}),
                    _FakeResponse({"chunks": [{"type": "Text", "step_index": 1, "text": "two"}]}),
                ]
                self.conversation_id = conversation_id
                self.prompts = []
                self.enter_calls = 0
                self.exit_calls = 0

            async def __aenter__(self):
                self.enter_calls += 1
                return self

            async def __aexit__(self, *_exc):
                self.exit_calls += 1
                return False

            async def chat(self, prompt):
                self.prompts.append(prompt)
                return self.responses.pop(0)

        sdk_agent = SequencedAgent()
        resume_ids = []
        conversation = _PersistentAntigravityConversation(
            lambda resume_id: resume_ids.append(resume_id) or sdk_agent
        )
        runner = self._runner(conversation)

        async def scenario():
            self.assertFalse(runner.conversation_active())
            first = await self._turn(runner, "one")
            self.assertTrue(runner.conversation_active())
            second = await self._turn(runner, "two")
            self.assertTrue(runner.conversation_active())
            await runner.close()
            await runner.close()
            self.assertFalse(runner.conversation_active())
            return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(
            [first[1].outcome, second[1].outcome],
            ["completed", "completed"],
        )
        self.assertEqual(sdk_agent.prompts, ["one", "two"])
        self.assertEqual((sdk_agent.enter_calls, sdk_agent.exit_calls), (1, 1))
        self.assertEqual(resume_ids, [None])
        for events, _outcome in (first, second):
            self.assertTrue(
                any(
                    (event.raw or {}).get("provider_session_id") == conversation_id
                    for event in events
                )
            )

    def test_reset_retains_id_and_next_connection_uses_strict_resume(self):
        conversation_id = "conv-" + ("b" * 32)
        resume_ids = []
        agents = []

        def factory(resume_id):
            resume_ids.append(resume_id)
            response = _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "ok"}]})
            sdk_agent = _FakeAgent(response, conversation_id=conversation_id)
            agents.append(sdk_agent)
            return sdk_agent

        conversation = _PersistentAntigravityConversation(factory)

        async def scenario():
            self.assertFalse(conversation.active())
            first = await conversation.run("one")
            conversation.note_session_id(first.conversation_id)
            self.assertTrue(conversation.active())
            await conversation.reset()
            self.assertTrue(conversation.active())
            second = await conversation.run("two")
            self.assertTrue(conversation.active())
            await conversation.close()
            await conversation.close()
            self.assertFalse(conversation.active())
            return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first.conversation_id, conversation_id)
        self.assertEqual(second.conversation_id, conversation_id)
        self.assertEqual(resume_ids, [None, conversation_id])
        self.assertTrue(all(agent.exited for agent in agents))

    def test_id_that_appears_during_resolve_is_retained_for_resume(self):
        conversation_id = "conv-" + ("j" * 32)
        resume_ids = []

        class DelayedIdResponse(_FakeResponse):
            def __init__(self, agent):
                super().__init__({"chunks": [{"type": "Text", "step_index": 0, "text": "first"}]})
                self.agent = agent

            async def resolve(self):
                self.agent.conversation_id = conversation_id
                return await super().resolve()

        def factory(resume_id):
            resume_ids.append(resume_id)
            agent = _FakeAgent(
                _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "resumed"}]}),
                conversation_id=conversation_id if resume_id is not None else None,
            )
            if resume_id is None:
                agent._response = DelayedIdResponse(agent)
            return agent

        conversation = _PersistentAntigravityConversation(factory)

        async def scenario():
            first = await conversation.run("first")
            await conversation.reset()
            second = await conversation.run("second")
            await conversation.close()
            return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first.conversation_id, conversation_id)
        self.assertEqual(second.conversation_id, conversation_id)
        self.assertEqual(resume_ids, [None, conversation_id])

    def test_default_conversation_reuses_save_dir_until_close(self):
        conversation_id = "conv-" + ("i" * 32)
        factory_calls = []

        def fake_agent_factory(
            _agent,
            _options,
            _workdir,
            *,
            conversation_id=None,
            save_dir=None,
        ):
            factory_calls.append((conversation_id, save_dir))
            return _FakeAgent(
                _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "ok"}]}),
                conversation_id="conv-" + ("i" * 32),
            )

        with mock.patch(
            "agent_collab.backends.antigravity_sdk.backend._default_agent_factory",
            side_effect=fake_agent_factory,
        ):
            conversation = _default_conversation(AGENT, {}, Path("/workspace"))

            async def scenario():
                await conversation.run("first")
                save_dir = factory_calls[0][1]
                self.assertIsNotNone(save_dir)
                self.assertTrue(Path(save_dir).is_dir())
                await conversation.reset()
                await conversation.run("resumed")
                await conversation.close()
                return save_dir

            save_dir = asyncio.run(scenario())

        self.assertEqual(
            factory_calls,
            [(None, save_dir), (conversation_id, save_dir)],
        )
        self.assertFalse(Path(save_dir).exists())

    def test_abnormal_completion_resets_exactly_once_and_keeps_identity_active(self):
        conversation_id = "conv-" + ("c" * 32)

        class EmptyConversation:
            def __init__(self):
                self.noted_ids = []
                self.reset_calls = 0
                self.close_calls = 0

            def active(self):
                return bool(self.noted_ids)

            async def run(self, _prompt):
                from agent_collab.backends.antigravity_sdk.backend import AntigravityTurn

                return AntigravityTurn([], None, conversation_id, True)

            def note_session_id(self, value):
                self.noted_ids.append(value)

            async def reset(self):
                self.reset_calls += 1

            async def close(self):
                self.close_calls += 1

        conversation = EmptyConversation()
        runner = self._runner(conversation)

        async def scenario():
            return await self._turn(runner, "empty")

        events, outcome = asyncio.run(scenario())
        self.assertEqual(
            (outcome.outcome, outcome.code),
            ("failed", "provider_empty_response"),
        )
        self.assertEqual(conversation.noted_ids, [conversation_id])
        self.assertEqual(conversation.reset_calls, 1)
        self.assertTrue(runner.conversation_active())
        self.assertTrue(
            any((event.raw or {}).get("provider_session_id") == conversation_id for event in events)
        )

    def test_abnormal_response_without_id_fails_once_then_restarts_fresh(self):
        class FailingResponse:
            usage_metadata = None

            def __init__(self):
                self.cancel_calls = 0
                self.close_calls = 0

            async def resolve(self):
                raise RuntimeError("resolve failed")

            async def cancel(self):
                self.cancel_calls += 1

            async def aclose(self):
                self.close_calls += 1

        response = FailingResponse()
        failed_agent = _FakeAgent(response)
        recovered_agent = _FakeAgent(
            _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "recovered"}]}),
            conversation_id="conv-" + ("k" * 32),
        )
        resume_ids = []

        def factory(resume_id):
            resume_ids.append(resume_id)
            return failed_agent if len(resume_ids) == 1 else recovered_agent

        conversation = _PersistentAntigravityConversation(factory)
        runner = self._runner(conversation)

        async def scenario():
            first = await self._turn(runner, "handed off")
            self.assertFalse(runner.conversation_active())
            second = await self._turn(runner, "required structural failure")
            self.assertFalse(runner.conversation_active())
            third = await self._turn(runner, "explicit fresh turn")
            self.assertTrue(runner.conversation_active())
            await runner.close()
            return first, second, third

        first, second, third = asyncio.run(scenario())
        self.assertEqual(first[1].code, "provider_transport_failed")
        self.assertEqual(second[1].code, "provider_transport_failed")
        self.assertEqual(third[1].outcome, "completed")
        self.assertEqual(response.cancel_calls, 1)
        self.assertEqual(response.close_calls, 1)
        self.assertTrue(failed_agent.exited)
        self.assertTrue(recovered_agent.exited)
        self.assertEqual(resume_ids, [None, None])
        self.assertEqual(conversation._pending_prompts, [])

    def test_concurrent_waiters_claim_only_their_own_queued_prompts(self):
        async def scenario():
            prompts = []

            class Agent(_FakeAgent):
                async def chat(self, prompt):
                    prompts.append(prompt)
                    return _FakeResponse(
                        {"chunks": [{"type": "Text", "step_index": 0, "text": prompt}]}
                    )

            conversation = _PersistentAntigravityConversation(
                lambda _resume_id: Agent(
                    _FakeResponse(
                        {"chunks": [{"type": "Text", "step_index": 0, "text": "unused"}]}
                    ),
                    conversation_id="conv-" + ("q" * 32),
                )
            )
            await conversation._lock.acquire()
            first = asyncio.create_task(conversation.run("prompt one"))
            await asyncio.sleep(0)
            second = asyncio.create_task(conversation.run("prompt two"))
            await asyncio.sleep(0)
            conversation._lock.release()
            turns = await asyncio.gather(first, second)
            await conversation.close()
            return turns, prompts

        turns, prompts = asyncio.run(scenario())
        self.assertEqual(prompts, ["prompt one", "prompt two"])
        self.assertEqual([turn.chunks[0].text for turn in turns], prompts)

    def test_prompt_cancelled_behind_slow_reset_is_replayed_after_resume(self):
        async def scenario():
            conversation_id = "conv-" + ("h" * 32)
            exit_entered = asyncio.Event()
            release_exit = asyncio.Event()
            prompts = []
            resume_ids = []

            class Agent(_FakeAgent):
                async def __aexit__(self, *_exc):
                    exit_entered.set()
                    await release_exit.wait()
                    return False

                async def chat(self, prompt):
                    prompts.append(prompt)
                    return self._response

            def factory(resume_id):
                resume_ids.append(resume_id)
                return Agent(
                    _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "done"}]}),
                    conversation_id=conversation_id,
                )

            conversation = _PersistentAntigravityConversation(factory)
            await conversation.run("first")
            reset_task = asyncio.create_task(conversation.reset())
            await asyncio.wait_for(exit_entered.wait(), timeout=1.0)

            blocked_run = asyncio.create_task(conversation.run("queued delta"))
            await asyncio.sleep(0)
            blocked_run.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await blocked_run

            release_exit.set()
            await reset_task
            resumed = await conversation.run("next delta")
            await conversation.close()
            return resumed, prompts, resume_ids

        resumed, prompts, resume_ids = asyncio.run(scenario())
        self.assertEqual(resumed.conversation_id, "conv-" + ("h" * 32))
        self.assertEqual(prompts, ["first", "queued delta\n\nnext delta"])
        self.assertEqual(resume_ids, [None, "conv-" + ("h" * 32)])

    def test_rejected_reopen_is_structured_and_never_falls_back_to_fresh(self):
        conversation_id = "conv-" + ("d" * 32)
        resume_ids = []

        def factory(resume_id):
            resume_ids.append(resume_id)
            if resume_id is not None:
                raise RuntimeError(f"conversation not found: {resume_id}")
            return _FakeAgent(
                _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "one"}]}),
                conversation_id=conversation_id,
            )

        conversation = _PersistentAntigravityConversation(factory)
        runner = self._runner(conversation)

        async def scenario():
            first = await self._turn(runner, "one")
            await conversation.reset()
            rejected = await self._turn(runner, "must resume")
            rejected_again = await self._turn(runner, "must still resume")
            await runner.close()
            return first, rejected, rejected_again

        first, rejected, rejected_again = asyncio.run(scenario())
        self.assertEqual(first[1].outcome, "completed")
        self.assertEqual(rejected[1].code, "provider_transport_failed")
        self.assertEqual(rejected_again[1].code, "provider_transport_failed")
        self.assertTrue(
            any(
                event.type == "error" and "conversation not found" in event.text
                for event in rejected[0]
            )
        )
        self.assertEqual(resume_ids, [None, conversation_id, conversation_id])
        self.assertFalse(any(value is None for value in resume_ids[1:]))

    def test_unsupported_reopen_is_structured_without_new_agent(self):
        conversation_id = "conv-" + ("e" * 32)
        resume_ids = []

        def factory(resume_id):
            resume_ids.append(resume_id)
            if resume_id is not None:
                raise BackendUnavailable(
                    "antigravity",
                    "sdk",
                    "strict resume is unsupported",
                    "install hint",
                )
            return _FakeAgent(
                _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "one"}]}),
                conversation_id=conversation_id,
            )

        conversation = _PersistentAntigravityConversation(factory)
        runner = self._runner(conversation)

        async def scenario():
            await self._turn(runner, "one")
            await conversation.reset()
            rejected = await self._turn(runner, "must resume")
            await runner.close()
            return rejected

        events, outcome = asyncio.run(scenario())
        self.assertEqual(outcome.code, "provider_transport_failed")
        self.assertTrue(any("strict resume is unsupported" in event.text for event in events))
        self.assertEqual(resume_ids, [None, conversation_id])

    def test_close_waits_for_cancellation_ignoring_run_then_is_idempotent(self):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            lifecycle = []

            class IgnoringResponse:
                usage_metadata = None

                async def resolve(self):
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    return [Text(0, "done")]

                async def aclose(self):
                    lifecycle.append("response_closed")

            class Agent(_FakeAgent):
                async def __aexit__(self, *_exc):
                    lifecycle.append("agent_closed")
                    return False

            conversation = _PersistentAntigravityConversation(
                lambda _resume_id: Agent(
                    IgnoringResponse(),
                    conversation_id="conv-" + ("f" * 32),
                )
            )
            run_task = asyncio.create_task(conversation.run("one"))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            run_task.cancel()
            close_task = asyncio.create_task(conversation.close())
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())
            release.set()
            await run_task
            await close_task
            await conversation.close()
            self.assertEqual(lifecycle, ["response_closed", "agent_closed"])

        asyncio.run(scenario())

    def test_agent_cleanup_failure_does_not_rewrite_committed_turn(self):
        class FailingExitAgent(_FakeAgent):
            async def __aexit__(self, *_exc):
                raise RuntimeError("cleanup failed")

        conversation = _PersistentAntigravityConversation(
            lambda _resume_id: FailingExitAgent(
                _FakeResponse({"chunks": [{"type": "Text", "step_index": 0, "text": "done"}]}),
                conversation_id="conv-" + ("g" * 32),
            )
        )
        runner = self._runner(conversation)

        async def scenario():
            _events, outcome = await self._turn(runner, "one")
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await runner.close()
            return outcome

        outcome = asyncio.run(scenario())
        self.assertEqual(outcome.outcome, "completed")


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
            model_fields = {
                "conversation_id": object(),
                "save_dir": object(),
                "session_continuation_mode": object(),
            }

            def __init__(self, **kwargs):
                captured["config"] = kwargs

        class Agent:
            def __init__(self, config):
                captured["agent_config"] = config

        fake_types = types.ModuleType("google.antigravity.types")

        class SessionContinuationMode:
            RESUME = "resume"

        fake_types.SessionContinuationMode = SessionContinuationMode
        fake_module.LocalAgentConfig = LocalAgentConfig
        fake_module.Agent = Agent
        with mock.patch.dict(
            sys.modules,
            {
                "google.antigravity": fake_module,
                "google.antigravity.types": fake_types,
            },
        ):
            result = _default_agent_factory(
                VERTEX_AGENT,
                {"model": "gemini-test"},
                Path("/tmp/antigravity-workspace"),
            )
            fresh_config = dict(captured["config"])
            resumed = _default_agent_factory(
                VERTEX_AGENT,
                {"model": "gemini-test"},
                Path("/tmp/antigravity-workspace"),
                conversation_id="conv-" + ("r" * 32),
                save_dir="/tmp/antigravity-state",
            )

        self.assertIsInstance(result, Agent)
        self.assertEqual(
            fresh_config,
            {
                "workspaces": ["/tmp/antigravity-workspace"],
                "model": "gemini-test",
                "vertex": True,
                "project": "test-project",
                "location": "us-central1",
            },
        )

        self.assertIsInstance(resumed, Agent)
        self.assertEqual(captured["config"]["conversation_id"], "conv-" + ("r" * 32))
        self.assertEqual(captured["config"]["session_continuation_mode"], "resume")
        self.assertEqual(captured["config"]["save_dir"], "/tmp/antigravity-state")

    def test_runner_with_default_factory_emits_actionable_error_event(self):
        runner = AntigravitySdkBackend().create_runner(AGENT, False, {})

        async def scenario():
            try:
                with mock.patch.dict(sys.modules, {"google.antigravity": None}):
                    return await _collect(runner)
            finally:
                await runner.close()

        events = asyncio.run(scenario())
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
        def dependency_health():
            return BackendHealth(
                status="ok",
                credentials=gemini_api_key_credentials(),
                version="0.1.8",
                checked_at="t",
                checks={"dependency": {"status": "present", "version": "0.1.8"}},
            )

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "abc"}):
            self.assertEqual(
                AntigravitySdkBackend(
                    dependency_probe=dependency_health,
                    libc_ver=lambda: ("glibc", "2.37"),
                    protobuf_version=lambda: "7.35.1",
                )
                .probe()
                .credentials,
                "ok",
            )
        env_no_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.dict(os.environ, env_no_key, clear=True):
            health = AntigravitySdkBackend(
                dependency_probe=dependency_health,
                libc_ver=lambda: ("glibc", "2.37"),
                protobuf_version=lambda: "7.35.1",
            ).probe()
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.credentials, "unknown")  # never "missing" -> never blocks


class NativeRuntimeProbeTests(unittest.TestCase):
    def _dependency_health(self):
        return BackendHealth(
            status="ok",
            credentials="ok",
            version="0.1.8",
            checked_at="t",
            checks={"dependency": {"status": "present", "version": "0.1.8"}},
        )

    def test_glibc_comparison_is_injectable(self):
        self.assertEqual(
            assess_native_runtime(("glibc", "2.25"), required="2.26")["status"],
            "incompatible",
        )
        self.assertEqual(
            assess_native_runtime(("glibc", "2.26"), required="2.26")["status"],
            "compatible",
        )
        self.assertEqual(
            assess_native_runtime(("", ""), required="2.26")["status"],
            "indeterminate",
        )

    def test_incompatible_native_runtime_is_definite_unavailability(self):
        health = AntigravitySdkBackend(
            dependency_probe=self._dependency_health,
            libc_ver=lambda: ("glibc", "2.25"),
            protobuf_version=lambda: "7.35.1",
        ).probe()
        self.assertEqual(health.status, "unavailable")
        self.assertEqual(health.reason_codes, ("native_runtime_incompatible",))
        self.assertEqual(health.checks["dependency"]["status"], "present")
        self.assertEqual(health.checks["native_runtime"]["status"], "incompatible")
        self.assertEqual(health.remediation[0]["code"], "use_compatible_native_runtime")
        self.assertIn("Do not replace", health.remediation[0]["message"])

    def test_compatible_native_runtime_preserves_dependency_health(self):
        health = AntigravitySdkBackend(
            dependency_probe=self._dependency_health,
            libc_ver=lambda: ("glibc", "2.43"),
            protobuf_version=lambda: "7.35.1",
        ).probe()
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.checks["native_runtime"]["status"], "compatible")

    def test_missing_distribution_version_blocks_unverified_runtime(self):
        def dependency_health():
            return BackendHealth(
                status="ok",
                credentials="unknown",
                version=None,
                checked_at="t",
                checks={"dependency": {"status": "present"}},
            )

        health = AntigravitySdkBackend(
            dependency_probe=dependency_health,
            libc_ver=lambda: ("glibc", "2.43"),
            protobuf_version=lambda: "6.33.6",
        ).probe()
        self.assertEqual(health.status, "unavailable")
        self.assertEqual(health.reason_codes, ("dependency_version_unknown",))
        self.assertEqual(
            health.checks["protobuf_runtime"]["status"],
            "indeterminate",
        )
        self.assertEqual(
            health.remediation[0]["code"],
            "reinstall_sdk_dependency",
        )

    def test_incompatible_protobuf_runtime_is_definite_unavailability(self):
        health = AntigravitySdkBackend(
            dependency_probe=self._dependency_health,
            libc_ver=lambda: ("glibc", "2.43"),
            protobuf_version=lambda: "6.33.6",
        ).probe()
        self.assertEqual(health.status, "unavailable")
        self.assertEqual(
            health.reason_codes,
            ("protobuf_runtime_incompatible",),
        )
        self.assertEqual(
            health.checks["protobuf_runtime"]["status"],
            "incompatible",
        )
        self.assertEqual(
            health.remediation[0]["code"],
            "use_compatible_protobuf_runtime",
        )
        self.assertIn("xai-sdk", health.remediation[0]["message"])


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
            validate_start_backends(
                self._config(), "solo", backend_options={"antigravity_sdk": {"mode": "plan"}}
            )
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "backend_options.antigravity_sdk.mode")
        self.assertIn("sdk", detail["message"])

    def test_vertex_configuration_is_separate_from_mcp_options(self):
        backend = AntigravitySdkBackend()
        schema = backend.option_schema(AGENT)
        self.assertEqual(set(schema), {"model"})
        self.assertEqual(
            backend.normalize_config(VERTEX_AGENT),
            {
                "vertex": True,
                "project": "test-project",
                "location": "us-central1",
            },
        )
        with self.assertRaises(BackendOptionError) as ctx:
            backend.normalize_options(AGENT, {"vertex": True})
        self.assertEqual(ctx.exception.field, "vertex")

    def test_vertex_requires_project_and_location(self):
        backend = AntigravitySdkBackend()
        with self.assertRaises(BackendOptionError) as missing_project:
            backend.normalize_config(
                AgentConfig(
                    id="ag",
                    type="antigravity",
                    backend="sdk",
                    backend_config={"vertex": True, "location": "us-central1"},
                )
            )
        self.assertEqual(missing_project.exception.field, "project")
        with self.assertRaises(BackendOptionError) as missing_location:
            backend.normalize_config(
                AgentConfig(
                    id="ag",
                    type="antigravity",
                    backend="sdk",
                    backend_config={"vertex": True, "project": "test-project"},
                )
            )
        self.assertEqual(missing_location.exception.field, "location")

    def test_vertex_fields_are_rejected_when_vertex_is_not_enabled(self):
        with self.assertRaises(BackendOptionError) as ctx:
            AntigravitySdkBackend().normalize_config(
                AgentConfig(
                    id="ag",
                    type="antigravity",
                    backend="sdk",
                    backend_config={"project": "test-project"},
                )
            )
        self.assertEqual(ctx.exception.field, "project")

    def test_inferred_cli_mode_does_not_block_sdk_selection(self):
        # The built-in antigravity agent carries `--mode accept-edits -p` (cli
        # posture). Selecting the sdk backend must NOT be blocked by that inferred
        # mode — only an explicit antigravity_sdk option is rejected.
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
        # SDK normalization never imports the CLI posture from argv.
        normalized = validate_start_options(config, "solo")
        self.assertNotIn("mode", normalized["antigravity_sdk"])
        selection = validate_start_backends(
            config, "solo", request_backend=None, backend_options={}
        )
        self.assertEqual(selection.agent_backends, {"ag": "sdk"})

    def test_sdk_settings_do_not_advertise_inferred_cli_mode(self):
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
        normalized = validate_start_options(config, "solo")
        settings = build_session_settings(config, "solo", normalized, agent_backends={"ag": "sdk"})
        entry = settings["agents"]["ag"]
        self.assertEqual(entry["backend"], "sdk")
        self.assertNotIn("mode", entry)  # mode is cli-only; not shown for sdk

    def test_settings_summary_replaces_command_preview_for_sdk(self):
        config = self._config()
        settings = build_session_settings(
            config,
            "solo",
            {"antigravity_sdk": {"model": "gemini-3.1-pro-high"}},
            agent_backends={"ag": "sdk"},
        )
        entry = settings["agents"]["ag"]
        self.assertEqual(entry["backend"], "sdk")
        self.assertNotIn("command_preview", entry)
        self.assertEqual(entry["backend_summary"]["package"], "google-antigravity")
        self.assertEqual(entry["backend_summary"]["options"], {"model": "gemini-3.1-pro-high"})
        self.assertEqual(entry["backend_summary"]["conversation"], "persistent")
        self.assertEqual(
            entry["capabilities"],
            {
                "resume": False,
                "interrupt": False,
                "tool_gate": False,
                "continuity": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
