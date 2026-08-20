"""Hermetic Claude SDK can_use_tool park coverage on worker and in-process paths."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType
from typing import Any, List, Mapping
import unittest
from unittest import mock

from agent_collab.backends.claude_sdk.backend import ClaudeSdkRunner, _default_conversation
from agent_collab.backends.claude_sdk.permissions import (
    permission_result_from_decision,
)
from agent_collab.backends.claude_sdk.worker import ClaudeSdkWorkerBackend
from agent_collab.config import AgentConfig
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.events import Event
from agent_collab.referee import Referee
from agent_collab.sandbox import sdk_worker
from agent_collab.sandbox.sdk_worker import _serve
from agent_collab.sandbox.worker_codec import make_frame, recv_frame, send_frame


AGENT = AgentConfig(id="claude_cli", type="claude", backend="sdk")


def _is_allow(result: Any) -> bool:
    return getattr(result, "behavior", None) == "allow"


def _is_deny(result: Any) -> bool:
    return getattr(result, "behavior", None) == "deny"


class _ResultMessage:
    def __init__(self, session_id: str = "sess-gate") -> None:
        self.subtype = "success"
        self.is_error = False
        self.session_id = session_id
        self.content = None
        self.terminal_reason = None


class _ParkingConversation:
    def __init__(self, backend: ClaudeSdkWorkerBackend, *, overlap: bool = False) -> None:
        self.backend = backend
        self.overlap = overlap
        self.executed: List[str] = []
        self.results: List[Any] = []
        self.noted: List[str] = []

    async def run(self, prompt: str):
        del prompt
        if self.overlap:
            first = asyncio.create_task(self.backend._can_use_tool("Bash", {"command": "true"}))
            second = asyncio.create_task(self.backend._can_use_tool("Edit", {"file": "a.py"}))
            results = await asyncio.gather(first, second)
        else:
            results = [await self.backend._can_use_tool("Bash", {"command": "true"})]
        self.results.extend(results)
        for result, name in zip(results, ("Bash", "Edit") if self.overlap else ("Bash",)):
            if _is_allow(result):
                self.executed.append(name)
        yield _ResultMessage()

    def note_session_id(self, session_id: str) -> None:
        self.noted.append(session_id)

    async def reset(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def interrupt(self) -> bool:
        return False


class PermissionHelperTests(unittest.TestCase):
    def test_decision_mapping_is_fail_closed(self):
        self.assertTrue(_is_allow(permission_result_from_decision({"decision": "approve"})))
        self.assertTrue(_is_deny(permission_result_from_decision({"decision": "deny"})))
        self.assertTrue(_is_deny(permission_result_from_decision({"decision": "maybe"})))
        self.assertTrue(_is_deny(permission_result_from_decision(None)))


class ClaudeSdkWorkerToolGateTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _drive(self, backend: ClaudeSdkWorkerBackend, fake_conv):
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        with (
            mock.patch.object(
                sdk_worker, "_registry", return_value={"claude_sdk": lambda: backend}
            ),
            mock.patch(
                "agent_collab.backends.claude_sdk.worker._default_conversation",
                fake_conv,
            ),
        ):
            serve_task = asyncio.create_task(_serve(worker.fileno()))
            reader, writer = await asyncio.open_connection(sock=daemon)
            try:
                hello = await recv_frame(reader)
                self.assertEqual(hello["type"], "hello")
                await send_frame(
                    writer,
                    make_frame(
                        "open",
                        request_id="open-1",
                        payload={
                            "backend": "claude_sdk",
                            "workspace": "/tmp",
                            "options": {"permission_mode": "default"},
                            "tool_gate": True,
                        },
                    ),
                )
                ready = await recv_frame(reader)
                self.assertEqual(ready["type"], "ready")
                yield reader, writer
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                worker.close()
                daemon.close()
                serve_task.cancel()
                await asyncio.gather(serve_task, return_exceptions=True)

    async def _recv_until(
        self, reader, frame_type: str, *, timeout: float = 2.0
    ) -> Mapping[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.fail(f"timed out waiting for {frame_type} frame; callback never parked")
            frame = await asyncio.wait_for(recv_frame(reader), timeout=remaining)
            if frame["type"] == frame_type:
                return frame
            if frame["type"] == "error":
                self.fail(f"worker error before {frame_type}: {frame}")

    async def test_worker_approve_continues_and_executes_tool(self) -> None:
        backend = ClaudeSdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            holder["can_use_tool"] = kwargs.get("can_use_tool")
            conv = _ParkingConversation(backend)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            self.assertIs(holder["can_use_tool"].__func__, backend._can_use_tool.__func__)
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            request = await self._recv_until(reader, "approval_request")
            self.assertEqual(request["tool_name"], "Bash")
            self.assertNotIn("tool_input", request)
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id=request["approval_id"],
                    decision="approve",
                    run_id="run-1",
                ),
            )
            result = await self._recv_until(reader, "result")
            self.assertEqual(result["type"], "result")
            self.assertEqual(holder["conv"].executed, ["Bash"])
            self.assertTrue(_is_allow(holder["conv"].results[0]))
            await send_frame(writer, make_frame("close", request_id="close-1"))
            closed = await self._recv_until(reader, "closed")
            self.assertEqual(closed["type"], "closed")

    async def test_worker_deny_does_not_execute_tool(self) -> None:
        backend = ClaudeSdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            conv = _ParkingConversation(backend)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            request = await self._recv_until(reader, "approval_request")
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id=request["approval_id"],
                    decision="deny",
                    run_id="run-1",
                ),
            )
            result = await self._recv_until(reader, "result")
            self.assertEqual(result["type"], "result")
            self.assertEqual(holder["conv"].executed, [])
            self.assertTrue(_is_deny(holder["conv"].results[0]))
            await send_frame(writer, make_frame("close", request_id="close-1"))
            await self._recv_until(reader, "closed")

    async def test_worker_overlapping_parks(self) -> None:
        backend = ClaudeSdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            conv = _ParkingConversation(backend, overlap=True)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            first = await self._recv_until(reader, "approval_request")
            second = await self._recv_until(reader, "approval_request")
            ids = {first["approval_id"], second["approval_id"]}
            self.assertEqual(len(ids), 2)
            for frame in (first, second):
                await send_frame(
                    writer,
                    make_frame(
                        "approval_decision",
                        approval_id=frame["approval_id"],
                        decision="approve",
                        run_id="run-1",
                    ),
                )
            result = await self._recv_until(reader, "result")
            self.assertEqual(result["type"], "result")
            self.assertEqual(sorted(holder["conv"].executed), ["Bash", "Edit"])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            await self._recv_until(reader, "closed")

    async def test_worker_unknown_decision_during_park_is_noop(self) -> None:
        backend = ClaudeSdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            conv = _ParkingConversation(backend)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            request = await self._recv_until(reader, "approval_request")
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id="no-such-id",
                    decision="approve",
                    run_id="run-1",
                ),
            )
            await asyncio.sleep(0.05)
            self.assertEqual(holder["conv"].results, [])
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id=request["approval_id"],
                    decision="deny",
                    run_id="run-1",
                ),
            )
            result = await self._recv_until(reader, "result")
            self.assertEqual(result["type"], "result")
            self.assertEqual(holder["conv"].executed, [])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            await self._recv_until(reader, "closed")


def _fake_sdk_module(state: dict[str, Any]) -> ModuleType:
    module = ModuleType("claude_agent_sdk")

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: Any) -> None:
            self.options = dict(options.kwargs)
            state.setdefault("clients", []).append(self.options)
            self.is_open = False

        async def connect(self) -> None:
            self.is_open = True

        async def query(self, prompt: str) -> None:
            del prompt

        async def receive_response(self):
            cb = self.options.get("can_use_tool")
            if cb is None:
                raise AssertionError("can_use_tool was not registered; this test must park")
            mode = state.get("gate_mode", "park")
            if mode == "overlap":
                first = asyncio.create_task(cb("Bash", {"command": "true"}, None))
                second = asyncio.create_task(cb("Edit", {"file": "a.py"}, None))
                results = await asyncio.gather(first, second)
            elif mode == "abandon":
                park = asyncio.create_task(cb("Bash", {"command": "true"}, None))
                state["park_task"] = park
                await state["release_result"].wait()
                results = []
            else:
                results = [await cb(state.get("tool_name", "Bash"), {"command": "true"}, None)]
            state.setdefault("permission_results", []).extend(results)
            for result, name in zip(results, ("Bash", "Edit") if mode == "overlap" else ("Bash",)):
                if _is_allow(result):
                    state.setdefault("executed", []).append(name)
            if mode != "abandon" and state.get("hang_after_decision"):
                await asyncio.Event().wait()
            yield _ResultMessage()

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.is_open = False

    module.ClaudeAgentOptions = FakeOptions
    module.ClaudeSDKClient = FakeClient
    return module


class ClaudeSdkInProcessToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_process_approve_parks_and_executes(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        held: dict[str, Any] = {}

        def callback(payload: Mapping[str, Any]) -> None:
            held["payload"] = payload

        runner = ClaudeSdkRunner(
            AGENT, False, {"permission_mode": "default"}, conversation_factory=_default_conversation
        )
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        module = _fake_sdk_module(state)
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):
            events: list[Event] = []

            async def emit(event: Event) -> None:
                events.append(event)

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if "payload" in held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("in-process can_use_tool never parked")
            await held["payload"]["send_decision"]("approve")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed"), ["Bash"])
        self.assertTrue(_is_allow(state["permission_results"][0]))

    async def test_in_process_deny_does_not_execute_tool(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        held: dict[str, Any] = {}

        def callback(payload: Mapping[str, Any]) -> None:
            held["payload"] = payload

        runner = ClaudeSdkRunner(
            AGENT, False, {"permission_mode": "default"}, conversation_factory=_default_conversation
        )
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        module = _fake_sdk_module(state)
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def emit(_event: Event) -> None:
                return None

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if "payload" in held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("in-process can_use_tool never parked")
            await held["payload"]["send_decision"]("deny")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed", []), [])
        self.assertTrue(_is_deny(state["permission_results"][0]))

    async def test_in_process_overlapping_parks(self) -> None:
        state: dict[str, Any] = {"gate_mode": "overlap"}
        held: list[Mapping[str, Any]] = []

        def callback(payload: Mapping[str, Any]) -> None:
            held.append(payload)

        runner = ClaudeSdkRunner(
            AGENT, False, {"permission_mode": "default"}, conversation_factory=_default_conversation
        )
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        module = _fake_sdk_module(state)
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": module}):

            async def emit(_event: Event) -> None:
                return None

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if len(held) >= 2:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("overlapping can_use_tool parks never both registered")
            self.assertEqual(len({item["request_id"] for item in held}), 2)
            for payload in held:
                await payload["send_decision"]("approve")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(sorted(state.get("executed", [])), ["Bash", "Edit"])


class ClaudeSdkSessionToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def _start_gated_session(
        self, state: dict[str, Any], root: Path, *, approval_deadline: float = 2.0
    ):
        runner = ClaudeSdkRunner(
            AGENT, False, {"permission_mode": "default"}, conversation_factory=_default_conversation
        )

        def _runners(self: Referee):
            if self.config.approval_callback is not None:
                runner.set_approval_callback(self.config.approval_callback)
            return {"claude_cli": runner}

        manager = SessionManager()
        module = _fake_sdk_module(state)
        module_patch = mock.patch.dict(sys.modules, {"claude_agent_sdk": module})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        patcher = mock.patch.object(Referee, "_runners", _runners)
        patcher.start()
        self.addCleanup(patcher.stop)
        session = await manager.start_session(
            StartSessionRequest(
                task="gate me",
                workflow="solo",
                mock=True,
                max_turns=1,
                timeout=5,
                workdir=root,
                sandbox="none",
                approval_deadline=approval_deadline,
            )
        )
        return manager, session, runner

    async def _wait_status(self, manager: SessionManager, session_id: str, status: str):
        deadline = asyncio.get_running_loop().time() + 3.0
        last = None
        while asyncio.get_running_loop().time() < deadline:
            last = await manager.wait_result(session_id, timeout_ms=50)
            if last.status == status:
                return last
            if last.terminal and last.status != status:
                self.fail(f"session became {last.status} before {status}")
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for status {status}; last={getattr(last, 'status', None)}")

    async def _wait_parked(self, manager: SessionManager, session_id: str, count: int = 1):
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            result = await manager.wait_result(session_id, timeout_ms=50)
            if result.status == "awaiting_approval" and len(result.pending_approvals) >= count:
                return result
            if result.settled and result.terminal:
                self.fail("session settled without parking can_use_tool")
            await asyncio.sleep(0.02)
        self.fail("timed out waiting for can_use_tool park")

    async def test_session_approve_emits_events_and_continues(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                parked = await self._wait_parked(manager, session.session_id)
                self.assertEqual(parked.pending_approvals[0]["tool_name"], "Bash")
                decided = await manager.resolve_approval(
                    session.session_id, parked.pending_approvals[0]["request_id"], "approve"
                )
                self.assertEqual(decided["outcome"], "approved")
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed"), ["Bash"])
                managed = manager._sessions[session.session_id]
                types = [event.get("type") for event in managed.events]
                self.assertIn("approval_request", types)
                self.assertIn("approval_resolved", types)
                await runner.close()

    async def test_session_deny_does_not_execute_tool(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                parked = await self._wait_parked(manager, session.session_id)
                await manager.resolve_approval(
                    session.session_id, parked.pending_approvals[0]["request_id"], "deny"
                )
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed", []), [])
                await runner.close()

    async def test_session_overlapping_parks(self) -> None:
        state: dict[str, Any] = {"gate_mode": "overlap"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                parked = await self._wait_parked(manager, session.session_id, count=2)
                self.assertEqual(len(parked.pending_approvals), 2)
                for block in parked.pending_approvals:
                    await manager.resolve_approval(
                        session.session_id, block["request_id"], "approve"
                    )
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(sorted(state.get("executed", [])), ["Bash", "Edit"])
                await runner.close()

    async def test_session_deadline_auto_denies(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                manager._sessions[session.session_id].request.approval_deadline = 0.05
                parked = await self._wait_parked(manager, session.session_id)
                # Restart the deadline against the already-parked entry.
                entry = manager._sessions[session.session_id].approvals.get_pending(
                    parked.pending_approvals[0]["request_id"]
                )
                self.assertIsNotNone(entry)
                if entry.deadline_task is not None:
                    entry.deadline_task.cancel()
                entry.deadline_task = asyncio.create_task(
                    manager._expire_approval(session.session_id, entry.request_id, 0.05)
                )
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed", []), [])
                self.assertTrue(_is_deny(state["permission_results"][0]))
                await runner.close()

    async def test_session_abandon_on_result(self) -> None:
        state: dict[str, Any] = {
            "gate_mode": "abandon",
            "release_result": asyncio.Event(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                await self._wait_parked(manager, session.session_id)
                state["release_result"].set()
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                park_task = state["park_task"]
                decision = await asyncio.wait_for(park_task, timeout=2.0)
                self.assertTrue(_is_deny(decision))
                self.assertEqual(state.get("executed", []), [])
                managed = manager._sessions[session.session_id]
                resolved = [
                    event for event in managed.events if event.get("type") == "approval_resolved"
                ]
                self.assertTrue(resolved)
                self.assertEqual(resolved[0]["raw"]["outcome"], "abandoned")
                await runner.close()

    async def test_session_deny_before_stop(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park", "hang_after_decision": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                await self._wait_parked(manager, session.session_id)
                stopped = await manager.stop_session(session.session_id)
                self.assertEqual(stopped.status, "stopped")
                self.assertNotEqual(stopped.status, "awaiting_approval")
                managed = manager._sessions[session.session_id]
                self.assertEqual(managed.approvals.unresolved_count(), 0)
                self.assertGreaterEqual(managed.approvals_denied_on_stop, 1)
                await asyncio.sleep(0.05)
                self.assertEqual(state.get("executed", []), [])
                await runner.close()


if __name__ == "__main__":
    unittest.main()
