"""Hermetic coverage for Stage 4 increment 5 turn-level interrupt (#20)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_collab import backends as backend_registry
from agent_collab.api_schema import SessionStateModel
from agent_collab.backends.base import BackendCapabilities
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import (
    SessionManager,
    SessionNotFoundError,
    StartSessionRequest,
    _PreparedSessionStart,
)
from agent_collab.events import Event
from agent_collab.logging import SessionLogger
from agent_collab.outcomes import TurnOutcome
from agent_collab.referee import Referee, RefereeConfig
from agent_collab.resume import InterruptError, ResumeError, descriptor_is_eligible
from agent_collab.runners import AgentRunner
from agent_collab.server_http import AgentCollabHttpServer
from agent_collab.session_index import SessionIndex


_REAL_CAPABILITIES_FOR = backend_registry.capabilities_for


def _resume_stub(agent_type, backend_id):
    caps = _REAL_CAPABILITIES_FOR(agent_type, backend_id)
    return BackendCapabilities(
        resume=True,
        interrupt=caps.interrupt,
        tool_gate=caps.tool_gate,
        continuity=caps.continuity,
    )


async def _wait_until(predicate, timeout=2.0, message="condition"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{message} not reached before timeout")


class _CaptureWriter:
    def __init__(self):
        self.buffer = bytearray()

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def _request_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class CompletingRunner(AgentRunner):
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls = 0

    async def run_turn(self, prompt, workdir, emit):
        del prompt, workdir
        self.calls += 1
        await emit(Event.create(self.source, "message", f"{self.source} ok"))
        return TurnOutcome("completed")


class HangRunner(AgentRunner):
    def __init__(self, source: str, started: asyncio.Event, *, acknowledge: bool = False) -> None:
        self.source = source
        self.started = started
        self.acknowledge = acknowledge
        self.calls = 0
        self.cancelled = False
        self.killed = False
        self._active = True

    def conversation_active(self) -> bool:
        return self._active

    async def interrupt_request(self) -> bool:
        return self.acknowledge

    async def cancel_active(self) -> None:
        self.killed = True
        self._active = False

    async def run_turn(self, prompt, workdir, emit):
        del prompt, workdir
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self._active = False
            await self.cancel_active()
            raise
        await emit(Event.create(self.source, "message", f"{self.source} hung"))
        return TurnOutcome("completed")


class FinishOnInterruptRunner(AgentRunner):
    def __init__(self, source: str, started: asyncio.Event, outcome: TurnOutcome) -> None:
        self.source = source
        self.started = started
        self.outcome = outcome
        self._release = asyncio.Event()
        self.calls = 0

    async def interrupt_request(self) -> bool:
        self._release.set()
        return True

    async def run_turn(self, prompt, workdir, emit):
        del prompt, workdir
        self.calls += 1
        self.started.set()
        await self._release.wait()
        if self.outcome.outcome == "completed":
            await emit(Event.create(self.source, "message", f"{self.source} finished"))
        return self.outcome


class FirstThenHangRunner(AgentRunner):
    def __init__(self, source: str, directed_started: asyncio.Event) -> None:
        self.source = source
        self.directed_started = directed_started
        self.calls = 0

    async def interrupt_request(self) -> bool:
        return False

    async def run_turn(self, prompt, workdir, emit):
        del prompt, workdir
        self.calls += 1
        if self.calls == 1:
            await emit(Event.create(self.source, "message", "first"))
            return TurnOutcome("completed")
        if self.calls == 2:
            self.directed_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
        await emit(Event.create(self.source, "message", "follow-up"))
        return TurnOutcome("completed")


class AckThenFallbackRunner(AgentRunner):
    """Acknowledge the first interrupt, then hang a later directed turn for fallback."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.planned_started = asyncio.Event()
        self.steer_started = asyncio.Event()
        self._release_planned = asyncio.Event()
        self.calls = 0

    async def interrupt_request(self) -> bool:
        if self.calls == 1:
            self._release_planned.set()
            return True
        return False

    async def run_turn(self, prompt, workdir, emit):
        del prompt, workdir
        self.calls += 1
        if self.calls == 1:
            self.planned_started.set()
            await self._release_planned.wait()
            return TurnOutcome("interrupted", "local_turn_interrupted")
        self.steer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        await emit(Event.create(self.source, "message", "should not finish"))
        return TurnOutcome("completed")


class LastStageThenPendingHangRunner(AgentRunner):
    """Complete the last planned stage, then hang a queued post-loop directed turn."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.planned_started = asyncio.Event()
        self.hold_planned = asyncio.Event()
        self.pending_directed = asyncio.Event()
        self.steer_finished = False
        self.calls = 0

    async def interrupt_request(self) -> bool:
        return False

    async def run_turn(self, prompt, workdir, emit):
        del prompt, workdir
        self.calls += 1
        if self.calls == 1:
            self.planned_started.set()
            await self.hold_planned.wait()
            await emit(Event.create(self.source, "message", "planned done"))
            return TurnOutcome("completed")
        if self.calls == 2:
            self.pending_directed.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
        await emit(Event.create(self.source, "message", "steered"))
        self.steer_finished = True
        return TurnOutcome("completed")


class InterruptParkTests(unittest.IsolatedAsyncioTestCase):
    async def test_planned_workflow_interrupt_parks_and_accepts_post_message(self) -> None:
        started = asyncio.Event()
        claude = CompletingRunner("claude")
        codex = HangRunner("codex", started)
        runners = {
            "claude_cli": claude,
            "codex_cli": codex,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="interrupt mid-stage",
                            mock=True,
                            workflow="cross-review",
                            max_turns=3,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    interrupted = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    self.assertEqual(interrupted.status, "awaiting_input")
                    self.assertNotEqual(interrupted.status, "failed")
                    self.assertEqual(
                        interrupted.workflow_phase,
                        {"completed_stages": 1, "parked_in_input_loop": True},
                    )
                    self.assertIsNotNone(interrupted.interrupt)
                    self.assertEqual(claude.calls, 1)
                    await manager.post_message(
                        state.session_id,
                        "steer now",
                        target="claude_cli",
                    )
                    follow = await asyncio.wait_for(
                        manager.wait_result(state.session_id, 2000),
                        timeout=3.0,
                    )
                    await manager.stop_session(state.session_id)
        self.assertEqual(follow.status, "awaiting_input")
        self.assertNotEqual(follow.status, "failed")
        self.assertEqual(claude.calls, 2)
        self.assertEqual(codex.calls, 1)

    async def test_persisted_park_phase_skips_remaining_on_resume(self) -> None:
        started = asyncio.Event()
        runners = {
            "claude_cli": CompletingRunner("claude"),
            "codex_cli": HangRunner("codex", started),
        }
        resume_calls = []

        class ResumeRunner(AgentRunner):
            def __init__(self, name: str) -> None:
                self.name = name

            async def run_turn(self, prompt, workdir, emit):
                del prompt, workdir
                resume_calls.append(self.name)
                await emit(Event.create(self.name, "message", f"{self.name} later"))
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await live.start_session(
                        StartSessionRequest(
                            task="original task",
                            mock=True,
                            workflow="cross-review",
                            max_turns=3,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    interrupted = await asyncio.wait_for(
                        live.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    phase = dict(interrupted.workflow_phase or {})
                    await live.stop_session(state.session_id)

            self.assertEqual(phase.get("completed_stages"), 1)
            self.assertTrue(phase.get("parked_in_input_loop"))

            index_path = root / "resume-index.json"
            workdir = str(root)
            settings = {
                "agents": {"claude": {"type": "claude", "backend": "sdk"}},
                "workflow": {"sequence": ["claude", "codex"]},
                "sandbox": {"effective": "none", "requested": None},
            }
            from agent_collab.resume import compute_resume_fingerprint

            fingerprint = compute_resume_fingerprint(
                agent_type="claude", backend_id="sdk", workdir=workdir
            )
            record = {
                "session_id": "resume-parked",
                "status": "interrupted",
                "task": "original task",
                "workflow": "pair",
                "workdir": workdir,
                "jsonl_path": str(root / "resume-parked.jsonl"),
                "markdown_path": str(root / "resume-parked.md"),
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "interactive": True,
                "settings": settings,
                "agent_sessions": {
                    "claude": {
                        "backend": "sdk",
                        "provider_session_id": "sess-1",
                        "provider_session_kind": "session",
                        "last_turn_status": "completed",
                        "prompt_event_cursor": 0,
                        "resume_fingerprint": fingerprint,
                        "backend_version": "",
                        "interrupt_acknowledged": False,
                        "quarantined": False,
                    }
                },
                "workflow_phase": phase,
            }
            (root / "resume-parked.jsonl").write_text("", encoding="utf-8")
            SessionIndex(index_path).upsert(record)
            restored = SessionManager(index_path=index_path, default_workdir=root)
            prepared = _PreparedSessionStart(
                workdir=Path(workdir),
                log_dir=root,
                collab_config=CollaborationConfig(
                    agents={
                        "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
                        "codex": AgentConfig(id="codex", type="codex", backend="sdk"),
                    },
                    workflows={"pair": WorkflowConfig(id="pair", sequence=["claude", "codex"])},
                ),
                normalized_options={},
                agent_options={},
                agent_backends={"claude": "sdk", "codex": "sdk"},
                settings=settings,
                capabilities={"resumable": True, "interruptible": False, "continuity": True},
                interactive_idle_timeout=600.0,
                approval_deadline=120.0,
                sandbox_plan=SimpleNamespace(),
            )
            live_status: list[str] = []

            async def on_status(status):
                live_status.append(status)

            async def park_run(managed, resume=False):
                del resume
                managed.referee = Referee(
                    RefereeConfig(
                        sandbox="none",
                        workflow="pair",
                        collab_config=prepared.collab_config,
                        workdir=root,
                        log_dir=root,
                        session_id=managed.state.session_id,
                        max_turns=2,
                        timeout=5,
                        color=False,
                        interactive=True,
                        interactive_idle_timeout=0.05,
                        input_queue=managed.input_queue,
                        resume=True,
                        resume_phase=phase,
                        status_callback=on_status,
                    ),
                    printer=lambda event: None,
                )
                managed.referee._runners = lambda: {
                    "claude": ResumeRunner("claude"),
                    "codex": ResumeRunner("codex"),
                }
                await managed.referee.run("original task")

            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(restored, "_prepare_session_start", return_value=prepared),
                mock.patch.object(restored, "_run_session", side_effect=park_run),
            ):
                resumed = await restored.resume_session("resume-parked")
                await _wait_until(
                    lambda: "awaiting_input" in live_status,
                    message="resumed park",
                )
            managed = restored._sessions["resume-parked"]
            if managed.task is not None:
                await managed.task

        self.assertEqual(resumed.status, "running")
        self.assertEqual(resume_calls, [])
        self.assertIn("awaiting_input", live_status)

    async def test_directed_follow_up_interrupt_parks_not_failed(self) -> None:
        directed_started = asyncio.Event()
        runner = FirstThenHangRunner("claude", directed_started)
        runners = {"claude_cli": runner, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="directed interrupt",
                            mock=True,
                            workflow="solo",
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await _wait_until(
                        lambda: manager.get_session(state.session_id).status == "awaiting_input",
                        message="first park",
                    )
                    await manager.post_message(state.session_id, "follow up")
                    await asyncio.wait_for(directed_started.wait(), timeout=2.0)
                    interrupted = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    self.assertEqual(interrupted.status, "awaiting_input")
                    self.assertNotEqual(interrupted.status, "failed")
                    await manager.post_message(state.session_id, "steer after interrupt")
                    follow = await asyncio.wait_for(
                        manager.wait_result(state.session_id, 2000),
                        timeout=3.0,
                    )
                    await manager.stop_session(state.session_id)
        self.assertEqual(follow.status, "awaiting_input")
        self.assertNotEqual(follow.status, "failed")
        self.assertGreaterEqual(runner.calls, 3)

    async def test_interrupt_after_last_planned_stage_consumes_before_steer(self) -> None:
        runner = LastStageThenPendingHangRunner("claude")
        runners = {"claude_cli": runner, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="last-stage leftover flag",
                            mock=True,
                            workflow="solo",
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await asyncio.wait_for(runner.planned_started.wait(), timeout=2.0)
                    await manager.post_message(state.session_id, "queued after last stage")
                    runner.hold_planned.set()
                    await asyncio.wait_for(runner.pending_directed.wait(), timeout=2.0)
                    self.assertEqual(manager.get_session(state.session_id).status, "running")
                    interrupted = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    self.assertEqual(interrupted.status, "awaiting_input")
                    self.assertNotEqual(interrupted.status, "failed")
                    self.assertEqual(
                        interrupted.workflow_phase,
                        {"completed_stages": 1, "parked_in_input_loop": True},
                    )
                    await manager.post_message(state.session_id, "steer after last-stage interrupt")
                    follow = await asyncio.wait_for(
                        manager.wait_result(state.session_id, 2000),
                        timeout=3.0,
                    )
                    later = manager.get_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(follow.status, "awaiting_input")
        self.assertNotEqual(follow.status, "failed")
        self.assertEqual(runner.calls, 3)
        self.assertTrue(runner.steer_finished)
        outcomes = [item["outcome"] for item in (later.turn_outcomes or [])]
        self.assertIn("completed", outcomes)
        self.assertEqual(outcomes[-1], "completed")

    async def test_second_queued_steer_survives_interrupt_drain(self) -> None:
        runner = LastStageThenPendingHangRunner("claude")
        runners = {"claude_cli": runner, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="two queued steers",
                            mock=True,
                            workflow="solo",
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await asyncio.wait_for(runner.planned_started.wait(), timeout=2.0)
                    await manager.post_message(state.session_id, "first queued")
                    await manager.post_message(state.session_id, "second queued")
                    runner.hold_planned.set()
                    await asyncio.wait_for(runner.pending_directed.wait(), timeout=2.0)
                    interrupted = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    self.assertEqual(interrupted.status, "awaiting_input")
                    self.assertNotEqual(interrupted.status, "failed")
                    follow = await asyncio.wait_for(
                        manager.wait_result(state.session_id, 2000),
                        timeout=3.0,
                    )
                    later = manager.get_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(follow.status, "awaiting_input")
        self.assertNotEqual(follow.status, "failed")
        self.assertEqual(runner.calls, 3)
        self.assertTrue(runner.steer_finished)
        outcomes = [item["outcome"] for item in (later.turn_outcomes or [])]
        self.assertEqual(outcomes[-1], "completed")

    async def test_completion_race_keeps_completed_and_still_parks(self) -> None:
        started = asyncio.Event()
        claude = CompletingRunner("claude")
        codex = FinishOnInterruptRunner("codex", started, TurnOutcome("completed"))
        runners = {"claude_cli": claude, "codex_cli": codex}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="completion race",
                            mock=True,
                            workflow="cross-review",
                            max_turns=3,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    interrupted = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    later = manager.get_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(interrupted.status, "awaiting_input")
        self.assertEqual(
            interrupted.workflow_phase,
            {"completed_stages": 2, "parked_in_input_loop": True},
        )
        outcomes = [item["outcome"] for item in (interrupted.turn_outcomes or [])]
        self.assertIn("completed", outcomes)
        self.assertNotIn("interrupted", outcomes)
        self.assertEqual(claude.calls, 1)
        self.assertEqual(codex.calls, 1)
        self.assertEqual(
            later.agent_sessions.get("codex_cli", {}).get("last_turn_status"), "completed"
        )
        self.assertFalse(
            later.agent_sessions.get("codex_cli", {}).get("interrupt_acknowledged", False)
        )

    async def test_non_interactive_is_conflict_and_does_not_enter_input_loop(self) -> None:
        started = asyncio.Event()
        hang = HangRunner("claude", started)
        runners = {"claude_cli": hang, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="batch",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    await started.wait()
                    with self.assertRaises(InterruptError) as raised:
                        await manager.interrupt_session(state.session_id)
                    current = manager.get_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(raised.exception.code, "conflict")
        self.assertIn("interactive", str(raised.exception))
        self.assertEqual(current.status, "running")
        self.assertNotEqual(current.status, "awaiting_input")
        self.assertNotEqual(current.status, "failed")

    async def test_idle_awaiting_input_is_conflict(self) -> None:
        runners = {"claude_cli": CompletingRunner("claude"), "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="idle",
                            mock=True,
                            workflow="solo",
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await _wait_until(
                        lambda: manager.get_session(state.session_id).status == "awaiting_input",
                        message="idle park",
                    )
                    with self.assertRaises(InterruptError) as raised:
                        await manager.interrupt_session(state.session_id)
                    current = manager.get_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(raised.exception.code, "conflict")
        self.assertIn("no in-flight turn", str(raised.exception))
        self.assertEqual(current.status, "awaiting_input")
        self.assertNotEqual(current.status, "failed")

    async def test_unknown_session_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with self.assertRaises(SessionNotFoundError):
                    await manager.interrupt_session("missing-session")


class ParallelInterruptExemptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_interrupted_members_are_abandoned_not_parallel_failed(self) -> None:
        started = asyncio.Event()
        live: set[str] = set()

        class MemberHang(AgentRunner):
            def __init__(self, name: str) -> None:
                self.name = name

            async def interrupt_request(self) -> bool:
                return True

            async def run_turn(self, prompt, workdir, emit):
                del prompt, workdir, emit
                live.add(self.name)
                if len(live) == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    ),
                    "codex": AgentConfig(id="codex", type="codex", command="codex", backend="cli"),
                },
                workflows={"parallel": WorkflowConfig(id="parallel", parallel=["claude", "codex"])},
            )
            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="parallel",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    session_id="par-int",
                    max_turns=1,
                    timeout=5,
                    color=False,
                    interactive=True,
                    interactive_idle_timeout=30,
                    input_queue=asyncio.Queue(),
                ),
                printer=lambda event: None,
            )
            referee._runners = lambda: {
                "claude": MemberHang("claude"),
                "codex": MemberHang("codex"),
            }
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                transcript = [
                    Event.create("human", "message", "task"),
                    Event.create("referee", "status", "workflow=parallel"),
                ]
                with SessionLogger(root, "task", "par-int") as logger:
                    stage = asyncio.create_task(
                        referee._run_parallel_stage(
                            logger,
                            transcript,
                            referee._runners(),
                            "task",
                            ["claude", "codex"],
                            1,
                        )
                    )
                    await started.wait()
                    referee.request_turn_interrupt()
                    accepted = await asyncio.wait_for(stage, timeout=2.0)
        self.assertFalse(accepted)
        self.assertTrue(referee.stop_signal.turn_interrupt_requested())

    async def test_operator_interrupt_parks_interactive_parallel_referee(self) -> None:
        started = asyncio.Event()
        live: set[str] = set()
        statuses: list[str] = []
        phases: list[tuple[int, bool]] = []

        class MemberHang(AgentRunner):
            def __init__(self, name: str) -> None:
                self.name = name

            async def interrupt_request(self) -> bool:
                return True

            async def run_turn(self, prompt, workdir, emit):
                del prompt, workdir, emit
                live.add(self.name)
                if len(live) == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise
                return TurnOutcome("completed")

        async def on_status(status: str) -> None:
            statuses.append(status)

        async def on_phase(completed: int, parked: bool) -> None:
            phases.append((completed, parked))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    ),
                    "codex": AgentConfig(id="codex", type="codex", command="codex", backend="cli"),
                },
                workflows={"parallel": WorkflowConfig(id="parallel", parallel=["claude", "codex"])},
            )
            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="parallel",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    session_id="par-park",
                    max_turns=1,
                    timeout=5,
                    color=False,
                    interactive=True,
                    interactive_idle_timeout=30,
                    input_queue=asyncio.Queue(),
                    status_callback=on_status,
                    phase_commit_callback=on_phase,
                ),
                printer=lambda event: None,
            )
            referee._runners = lambda: {
                "claude": MemberHang("claude"),
                "codex": MemberHang("codex"),
            }
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                run = asyncio.create_task(referee.run("review"))
                await started.wait()
                referee.request_turn_interrupt()
                await _wait_until(
                    lambda: "awaiting_input" in statuses,
                    message="parallel park",
                )
                self.assertFalse(run.done())
                self.assertIn((0, True), phases)
                run.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await run
        self.assertNotIn("failed", statuses)


class InterruptLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_interrupt_after_park_is_idle_conflict(self) -> None:
        started = asyncio.Event()
        runners = {
            "claude_cli": HangRunner("claude", started),
            "codex_cli": CompletingRunner("codex"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="double interrupt",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    first = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    with self.assertRaises(InterruptError) as raised:
                        await manager.interrupt_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(first.status, "awaiting_input")
        self.assertEqual(raised.exception.code, "conflict")
        self.assertIn("no in-flight turn", str(raised.exception))

    async def test_resume_during_live_interrupt_is_conflict(self) -> None:
        started = asyncio.Event()
        runners = {
            "claude_cli": HangRunner("claude", started),
            "codex_cli": CompletingRunner("codex"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="interrupt vs resume",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    resume_task = asyncio.create_task(manager.resume_session(state.session_id))
                    interrupt_task = asyncio.create_task(
                        manager.interrupt_session(state.session_id)
                    )
                    resume_result, interrupt_result = await asyncio.gather(
                        resume_task,
                        interrupt_task,
                        return_exceptions=True,
                    )
                    await manager.stop_session(state.session_id)
        self.assertIsInstance(resume_result, ResumeError)
        self.assertEqual(resume_result.code, "conflict")
        self.assertFalse(isinstance(interrupt_result, Exception))
        self.assertEqual(interrupt_result.status, "awaiting_input")

    async def test_membership_recheck_after_lock_does_not_resurrect(self) -> None:
        started = asyncio.Event()
        runners = {
            "claude_cli": HangRunner("claude", started),
            "codex_cli": CompletingRunner("codex"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="prune during interrupt",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    managed = manager._sessions[state.session_id]
                    async with managed.resume_lock:
                        task = asyncio.create_task(manager.interrupt_session(state.session_id))
                        await asyncio.sleep(0.05)
                        manager._sessions.pop(state.session_id)
                    with self.assertRaises(InterruptError) as raised:
                        await task
                    self.assertNotIn(state.session_id, manager._sessions)
                    if managed.task is not None and not managed.task.done():
                        managed.task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await managed.task
        self.assertEqual(raised.exception.code, "not_found")

    async def test_interrupt_after_stop_is_conflict(self) -> None:
        started = asyncio.Event()
        runners = {
            "claude_cli": HangRunner("claude", started),
            "codex_cli": CompletingRunner("codex"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="stop then interrupt",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    await manager.stop_session(state.session_id)
                    with self.assertRaises(InterruptError) as raised:
                        await manager.interrupt_session(state.session_id)
        self.assertEqual(raised.exception.code, "conflict")
        self.assertIn("not live", str(raised.exception))


class InterruptFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unacknowledged_interrupt_parks_without_widening_eligibility(self) -> None:
        started = asyncio.Event()
        hang = HangRunner("claude", started, acknowledge=True)
        runners = {"claude_cli": hang, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = Path(tmp) / "session-index.json"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    with mock.patch("agent_collab.daemon.INTERRUPT_ACKNOWLEDGE_SECONDS", 0.05):
                        state = await manager.start_session(
                            StartSessionRequest(
                                task="fallback park",
                                mock=True,
                                max_turns=1,
                                timeout=5,
                                workdir=root,
                                interactive=True,
                                interactive_idle_timeout=30,
                            )
                        )
                        await started.wait()
                        interrupted = await asyncio.wait_for(
                            manager.interrupt_session(state.session_id),
                            timeout=3.0,
                        )
                        later = manager.get_session(state.session_id)
                        record = json.loads(index_path.read_text(encoding="utf-8"))["sessions"][
                            state.session_id
                        ]
                        await manager.stop_session(state.session_id)
        self.assertEqual(interrupted.status, "awaiting_input")
        self.assertTrue(interrupted.interrupt["requested"])
        self.assertFalse(interrupted.interrupt["provider_acknowledged"])
        self.assertTrue(interrupted.interrupt["fallback_cancelled"])
        self.assertFalse(hang.conversation_active())
        entry = later.agent_sessions.get("claude_cli") or {}
        self.assertEqual(entry.get("last_turn_status"), "interrupted")
        self.assertFalse(entry.get("interrupt_acknowledged"))
        self.assertFalse(descriptor_is_eligible(entry))
        self.assertNotIn("interrupt", record)

    async def test_acknowledged_interrupt_sets_marker_on_interrupted_turn(self) -> None:
        started = asyncio.Event()
        runner = FinishOnInterruptRunner(
            "claude",
            started,
            TurnOutcome("interrupted", "local_turn_interrupted"),
        )
        runners = {"claude_cli": runner, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="ack interrupt",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await started.wait()
                    interrupted = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    later = manager.get_session(state.session_id)
                    await manager.stop_session(state.session_id)
        self.assertEqual(interrupted.status, "awaiting_input")
        self.assertTrue(interrupted.interrupt["requested"])
        self.assertTrue(interrupted.interrupt["provider_acknowledged"])
        self.assertFalse(interrupted.interrupt["fallback_cancelled"])
        entry = later.agent_sessions.get("claude_cli") or {}
        self.assertEqual(entry.get("last_turn_status"), "interrupted")
        self.assertTrue(entry.get("interrupt_acknowledged"))
        self.assertFalse(descriptor_is_eligible(entry))
        outcomes = [item["outcome"] for item in (interrupted.turn_outcomes or [])]
        self.assertIn("interrupted", outcomes)

    async def test_interrupt_acknowledged_clears_on_later_turn(self) -> None:
        runner = AckThenFallbackRunner("claude")
        runners = {"claude_cli": runner, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="ack then fallback",
                            mock=True,
                            workflow="solo",
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=30,
                        )
                    )
                    await asyncio.wait_for(runner.planned_started.wait(), timeout=2.0)
                    first = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    after_ack = manager.get_session(state.session_id)
                    ack_entry = after_ack.agent_sessions.get("claude_cli") or {}
                    self.assertEqual(first.status, "awaiting_input")
                    self.assertTrue(first.interrupt["provider_acknowledged"])
                    self.assertFalse(first.interrupt["fallback_cancelled"])
                    self.assertEqual(ack_entry.get("last_turn_status"), "interrupted")
                    self.assertTrue(ack_entry.get("interrupt_acknowledged"))
                    self.assertFalse(descriptor_is_eligible(ack_entry))

                    await manager.post_message(state.session_id, "steer after ack")
                    await asyncio.wait_for(runner.steer_started.wait(), timeout=2.0)
                    during_steer = manager.get_session(state.session_id)
                    handoff = during_steer.agent_sessions.get("claude_cli") or {}
                    self.assertEqual(handoff.get("last_turn_status"), "in_flight")
                    self.assertFalse(handoff.get("interrupt_acknowledged"))

                    later = await asyncio.wait_for(
                        manager.interrupt_session(state.session_id),
                        timeout=2.0,
                    )
                    after_fallback = manager.get_session(state.session_id)
                    fallback_entry = after_fallback.agent_sessions.get("claude_cli") or {}
                    await manager.stop_session(state.session_id)
        self.assertEqual(later.status, "awaiting_input")
        self.assertFalse(later.interrupt["requested"])
        self.assertTrue(later.interrupt["fallback_cancelled"])
        self.assertEqual(fallback_entry.get("last_turn_status"), "interrupted")
        self.assertFalse(fallback_entry.get("interrupt_acknowledged"))
        self.assertFalse(descriptor_is_eligible(fallback_entry))

    async def test_interrupt_denies_pending_approvals_first(self) -> None:
        started = asyncio.Event()
        interrupt_after_deny: list[bool] = []

        class TrackingHang(HangRunner):
            async def interrupt_request(self) -> bool:
                interrupt_after_deny.append(True)
                return True

        hang = TrackingHang("claude", started)
        runners = {"claude_cli": hang, "codex_cli": CompletingRunner("codex")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    with mock.patch("agent_collab.daemon.INTERRUPT_ACKNOWLEDGE_SECONDS", 0.05):
                        state = await manager.start_session(
                            StartSessionRequest(
                                task="deny then interrupt",
                                mock=True,
                                max_turns=1,
                                timeout=5,
                                workdir=root,
                                interactive=True,
                                interactive_idle_timeout=30,
                            )
                        )
                        await started.wait()
                        await manager.register_approval(
                            state.session_id,
                            request_id="a1",
                            agent_id="claude_cli",
                            tool_name="Bash",
                            summary="true",
                            turn_id="turn-1",
                        )
                        interrupted = await asyncio.wait_for(
                            manager.interrupt_session(state.session_id),
                            timeout=3.0,
                        )
                        managed = manager._sessions[state.session_id]
                        await manager.stop_session(state.session_id)
        self.assertTrue(interrupt_after_deny)
        self.assertEqual(interrupted.interrupt["approvals_denied"], 1)
        self.assertEqual(managed.approvals.unresolved_count(), 0)


class InterruptSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_maps_interrupt_errors_to_404_and_409(self) -> None:
        manager = mock.Mock()
        server = AgentCollabHttpServer(manager=manager)
        cases = (
            (InterruptError("not_found", "unknown session_id gone"), 404),
            (InterruptError("conflict", "no in-flight turn to interrupt"), 409),
        )
        for exc, status in cases:
            manager.interrupt_session = mock.AsyncMock(side_effect=exc)
            writer = _CaptureWriter()
            body = b"{}"
            request = (
                b"POST /sessions/s1/interrupt HTTP/1.1\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await server._handle_connection(_request_reader(request), writer)
            head, response_body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
            self.assertIn(f"HTTP/1.1 {status}".encode(), head)
            payload = json.loads(response_body)
            self.assertEqual(payload["code"], exc.code)
            self.assertEqual(payload["error"], str(exc))

    def test_cli_interrupt_calls_client_interrupt_session(self) -> None:
        from agent_collab.cli import _main_interrupt

        session = SessionStateModel.from_dict(
            {"session_id": "s1", "status": "awaiting_input", "workflow": "solo"}
        )
        client = mock.Mock()
        client.interrupt_session.return_value = session
        with (
            mock.patch("agent_collab.cli._client", return_value=client),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = _main_interrupt(["s1"])
        self.assertEqual(code, 0)
        client.interrupt_session.assert_called_once_with("s1")
        self.assertIn("Interrupted s1", stdout.getvalue())

    def test_tui_interrupt_keeps_session_attached(self) -> None:
        from agent_collab.tui import TuiApp
        from agent_collab.tui_core import parse_input

        class Screen:
            def getmaxyx(self):
                return (24, 80)

        client = mock.Mock()
        client.interrupt_session.return_value = SessionStateModel.from_dict(
            {"session_id": "session-1", "status": "awaiting_input", "interactive": True}
        )
        app = TuiApp(Screen(), client, initial_session_id=None)
        app.session_id = "session-1"
        app.session = SessionStateModel.from_dict(
            {"session_id": "session-1", "status": "running", "interactive": True}
        )
        with mock.patch.object(app, "activate_session") as activate:
            with mock.patch.object(app, "_stop_poller") as stop_poller:
                app._dispatch(parse_input("/interrupt"))
        client.interrupt_session.assert_called_once_with("session-1")
        activate.assert_called_once_with("session-1")
        stop_poller.assert_not_called()
        self.assertEqual(app.message, "interrupted session-1")
