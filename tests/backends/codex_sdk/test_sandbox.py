"""Hermetic coverage for the Codex SDK outer-sandbox adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent_collab.backends.codex_sdk.backend import CodexSdkRunner, CodexTurnOutcome
from agent_collab.backends.codex_sdk.sandbox import CodexSdkSandboxAdapter
from agent_collab.backends.codex_sdk.worker import CodexSdkWorkerBackend
from agent_collab.backends.common.sdk import provider_session_event
from agent_collab.config import AgentConfig
from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.sandbox.specs import SandboxContext, SandboxPolicy, SandboxSupport


class CodexSdkSandboxAdapterTests(unittest.TestCase):
    def test_describes_sdk_worker_support(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "codex-home"
            state.mkdir(mode=0o700)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            adapter = CodexSdkSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {"CODEX_HOME": str(state), "HOME": str(root)},
                )
            )
            self.assertIs(spec.support, SandboxSupport.SDK_WORKER)
            self.assertIn(SandboxPolicy.READ_ONLY, spec.policies)
            self.assertEqual(spec.state_roots[0].destination, state)
            self.assertEqual(
                dict(spec.native_profile.sdk_options).get("sandbox"),
                "danger-full-access",
            )
            payload = adapter.worker_open_payload(
                options={"model": "gpt-5.6-luna", "sandbox": "read-only"},
                workspace=workspace,
                cwd=workspace / "sub",
                agent_env={},
                codex_bin=None,
                verbose=False,
            )
            self.assertEqual(payload["backend"], "codex_sdk")
            self.assertEqual(payload["options"]["sandbox"], "danger-full-access")
            self.assertEqual(payload["native"]["sandbox"], "danger-full-access")
            self.assertEqual(payload["cwd"], str(workspace / "sub"))


class CodexSdkWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_close_force_tears_down_worker_when_cancelled(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )

        class _Session:
            def __init__(self) -> None:
                self.close_started = asyncio.Event()
                self.force_teardowns = 0

            async def close(self) -> None:
                self.close_started.set()
                await asyncio.Event().wait()

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        session = _Session()
        runner._worker_session = session
        task = asyncio.create_task(runner.close())
        await session.close_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.force_teardowns, 1)
        self.assertIsNone(runner._worker_session)

    def test_conversation_active_requires_captured_id_while_worker_is_live(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )

        class _Session:
            terminal = False

        runner._worker_session = _Session()
        self.assertFalse(runner.conversation_active())
        runner._worker_provider_active = True
        self.assertTrue(runner.conversation_active())

    async def test_completed_turn_without_thread_id_soft_drops_worker(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self) -> None:
                self.force_teardowns = 0

            async def run(self, _prompt, emit=None):
                del emit
                return [], TurnOutcome("completed")

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        session = _Session()

        async def worker_for(_workdir):
            runner._worker_session = session
            return session

        runner._worker_for = worker_for

        async def emit(_event) -> None:
            return None

        outcome = await runner.run_turn("prompt", Path("/workspace"), emit)

        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(session.force_teardowns, 1)
        self.assertIsNone(runner._worker_session)
        self.assertFalse(runner._worker_terminal)
        self.assertFalse(runner.conversation_active())

    async def test_captured_thread_id_keeps_worker_and_marks_conversation_active(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self) -> None:
                self.force_teardowns = 0

            async def run(self, _prompt, emit=None):
                await emit(provider_session_event("codex", "codex", "thread-1", "thread"))
                return [], TurnOutcome("completed")

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        session = _Session()

        async def worker_for(_workdir):
            runner._worker_session = session
            return session

        runner._worker_for = worker_for

        async def emit(_event) -> None:
            return None

        outcome = await runner.run_turn("prompt", Path("/workspace"), emit)

        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(session.force_teardowns, 0)
        self.assertIs(runner._worker_session, session)
        self.assertTrue(runner._worker_provider_active)
        self.assertTrue(runner.conversation_active())

    async def test_tracking_emit_accepts_marked_session_or_raw_id(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self, event: Event) -> None:
                self.event = event
                self.force_teardowns = 0

            async def run(self, _prompt, emit=None):
                await emit(self.event)
                return [], TurnOutcome("completed")

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        marked = Event.create("codex", "status", "marked only", {}).mark_provider_session(
            agent_id="codex",
            session_id="thread-marked",
            kind="thread",
        )
        raw_only = Event.create(
            "codex",
            "status",
            "raw only",
            {"provider_session_id": "thread-raw", "provider_session_kind": "thread"},
        )
        for event in (marked, raw_only):
            session = _Session(event)
            runner._worker_session = None
            runner._worker_provider_active = False
            runner._worker_terminal = False

            async def worker_for(_workdir, captured=session):
                runner._worker_session = captured
                return captured

            runner._worker_for = worker_for

            async def emit(_event) -> None:
                return None

            outcome = await runner.run_turn("prompt", Path("/workspace"), emit)
            self.assertEqual(outcome.outcome, "completed")
            self.assertEqual(session.force_teardowns, 0)
            self.assertTrue(runner.conversation_active())

    async def test_second_worker_turn_without_reemit_keeps_continuity(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self) -> None:
                self.runs = 0
                self.force_teardowns = 0

            async def run(self, _prompt, emit=None):
                self.runs += 1
                if self.runs == 1:
                    await emit(provider_session_event("codex", "codex", "thread-1", "thread"))
                    return [], TurnOutcome("completed")
                return [], TurnOutcome("failed", "provider_terminal_failure")

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        session = _Session()

        async def worker_for(_workdir):
            runner._worker_session = session
            return session

        runner._worker_for = worker_for

        async def emit(_event) -> None:
            return None

        first = await runner.run_turn("one", Path("/workspace"), emit)
        self.assertEqual(first.outcome, "completed")
        self.assertTrue(runner.conversation_active())
        second = await runner.run_turn("two", Path("/workspace"), emit)
        self.assertEqual(second.outcome, "failed")
        self.assertEqual(session.force_teardowns, 0)
        self.assertIs(runner._worker_session, session)
        self.assertTrue(runner.conversation_active())

    async def test_cancel_during_soft_drop_preserves_relaunch_eligibility(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self) -> None:
                self.drop_started = asyncio.Event()

            async def run(self, _prompt, emit=None):
                del emit
                return [], TurnOutcome("failed", "provider_empty_response")

            async def force_teardown(self) -> None:
                self.drop_started.set()
                await asyncio.Event().wait()

        session = _Session()

        async def worker_for(_workdir):
            runner._worker_session = session
            return session

        runner._worker_for = worker_for

        async def emit(_event) -> None:
            return None

        task = asyncio.create_task(runner.run_turn("prompt", Path("/workspace"), emit))
        await session.drop_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(runner._worker_session)
        self.assertFalse(runner._worker_terminal)

    async def test_interrupt_without_conversation_is_noop(self) -> None:
        backend = CodexSdkWorkerBackend()
        await backend.interrupt("run-1")

    async def test_interrupt_calls_conversation_once_and_maps_interrupted_status(self) -> None:
        class _Conversation:
            def __init__(self) -> None:
                self.interrupt_calls = 0
                self.reset_calls = 0
                self.started = asyncio.Event()
                self._release = asyncio.Event()

            async def run(self, prompt: str):
                del prompt
                self.started.set()
                await self._release.wait()
                return CodexTurnOutcome(
                    "thread-1",
                    SimpleNamespace(
                        id="turn-1",
                        status=SimpleNamespace(value="interrupted"),
                        error=None,
                        final_response=None,
                        items=[],
                    ),
                )

            def note_session_id(self, thread_id: str) -> None:
                del thread_id

            async def interrupt(self) -> bool:
                self.interrupt_calls += 1
                self._release.set()
                return True

            async def reset(self) -> None:
                self.reset_calls += 1

            async def close(self) -> None:
                return None

        conversation = _Conversation()
        backend = CodexSdkWorkerBackend()
        backend._conversation = conversation
        task = asyncio.create_task(backend.run("hello", run_id="r1"))
        await conversation.started.wait()
        await backend.interrupt("r1")
        _residual, outcome = await task
        self.assertEqual(conversation.interrupt_calls, 1)
        self.assertEqual(
            (outcome.outcome, outcome.code),
            ("interrupted", "local_turn_interrupted"),
        )
        self.assertEqual(conversation.reset_calls, 0)
        self.assertIs(backend._conversation, conversation)

    async def test_interrupt_completion_race_keeps_completed_outcome(self) -> None:
        class _Conversation:
            def __init__(self) -> None:
                self.interrupt_calls = 0
                self.reset_calls = 0
                self.started = asyncio.Event()
                self._release = asyncio.Event()

            async def run(self, prompt: str):
                del prompt
                self.started.set()
                await self._release.wait()
                return CodexTurnOutcome(
                    "thread-1",
                    SimpleNamespace(
                        id="turn-1",
                        status=SimpleNamespace(value="completed"),
                        error=None,
                        final_response="Done.",
                        items=[],
                    ),
                )

            def note_session_id(self, thread_id: str) -> None:
                del thread_id

            async def interrupt(self) -> bool:
                self.interrupt_calls += 1
                self._release.set()
                return True

            async def reset(self) -> None:
                self.reset_calls += 1

            async def close(self) -> None:
                return None

        conversation = _Conversation()
        backend = CodexSdkWorkerBackend()
        backend._conversation = conversation
        task = asyncio.create_task(backend.run("hello", run_id="r1"))
        await conversation.started.wait()
        await backend.interrupt("r1")
        _residual, outcome = await task
        self.assertEqual(conversation.interrupt_calls, 1)
        self.assertEqual(outcome.outcome, "completed")
        self.assertIsNone(outcome.code)
        self.assertEqual(conversation.reset_calls, 0)
        self.assertIs(backend._conversation, conversation)


if __name__ == "__main__":
    unittest.main()
