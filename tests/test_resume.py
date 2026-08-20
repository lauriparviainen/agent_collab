"""Hermetic coverage for increment-4 resume eligibility, claim, and phase restore."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_collab import backends as backend_registry
from agent_collab.backends.base import BackendCapabilities
from agent_collab.backends.common.sdk import provider_session_event
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import (
    SessionManager,
    SessionState,
    StartSessionRequest,
    _execute_transcript_unlinks,
    _PreparedSessionStart,
)
from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.referee import Referee, RefereeConfig, RefereeInput
from agent_collab.resume import (
    ResumeError,
    attach_resume_block,
    compute_resume_fingerprint,
    descriptor_is_eligible,
    eligible_resume_agent_ids,
    fingerprint_from_session,
    fingerprints_match,
    last_turn_status_from_record,
    projection_captured_resume_agent_ids,
    required_resume_agent_ids,
    require_resume_session_id,
    session_phase_blocks_resume,
    unstarted_resume_agent_ids,
    validate_session_resume,
)
from agent_collab.runners import AgentRunner
from agent_collab.session_index import SessionIndex


def _fingerprint(*, workdir: str = ".", backend_id: str = "sdk") -> dict:
    return compute_resume_fingerprint(agent_type="claude", backend_id=backend_id, workdir=workdir)


def _eligible_entry(*, workdir: str = ".", cursor: int = 2) -> dict:
    return {
        "backend": "sdk",
        "provider_session_id": "sess-1",
        "provider_session_kind": "session",
        "last_turn_status": "completed",
        "prompt_event_cursor": cursor,
        "resume_fingerprint": _fingerprint(workdir=workdir),
        "backend_version": "",
        "interrupt_acknowledged": False,
        "quarantined": False,
    }


def _state(**overrides) -> SessionState:
    data = dict(
        session_id="s1",
        status="interrupted",
        task="t",
        workflow="solo",
        workdir=".",
        jsonl_path="s1.jsonl",
        markdown_path="s1.md",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        settings={
            "agents": {"claude": {"type": "claude", "backend": "sdk"}},
            "workflow": {"sequence": ["claude"]},
        },
        agent_sessions={"claude": _eligible_entry()},
        workflow_phase={"completed_stages": 0, "parked_in_input_loop": False},
        capabilities={"resumable": False, "interruptible": False, "continuity": False},
    )
    data.update(overrides)
    return SessionState(**data)


_REAL_CAPABILITIES_FOR = backend_registry.capabilities_for


def _resume_stub(agent_type, backend_id):
    caps = _REAL_CAPABILITIES_FOR(agent_type, backend_id)
    return BackendCapabilities(
        resume=True,
        interrupt=caps.interrupt,
        tool_gate=caps.tool_gate,
        continuity=caps.continuity,
    )


class ResumeEligibilityTests(unittest.TestCase):
    def test_capture_alone_is_not_eligible(self):
        entry = {
            "backend": "sdk",
            "provider_session_id": "sess-1",
            "provider_session_kind": "session",
        }
        self.assertFalse(descriptor_is_eligible(entry))
        self.assertEqual(
            eligible_resume_agent_ids(_state(agent_sessions={"claude": entry})), frozenset()
        )

    def test_completed_descriptor_is_eligible(self):
        self.assertTrue(descriptor_is_eligible(_eligible_entry()))
        self.assertEqual(eligible_resume_agent_ids(_state()), frozenset({"claude"}))

    def test_ineligible_turn_statuses_reject(self):
        for status in (
            "in_flight",
            "interrupted",
            "timed_out",
            "failed",
            "missing",
            "resume_rejected",
            "resume_uncertain",
        ):
            entry = _eligible_entry()
            entry["last_turn_status"] = status
            with self.subTest(status=status):
                self.assertFalse(descriptor_is_eligible(entry))

    def test_interrupt_acknowledged_does_not_make_interrupted_eligible(self):
        entry = _eligible_entry()
        entry["last_turn_status"] = "interrupted"
        entry["interrupt_acknowledged"] = True
        self.assertFalse(descriptor_is_eligible(entry))

    def test_bad_cursor_rejects(self):
        for cursor in (-1, True, "2", None):
            entry = _eligible_entry()
            entry["prompt_event_cursor"] = cursor
            with self.subTest(cursor=cursor):
                self.assertFalse(descriptor_is_eligible(entry))
        self.assertFalse(descriptor_is_eligible(_eligible_entry(cursor=9), transcript_len=3))

    def test_fingerprint_mismatch_rejects(self):
        entry = _eligible_entry(workdir="/a")
        other = _fingerprint(workdir="/b")
        self.assertFalse(descriptor_is_eligible(entry, expected_fingerprint=other))
        self.assertTrue(fingerprints_match(entry["resume_fingerprint"], _fingerprint(workdir="/a")))

    def test_fingerprint_strips_secrets_and_omits_invented_floors(self):
        fingerprint = compute_resume_fingerprint(
            agent_type="claude",
            backend_id="sdk",
            workdir="/w",
            static_config={"model": "sonnet", "api_key": "secret", "token": "x"},
        )
        self.assertNotIn("api_key", fingerprint["static_config"])
        self.assertNotIn("token", fingerprint["static_config"])
        self.assertEqual(fingerprint["static_config"]["model"], "sonnet")
        self.assertNotIn("version_floor", fingerprint)
        self.assertEqual(fingerprint["state_root"], "CLAUDE_CONFIG_DIR")
        agy = compute_resume_fingerprint(agent_type="antigravity", backend_id="cli", workdir="/w")
        self.assertEqual(agy["version_floor"], "1.1.8")

    def test_quarantine_empties_session_eligible_set(self):
        state = _state(
            agent_sessions={
                "claude": {
                    **_eligible_entry(),
                    "quarantined": True,
                    "last_turn_status": "resume_rejected",
                }
            }
        )
        self.assertEqual(eligible_resume_agent_ids(state), frozenset())
        with self.assertRaises(ResumeError) as raised:
            validate_session_resume(state)
        self.assertEqual(raised.exception.code, "quarantined")

    def test_missing_phase_and_completed_noninteractive_block(self):
        self.assertIsNotNone(session_phase_blocks_resume(_state(workflow_phase=None)))
        done = _state(workflow_phase={"completed_stages": 1, "parked_in_input_loop": False})
        self.assertIn("completed every planned stage", session_phase_blocks_resume(done) or "")
        parked = _state(
            interactive=True,
            workflow_phase={"completed_stages": 1, "parked_in_input_loop": True},
        )
        self.assertIsNone(session_phase_blocks_resume(parked))

    def test_mock_and_done_status_are_not_resumable(self):
        with self.assertRaises(ResumeError) as mock_err:
            validate_session_resume(_state(mock=True))
        self.assertEqual(mock_err.exception.code, "ineligible")
        with self.assertRaises(ResumeError) as done_err:
            validate_session_resume(_state(status="done"))
        self.assertEqual(done_err.exception.code, "ineligible")

    def test_require_resume_block_never_falls_through(self):
        self.assertIsNone(require_resume_session_id({}))
        self.assertEqual(
            require_resume_session_id({"resume": {"provider_session_id": "abc"}}),
            "abc",
        )
        with self.assertRaises(RuntimeError):
            require_resume_session_id({"resume": {}})
        with self.assertRaises(RuntimeError):
            require_resume_session_id({"resume": "sess"})
        payload = attach_resume_block(
            {}, {"provider_session_id": "abc", "provider_session_kind": "session"}
        )
        self.assertEqual(payload["resume"]["provider_session_id"], "abc")

    def test_last_turn_status_maps_resume_failures(self):
        record = SimpleNamespace(code="resume_rejected", outcome="failed")
        self.assertEqual(last_turn_status_from_record(record), "resume_rejected")
        self.assertEqual(
            last_turn_status_from_record(SimpleNamespace(code="ok", outcome="completed")),
            "completed",
        )

    def _pair_settings(self):
        return {
            "agents": {
                "claude": {"type": "claude", "backend": "sdk"},
                "codex": {"type": "codex", "backend": "sdk"},
            },
            "workflow": {"sequence": ["claude", "codex"]},
        }

    def test_unstarted_peer_is_not_required_for_resume(self):
        phase = {"completed_stages": 1, "parked_in_input_loop": False}
        mid = _state(
            settings=self._pair_settings(),
            agent_sessions={"claude": _eligible_entry()},
            workflow_phase=phase,
        )
        self.assertEqual(required_resume_agent_ids(mid), frozenset({"claude"}))
        self.assertEqual(unstarted_resume_agent_ids(mid), frozenset({"codex"}))
        claim = validate_session_resume(mid)
        self.assertEqual(claim["eligible_agent_ids"], frozenset({"claude"}))
        self.assertNotIn("codex", claim["descriptors"])
        with mock.patch("agent_collab.backends.capabilities_for", side_effect=_resume_stub):
            summary = SessionManager._project_session_capabilities(mid)
        self.assertTrue(summary["resumable"])

    def test_ineligible_row_is_not_treated_as_unstarted(self):
        phase = {"completed_stages": 1, "parked_in_input_loop": False}
        ineligible = _eligible_entry()
        ineligible["last_turn_status"] = "in_flight"
        blocked = _state(
            settings=self._pair_settings(),
            agent_sessions={"claude": _eligible_entry(), "codex": ineligible},
            workflow_phase=phase,
        )
        self.assertEqual(required_resume_agent_ids(blocked), frozenset({"claude", "codex"}))
        self.assertEqual(unstarted_resume_agent_ids(blocked), frozenset())
        with self.assertRaises(ResumeError) as raised:
            validate_session_resume(blocked)
        self.assertEqual(raised.exception.code, "ineligible")
        with mock.patch("agent_collab.backends.capabilities_for", side_effect=_resume_stub):
            summary = SessionManager._project_session_capabilities(blocked)
        self.assertFalse(summary["resumable"])

    def test_quarantine_blocks_resume_even_with_unstarted_peer(self):
        phase = {"completed_stages": 1, "parked_in_input_loop": False}
        state = _state(
            settings=self._pair_settings(),
            agent_sessions={
                "claude": {
                    **_eligible_entry(),
                    "quarantined": True,
                    "last_turn_status": "resume_rejected",
                }
            },
            workflow_phase=phase,
        )
        self.assertEqual(eligible_resume_agent_ids(state), frozenset())
        with self.assertRaises(ResumeError) as raised:
            validate_session_resume(state)
        self.assertEqual(raised.exception.code, "quarantined")
        with mock.patch("agent_collab.backends.capabilities_for", side_effect=_resume_stub):
            self.assertFalse(SessionManager._project_session_capabilities(state)["resumable"])

    def test_resumable_projection_agrees_with_the_resume_gate(self):
        parked = {"completed_stages": 1, "parked_in_input_loop": False}
        for status in ("done", "failed"):
            state = _state(status=status, interactive=True, workflow_phase=parked)
            self.assertEqual(projection_captured_resume_agent_ids(state), frozenset())
            self.assertFalse(SessionManager._project_session_capabilities(state)["resumable"])
            with self.assertRaises(ResumeError) as raised:
                validate_session_resume(state)
            self.assertEqual(raised.exception.code, "ineligible")
        for status in ("stopped", "interrupted"):
            state = _state(status=status, interactive=True, workflow_phase=parked)
            self.assertTrue(SessionManager._project_session_capabilities(state)["resumable"])
            validate_session_resume(state)
        live = _state(status="running", interactive=True, workflow_phase=parked)
        self.assertTrue(SessionManager._project_session_capabilities(live)["resumable"])

    def test_never_started_session_is_not_resumable(self):
        state = _state(agent_sessions={})
        self.assertEqual(required_resume_agent_ids(state), frozenset())
        self.assertEqual(unstarted_resume_agent_ids(state), frozenset({"claude"}))
        with self.assertRaises(ResumeError) as raised:
            validate_session_resume(state)
        self.assertEqual(raised.exception.code, "ineligible")
        with mock.patch("agent_collab.backends.capabilities_for", side_effect=_resume_stub):
            self.assertFalse(SessionManager._project_session_capabilities(state)["resumable"])


class ResumeClaimTests(unittest.IsolatedAsyncioTestCase):
    def _prepared(self, state: SessionState) -> _PreparedSessionStart:
        config = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", backend="sdk")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        return _PreparedSessionStart(
            workdir=Path(state.workdir),
            log_dir=Path(state.jsonl_path).parent,
            collab_config=config,
            normalized_options={},
            agent_options={},
            agent_backends={"claude": "sdk"},
            settings=state.settings or {},
            capabilities={"resumable": True, "interruptible": False, "continuity": True},
            interactive_idle_timeout=600.0,
            approval_deadline=120.0,
            sandbox_plan=SimpleNamespace(),
        )

    def _prepared_pair(self, state: SessionState) -> _PreparedSessionStart:
        config = CollaborationConfig(
            agents={
                "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
                "codex": AgentConfig(id="codex", type="codex", backend="sdk"),
            },
            workflows={"pair": WorkflowConfig(id="pair", sequence=["claude", "codex"])},
        )
        return _PreparedSessionStart(
            workdir=Path(state.workdir),
            log_dir=Path(state.jsonl_path).parent,
            collab_config=config,
            normalized_options={},
            agent_options={},
            agent_backends={"claude": "sdk", "codex": "sdk"},
            settings=state.settings or {},
            capabilities={"resumable": True, "interruptible": False, "continuity": True},
            interactive_idle_timeout=600.0,
            approval_deadline=120.0,
            sandbox_plan=SimpleNamespace(),
        )

    async def _index_manager(self, root: Path, *, status: str) -> tuple[SessionManager, str]:
        index_path = root / "index.json"
        workdir = str(root)
        fingerprint = fingerprint_from_session(
            _state(
                workdir=workdir,
                settings={
                    "agents": {"claude": {"type": "claude", "backend": "sdk"}},
                    "workflow": {"sequence": ["claude"]},
                    "sandbox": {"effective": "none"},
                },
            ),
            "claude",
        )
        record = {
            "session_id": "resume-1",
            "status": status,
            "task": "t",
            "workflow": "solo",
            "workdir": workdir,
            "jsonl_path": str(root / "resume-1.jsonl"),
            "markdown_path": str(root / "resume-1.md"),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "settings": {
                "agents": {"claude": {"type": "claude", "backend": "sdk"}},
                "workflow": {"sequence": ["claude"]},
                "sandbox": {"effective": "none", "requested": None},
            },
            "agent_sessions": {
                "claude": {
                    **_eligible_entry(workdir=workdir, cursor=0),
                    "resume_fingerprint": fingerprint,
                }
            },
            "workflow_phase": {"completed_stages": 0, "parked_in_input_loop": False},
        }
        (root / "resume-1.jsonl").write_text("", encoding="utf-8")
        SessionIndex(index_path).upsert(record)
        manager = SessionManager(index_path=index_path, default_workdir=root)
        return manager, "resume-1"

    async def _interrupted_manager(self, root: Path) -> tuple[SessionManager, str]:
        return await self._index_manager(root, status="interrupted")

    async def _stopped_manager(self, root: Path) -> tuple[SessionManager, str]:
        manager, session_id = await self._index_manager(root, status="stopped")
        managed = manager._sessions[session_id]
        managed.state.stop = {
            "requested": True,
            "provider_acknowledged": False,
            "fallback_cancelled": True,
            "approvals_denied": 0,
        }
        managed.state.interrupt = {
            "requested": True,
            "provider_acknowledged": True,
            "fallback_cancelled": False,
            "approvals_denied": 1,
        }
        return manager, session_id

    async def test_resume_clears_stop_and_interrupt_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._stopped_manager(root)
            hang = asyncio.Event()

            async def hang_run(managed, resume=False):
                del managed, resume
                await hang.wait()

            before = manager.get_session(session_id)
            self.assertIsNotNone(before.stop)
            self.assertIsNotNone(before.interrupt)
            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(manager, "_run_session", side_effect=hang_run),
            ):
                resumed = await manager.resume_session(session_id)
            self.assertEqual(resumed.status, "running")
            self.assertIsNone(resumed.stop)
            self.assertIsNone(resumed.interrupt)
            later = manager.get_session(session_id)
            self.assertIsNone(later.stop)
            self.assertIsNone(later.interrupt)
            hang.set()
            managed = manager._sessions[session_id]
            if managed.task is not None:
                await managed.task

    async def test_resume_of_stopped_parked_session_reopens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._stopped_manager(root)
            hang = asyncio.Event()

            async def hang_run(managed, resume=False):
                del managed, resume
                await hang.wait()

            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(manager, "_run_session", side_effect=hang_run),
            ):
                resumed = await manager.resume_session(session_id)
            self.assertEqual(resumed.status, "running")
            hang.set()
            managed = manager._sessions[session_id]
            if managed.task is not None:
                await managed.task

    async def test_manager_resume_of_done_session_is_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._index_manager(root, status="done")
            with self.assertRaises(ResumeError) as raised:
                await manager.resume_session(session_id)
            self.assertEqual(raised.exception.code, "ineligible")
            after = manager.get_session(session_id)
            self.assertEqual(after.status, "done")
            self.assertFalse((after.capabilities or {}).get("resumable"))

    async def test_manager_resume_of_failed_session_is_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._index_manager(root, status="failed")
            with self.assertRaises(ResumeError) as raised:
                await manager.resume_session(session_id)
            self.assertEqual(raised.exception.code, "ineligible")
            after = manager.get_session(session_id)
            self.assertEqual(after.status, "failed")
            self.assertFalse((after.capabilities or {}).get("resumable"))

    async def test_failed_resume_leaves_session_status_and_request_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._interrupted_manager(root)
            before = manager.get_session(session_id)
            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(
                    manager,
                    "_refresh_session_capabilities",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    await manager.resume_session(session_id)
            after = manager.get_session(session_id)
            self.assertEqual(after.status, "interrupted")
            self.assertEqual(after.status, before.status)
            index = SessionIndex(root / "index.json").load()
            self.assertEqual(index["resume-1"]["status"], "interrupted")

    async def test_failed_resume_rolls_back_when_prior_task_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._stopped_manager(root)
            managed = manager._sessions[session_id]

            async def already_done():
                return None

            managed.task = asyncio.create_task(already_done())
            await managed.task
            self.assertIsNotNone(managed.task)
            self.assertTrue(managed.task.done())
            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(
                    manager,
                    "_refresh_session_capabilities",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    await manager.resume_session(session_id)
            after = manager.get_session(session_id)
            self.assertEqual(after.status, "stopped")
            index = SessionIndex(root / "index.json").load()
            self.assertEqual(index["resume-1"]["status"], "stopped")

    async def test_approval_registry_is_reset_by_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._interrupted_manager(root)
            hang = asyncio.Event()

            async def hang_run(managed, resume=False):
                del managed, resume
                await hang.wait()

            managed = manager._sessions[session_id]
            managed.approvals.finish_turn("turn-1")
            managed.approval_generation = 7
            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(manager, "_run_session", side_effect=hang_run),
            ):
                await manager.resume_session(session_id)
            self.assertFalse(managed.approvals.turn_finished("turn-1"))
            self.assertEqual(managed.approval_generation, 0)
            parked = await manager.register_approval(
                session_id,
                request_id="a2",
                agent_id="claude",
                tool_name="Bash",
                summary="late",
                turn_id="turn-1",
            )
            self.assertNotEqual(parked.get("status"), "late_frame")
            self.assertEqual(managed.approvals.unresolved_count(), 1)
            pending = managed.approvals.get_pending("a2")
            if pending is not None:
                from agent_collab.approvals import cancel_approval_deadline

                cancel_approval_deadline(pending)
            hang.set()
            if managed.task is not None:
                await managed.task

    async def test_live_session_resume_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager(default_workdir=root)
            state = await manager.start_session(
                StartSessionRequest(task="live", mock=True, max_turns=1, timeout=5, workdir=root)
            )
            with self.assertRaises(ResumeError) as raised:
                await manager.resume_session(state.session_id)
            self.assertEqual(raised.exception.code, "conflict")
            await manager.stop_session(state.session_id)

    async def test_concurrent_resumes_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._interrupted_manager(root)
            hang = asyncio.Event()

            async def hang_run(managed, resume=False):
                del managed, resume
                await hang.wait()

            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(manager, "_run_session", side_effect=hang_run),
            ):
                first = await manager.resume_session(session_id)
                self.assertEqual(first.status, "running")
                with self.assertRaises(ResumeError) as raised:
                    await manager.resume_session(session_id)
                self.assertEqual(raised.exception.code, "conflict")
            hang.set()
            managed = manager._sessions[session_id]
            if managed.task is not None:
                await managed.task

    async def test_fingerprint_mismatch_is_incompatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._interrupted_manager(root)
            base = self._prepared(manager.get_session(session_id))
            prepared = _PreparedSessionStart(
                workdir=base.workdir,
                log_dir=base.log_dir,
                collab_config=base.collab_config,
                normalized_options=base.normalized_options,
                agent_options=base.agent_options,
                agent_backends=base.agent_backends,
                settings={
                    "agents": {"claude": {"type": "claude", "backend": "sdk", "model": "other"}},
                    "workflow": {"sequence": ["claude"]},
                    "sandbox": {"effective": "none"},
                },
                capabilities=base.capabilities,
                interactive_idle_timeout=base.interactive_idle_timeout,
                approval_deadline=base.approval_deadline,
                sandbox_plan=base.sandbox_plan,
            )
            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(manager, "_prepare_session_start", return_value=prepared),
            ):
                with self.assertRaises(ResumeError) as raised:
                    await manager.resume_session(session_id)
            self.assertEqual(raised.exception.code, "incompatible")

    async def test_ineligible_status_after_restore_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._interrupted_manager(root)
            managed = manager._sessions[session_id]
            managed.state.agent_sessions["claude"]["last_turn_status"] = "in_flight"
            with self.assertRaises(ResumeError) as raised:
                await manager.resume_session(session_id)
            self.assertEqual(raised.exception.code, "ineligible")

    async def test_wait_result_after_resume_keeps_restored_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._interrupted_manager(root)
            managed = manager._sessions[session_id]
            events = [
                Event.create("human", "message", "task", {"task": "task"}),
                Event.create("claude", "message", "prior answer", agent_id="claude"),
                Event.create(
                    "referee",
                    "status",
                    "turn-1 claude completed",
                    {
                        "turn_outcome": {
                            "turn_id": "turn-1",
                            "stage_index": 1,
                            "agent_id": "claude",
                            "backend": "claude_sdk",
                            "outcome": "completed",
                        }
                    },
                    agent_id="claude",
                ),
            ]
            Path(managed.state.jsonl_path).write_text(
                "\n".join(event.to_json() for event in events) + "\n",
                encoding="utf-8",
            )
            managed.events = [event.to_dict() for event in events]
            managed.state.interactive = True
            managed.state.workflow_phase = {
                "completed_stages": 1,
                "parked_in_input_loop": True,
            }
            before = await manager.wait_result(session_id, timeout_ms=0)
            hang = asyncio.Event()

            async def park_run(managed_session, resume=False):
                del resume
                managed_session.input_accepting = True
                await manager._set_status(managed_session, "awaiting_input")
                await hang.wait()

            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared(manager.get_session(session_id)),
                ),
                mock.patch.object(manager, "_run_session", side_effect=park_run),
            ):
                await manager.resume_session(session_id)
                after = await manager.wait_result(session_id, timeout_ms=1000)
            hang.set()
            live = manager._sessions[session_id]
            if live.task is not None:
                await live.task

        self.assertEqual([answer["text"] for answer in before.answers], ["prior answer"])
        self.assertEqual(after.status, "awaiting_input")
        self.assertTrue(after.settled)
        self.assertEqual([answer["text"] for answer in after.answers], ["prior answer"])
        self.assertTrue(live.answer_ledger)

    async def _mid_workflow_manager(self, root: Path) -> tuple[SessionManager, str]:
        index_path = root / "index.json"
        workdir = str(root)
        settings = {
            "agents": {
                "claude": {"type": "claude", "backend": "sdk"},
                "codex": {"type": "codex", "backend": "sdk"},
            },
            "workflow": {"sequence": ["claude", "codex"]},
            "sandbox": {"effective": "none", "requested": None},
        }
        fingerprint = fingerprint_from_session(
            _state(workdir=workdir, settings=settings),
            "claude",
        )
        record = {
            "session_id": "resume-pair",
            "status": "interrupted",
            "task": "original task",
            "workflow": "pair",
            "workdir": workdir,
            "jsonl_path": str(root / "resume-pair.jsonl"),
            "markdown_path": str(root / "resume-pair.md"),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "max_turns": 2,
            "timeout": 5,
            "settings": settings,
            "agent_sessions": {
                "claude": {
                    **_eligible_entry(workdir=workdir, cursor=3),
                    "resume_fingerprint": fingerprint,
                }
            },
            "workflow_phase": {"completed_stages": 1, "parked_in_input_loop": False},
        }
        (root / "resume-pair.jsonl").write_text("", encoding="utf-8")
        SessionIndex(index_path).upsert(record)
        manager = SessionManager(index_path=index_path, default_workdir=root)
        return manager, "resume-pair"

    async def test_mid_workflow_resume_runs_only_remaining_stage(self):
        calls = []

        class Runner(AgentRunner):
            def __init__(self, name):
                self.name = name

            async def run_turn(self, prompt, workdir, emit):
                calls.append(self.name)
                await emit(Event.create(self.name, "message", f"{self.name} later"))
                return TurnOutcome("completed")

        def fake_runners(self):
            del self
            return {"claude": Runner("claude"), "codex": Runner("codex")}

        existing = [
            Event.create("human", "message", "original task", {"task": "original task"}),
            Event.create("referee", "status", "turn 1: claude", agent_id="claude"),
            Event.create("claude", "message", "first answer", agent_id="claude"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, session_id = await self._mid_workflow_manager(root)
            managed = manager._sessions[session_id]
            managed.events = [event.to_dict() for event in existing]
            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(
                    manager,
                    "_prepare_session_start",
                    side_effect=lambda request: self._prepared_pair(
                        manager.get_session(session_id)
                    ),
                ),
                mock.patch.object(Referee, "_runners", fake_runners),
            ):
                manager._refresh_session_capabilities(manager._sessions[session_id].state)
                self.assertTrue(manager.get_session(session_id).capabilities["resumable"])
                resumed = await manager.resume_session(session_id)
                self.assertEqual(resumed.status, "running")
                live = manager._sessions[session_id]
                if live.task is not None:
                    await live.task
            jsonl = Path(managed.state.jsonl_path).read_text(encoding="utf-8")

        self.assertEqual(calls, ["codex"])
        self.assertNotIn('"task": "original task"', jsonl)
        self.assertIn("turn 2: codex", jsonl)

    async def test_resume_after_apply_prune_is_not_found_and_does_not_resurrect(self):
        now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
        old = (now - timedelta(days=60)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "sessions"
            session_dir.mkdir()
            index_path = root / "index.json"
            workdir = str(root)
            settings = {
                "agents": {"claude": {"type": "claude", "backend": "sdk"}},
                "workflow": {"sequence": ["claude"]},
                "sandbox": {"effective": "none", "requested": None},
            }
            fingerprint = fingerprint_from_session(
                _state(workdir=workdir, settings=settings),
                "claude",
            )
            record = {
                "session_id": "pruned-resume",
                "status": "interrupted",
                "task": "t",
                "workflow": "solo",
                "workdir": workdir,
                "jsonl_path": str(session_dir / "pruned-resume.jsonl"),
                "markdown_path": str(session_dir / "pruned-resume.md"),
                "created_at": old,
                "updated_at": old,
                "ended_at": old,
                "settings": settings,
                "agent_sessions": {
                    "claude": {
                        **_eligible_entry(workdir=workdir, cursor=0),
                        "resume_fingerprint": fingerprint,
                    }
                },
                "workflow_phase": {"completed_stages": 0, "parked_in_input_loop": False},
            }
            (session_dir / "pruned-resume.jsonl").write_text("", encoding="utf-8")
            SessionIndex(index_path).upsert(record)
            manager = SessionManager(
                index_path=index_path,
                default_workdir=root,
                default_log_dir=session_dir,
            )

            held = threading.Event()
            release = threading.Event()

            def slow_unlink(plans, apply):
                held.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("prune unlink was not released")
                return _execute_transcript_unlinks(plans, apply)

            hang = asyncio.Event()
            prepared = self._prepared(manager.get_session("pruned-resume"))

            async def hang_run(managed_session, resume=False):
                del managed_session, resume
                await hang.wait()

            with (
                mock.patch(
                    "agent_collab.backends.capabilities_for",
                    side_effect=_resume_stub,
                ),
                mock.patch.object(manager, "_prepare_session_start", return_value=prepared),
                mock.patch.object(manager, "_run_session", side_effect=hang_run),
                mock.patch(
                    "agent_collab.daemon._execute_transcript_unlinks",
                    side_effect=slow_unlink,
                ),
            ):
                prune_task = asyncio.create_task(
                    manager.prune_sessions(apply=True, retention=timedelta(days=30), now=now)
                )
                await asyncio.to_thread(held.wait, 5)
                self.assertTrue(held.is_set())
                resume_task = asyncio.create_task(manager.resume_session("pruned-resume"))
                await asyncio.sleep(0.05)
                release.set()
                prune_result = await prune_task
                hang.set()
                with self.assertRaises(ResumeError) as raised:
                    await resume_task

        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(prune_result.pruned, 1)
        self.assertNotIn("pruned-resume", SessionIndex(index_path).load())
        self.assertNotIn("pruned-resume", manager._sessions)


class ResumePhaseRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_skips_completed_stages_and_does_not_reemit_task(self):
        calls = []
        prompts = []

        class Runner(AgentRunner):
            def __init__(self, name):
                self.name = name

            async def run_turn(self, prompt, workdir, emit):
                calls.append(self.name)
                prompts.append(prompt)
                await emit(Event.create(self.name, "message", f"{self.name} later"))
                return TurnOutcome("completed")

        existing = [
            Event.create("human", "message", "original task", {"task": "original task"}),
            Event.create("referee", "status", "turn 1: claude", agent_id="claude"),
            Event.create("claude", "message", "first answer", agent_id="claude"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    ),
                    "codex": AgentConfig(id="codex", type="codex", command="codex", backend="cli"),
                },
                workflows={"test": WorkflowConfig(id="test", sequence=["claude", "codex"])},
            )
            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="test",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    session_id="resume-phase",
                    max_turns=2,
                    timeout=5,
                    color=False,
                    resume=True,
                    resume_events=existing,
                    resume_watermarks={"claude": 3},
                    resume_phase={"completed_stages": 1, "parked_in_input_loop": False},
                    resume_descriptors={
                        "claude": _eligible_entry(cursor=3),
                    },
                ),
                printer=lambda event: None,
            )
            referee._runners = lambda: {
                "claude": Runner("claude"),
                "codex": Runner("codex"),
            }
            await referee.run("original task")
            jsonl = (root / "resume-phase.jsonl").read_text(encoding="utf-8")

        self.assertEqual(calls, ["codex"])
        self.assertNotIn('"task": "original task"', jsonl)
        self.assertIn("turn 2: codex", jsonl)

    async def test_restored_provider_session_status_stays_out_of_peer_prompt(self):
        live = provider_session_event("claude", "claude", "sess-123", "session")
        restored = Event.from_dict(live.to_dict())
        self.assertIsNone(restored.provider_session)
        transcript = [
            Event.create("human", "message", "original task", {"task": "original task"}),
            Event.create("claude", "message", "first answer", agent_id="claude"),
            restored,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    ),
                    "codex": AgentConfig(id="codex", type="codex", command="codex", backend="cli"),
                },
                workflows={"test": WorkflowConfig(id="test", sequence=["claude", "codex"])},
            )
            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="test",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    session_id="resume-bookkeeping",
                    max_turns=2,
                    timeout=5,
                    color=False,
                    resume=True,
                    resume_events=transcript,
                    resume_phase={"completed_stages": 1, "parked_in_input_loop": False},
                    resume_descriptors={"claude": _eligible_entry(cursor=3)},
                ),
                printer=lambda event: None,
            )
            prompt = referee._prompt_for("original task", "codex", 2, transcript)

        self.assertIn("first answer", prompt)
        self.assertNotIn("sess-123", prompt)
        self.assertNotIn("session_id=", prompt)

    async def test_parked_interactive_resume_enters_input_loop(self):
        calls = []

        class Runner(AgentRunner):
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                calls.append(prompt)
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    )
                },
                workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
            )
            statuses = []

            async def on_status(status):
                statuses.append(status)

            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="solo",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    session_id="resume-parked",
                    max_turns=1,
                    timeout=5,
                    color=False,
                    interactive=True,
                    interactive_idle_timeout=0.05,
                    input_queue=asyncio.Queue(),
                    resume=True,
                    resume_events=[Event.create("human", "message", "task")],
                    resume_phase={"completed_stages": 1, "parked_in_input_loop": True},
                    status_callback=on_status,
                ),
                printer=lambda event: None,
            )
            referee._runners = lambda: {"claude": Runner()}
            await referee.run("task")

        self.assertEqual(calls, [])
        self.assertIn("awaiting_input", statuses)

    async def test_resume_allocates_turn_ids_after_persisted_outcomes(self):
        turn_ids = []

        class Runner(AgentRunner):
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "later"))
                return TurnOutcome("completed")

        async def commit(record, boundary, completed_stages=None, persist=True):
            del completed_stages, persist
            turn_ids.append(record.turn_id)

        queue = asyncio.Queue()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    )
                },
                workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
            )
            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="solo",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    session_id="resume-turn-ids",
                    max_turns=1,
                    timeout=5,
                    color=False,
                    interactive=True,
                    interactive_idle_timeout=2,
                    input_queue=queue,
                    resume=True,
                    resume_events=[Event.create("human", "message", "task")],
                    resume_phase={"completed_stages": 1, "parked_in_input_loop": True},
                    resume_turn_outcomes=[{"turn_id": "turn-1", "outcome": "completed"}],
                    outcome_commit_callback=commit,
                ),
                printer=lambda event: None,
            )
            referee._runners = lambda: {"claude": Runner()}
            run = asyncio.create_task(referee.run("task"))
            await asyncio.sleep(0.05)
            await queue.put(RefereeInput(event=Event.create("human", "message", "continue")))
            await run

        self.assertEqual(turn_ids, ["turn-2"])

    async def test_handoff_persists_in_flight_before_runner(self):
        order = []

        class Runner(AgentRunner):
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                order.append("run")
                await emit(Event.create("claude", "message", "ok"))
                return TurnOutcome("completed")

        async def handoff(agent_id, cursor):
            order.append(("handoff", agent_id, cursor))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = CollaborationConfig(
                agents={
                    "claude": AgentConfig(
                        id="claude", type="claude", command="claude", backend="cli"
                    )
                },
                workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
            )
            referee = Referee(
                RefereeConfig(
                    sandbox="none",
                    workflow="solo",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    max_turns=1,
                    timeout=5,
                    color=False,
                    prompt_handoff_callback=handoff,
                ),
                printer=lambda event: None,
            )
            referee._runners = lambda: {"claude": Runner()}
            await referee.run("task")

        self.assertEqual(order[0][0], "handoff")
        self.assertEqual(order[1], "run")

    async def test_outcome_persist_includes_completed_stages(self):
        # A crash immediately after the persist that first writes
        # last_turn_status=completed must already have the new completed_stages.
        # The old two-write sequence left completed + stale stage count.
        original = SessionManager._persist

        def persist_then_crash(self, state):
            original(self, state)
            sessions = state.agent_sessions or {}
            if any(
                isinstance(entry, dict) and entry.get("last_turn_status") == "completed"
                for entry in sessions.values()
            ):
                raise RuntimeError("simulated crash after outcome persist")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index.json"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(SessionManager, "_persist", persist_then_crash):
                    first = SessionManager(index_path=index_path)
                    state = await first.start_session(
                        StartSessionRequest(
                            task="atomic phase",
                            workflow="solo",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    managed = first._sessions[state.session_id]
                    for _ in range(200):
                        if managed.task is None or managed.task.done():
                            break
                        await asyncio.sleep(0.01)
                    if managed.task is not None:
                        with self.assertRaises(RuntimeError):
                            await managed.task
                second = SessionManager(index_path=index_path)
                restored = second.get_session(state.session_id)

        sessions = restored.agent_sessions or {}
        self.assertTrue(
            any(
                isinstance(entry, dict) and entry.get("last_turn_status") == "completed"
                for entry in sessions.values()
            )
        )
        phase = restored.workflow_phase or {}
        self.assertEqual(phase.get("completed_stages"), 1)

    async def test_parallel_stage_persist_includes_completed_stages(self):
        original = SessionManager._persist

        def persist_then_crash(self, state):
            original(self, state)
            sessions = state.agent_sessions or {}
            if any(
                isinstance(entry, dict) and entry.get("last_turn_status") == "completed"
                for entry in sessions.values()
            ):
                raise RuntimeError("simulated crash after parallel outcome persist")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index.json"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(SessionManager, "_persist", persist_then_crash):
                    first = SessionManager(index_path=index_path)
                    state = await first.start_session(
                        StartSessionRequest(
                            task="atomic parallel phase",
                            workflow="dual-review",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    managed = first._sessions[state.session_id]
                    for _ in range(200):
                        if managed.task is None or managed.task.done():
                            break
                        await asyncio.sleep(0.01)
                    if managed.task is not None:
                        with self.assertRaises(RuntimeError):
                            await managed.task
                second = SessionManager(index_path=index_path)
                restored = second.get_session(state.session_id)

        sessions = restored.agent_sessions or {}
        self.assertTrue(
            any(
                isinstance(entry, dict) and entry.get("last_turn_status") == "completed"
                for entry in sessions.values()
            )
        )
        phase = restored.workflow_phase or {}
        self.assertEqual(phase.get("completed_stages"), 1)

    async def test_directed_followup_does_not_increment_completed_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="directed phase",
                        workflow="solo",
                        mock=True,
                        max_turns=1,
                        timeout=5,
                        workdir=root,
                        interactive=True,
                        interactive_idle_timeout=5,
                    )
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2
                while loop.time() < deadline:
                    current = manager.get_session(state.session_id)
                    if current.status == "awaiting_input":
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("session did not reach awaiting_input")
                parked = manager.get_session(state.session_id)
                self.assertEqual((parked.workflow_phase or {}).get("completed_stages"), 1)
                await manager.post_message(state.session_id, "again please")
                deadline = loop.time() + 2
                while loop.time() < deadline:
                    current = manager.get_session(state.session_id)
                    if current.status == "awaiting_input" and current.turn_outcomes:
                        if len(current.turn_outcomes) >= 2:
                            break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("directed follow-up did not complete")
                after = manager.get_session(state.session_id)
                await manager.stop_session(state.session_id)

        self.assertGreaterEqual(len(after.turn_outcomes or []), 2)
        self.assertEqual((after.workflow_phase or {}).get("completed_stages"), 1)


class ResumeErrorContractTests(unittest.TestCase):
    def test_resume_error_codes_are_exactly_the_documented_set(self):
        root = Path(__file__).resolve().parents[1]
        resume_src = (root / "agent_collab" / "resume.py").read_text(encoding="utf-8")
        daemon_src = (root / "agent_collab" / "daemon.py").read_text(encoding="utf-8")
        match = re.search(
            r"class ResumeError.*?\n    \"\"\"(.*?)\"\"\"",
            resume_src,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        docstring = " ".join(match.group(1).split())
        codes_match = re.search(r"Codes:\s*(.*?)\.", docstring)
        self.assertIsNotNone(codes_match)
        documented = set(re.findall(r"``(\w+)``", codes_match.group(1)))
        constructed: set[str] = set()
        for source in (resume_src, daemon_src):
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name != "ResumeError" or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    constructed.add(first.value)
        self.assertEqual(documented, constructed)
        self.assertNotIn("live", documented)
        self.assertNotIn("live", constructed)


class EventResumeLoadTests(unittest.TestCase):
    def test_from_dict_does_not_restore_provider_identity_from_raw(self):
        event = Event.from_dict(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "source": "claude",
                "type": "status",
                "text": "session",
                "raw": {"provider_session_id": "should-not-restore", "session_id": "x"},
                "agent_id": "claude",
            }
        )
        self.assertIsNone(event.provider_session)
        self.assertEqual(event.agent_id, "claude")
        self.assertEqual(event.text, "session")
