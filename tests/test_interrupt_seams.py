"""Hermetic coverage for Stage 1 interrupt seams (#20 slice d)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.backends.antigravity_sdk.backend import AntigravitySdkRunner
from agent_collab.backends.claude_sdk.backend import ClaudeSdkRunner
from agent_collab.backends.codex_sdk.backend import CodexSdkRunner
from agent_collab.backends.xai_sdk.backend import XaiSdkRunner
from agent_collab.config import AgentConfig
from agent_collab.daemon import INTERRUPT_ACKNOWLEDGE_SECONDS, SessionManager, StartSessionRequest
from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.referee import Referee
from agent_collab.runners import AgentRunner, BackendDryRunRunner, DryRunRunner, MockRunner
from agent_collab.sandbox.worker_session import interrupt_active_session


def _sdk_runner(cls, agent_id: str, agent_type: str):
    return cls(
        AgentConfig(id=agent_id, type=agent_type, backend="sdk"),
        False,
        {},
        lambda _options, _workdir: None,
    )


class _FakeWorkerSession:
    def __init__(self, *, active_run: str | None = "run-1") -> None:
        self._active_run = active_run
        self._closed = False
        self._terminal = False
        self.interrupt_calls: list[str] = []

    async def interrupt_active(self) -> bool:
        if self._closed or self._terminal or not self._active_run:
            return False
        self.interrupt_calls.append(self._active_run)
        return True


class DefaultInterruptRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_mock_dry_run_and_xai_remain_false(self) -> None:
        self.assertFalse(await AgentRunner().interrupt_request())
        self.assertFalse(await MockRunner("claude").interrupt_request())
        self.assertFalse(await DryRunRunner("claude", ["echo"]).interrupt_request())
        self.assertFalse(await BackendDryRunRunner("claude", "claude_sdk").interrupt_request())
        xai = _sdk_runner(XaiSdkRunner, "xai", "xai")
        self.assertFalse(await xai.interrupt_request())


class WorkerBackedInterruptRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_request_writes_for_active_run_else_false(self) -> None:
        for cls, agent_id, agent_type in (
            (ClaudeSdkRunner, "claude", "claude"),
            (CodexSdkRunner, "codex", "codex"),
            (AntigravitySdkRunner, "antigravity", "antigravity"),
        ):
            runner = _sdk_runner(cls, agent_id, agent_type)
            self.assertFalse(await runner.interrupt_request())
            idle = _FakeWorkerSession(active_run=None)
            runner._worker_session = idle
            self.assertFalse(await runner.interrupt_request())
            live = _FakeWorkerSession(active_run="run-9")
            runner._worker_session = live
            self.assertTrue(await runner.interrupt_request())
            self.assertEqual(live.interrupt_calls, ["run-9"])

    async def test_interrupt_active_session_helper_matches_session(self) -> None:
        self.assertFalse(await interrupt_active_session(None))
        idle = _FakeWorkerSession(active_run=None)
        self.assertFalse(await interrupt_active_session(idle))
        live = _FakeWorkerSession()
        self.assertTrue(await interrupt_active_session(live))
        self.assertEqual(live.interrupt_calls, ["run-1"])


class StopPathInterruptSeamTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_until(self, predicate, timeout=2.0, message="condition"):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail(f"{message} not reached before timeout")

    async def test_pause_runner_stop_is_unrequested_fallback_without_delay(self) -> None:
        turn_started = asyncio.Event()

        class PauseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await asyncio.Event().wait()
                return TurnOutcome("completed")

        runners = {"claude_cli": PauseRunner(), "codex_cli": PauseRunner()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="stop me",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    await turn_started.wait()
                    stopped = await asyncio.wait_for(
                        manager.stop_session(state.session_id),
                        timeout=1.0,
                    )
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(
            stopped.stop,
            {
                "requested": False,
                "provider_acknowledged": False,
                "fallback_cancelled": True,
                "approvals_denied": 0,
            },
        )
        self.assertNotEqual(stopped.status, "done")

    async def test_interrupt_acknowledged_keeps_completed_turn_and_stops(self) -> None:
        turn_started = asyncio.Event()

        class FinishOnInterruptRunner(AgentRunner):
            def __init__(self) -> None:
                self._release = asyncio.Event()
                self.cancelled = False

            async def interrupt_request(self) -> bool:
                self._release.set()
                return True

            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await self._release.wait()
                await emit(Event.create("claude", "message", "finished first"))
                return TurnOutcome("completed")

        runners = {
            "claude_cli": FinishOnInterruptRunner(),
            "codex_cli": FinishOnInterruptRunner(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="ack me",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    await turn_started.wait()
                    stopped = await manager.stop_session(state.session_id)
                    later = manager.get_session(state.session_id)
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(later.status, "stopped")
        self.assertEqual(
            stopped.stop,
            {
                "requested": True,
                "provider_acknowledged": True,
                "fallback_cancelled": False,
                "approvals_denied": 0,
            },
        )
        outcomes = [item["outcome"] for item in (stopped.turn_outcomes or [])]
        self.assertIn("completed", outcomes)
        self.assertNotIn("interrupted", outcomes)

    async def test_interrupt_hang_falls_back_to_cancel_active(self) -> None:
        turn_started = asyncio.Event()

        class HangRunner(AgentRunner):
            def __init__(self) -> None:
                self.cancelled = False
                self.killed = False

            async def interrupt_request(self) -> bool:
                return True

            async def cancel_active(self) -> None:
                self.killed = True

            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    await self.cancel_active()
                    raise

        hang = HangRunner()
        runners = {"claude_cli": hang, "codex_cli": HangRunner()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    with mock.patch(
                        "agent_collab.daemon.INTERRUPT_ACKNOWLEDGE_SECONDS",
                        0.05,
                    ):
                        state = await manager.start_session(
                            StartSessionRequest(
                                task="hang me",
                                mock=True,
                                max_turns=1,
                                timeout=5,
                                workdir=root,
                            )
                        )
                        await turn_started.wait()
                        stopped = await asyncio.wait_for(
                            manager.stop_session(state.session_id),
                            timeout=2.0,
                        )
        self.assertEqual(stopped.status, "stopped")
        self.assertTrue(stopped.stop["requested"])
        self.assertFalse(stopped.stop["provider_acknowledged"])
        self.assertTrue(stopped.stop["fallback_cancelled"])
        self.assertTrue(hang.cancelled)
        self.assertTrue(hang.killed)
        self.assertEqual(INTERRUPT_ACKNOWLEDGE_SECONDS, 2.0)

    async def test_stop_denies_pending_first_then_records_count(self) -> None:
        turn_started = asyncio.Event()
        interrupt_after_deny = []

        class TrackingPause(AgentRunner):
            async def interrupt_request(self) -> bool:
                interrupt_after_deny.append(True)
                return False

            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await asyncio.Event().wait()
                return TurnOutcome("completed")

        runners = {"claude_cli": TrackingPause(), "codex_cli": TrackingPause()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="deny then interrupt",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    await turn_started.wait()
                    await manager.register_approval(
                        state.session_id,
                        request_id="a1",
                        agent_id="claude_cli",
                        tool_name="Bash",
                        summary="true",
                        turn_id="turn-1",
                    )
                    managed = manager._sessions[state.session_id]
                    self.assertGreaterEqual(managed.approvals.unresolved_count(), 1)
                    stopped = await manager.stop_session(state.session_id)
        self.assertEqual(stopped.status, "stopped")
        self.assertTrue(interrupt_after_deny)
        self.assertEqual(stopped.stop["approvals_denied"], 1)
        self.assertFalse(stopped.stop["requested"])
        self.assertTrue(stopped.stop["fallback_cancelled"])
        self.assertEqual(managed.approvals.unresolved_count(), 0)

    async def test_stop_detail_is_stripped_from_session_index(self) -> None:
        turn_started = asyncio.Event()

        class PauseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await asyncio.Event().wait()
                return TurnOutcome("completed")

        runners = {"claude_cli": PauseRunner(), "codex_cli": PauseRunner()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = Path(tmp) / "session-index.json"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="strip stop",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    await turn_started.wait()
                    stopped = await manager.stop_session(state.session_id)
                    record = json.loads(index_path.read_text(encoding="utf-8"))["sessions"][
                        state.session_id
                    ]
        self.assertIsNotNone(stopped.stop)
        self.assertNotIn("stop", record)
        self.assertNotIn("pending_approvals", record)
        status = manager.get_session(state.session_id)
        self.assertEqual(status.stop, stopped.stop)
