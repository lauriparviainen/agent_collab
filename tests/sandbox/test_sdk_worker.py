"""Hermetic coverage for WorkerBackend interrupt and approval-bind seams."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import socket
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Tuple
import unittest
from unittest import mock

from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend
from agent_collab.backends.claude_sdk.worker import ClaudeSdkWorkerBackend
from agent_collab.backends.codex_sdk.worker import CodexSdkWorkerBackend
from agent_collab.outcomes import TurnOutcome
from agent_collab.sandbox import sdk_worker
from agent_collab.sandbox.sdk_worker import _register_pending_approval, _serve
from agent_collab.sandbox.worker_codec import make_frame, recv_frame, send_frame


class _FakeWorkerBackend:
    def __init__(self) -> None:
        self.bound: Optional[Callable[..., Awaitable[Mapping[str, Any]]]] = None
        self.interrupts: List[str] = []
        self._release = asyncio.Event()
        self._park: Optional[Callable[..., Awaitable[Mapping[str, Any]]]] = None
        self.duplicate_park = False
        self.use_park = False
        self.park_results: List[Mapping[str, Any]] = []

    async def open(self, payload: Mapping[str, Any]) -> None:
        del payload

    async def run(
        self,
        prompt: str,
        *,
        run_id: str,
        emit: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Tuple[list[Any], TurnOutcome]:
        del prompt, emit
        if self.duplicate_park or self.use_park:
            first = asyncio.create_task(self._park("appr-1", tool_name="Bash", summary="true"))
            if self.duplicate_park:
                second = asyncio.create_task(
                    self._park("appr-1", tool_name="Bash", summary="again")
                )
                results = await asyncio.gather(first, second)
                self.park_results.extend(results)
            else:
                self.park_results.append(await first)
            return [], TurnOutcome("completed")
        await self._release.wait()
        return [], TurnOutcome("interrupted", "local_turn_interrupted")

    async def reset(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def interrupt(self, run_id: str) -> None:
        self.interrupts.append(run_id)
        self._release.set()

    def bind_approvals(self, request_approval: Callable[..., Awaitable[Mapping[str, Any]]]) -> None:
        self.bound = request_approval
        self._park = request_approval


class WorkerBackendHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_concrete_backends_interrupt_is_noop_and_bind_stores(self) -> None:
        async def unused(**_fields: Any) -> Mapping[str, Any]:
            raise AssertionError("Stage 1 must not invoke the approval producer")

        for backend in (
            ClaudeSdkWorkerBackend(),
            CodexSdkWorkerBackend(),
            AntigravitySdkWorkerBackend(),
        ):
            await backend.interrupt("run-1")
            backend.bind_approvals(unused)
            self.assertIs(backend._request_approval, unused)

    def test_serve_loop_calls_protocol_interrupt_not_getattr(self) -> None:
        source = inspect.getsource(sdk_worker._serve)
        self.assertNotIn('getattr(backend, "interrupt"', source)
        self.assertIn("await backend.interrupt(target)", source)
        self.assertIn("backend.bind_approvals(request_approval)", source)

    async def test_register_pending_approval_duplicate_does_not_double_park(self) -> None:
        pending: dict[str, asyncio.Future[Any]] = {}
        first, parked_first = _register_pending_approval(pending, "appr-1")
        second, parked_second = _register_pending_approval(pending, "appr-1")
        self.assertTrue(parked_first)
        self.assertFalse(parked_second)
        self.assertIs(first, second)
        self.assertEqual(len(pending), 1)
        first.set_result({"decision": "deny"})
        self.assertIs(await second, first.result())

    @asynccontextmanager
    async def _drive(self, backend: _FakeWorkerBackend):
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        with mock.patch.object(sdk_worker, "_registry", return_value={"fake": lambda: backend}):
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
                        payload={"backend": "fake", "workspace": "/tmp"},
                    ),
                )
                ready = await recv_frame(reader)
                self.assertEqual(ready["type"], "ready")
                self.assertIsNotNone(backend.bound)
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

    async def test_serve_binds_after_open_and_dispatches_interrupt(self) -> None:
        backend = _FakeWorkerBackend()
        async with self._drive(backend) as (reader, writer):
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="hang"))
            started = await recv_frame(reader)
            self.assertEqual(started["type"], "event")
            await send_frame(writer, make_frame("interrupt", run_id="other"))
            await asyncio.sleep(0.08)
            self.assertEqual(backend.interrupts, [])
            await send_frame(writer, make_frame("interrupt", run_id="run-1"))
            result = await recv_frame(reader)
            self.assertEqual(result["type"], "result")
            self.assertEqual(backend.interrupts, ["run-1"])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            closed = await recv_frame(reader)
            self.assertEqual(closed["type"], "closed")

    async def test_approval_helper_enqueues_and_duplicate_does_not_double_park(self) -> None:
        backend = _FakeWorkerBackend()
        backend.duplicate_park = True
        async with self._drive(backend) as (reader, writer):
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            started = await recv_frame(reader)
            self.assertEqual(started["type"], "event")
            request = await recv_frame(reader)
            self.assertEqual(request["type"], "approval_request")
            self.assertEqual(request["approval_id"], "appr-1")
            # Duplicate id must not enqueue a second frame before the decision.
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id="appr-1",
                    decision="deny",
                    run_id="run-1",
                ),
            )
            result = await recv_frame(reader)
            self.assertEqual(result["type"], "result")
            self.assertEqual(len(backend.park_results), 2)
            self.assertEqual(backend.park_results[0].get("decision"), "deny")
            self.assertEqual(backend.park_results[1].get("decision"), "deny")
            await send_frame(writer, make_frame("close", request_id="close-1"))
            closed = await recv_frame(reader)
            self.assertEqual(closed["type"], "closed")
