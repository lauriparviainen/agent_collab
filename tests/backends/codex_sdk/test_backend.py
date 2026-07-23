"""Codex ``sdk`` backend tests (real-shape fakes; no live model call).

The fake object graph mirrors ``openai-codex==0.1.0b3``: a collected
``TurnResult`` owns ``ThreadItem`` roots, and one ``AsyncThread`` accepts several
``run`` calls. Production tests replace the lazy SDK import with a persistent
client/thread fake, including resume after reset and cancellation-insensitive
provider work.
"""

import asyncio
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock

from agent_collab import backends
from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.codex_sdk.backend import (
    CodexSdkBackend,
    CodexSdkRunner,
    CodexTurnOutcome,
    _default_conversation,
    _map_sdk_options,
    iter_codex_events,
    iter_codex_turn_events,
    sandbox_member_name,
)
from agent_collab.config import AgentConfig
from agent_collab.referee import Referee, RefereeConfig

AGENT = AgentConfig(id="codex", type="codex", backend="sdk")


class _TurnStatus(Enum):
    completed = "completed"
    interrupted = "interrupted"
    failed = "failed"


class _MessagePhase(Enum):
    commentary = "commentary"
    final_answer = "final_answer"


class _CommandStatus(Enum):
    completed = "completed"
    failed = "failed"


class _PatchStatus(Enum):
    completed = "completed"


class _Object:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ThreadItem:
    """Shape of the SDK's pydantic ``ThreadItem`` RootModel."""

    def __init__(self, root):
        self.root = root


def _agent_message(text, *, phase=_MessagePhase.final_answer, item_id="msg-1"):
    return _ThreadItem(_Object(type="agentMessage", id=item_id, text=text, phase=phase))


def _reasoning(*, summary=None, content=None, item_id="reason-1"):
    return _ThreadItem(
        _Object(
            type="reasoning",
            id=item_id,
            summary=[] if summary is None else summary,
            content=[] if content is None else content,
        )
    )


def _command(command="pytest -q", *, status=_CommandStatus.completed, exit_code=0):
    return _ThreadItem(
        _Object(
            type="commandExecution",
            id="cmd-1",
            command=command,
            cwd="/workspace",
            status=status,
            exit_code=exit_code,
            aggregated_output="1 passed",
            duration_ms=125,
        )
    )


def _file_change():
    # PatchChangeKind is itself another generated RootModel, not an enum.
    kind = _Object(root=_Object(type="update"))
    change = _Object(path="hello.py", kind=kind, diff="@@ changed @@")
    return _ThreadItem(
        _Object(type="fileChange", id="patch-1", changes=[change], status=_PatchStatus.completed)
    )


def _turn_result(
    *,
    final_response="Done.",
    items=None,
    status=_TurnStatus.completed,
    error=None,
):
    # All public TurnResult fields are represented, even when a focused mapper
    # test only consumes a subset.
    return _Object(
        id="turn-1",
        status=status,
        error=error,
        started_at=100,
        completed_at=200,
        duration_ms=100,
        final_response=final_response,
        items=list(items or []),
        usage=None,
    )


class _FakeConversation:
    def __init__(self, outcomes, *, error=None):
        self.outcomes = list(outcomes)
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
        if not self.outcomes:
            raise RuntimeError("no fake outcome")
        return self.outcomes.pop(0)

    def note_session_id(self, thread_id):
        self.noted_ids.append(thread_id)

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


def _run(result=None, *, verbose=False, options=None, error=None, thread_id="thread-9"):
    outcomes = [] if result is None else [CodexTurnOutcome(thread_id, result)]
    conversation = _FakeConversation(outcomes, error=error)
    runner = CodexSdkRunner(
        AGENT,
        verbose,
        options or {},
        conversation_factory=_conversation_factory(conversation),
    )

    async def collect():
        events = []

        async def emit(event):
            events.append(event)

        await runner.run_turn("do a thing", Path("."), emit)
        return events

    return asyncio.run(collect())


def _outcome(result=None, *, error=None):
    outcomes = [] if result is None else [CodexTurnOutcome("thread-9", result)]
    conversation = _FakeConversation(outcomes, error=error)
    runner = CodexSdkRunner(
        AGENT,
        False,
        {},
        conversation_factory=_conversation_factory(conversation),
    )

    async def collect():
        async def emit(_event):
            return None

        return await runner.run_turn("do a thing", Path("."), emit)

    return asyncio.run(collect())


class CodexEventMappingTests(unittest.TestCase):
    def test_collected_turn_status_controls_outcome(self):
        self.assertEqual(_outcome(_turn_result()).outcome, "completed")
        interrupted = _outcome(_turn_result(status=_TurnStatus.interrupted))
        self.assertEqual(
            (interrupted.outcome, interrupted.code), ("cancelled", "provider_turn_cancelled")
        )
        failed = _outcome(_turn_result(status=_TurnStatus.failed))
        self.assertEqual((failed.outcome, failed.code), ("failed", "provider_terminal_failure"))
        self.assertEqual(_outcome().code, "provider_transport_failed")

    def test_abnormal_result_resets_once_and_retains_continuation_identity(self):
        conversation = _FakeConversation(
            [CodexTurnOutcome("thread-9", _turn_result(status=_TurnStatus.failed))]
        )
        runner = CodexSdkRunner(
            AGENT,
            False,
            {},
            conversation_factory=_conversation_factory(conversation),
        )

        async def scenario():
            async def emit(_event):
                return None

            outcome = await runner.run_turn("fail", Path("."), emit)
            return outcome, runner.conversation_active()

        outcome, active = asyncio.run(scenario())
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(conversation.reset_calls, 1)
        self.assertTrue(active)

    def test_slow_reset_preserves_definitive_provider_outcome(self):
        class SlowResetConversation(_FakeConversation):
            async def reset(self):
                self.reset_calls += 1
                await asyncio.sleep(0.05)

        conversation = SlowResetConversation(
            [CodexTurnOutcome("thread-9", _turn_result(status=_TurnStatus.interrupted))]
        )
        runner = CodexSdkRunner(
            AGENT,
            False,
            {},
            conversation_factory=_conversation_factory(conversation),
        )

        async def scenario():
            async def emit(_event):
                return None

            return await runner.run_turn("interrupt", Path("."), emit)

        with mock.patch(
            "agent_collab.backends.codex_sdk.backend.SDK_CLOSE_GRACE_SECONDS",
            0.001,
        ):
            outcome = asyncio.run(scenario())

        self.assertEqual(
            (outcome.outcome, outcome.code),
            ("cancelled", "provider_turn_cancelled"),
        )
        self.assertEqual(conversation.reset_calls, 1)

    def test_provider_id_is_fed_back_and_close_is_idempotent_at_runner_seam(self):
        conversation = _FakeConversation([CodexTurnOutcome("thread-9", _turn_result())])
        runner = CodexSdkRunner(
            AGENT,
            False,
            {},
            conversation_factory=_conversation_factory(conversation),
        )

        async def scenario():
            async def emit(_event):
                return None

            outcome = await runner.run_turn("remember", Path("."), emit)
            self.assertTrue(runner.conversation_active())
            await runner.close()
            await runner.close()
            return outcome

        outcome = asyncio.run(scenario())
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(conversation.noted_ids, ["thread-9"])
        self.assertEqual(conversation.close_calls, 1)
        self.assertFalse(conversation.active())

    def test_final_response_is_message_first_then_verified_items_map(self):
        result = _turn_result(
            final_response="Ran the tests and edited hello.py.",
            items=[
                _command(),
                _file_change(),
                _agent_message("Ran the tests and edited hello.py."),
            ],
        )
        events = _run(result)
        content = [event for event in events if event.type in {"message", "command", "file_change"}]

        self.assertEqual(content[0].source, "codex")
        self.assertEqual(content[0].text, "Ran the tests and edited hello.py.")
        self.assertEqual([event.type for event in content], ["message", "command", "file_change"])

        command = content[1]
        self.assertEqual(command.source, "tool")
        self.assertEqual(command.raw["cwd"], "/workspace")
        self.assertEqual(command.raw["status"], "completed")
        self.assertEqual(command.raw["exit_code"], 0)
        self.assertEqual(command.raw["aggregated_output"], "1 passed")

        file_change = content[2]
        self.assertEqual(file_change.source, "tool")
        self.assertEqual(file_change.raw["status"], "completed")
        self.assertEqual(
            file_change.raw["changes"],
            [{"path": "hello.py", "kind": "update", "diff": "@@ changed @@"}],
        )

    def test_non_final_agent_message_is_preserved(self):
        result = _turn_result(
            final_response="Final answer.",
            items=[
                _agent_message("I am checking the tests.", phase=_MessagePhase.commentary),
                _agent_message("Final answer."),
            ],
        )
        messages = [event for event in _run(result) if event.type == "message"]
        self.assertEqual(
            [event.text for event in messages], ["Final answer.", "I am checking the tests."]
        )
        self.assertEqual(messages[1].raw["phase"], "commentary")

    def test_final_response_marker_wins_answer_ledger_over_trailing_message(self):
        result = _turn_result(
            final_response="Final answer.",
            items=[
                _agent_message("Final answer."),
                _agent_message("Trailing commentary.", phase=_MessagePhase.commentary),
            ],
        )
        events = list(iter_codex_turn_events(result, verbose=False))
        for event in events:
            event.agent_id = AGENT.id

        referee = Referee(RefereeConfig(mock=True, workdir=Path("."), color=False))
        answer = referee._find_turn_answer(events, 0, AGENT.id)

        self.assertTrue(events[0].raw["final"])
        self.assertEqual(answer["text"], "Final answer.")

    def test_agent_item_is_fallback_when_final_response_is_absent(self):
        result = _turn_result(final_response=None, items=[_agent_message("Collected message.")])
        messages = [event for event in _run(result) if event.type == "message"]
        self.assertEqual([event.text for event in messages], ["Collected message."])

    def test_reasoning_uses_summary_then_content_and_is_verbose_only(self):
        summary_result = _turn_result(
            final_response=None,
            items=[_reasoning(summary=["public summary"], content=["reasoning content"])],
        )
        quiet = _run(summary_result, verbose=False)
        self.assertFalse(any((event.raw or {}).get("reasoning") for event in quiet))
        loud = _run(summary_result, verbose=True)
        reasoning = [event for event in loud if (event.raw or {}).get("reasoning")]
        self.assertEqual(reasoning[0].text, "public summary")
        self.assertEqual(reasoning[0].raw["content"], ["reasoning content"])

        content_result = _turn_result(
            final_response=None, items=[_reasoning(content=["content fallback"])]
        )
        fallback = [
            event
            for event in _run(content_result, verbose=True)
            if (event.raw or {}).get("reasoning")
        ]
        self.assertEqual(fallback[0].text, "content fallback")

    def test_failed_command_remains_a_command_with_real_status(self):
        result = _turn_result(
            final_response=None,
            items=[_command("false", status=_CommandStatus.failed, exit_code=1)],
        )
        events = _run(result)
        commands = [event for event in events if event.type == "command"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].text, "false")
        self.assertEqual(commands[0].raw["status"], "failed")
        self.assertFalse(any(event.type == "error" for event in events))

    def test_turn_error_maps_to_error_event(self):
        error = _Object(message="app-server crashed", additional_details="transport closed")
        result = _turn_result(final_response=None, status=_TurnStatus.failed, error=error)
        errors = [event for event in _run(result) if event.type == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].text, "app-server crashed")
        self.assertEqual(errors[0].raw["additional_details"], "transport closed")
        self.assertEqual(errors[0].raw["status"], "failed")

    def test_unknown_root_is_only_a_verbose_status(self):
        unknown = _ThreadItem(_Object(type="webSearch", id="search-1", query="docs"))
        quiet = list(iter_codex_events(unknown, verbose=False))
        loud = list(iter_codex_events(unknown, verbose=True))
        self.assertEqual(quiet, [])
        self.assertEqual(loud[0].type, "status")
        self.assertEqual(loud[0].raw["item_type"], "webSearch")

    def test_missing_runtime_and_sdk_failures_surface_as_error_events(self):
        unavailable = BackendUnavailable("codex", "sdk", "openai_codex is not importable", "hint")
        missing = _run(error=unavailable)
        self.assertTrue(
            any(event.type == "error" and "not importable" in event.text for event in missing)
        )

        failed = _run(error=RuntimeError("authentication failed"))
        self.assertTrue(
            any(event.type == "error" and "authentication failed" in event.text for event in failed)
        )


class CodexSessionCaptureTests(unittest.TestCase):
    def test_thread_id_comes_from_thread_outcome_regardless_of_verbose(self):
        for verbose in (False, True):
            events = _run(_turn_result(), verbose=verbose, thread_id="thread-9")
            captured = [
                event
                for event in events
                if (event.raw or {}).get("provider_session_id") == "thread-9"
            ]
            self.assertEqual(len(captured), 1, "verbose={}".format(verbose))
            raw = captured[0].raw
            self.assertEqual(raw["provider_session_kind"], "thread")
            self.assertEqual(raw["agent_id"], AGENT.id)
            self.assertEqual(captured[0].source, "codex")


class CodexOptionMappingTests(unittest.TestCase):
    def test_map_sdk_options_keeps_verified_keys_and_normalizes_effort_alias(self):
        mapped = _map_sdk_options(
            {
                "model": "gpt-5-codex",
                "sandbox": "workspace-write",
                "reasoning_effort": "high",
                "profile": "p",
                "approval_policy": "never",
            }
        )
        self.assertEqual(
            mapped,
            {"model": "gpt-5-codex", "sandbox": "workspace-write", "reasoning_effort": "high"},
        )
        self.assertEqual(
            _map_sdk_options({"thinking_level": "minimal"}), {"reasoning_effort": "minimal"}
        )

    def test_sandbox_member_name_maps_cli_values_to_enum_members(self):
        self.assertEqual(sandbox_member_name("read-only"), "read_only")
        self.assertEqual(sandbox_member_name("workspace-write"), "workspace_write")
        self.assertEqual(sandbox_member_name("danger-full-access"), "full_access")
        self.assertIsNone(sandbox_member_name("nonesuch"))


class CodexProductionFactoryTests(unittest.TestCase):
    @staticmethod
    def _fake_module(state, results):
        module = ModuleType("openai_codex")
        state.setdefault("clients", 0)
        state.setdefault("entered", 0)
        state.setdefault("closed", 0)
        state.setdefault("starts", [])
        state.setdefault("resumes", [])
        state.setdefault("runs", [])
        state.setdefault("open", 0)
        state["results"] = list(results)

        class FakeCodexConfig:
            def __init__(self, *, codex_bin=None, env=None):
                self.codex_bin = codex_bin
                self.env = env

        class FakeSandbox:
            read_only = object()
            workspace_write = object()
            full_access = object()

        class FakeReasoningEffort:
            minimal = object()
            low = object()
            medium = object()
            high = object()
            xhigh = object()

        class FakeThread:
            def __init__(self, thread_id="thread-production"):
                self.id = thread_id

            async def run(self, prompt, **kwargs):
                state["runs"].append((prompt, kwargs))
                self.assert_open()
                result = state["results"].pop(0)
                if callable(result):
                    result = await result()
                if isinstance(result, BaseException):
                    raise result
                return result

            @staticmethod
            def assert_open():
                if state["open"] <= 0:
                    raise AssertionError("provider thread used after client close")

        class FakeAsyncCodex:
            def __init__(self, config=None):
                state["clients"] += 1
                state["client_config"] = config
                self.is_open = False

            async def __aenter__(self):
                state["entered"] += 1
                state["open"] += 1
                self.is_open = True
                return self

            async def __aexit__(self, exc_type, exc, tb):
                await self.close()

            async def close(self):
                if not self.is_open:
                    return
                self.is_open = False
                state["closed"] += 1
                state["open"] -= 1

            async def thread_start(self, **kwargs):
                state["starts"].append(kwargs)
                return FakeThread()

            async def thread_resume(self, thread_id, **kwargs):
                state["resumes"].append((thread_id, kwargs))
                error = state.get("resume_error")
                if error is not None:
                    raise error
                return FakeThread(thread_id)

        module.AsyncCodex = FakeAsyncCodex
        module.CodexConfig = FakeCodexConfig
        module.Sandbox = FakeSandbox
        module.generated = SimpleNamespace(
            v2_all=SimpleNamespace(ReasoningEffort=FakeReasoningEffort)
        )
        return module, FakeSandbox, FakeReasoningEffort

    @staticmethod
    def _runner(agent=AGENT, options=None):
        return CodexSdkRunner(
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

    def test_one_client_and_thread_are_reused_across_two_turns(self):
        state = {}
        module, sandbox, effort = self._fake_module(
            state,
            [_turn_result(final_response="one"), _turn_result(final_response="two")],
        )
        configured_agent = AgentConfig(
            id="codex",
            type="codex",
            command="codex",
            backend="sdk",
            env={"OPENAI_API_KEY": "agent-scoped-key"},
        )
        runner = self._runner(
            configured_agent,
            {
                "model": "gpt-5-codex",
                "sandbox": "workspace-write",
                "reasoning_effort": "high",
            },
        )

        with (
            mock.patch.dict(sys.modules, {"openai_codex": module}),
            mock.patch(
                "agent_collab.backends.codex_sdk.backend.shutil.which",
                return_value="/opt/codex/bin/codex",
            ),
        ):

            async def scenario():
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
        self.assertEqual(state["entered"], 1)
        self.assertEqual(state["closed"], 1)
        self.assertEqual(state["open"], 0)
        self.assertEqual(len(state["starts"]), 1)
        self.assertEqual(state["resumes"], [])
        self.assertEqual(state["client_config"].codex_bin, "/opt/codex/bin/codex")
        self.assertEqual(
            state["client_config"].env,
            {"OPENAI_API_KEY": "agent-scoped-key"},
        )
        self.assertEqual(
            state["starts"][0],
            {"cwd": "/workspace", "model": "gpt-5-codex", "sandbox": sandbox.workspace_write},
        )
        self.assertEqual(
            state["runs"],
            [("turn one", {"effort": effort.high}), ("turn two", {"effort": effort.high})],
        )
        self.assertNotIn("working_directory", state["starts"][0])
        for events, _outcome in (first, second):
            self.assertTrue(any(event.type == "message" for event in events))
            self.assertTrue(
                any(
                    (event.raw or {}).get("provider_session_id") == "thread-production"
                    for event in events
                )
            )
        self.assertFalse(runner.conversation_active())

    def test_abnormal_turn_resets_once_then_reconnects_with_captured_id(self):
        state = {}
        module, _, _ = self._fake_module(
            state,
            [
                _turn_result(final_response="one"),
                _turn_result(final_response=None, status=_TurnStatus.failed),
                _turn_result(final_response="three"),
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"openai_codex": module}):

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
            [
                "completed",
                "failed",
                "completed",
            ],
        )
        self.assertEqual(len(state["starts"]), 1)
        self.assertEqual(
            state["resumes"],
            [("thread-production", {"cwd": "/workspace"})],
        )
        self.assertEqual(state["entered"], 2)
        self.assertEqual(state["closed"], 2)

    def test_resume_rejection_is_structured_and_never_starts_fresh(self):
        state = {}
        module, _, _ = self._fake_module(
            state,
            [
                _turn_result(final_response="one"),
                _turn_result(final_response=None, status=_TurnStatus.failed),
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"openai_codex": module}):

            async def scenario():
                await self._collect(runner, "one")
                await self._collect(runner, "fail and reset")
                state["resume_error"] = RuntimeError("thread expired")
                rejected = await self._collect(runner, "must resume")
                rejected_again = await self._collect(runner, "must still resume")
                await runner.close()
                return rejected, rejected_again

            rejected, rejected_again = asyncio.run(scenario())

        self.assertEqual(rejected[1].code, "provider_transport_failed")
        self.assertEqual(rejected_again[1].code, "provider_transport_failed")
        self.assertEqual(len(state["starts"]), 1)
        self.assertEqual(
            [thread_id for thread_id, _kwargs in state["resumes"]],
            ["thread-production", "thread-production"],
        )
        self.assertFalse(runner.conversation_active())

    def test_failed_resume_replays_undelivered_delta_after_recovery(self):
        state = {}
        module, _, _ = self._fake_module(
            state,
            [
                _turn_result(final_response="one"),
                _turn_result(final_response=None, status=_TurnStatus.failed),
                _turn_result(final_response="recovered"),
            ],
        )
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"openai_codex": module}):

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
        self.assertEqual(len(state["starts"]), 1)
        self.assertEqual(
            [thread_id for thread_id, _kwargs in state["resumes"]],
            ["thread-production", "thread-production"],
        )
        self.assertEqual(
            state["runs"],
            [
                ("one", {}),
                ("fail and reset", {}),
                ("SECRET_BLUE\n\nwhat was blue?", {}),
            ],
        )

    def test_close_serializes_behind_cancelled_provider_run(self):
        state = {}
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_result():
            started.set()
            await release.wait()
            return _turn_result()

        module, _, _ = self._fake_module(state, [blocking_result])
        runner = self._runner()

        with mock.patch.dict(sys.modules, {"openai_codex": module}):

            async def scenario():
                turn = asyncio.create_task(self._collect(runner, "block"))
                await started.wait()
                turn.cancel()
                await asyncio.sleep(0)
                close = asyncio.create_task(runner.close())
                await asyncio.sleep(0)
                self.assertFalse(close.done())
                self.assertEqual(state["closed"], 0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await turn
                await close

            asyncio.run(scenario())

        self.assertEqual(state["closed"], 1)
        self.assertEqual(state["open"], 0)

    def test_default_conversation_reports_missing_or_incompatible_module(self):
        with mock.patch.dict(sys.modules, {"openai_codex": None}):
            with self.assertRaises(BackendUnavailable) as missing:
                _default_conversation(AGENT, {}, Path("."))
        self.assertIn("openai-codex", str(missing.exception))

        incompatible = ModuleType("openai_codex")
        incompatible.AsyncCodex = object
        with mock.patch.dict(sys.modules, {"openai_codex": incompatible}):
            with self.assertRaises(BackendUnavailable) as wrong_api:
                _default_conversation(AGENT, {}, Path("."))
        self.assertIn("thread_start", str(wrong_api.exception))


class CodexBackendSurfaceTests(unittest.TestCase):
    def test_registered_pair_and_honest_capabilities(self):
        self.assertTrue(backends.is_registered("codex", "sdk"))
        caps = backends.capabilities_for("codex", "sdk")
        self.assertEqual(
            caps.to_dict(),
            {"resume": False, "interrupt": False, "tool_gate": False, "continuity": True},
        )

    def test_probe_reports_unavailable_with_install_hint(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            health = CodexSdkBackend().probe()
        self.assertEqual(health.status, "unavailable")
        self.assertIn("openai-codex", health.reason)

    def test_settings_summary_has_package_and_verified_options(self):
        summary = CodexSdkBackend().settings_summary(
            AGENT,
            {"model": "gpt-5-codex", "sandbox": "read-only", "thinking_level": "high"},
        )
        self.assertEqual(summary["backend"], "sdk")
        self.assertEqual(summary["package"], "openai-codex")
        self.assertEqual(summary["conversation"], "persistent")
        self.assertEqual(
            summary["options"],
            {"model": "gpt-5-codex", "sandbox": "read-only", "reasoning_effort": "high"},
        )


if __name__ == "__main__":
    unittest.main()
