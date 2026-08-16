"""Hermetic Codex SDK approval_handler park coverage on worker and in-process paths."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import socket
import sys
import tempfile
from typing import Any, List, Mapping
import unittest
from unittest import mock

from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.codex_sdk.backend import (
    CodexSdkRunner,
    CodexTurnOutcome,
    _default_conversation,
    _thread_resume,
    _thread_start,
)
from agent_collab.backends.codex_sdk.permissions import (
    COMMAND_EXECUTION_APPROVAL,
    FILE_CHANGE_APPROVAL,
    HOST_REVIEW_APPROVAL_POLICY,
    HOST_REVIEW_REVIEWER,
    approval_result_from_decision,
    deny_payload,
    host_review_start_payload,
    make_sync_approval_handler,
    park_codex_tool_approval,
)
from agent_collab.backends.codex_sdk.worker import CodexSdkWorkerBackend
from agent_collab.config import AgentConfig
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.events import Event
from agent_collab.referee import Referee
from agent_collab.sandbox import sdk_worker
from agent_collab.sandbox.sdk_worker import _serve
from agent_collab.sandbox.worker_codec import make_frame, recv_frame, send_frame

from tests.backends.codex_sdk import test_backend as _codex_backend_tests
from tests.backends.codex_sdk.test_backend import _turn_result


def _fake_module(state: dict[str, Any], results: list[Any]):
    return _codex_backend_tests.CodexProductionFactoryTests._fake_module(state, results)


def _patch_openai_codex(module: ModuleType):
    mapping: dict[str, Any] = {"openai_codex": module}
    types = getattr(module, "types", None)
    if types is not None:
        mapping["openai_codex.types"] = types
    return mock.patch.dict(sys.modules, mapping)


AGENT = AgentConfig(id="claude_cli", type="codex", backend="sdk")


def _is_accept(result: Any) -> bool:
    return isinstance(result, Mapping) and result.get("decision") == "accept"


def _is_deny(result: Any) -> bool:
    return isinstance(result, Mapping) and result.get("decision") == "decline"


def _completed_turn() -> Any:
    return SimpleNamespace(
        id="turn-gate",
        status=SimpleNamespace(value="completed"),
        error=None,
        final_response="Done.",
        items=[],
    )


class _ParkingConversation:
    def __init__(self, backend: CodexSdkWorkerBackend, *, sequential: bool = False) -> None:
        self.backend = backend
        self.sequential = sequential
        self.executed: List[str] = []
        self.results: List[Any] = []
        self.started_parks = 0
        self.noted: List[str] = []

    async def run(self, prompt: str) -> CodexTurnOutcome:
        del prompt
        handler = self.backend._sync_approval_handler
        if handler is None:
            raise AssertionError("sync approval handler was not installed; this test must park")
        calls = [(COMMAND_EXECUTION_APPROVAL, {"command": "true"})]
        if self.sequential:
            calls.append((FILE_CHANGE_APPROVAL, {"changes": [{"path": "a.py"}]}))
        for method, params in calls:
            self.started_parks += 1
            result = await asyncio.to_thread(handler, method, params)
            self.results.append(result)
            name = "command" if "commandExecution" in method else "file_change"
            if _is_accept(result):
                self.executed.append(name)
        return CodexTurnOutcome("thread-gate", _completed_turn())

    def note_session_id(self, thread_id: str) -> None:
        self.noted.append(thread_id)

    async def reset(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def interrupt(self) -> bool:
        return False


class PermissionHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_decision_mapping_is_fail_closed(self):
        self.assertTrue(_is_accept(approval_result_from_decision({"decision": "approve"})))
        self.assertTrue(_is_deny(approval_result_from_decision({"decision": "deny"})))
        self.assertTrue(_is_deny(approval_result_from_decision({"decision": "maybe"})))
        self.assertTrue(_is_deny(approval_result_from_decision(None)))

    def test_unknown_method_returns_empty_like_sdk_default(self):
        handler = make_sync_approval_handler(loop=None, park_async=None)
        self.assertEqual(handler("other/method", {}), {})

    def test_missing_helper_denies_immediately(self):
        handler = make_sync_approval_handler(loop=None, park_async=None)
        self.assertEqual(handler(COMMAND_EXECUTION_APPROVAL, {"command": "true"}), deny_payload())

    async def test_unbound_park_helper_denies_immediately(self):
        result = await park_codex_tool_approval(
            request_approval=None,
            method=COMMAND_EXECUTION_APPROVAL,
            params={"command": "true"},
        )
        self.assertTrue(_is_deny(result))

    async def test_sync_handler_returns_only_after_host_decision(self):
        released = asyncio.Event()
        started = asyncio.Event()

        async def park(method: str, params: Any) -> Mapping[str, Any]:
            del method, params
            started.set()
            await released.wait()
            return {"decision": "accept"}

        handler = make_sync_approval_handler(
            loop=asyncio.get_running_loop(),
            park_async=park,
        )
        task = asyncio.create_task(
            asyncio.to_thread(handler, COMMAND_EXECUTION_APPROVAL, {"command": "true"})
        )
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("park helper never started")
        self.assertFalse(task.done())
        released.set()
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(_is_accept(result))

    def test_host_review_payload_routes_to_user(self):
        payload = host_review_start_payload(
            {
                "cwd": "/workspace",
                "model": "gpt-5.6-luna",
                "sandbox": SimpleNamespace(value="full-access"),
            }
        )
        self.assertEqual(payload["approvalPolicy"], HOST_REVIEW_APPROVAL_POLICY)
        self.assertEqual(payload["approvalsReviewer"], HOST_REVIEW_REVIEWER)
        self.assertEqual(payload["cwd"], "/workspace")
        self.assertEqual(payload["model"], "gpt-5.6-luna")
        self.assertEqual(payload["sandbox"], "danger-full-access")

    async def test_gated_start_uses_inner_client_host_review(self):
        recorded: dict[str, Any] = {}

        class Inner:
            async def thread_start(self, params=None):
                recorded["params"] = params
                return SimpleNamespace(thread=SimpleNamespace(id="thread-host-review"))

        async def public_start(**kwargs):
            raise AssertionError(f"gated start used public thread_start kwargs={kwargs}")

        client = SimpleNamespace(_client=Inner(), thread_start=public_start)
        with (
            mock.patch("agent_collab.backends.codex_sdk.backend._require_host_review_types"),
            mock.patch(
                "agent_collab.backends.codex_sdk.backend._wrap_started_thread",
                side_effect=lambda _client, started: SimpleNamespace(id=started.thread.id),
            ),
        ):
            thread = await _thread_start(
                client, {"cwd": "/workspace", "model": "gpt-5.6-luna"}, host_review=True
            )
        self.assertEqual(thread.id, "thread-host-review")
        self.assertEqual(recorded["params"]["approvalsReviewer"], HOST_REVIEW_REVIEWER)
        self.assertEqual(recorded["params"]["approvalPolicy"], HOST_REVIEW_APPROVAL_POLICY)

    async def test_ungated_start_keeps_public_thread_start(self):
        recorded: dict[str, Any] = {}

        class Inner:
            async def thread_start(self, params=None):
                raise AssertionError("ungated start must not use inner host-review start")

        async def public_start(**kwargs):
            recorded["kwargs"] = kwargs
            return SimpleNamespace(id="thread-public")

        client = SimpleNamespace(_client=Inner(), thread_start=public_start)
        thread = await _thread_start(client, {"cwd": "/workspace"}, host_review=False)
        self.assertEqual(thread.id, "thread-public")
        self.assertEqual(recorded["kwargs"], {"cwd": "/workspace"})

    async def test_gated_resume_uses_inner_client_host_review(self):
        recorded: dict[str, Any] = {}

        class Inner:
            async def thread_resume(self, thread_id, params=None):
                recorded["thread_id"] = thread_id
                recorded["params"] = params
                return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

        async def public_resume(thread_id, **kwargs):
            raise AssertionError(f"gated resume used public thread_resume {thread_id} {kwargs}")

        client = SimpleNamespace(_client=Inner(), thread_resume=public_resume)
        with (
            mock.patch("agent_collab.backends.codex_sdk.backend._require_host_review_types"),
            mock.patch(
                "agent_collab.backends.codex_sdk.backend._wrap_started_thread",
                side_effect=lambda _client, started: SimpleNamespace(id=started.thread.id),
            ),
        ):
            thread = await _thread_resume(
                client, "thread-resume", {"cwd": "/workspace"}, host_review=True
            )
        self.assertEqual(thread.id, "thread-resume")
        self.assertEqual(recorded["thread_id"], "thread-resume")
        self.assertEqual(recorded["params"]["approvalsReviewer"], HOST_REVIEW_REVIEWER)
        self.assertEqual(recorded["params"]["threadId"], "thread-resume")

    async def test_gated_start_fails_closed_when_inner_thread_start_missing(self):
        recorded: list[Any] = []

        async def public_start(**kwargs):
            recorded.append(kwargs)
            raise AssertionError(f"gated start used public thread_start kwargs={kwargs}")

        client = SimpleNamespace(_client=SimpleNamespace(), thread_start=public_start)
        with self.assertRaises(BackendUnavailable) as raised:
            await _thread_start(client, {"cwd": "/workspace"}, host_review=True)
        self.assertEqual(recorded, [])
        self.assertIn("inner thread_start", str(raised.exception))

    async def test_gated_resume_fails_closed_when_inner_thread_resume_missing(self):
        recorded: list[Any] = []

        async def public_resume(thread_id, **kwargs):
            recorded.append((thread_id, kwargs))
            raise AssertionError(f"gated resume used public thread_resume {thread_id} {kwargs}")

        client = SimpleNamespace(_client=SimpleNamespace(), thread_resume=public_resume)
        with self.assertRaises(BackendUnavailable) as raised:
            await _thread_resume(client, "thread-resume", {"cwd": "/workspace"}, host_review=True)
        self.assertEqual(recorded, [])
        self.assertIn("inner thread_resume", str(raised.exception))


class CodexSdkWorkerToolGateTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _drive(self, backend: CodexSdkWorkerBackend, fake_conv):
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        with (
            mock.patch.object(sdk_worker, "_registry", return_value={"codex_sdk": lambda: backend}),
            mock.patch(
                "agent_collab.backends.codex_sdk.worker._default_conversation",
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
                            "backend": "codex_sdk",
                            "workspace": "/tmp",
                            "options": {"sandbox": "read-only"},
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
        backend = CodexSdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            holder["approval_handler"] = kwargs.get("approval_handler")
            conv = _ParkingConversation(backend)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            self.assertIs(holder["approval_handler"], backend._sync_approval_handler)
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            request = await self._recv_until(reader, "approval_request")
            self.assertEqual(request["tool_name"], "command")
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
            self.assertEqual(holder["conv"].executed, ["command"])
            self.assertTrue(_is_accept(holder["conv"].results[0]))
            await send_frame(writer, make_frame("close", request_id="close-1"))
            closed = await self._recv_until(reader, "closed")
            self.assertEqual(closed["type"], "closed")

    async def test_worker_deny_does_not_execute_tool(self) -> None:
        backend = CodexSdkWorkerBackend()
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

    async def test_worker_sequential_parks(self) -> None:
        backend = CodexSdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            conv = _ParkingConversation(backend, sequential=True)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            first = await self._recv_until(reader, "approval_request")
            await asyncio.sleep(0.05)
            self.assertEqual(holder["conv"].started_parks, 1)
            self.assertEqual(holder["conv"].results, [])
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id=first["approval_id"],
                    decision="approve",
                    run_id="run-1",
                ),
            )
            second = await self._recv_until(reader, "approval_request")
            self.assertNotEqual(first["approval_id"], second["approval_id"])
            self.assertEqual(second["tool_name"], "file_change")
            self.assertNotIn("tool_input", second)
            await send_frame(
                writer,
                make_frame(
                    "approval_decision",
                    approval_id=second["approval_id"],
                    decision="approve",
                    run_id="run-1",
                ),
            )
            result = await self._recv_until(reader, "result")
            self.assertEqual(result["type"], "result")
            self.assertEqual(holder["conv"].executed, ["command", "file_change"])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            await self._recv_until(reader, "closed")

    async def test_worker_unknown_decision_during_park_is_noop(self) -> None:
        backend = CodexSdkWorkerBackend()
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

    async def test_worker_park_with_no_listener_denies_immediately(self) -> None:
        backend = CodexSdkWorkerBackend()
        conv = _ParkingConversation(backend)

        def fake_conv(*_args: Any, **kwargs: Any):
            return conv

        with mock.patch(
            "agent_collab.backends.codex_sdk.worker._default_conversation",
            fake_conv,
        ):
            await backend.open(
                {
                    "workspace": "/tmp",
                    "options": {},
                    "verbose": False,
                    "agent_env": {},
                }
            )
        self.assertIsNone(backend._request_approval)
        outcome = await asyncio.wait_for(backend.run("gate", run_id="run-1"), timeout=2.0)
        _events, result = outcome
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(conv.executed, [])
        self.assertTrue(_is_deny(conv.results[0]))
        await backend.close()


class CodexSdkInProcessToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_process_approve_parks_and_executes(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        held: dict[str, Any] = {}

        def callback(payload: Mapping[str, Any]) -> None:
            held["payload"] = payload

        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        module, _, _ = _fake_module(state, [_turn_result(final_response="Done.")])
        with _patch_openai_codex(module):
            events: list[Event] = []

            async def emit(event: Event) -> None:
                events.append(event)

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if "payload" in held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("in-process approval_handler never parked")
            self.assertTrue(
                callable(state["async_codex_clients"][0]._client._sync._approval_handler)
            )
            self.assertNotIn("tool_input", held["payload"])
            await held["payload"]["send_decision"]("approve")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed"), ["command"])
        self.assertTrue(_is_accept(state["approval_results"][0]))

    async def test_in_process_deny_does_not_execute_tool(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        held: dict[str, Any] = {}

        def callback(payload: Mapping[str, Any]) -> None:
            held["payload"] = payload

        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        module, _, _ = _fake_module(state, [_turn_result(final_response="Done.")])
        with _patch_openai_codex(module):

            async def emit(_event: Event) -> None:
                return None

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if "payload" in held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("in-process approval_handler never parked")
            await held["payload"]["send_decision"]("deny")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed", []), [])
        self.assertTrue(_is_deny(state["approval_results"][0]))

    async def test_in_process_sequential_parks(self) -> None:
        state: dict[str, Any] = {"gate_mode": "sequential"}
        held: list[Mapping[str, Any]] = []

        def callback(payload: Mapping[str, Any]) -> None:
            held.append(payload)

        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        module, _, _ = _fake_module(state, [_turn_result(final_response="Done.")])
        with _patch_openai_codex(module):

            async def emit(_event: Event) -> None:
                return None

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("first sequential park never registered")
            self.assertEqual(len(held), 1)
            await held[0]["send_decision"]("approve")
            for _ in range(200):
                if len(held) >= 2:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("second sequential park never registered")
            self.assertEqual(len({item["request_id"] for item in held}), 2)
            await held[1]["send_decision"]("approve")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed", []), ["command", "file_change"])

    async def test_ungated_in_process_keeps_default_accept(self) -> None:
        state: dict[str, Any] = {}
        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        module, _, _ = _fake_module(state, [_turn_result(final_response="Done.")])
        with mock.patch.dict(sys.modules, {"openai_codex": module}):

            async def emit(_event: Event) -> None:
                return None

            outcome = await runner.run_turn("plain", Path("/workspace"), emit)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed", []), [])
        self.assertIsNone(state["async_codex_clients"][0]._client._sync._approval_handler)

    async def test_gated_connect_fails_closed_when_approval_hook_missing(self) -> None:
        module = ModuleType("openai_codex")

        class FakeThread:
            def __init__(self):
                self.id = "thread-broken"

            async def turn(self, prompt, **kwargs):
                del prompt, kwargs
                raise AssertionError("connect must fail before turn")

        class BrokenAsyncCodex:
            def __init__(self, config=None):
                del config

            async def __aenter__(self):
                return self

            async def close(self):
                return None

            async def thread_start(self, **kwargs):
                del kwargs
                return FakeThread()

        module.AsyncCodex = BrokenAsyncCodex
        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(lambda payload: None)
        with mock.patch.dict(sys.modules, {"openai_codex": module}):

            async def emit(_event: Event) -> None:
                return None

            outcome = await runner.run_turn("gate", Path("/workspace"), emit)
            await runner.close()
        self.assertEqual(outcome.outcome, "failed")

    async def test_gated_connect_fails_closed_when_inner_thread_start_missing(self) -> None:
        module = ModuleType("openai_codex")
        started: list[Any] = []

        class _FakeSync:
            def __init__(self) -> None:
                self._approval_handler = None

        class FakeThread:
            def __init__(self):
                self.id = "thread-public"

            async def turn(self, prompt, **kwargs):
                del prompt, kwargs
                raise AssertionError("gated connect must not reach public thread_start")

        class FakeAsyncCodex:
            def __init__(self, config=None):
                del config
                self._client = SimpleNamespace(_sync=_FakeSync())

            async def __aenter__(self):
                return self

            async def close(self):
                return None

            async def thread_start(self, **kwargs):
                started.append(kwargs)
                return FakeThread()

            async def thread_resume(self, thread_id, **kwargs):
                del thread_id, kwargs
                raise AssertionError("gated connect must not resume")

        module.AsyncCodex = FakeAsyncCodex
        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(lambda payload: None)
        with mock.patch.dict(sys.modules, {"openai_codex": module}):
            events: list[Event] = []

            async def emit(event: Event) -> None:
                events.append(event)

            outcome = await runner.run_turn("gate", Path("/workspace"), emit)
            await runner.close()
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(outcome.code, "provider_transport_failed")
        self.assertEqual(started, [])
        self.assertTrue(
            any("inner thread_start" in event.text for event in events),
            events,
        )


class CodexSdkSessionToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def _start_gated_session(
        self,
        state: dict[str, Any],
        root: Path,
        *,
        approval_deadline: float = 2.0,
        timeout: int = 5,
    ):
        runner = CodexSdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)

        def _runners(self: Referee):
            if self.config.approval_callback is not None:
                runner.set_approval_callback(self.config.approval_callback)
            return {"claude_cli": runner}

        manager = SessionManager()
        module, _, _ = _fake_module(state, [_turn_result(final_response="Done.")])
        module_patch = _patch_openai_codex(module)
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
                timeout=timeout,
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
                self.fail("session settled without parking approval_handler")
            await asyncio.sleep(0.02)
        self.fail("timed out waiting for approval_handler park")

    async def test_session_approve_emits_events_and_continues(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                parked = await self._wait_parked(manager, session.session_id)
                self.assertEqual(parked.pending_approvals[0]["tool_name"], "command")
                decided = await manager.resolve_approval(
                    session.session_id, parked.pending_approvals[0]["request_id"], "approve"
                )
                self.assertEqual(decided["outcome"], "approved")
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed"), ["command"])
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

    async def test_session_sequential_parks(self) -> None:
        state: dict[str, Any] = {"gate_mode": "sequential"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                first = await self._wait_parked(manager, session.session_id)
                self.assertEqual(len(first.pending_approvals), 1)
                await manager.resolve_approval(
                    session.session_id, first.pending_approvals[0]["request_id"], "approve"
                )
                second = await self._wait_parked(manager, session.session_id)
                self.assertEqual(len(second.pending_approvals), 1)
                await manager.resolve_approval(
                    session.session_id, second.pending_approvals[0]["request_id"], "approve"
                )
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed", []), ["command", "file_change"])
                await runner.close()

    async def test_session_deadline_auto_denies(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                manager._sessions[session.session_id].request.approval_deadline = 0.05
                parked = await self._wait_parked(manager, session.session_id)
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
                self.assertTrue(_is_deny(state["approval_results"][0]))
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

    async def test_session_parked_interval_excluded_from_turn_clock(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(
                    state, root, timeout=1, approval_deadline=2.0
                )
                parked = await self._wait_parked(manager, session.session_id)
                await asyncio.sleep(1.3)
                await manager.resolve_approval(
                    session.session_id, parked.pending_approvals[0]["request_id"], "approve"
                )
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed"), ["command"])
                await runner.close()


class CodexSdkToolGateCapabilityTests(unittest.TestCase):
    def test_production_capabilities_stay_false_except_continuity(self):
        from agent_collab import backends

        caps = backends.capabilities_for("codex", "sdk")
        self.assertEqual(
            caps.to_dict(),
            {"resume": False, "interrupt": False, "tool_gate": False, "continuity": True},
        )


if __name__ == "__main__":
    unittest.main()
