"""Hermetic coverage for the session approval registry and settle arm (#20 slice b)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.approvals import (
    MAX_PENDING_APPROVALS_PAYLOAD_BYTES,
    ApprovalDecisionError,
    ApprovalEntry,
    ApprovalRegistry,
    build_approval_summary,
    dispatch_worker_approval,
    middle_elide,
    park_payload,
    worker_session_run_kwargs,
)
from agent_collab.daemon import (
    SessionManager,
    SessionRequestError,
    SessionState,
    StartSessionRequest,
    _ManagedSession,
    _digest_event,
    _tool_event_summary,
)
from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.referee import Referee
from agent_collab.runners import AgentRunner


class MiddleElideTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        text, truncated = middle_elide("python -m unittest", limit=40)
        self.assertEqual(text, "python -m unittest")
        self.assertFalse(truncated)

    def test_long_text_keeps_head_and_tail(self):
        source = "head-command " + ("x" * 80) + " tail-flag"
        text, truncated = middle_elide(source, limit=40)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 40)
        self.assertTrue(text.startswith("head"))
        self.assertTrue(text.endswith("flag"))
        self.assertIn("…", text)

    def test_tool_input_secrets_are_redacted_before_elision(self):
        summary, truncated = build_approval_summary(
            tool_input={"command": "export TOKEN=sk-abcdefghijklmnop true"}
        )
        self.assertIn("<redacted>", summary)
        self.assertNotIn("sk-abcdefghijklmnop", summary)
        self.assertIsInstance(truncated, bool)


class ParkPayloadBudgetTests(unittest.TestCase):
    def test_overflow_is_counted_not_dropped(self):
        entries = [
            ApprovalEntry(
                request_id=f"r{i}",
                agent_id="claude_cli",
                tool_name="Bash",
                summary=("n" * 120) + f"-{i}",
                summary_truncated=True,
                seq=i,
            )
            for i in range(20)
        ]
        blocks, omitted = park_payload(entries, budget=MAX_PENDING_APPROVALS_PAYLOAD_BYTES)
        self.assertGreater(omitted, 0)
        self.assertEqual(len(blocks) + omitted, 20)
        self.assertEqual(blocks[0]["request_id"], "r0")
        self.assertIn("decision_options", blocks[0])

    def test_first_block_is_fitted_to_budget(self):
        entry = ApprovalEntry(
            request_id="r0",
            agent_id="claude_cli",
            tool_name="Bash",
            summary="n" * 4000,
            summary_truncated=True,
            seq=1,
        )
        blocks, omitted = park_payload([entry, entry], budget=200)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(omitted, 1)
        encoded = len(json.dumps(blocks, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLessEqual(encoded, 200)
        self.assertTrue(blocks[0]["summary_truncated"])

    def test_unfittable_identifiers_are_omitted_not_shipped_over_budget(self):
        entry = ApprovalEntry(
            request_id="r" * 3000,
            agent_id="claude_cli",
            tool_name="Bash",
            summary="",
            summary_truncated=False,
            seq=1,
        )
        blocks, omitted = park_payload([entry], budget=80)
        self.assertEqual(blocks, [])
        self.assertEqual(omitted, 1)


class DispatchWorkerApprovalTests(unittest.TestCase):
    def test_maps_approval_id_to_session_request_id_not_envelope_id(self):
        seen = []

        class Runner:
            name = "claude"
            _bound_agent_id = "claude_cli"
            _bound_turn_id = "turn-1"
            _approval_callback = staticmethod(lambda payload: seen.append(payload) or payload)
            _worker_session = type("S", (), {"instance": "inst-9", "_active_run": "run-1"})()

        payload = dispatch_worker_approval(
            Runner(),
            {
                "request_id": "envelope-lifecycle-id",
                "approval_id": "appr-1",
                "tool_name": "Bash",
                "summary": "true",
                "run_id": "run-1",
            },
        )
        self.assertEqual(payload["request_id"], "appr-1")
        self.assertEqual(payload["approval_id"], "appr-1")
        self.assertEqual(payload["worker_instance"], "inst-9")
        self.assertNotEqual(payload["request_id"], "envelope-lifecycle-id")
        self.assertEqual(seen[0]["request_id"], "appr-1")

    def test_dispatch_send_reports_false_when_worker_did_not_write(self):
        class Session:
            instance = "inst-9"
            _active_run = "run-1"

            async def send_approval_decision(self, **kwargs):
                return False

        class Runner:
            name = "claude"
            _bound_agent_id = "claude_cli"
            _bound_turn_id = "turn-1"
            _approval_callback = staticmethod(lambda payload: payload)
            _worker_session = Session()

        payload = dispatch_worker_approval(
            Runner(),
            {"approval_id": "appr-1", "tool_name": "Bash", "run_id": "run-1"},
        )
        self.assertFalse(asyncio.run(payload["send_decision"]("approve")))

    def test_worker_run_kwargs_omit_on_approval_without_callback(self):
        class Runner:
            name = "claude"

        kwargs = worker_session_run_kwargs(Runner(), emit="sink")
        self.assertEqual(kwargs, {"emit": "sink"})
        self.assertNotIn("on_approval", kwargs)

        class Wired:
            name = "claude"
            _approval_callback = staticmethod(lambda payload: payload)

        wired = worker_session_run_kwargs(Wired(), emit="sink")
        self.assertIn("on_approval", wired)
        self.assertIsNotNone(wired["on_approval"])


class ApprovalRegistrySettleTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, session_id="s-approval", status="running"):
        return SessionState(
            session_id=session_id,
            status=status,
            task="t",
            workflow="solo",
            workdir=".",
            jsonl_path="a.jsonl",
            markdown_path="a.md",
            created_at="t",
            updated_at="t",
        )

    def _managed(self, manager, status="running"):
        state = self._state(status=status)
        managed = _ManagedSession(
            request=StartSessionRequest(task="t"),
            state=state,
            events=[],
            condition=asyncio.Condition(),
        )
        manager._sessions[state.session_id] = managed
        return managed

    async def test_register_parks_wait_result_not_wait_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="python -m unittest",
                )
                self.assertEqual(managed.state.status, "awaiting_approval")
                result = await manager.wait_result(managed.state.session_id, timeout_ms=0)
                self.assertTrue(result.settled)
                self.assertFalse(result.terminal)
                self.assertEqual(result.status, "awaiting_approval")
                self.assertEqual(result.events_tail, [])
                self.assertEqual(len(result.pending_approvals), 1)
                self.assertEqual(result.pending_approvals[0]["request_id"], "a1")
                self.assertEqual(
                    result.pending_approvals[0]["decision_options"], ["approve", "deny"]
                )
                status = manager.get_session(managed.state.session_id)
                self.assertEqual(status.pending_approvals[0]["request_id"], "a1")
                batch = await manager.wait_events(managed.state.session_id, 0, timeout_ms=0)
                self.assertFalse(hasattr(batch, "pending_approvals"))
                self.assertNotIn("pending_approvals", batch.to_dict())
                types = {event["type"] for event in batch.events}
                self.assertIn("approval_request", types)
                request = next(
                    event for event in batch.events if event["type"] == "approval_request"
                )
                self.assertEqual(request["source"], "tool")
                self.assertNotIn("input", request.get("raw") or {})
                self.assertNotIn("tool_input", request.get("raw") or {})

    async def test_result_settled_ignores_input_accepting_and_queued_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                managed.input_accepting = False
                managed.input_queue.put_nowait(object())
                self.assertEqual(managed.input_queue.unfinished, 1)
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                self.assertTrue(manager._result_settled(managed))
                result = await manager.wait_result(managed.state.session_id, timeout_ms=0)
                self.assertTrue(result.settled)
                self.assertEqual(result.status, "awaiting_approval")

    async def test_resolve_last_returns_to_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a2",
                    agent_id="claude_cli",
                    tool_name="Edit",
                    summary="file.py",
                )
                one = await manager.resolve_approval(managed.state.session_id, "a1", "approve")
                self.assertEqual(one["status"], "ok")
                self.assertEqual(managed.state.status, "awaiting_approval")
                parked = await manager.wait_result(managed.state.session_id, timeout_ms=0)
                self.assertEqual(
                    [block["request_id"] for block in parked.pending_approvals], ["a2"]
                )
                await manager.resolve_approval(managed.state.session_id, "a2", "deny")
                self.assertEqual(managed.state.status, "running")
                types = [event["type"] for event in managed.events]
                self.assertEqual(types.count("approval_request"), 2)
                self.assertEqual(types.count("approval_resolved"), 2)
                resolved = [
                    event for event in managed.events if event["type"] == "approval_resolved"
                ]
                self.assertEqual(resolved[0]["raw"]["outcome"], "approved")
                self.assertEqual(resolved[1]["raw"]["outcome"], "denied")
                self.assertNotIn("input", resolved[0]["raw"])

    async def test_duplicate_stale_unknown_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                first = await manager.resolve_approval(managed.state.session_id, "a1", "approve")
                self.assertEqual(first["status"], "ok")
                again = await manager.resolve_approval(managed.state.session_id, "a1", "approve")
                self.assertEqual(again["status"], "idempotent")
                with self.assertRaises(ApprovalDecisionError) as conflict:
                    await manager.resolve_approval(managed.state.session_id, "a1", "deny")
                self.assertEqual(conflict.exception.code, "conflict")
                with self.assertRaises(ApprovalDecisionError) as missing:
                    await manager.resolve_approval(managed.state.session_id, "nope", "approve")
                self.assertEqual(missing.exception.code, "not_found")
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a2",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    turn_id="turn-1",
                )
                await manager._abandon_turn(managed, "turn-1")
                with self.assertRaises(ApprovalDecisionError) as stale:
                    await manager.resolve_approval(managed.state.session_id, "a2", "approve")
                self.assertEqual(stale.exception.code, "stale")

    async def test_heartbeat_does_not_carry_pending_approvals(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                heartbeat = await manager.wait_result(managed.state.session_id, timeout_ms=0)
                self.assertFalse(heartbeat.settled)
                self.assertEqual(heartbeat.pending_approvals, [])
                self.assertEqual(heartbeat.pending_approvals_omitted, 0)

    async def test_late_register_after_turn_release_does_not_park(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                sent = []

                async def send_decision(decision):
                    sent.append(decision)

                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    turn_id="turn-1",
                )
                await manager._abandon_turn(managed, "turn-1")
                self.assertEqual(managed.state.status, "running")
                late = await manager.register_approval(
                    managed.state.session_id,
                    request_id="a2",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="late",
                    turn_id="turn-1",
                    send_decision=send_decision,
                )
                self.assertEqual(late["status"], "late_frame")
                self.assertEqual(managed.approvals.unresolved_count(), 0)
                self.assertEqual(managed.state.status, "running")
                self.assertEqual(sent, ["deny"])
                result = await manager.wait_result(managed.state.session_id, timeout_ms=0)
                self.assertFalse(result.settled)

    async def test_wait_result_wakes_when_approval_registers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                waiter = asyncio.create_task(
                    manager.wait_result(managed.state.session_id, timeout_ms=2000)
                )
                await asyncio.sleep(0.05)
                self.assertFalse(waiter.done())
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                result = await asyncio.wait_for(waiter, timeout=1.0)
                self.assertTrue(result.settled)
                self.assertEqual(result.status, "awaiting_approval")
                self.assertEqual(result.pending_approvals[0]["request_id"], "a1")

    async def test_resolve_delivers_worker_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                sent = []

                async def send_decision(decision):
                    sent.append(decision)

                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    send_decision=send_decision,
                )
                await manager.resolve_approval(managed.state.session_id, "a1", "approve")
                self.assertEqual(sent, ["approve"])
                pending = managed.approvals.get_resolved("a1")
                self.assertIsNone(pending.send_decision)

    async def test_approve_delivery_failure_is_not_recorded_as_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                sent = []

                async def send_decision(decision):
                    sent.append(decision)
                    if decision == "approve":
                        raise RuntimeError("worker lost")

                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    send_decision=send_decision,
                )
                result = await manager.resolve_approval(managed.state.session_id, "a1", "approve")
                self.assertEqual(result["status"], "delivery_failed")
                self.assertEqual(result["outcome"], "auto_denied")
                self.assertEqual(sent, ["approve", "deny"])
                resolved = managed.approvals.get_resolved("a1")
                self.assertEqual(resolved.outcome, "auto_denied")
                self.assertEqual(resolved.reason, "delivery_failed")
                events = [event for event in managed.events if event["type"] == "approval_resolved"]
                self.assertEqual(events[0]["raw"]["outcome"], "auto_denied")
                self.assertNotEqual(events[0]["raw"]["outcome"], "approved")

    async def test_silent_delivery_noop_is_not_recorded_as_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                sent = []

                async def send_decision(decision):
                    sent.append(decision)
                    return False

                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    send_decision=send_decision,
                )
                result = await manager.resolve_approval(managed.state.session_id, "a1", "approve")
                self.assertEqual(result["status"], "delivery_failed")
                self.assertEqual(result["outcome"], "auto_denied")
                self.assertEqual(sent, ["approve", "deny"])
                resolved = [
                    event for event in managed.events if event["type"] == "approval_resolved"
                ]
                self.assertEqual(resolved[0]["raw"]["outcome"], "auto_denied")
                self.assertEqual(managed.approvals.get_resolved("a1").outcome, "auto_denied")

    async def test_reregister_of_resolved_id_does_not_park(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager)
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                await manager.resolve_approval(managed.state.session_id, "a1", "deny")
                again = await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                self.assertEqual(again["status"], "duplicate")
                self.assertEqual(managed.approvals.unresolved_count(), 0)
                self.assertNotEqual(managed.state.status, "awaiting_approval")

    async def test_register_on_terminal_denies_without_parking(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager, status="stopped")
                sent = []

                async def send_decision(decision):
                    sent.append(decision)

                result = await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    send_decision=send_decision,
                    worker_instance="inst-1",
                )
                self.assertEqual(result["status"], "stale")
                self.assertEqual(sent, ["deny"])
                self.assertEqual(managed.approvals.unresolved_count(), 0)
                self.assertNotEqual(managed.state.status, "awaiting_approval")

    async def test_persist_strips_park_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "session-index.json"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager(index_path=index_path)
                managed = self._managed(manager)
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                    send_decision=lambda decision: decision,
                )
                record = json.loads(index_path.read_text(encoding="utf-8"))["sessions"][
                    managed.state.session_id
                ]
                self.assertNotIn("pending_approvals", record)
                self.assertNotIn("pending_approvals_omitted", record)
                self.assertNotIn("send_decision", json.dumps(record))

    async def test_last_resolve_restores_awaiting_input_when_accepting(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                manager = SessionManager()
                managed = self._managed(manager, status="awaiting_input")
                managed.input_accepting = True
                await manager.register_approval(
                    managed.state.session_id,
                    request_id="a1",
                    agent_id="claude_cli",
                    tool_name="Bash",
                    summary="true",
                )
                self.assertEqual(managed.state.status, "awaiting_approval")
                await manager.resolve_approval(managed.state.session_id, "a1", "deny")
                self.assertEqual(managed.state.status, "awaiting_input")


class LiveApprovalParkTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_until(self, predicate, timeout=2.0, message="condition"):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail(f"{message} not reached before timeout")

    async def test_interactive_false_parks_and_settles(self):
        turn_started = asyncio.Event()
        release = asyncio.Event()

        class PauseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await release.wait()
                await emit(Event.create("claude", "message", "ok"))
                return TurnOutcome("completed")

        runners = {"claude_cli": PauseRunner(), "codex_cli": PauseRunner()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="review",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=False,
                        )
                    )
                    await turn_started.wait()
                    try:
                        await manager.register_approval(
                            state.session_id,
                            request_id="a1",
                            agent_id="claude_cli",
                            tool_name="Bash",
                            summary="true",
                            turn_id="turn-1",
                        )
                        result = await manager.wait_result(state.session_id, timeout_ms=1000)
                        self.assertTrue(result.settled)
                        self.assertFalse(result.terminal)
                        self.assertEqual(result.status, "awaiting_approval")
                        self.assertEqual(result.events_tail, [])
                        self.assertTrue(result.pending_approvals)
                        with self.assertRaises(SessionRequestError):
                            await manager.post_message(state.session_id, "queued")
                    finally:
                        release.set()
                        await manager.stop_session(state.session_id)

    async def test_queued_post_message_neither_satisfies_nor_blocks(self):
        turn_started = asyncio.Event()
        release = asyncio.Event()

        class PauseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await release.wait()
                await emit(Event.create("claude", "message", "ok"))
                return TurnOutcome("completed")

        runners = {"claude_cli": PauseRunner(), "codex_cli": PauseRunner()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="interactive review",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=5,
                        )
                    )
                    await turn_started.wait()
                    try:
                        await manager.register_approval(
                            state.session_id,
                            request_id="a1",
                            agent_id="claude_cli",
                            tool_name="Bash",
                            summary="true",
                            turn_id="turn-1",
                        )
                        await manager.post_message(state.session_id, "please continue")
                        managed = manager._sessions[state.session_id]
                        self.assertGreater(managed.input_queue.unfinished, 0)
                        self.assertFalse(managed.input_accepting)
                        result = await manager.wait_result(state.session_id, timeout_ms=1000)
                        self.assertTrue(result.settled)
                        self.assertEqual(result.status, "awaiting_approval")
                        self.assertTrue(result.pending_approvals)
                        await manager.resolve_approval(state.session_id, "a1", "deny")
                    finally:
                        release.set()
                        await manager.stop_session(state.session_id)

    async def test_stop_denies_pending_before_teardown(self):
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
                    try:
                        await manager.register_approval(
                            state.session_id,
                            request_id="a1",
                            agent_id="claude_cli",
                            tool_name="Bash",
                            summary="true",
                            turn_id="turn-1",
                        )
                        stopped = await manager.stop_session(state.session_id)
                        self.assertEqual(stopped.status, "stopped")
                        self.assertNotEqual(stopped.status, "awaiting_approval")
                        managed = manager._sessions[state.session_id]
                        self.assertEqual(managed.approvals.unresolved_count(), 0)
                        self.assertGreaterEqual(managed.approvals_denied_on_stop, 1)
                        resolved = [
                            event
                            for event in managed.events
                            if event.get("type") == "approval_resolved"
                        ]
                        self.assertTrue(resolved)
                        self.assertEqual(resolved[0]["raw"]["outcome"], "auto_denied")
                        self.assertEqual(resolved[0]["raw"]["reason"], "stop")
                    finally:
                        await manager.stop_session(state.session_id)

    async def test_result_with_pending_is_abandoned(self):
        turn_started = asyncio.Event()
        release = asyncio.Event()

        class PauseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                turn_started.set()
                await release.wait()
                await emit(Event.create("claude", "message", "finished without the tool"))
                return TurnOutcome("completed")

        runners = {"claude_cli": PauseRunner(), "codex_cli": PauseRunner()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="abandon me",
                            mock=True,
                            max_turns=1,
                            timeout=5,
                            workdir=root,
                        )
                    )
                    await turn_started.wait()
                    try:
                        await manager.register_approval(
                            state.session_id,
                            request_id="a1",
                            agent_id="claude_cli",
                            tool_name="Bash",
                            summary="true",
                            turn_id="turn-1",
                        )
                        release.set()
                        await self._wait_until(
                            lambda: (
                                manager.get_session(state.session_id).status
                                in {"done", "failed", "stopped"}
                            ),
                            message="session terminal after abandon",
                        )
                        managed = manager._sessions[state.session_id]
                        self.assertEqual(managed.approvals.unresolved_count(), 0)
                        resolved = [
                            event
                            for event in managed.events
                            if event.get("type") == "approval_resolved"
                        ]
                        self.assertTrue(resolved)
                        self.assertEqual(resolved[0]["raw"]["outcome"], "abandoned")
                        self.assertNotEqual(
                            manager.get_session(state.session_id).status, "awaiting_approval"
                        )
                    finally:
                        release.set()
                        await manager.stop_session(state.session_id)

    async def test_turn_deadline_denies_before_cancel(self):
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
                            task="timeout",
                            mock=True,
                            max_turns=1,
                            timeout=1,
                            workdir=root,
                        )
                    )
                    await turn_started.wait()
                    try:
                        await manager.register_approval(
                            state.session_id,
                            request_id="a1",
                            agent_id="claude_cli",
                            tool_name="Bash",
                            summary="true",
                            turn_id="turn-1",
                        )
                        await self._wait_until(
                            lambda: (
                                manager.get_session(state.session_id).status
                                in {"done", "failed", "stopped"}
                            ),
                            timeout=3.0,
                            message="session terminal after turn deadline",
                        )
                        managed = manager._sessions[state.session_id]
                        self.assertEqual(managed.approvals.unresolved_count(), 0)
                        resolved = [
                            event
                            for event in managed.events
                            if event.get("type") == "approval_resolved"
                        ]
                        self.assertTrue(resolved)
                        self.assertEqual(resolved[0]["raw"]["outcome"], "auto_denied")
                        self.assertEqual(resolved[0]["raw"]["reason"], "turn_deadline")
                        self.assertNotEqual(
                            manager.get_session(state.session_id).status, "awaiting_approval"
                        )
                    finally:
                        await manager.stop_session(state.session_id)


class DigestApprovalSkipTests(unittest.TestCase):
    def test_tool_call_digest_uses_tool_event_summary_approval_does_not(self):
        tool = {
            "timestamp": "t",
            "source": "tool",
            "type": "tool_call",
            "text": "short",
            "raw": {"name": "Bash", "input": {"command": "ls"}},
        }
        digested = _digest_event(tool, 3)
        self.assertIn(" — result ", digested["text"])
        self.assertIn("Bash", _tool_event_summary(tool, 3))
        request = {
            "timestamp": "t",
            "source": "tool",
            "type": "approval_request",
            "text": "claude_cli: Bash needs approval",
            "raw": {"request_id": "a1", "tool_name": "Bash", "summary": "ls"},
        }
        digested_request = _digest_event(request, 4)
        self.assertEqual(digested_request["text"], "claude_cli: Bash needs approval")
        self.assertNotIn(" — result ", digested_request["text"])

    def test_registry_is_in_memory_only(self):
        registry = ApprovalRegistry()
        entry = ApprovalEntry(
            request_id="a1",
            agent_id="claude_cli",
            tool_name="Bash",
            summary="true",
            summary_truncated=False,
            send_decision=lambda decision: decision,
        )
        registry.add(entry)
        self.assertEqual(registry.unresolved_count(), 1)
        taken = registry.take_all()
        self.assertEqual(taken[0].request_id, "a1")
        self.assertEqual(registry.unresolved_count(), 0)


if __name__ == "__main__":
    unittest.main()
