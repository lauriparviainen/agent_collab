"""Hermetic Antigravity SDK ask_user park coverage on worker and in-process paths."""

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

from agent_collab.backends.antigravity_sdk.backend import (
    AntigravitySdkRunner,
    AntigravityTurn,
    _default_agent_factory,
    _default_conversation,
)
from agent_collab.backends.antigravity_sdk.permissions import (
    PinnedAskUserHandler,
    approval_result_from_decision,
    park_antigravity_tool_approval,
    tool_name_from_call,
)
from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend
from agent_collab.backends.base import BackendUnavailable
from agent_collab.config import AgentConfig
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.events import Event
from agent_collab.referee import Referee
from agent_collab.sandbox import sdk_worker
from agent_collab.sandbox.sdk_worker import _serve
from agent_collab.sandbox.worker_codec import make_frame, recv_frame, send_frame


AGENT = AgentConfig(id="claude_cli", type="antigravity", backend="sdk")


def _attach_durable_plan(runner: AntigravitySdkRunner, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="ag-traj-"))
    save_dir = base / "trajectories" / "sess-gate"
    runner.sandbox_plan = SimpleNamespace(
        spec=SimpleNamespace(
            environment=SimpleNamespace(set_values={"ANTIGRAVITY_SAVE_DIR": str(save_dir)})
        )
    )
    return base


def _tool_call(name: str, args: Mapping[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, args=dict(args or {}))


class _Text:
    def __init__(self, text: str) -> None:
        self.step_index = 0
        self.text = text


class _DoneResponse:
    usage_metadata = None

    async def resolve(self) -> list[Any]:
        return [_Text("Done.")]


def _done_turn() -> AntigravityTurn:
    return AntigravityTurn(
        chunks=[_Text("Done.")],
        usage_metadata=None,
        conversation_id="conv-gate",
        response_clean_close=True,
    )


class _ParkingConversation:
    def __init__(self, backend: AntigravitySdkWorkerBackend, *, overlap: bool = False) -> None:
        self.backend = backend
        self.overlap = overlap
        self.executed: List[str] = []
        self.results: List[Any] = []
        self.noted: List[str] = []

    async def run(self, prompt: str) -> AntigravityTurn:
        del prompt
        calls = [_tool_call("run_command", {"CommandLine": "true"})]
        if self.overlap:
            calls.append(_tool_call("edit_file", {"TargetFile": "a.py"}))
            tasks = [asyncio.create_task(self.backend._ask_user(call)) for call in calls]
            results = await asyncio.gather(*tasks)
        else:
            results = [await self.backend._ask_user(calls[0])]
        self.results.extend(results)
        names = ("run_command", "edit_file") if self.overlap else ("run_command",)
        for result, name in zip(results, names):
            if result is True:
                self.executed.append(name)
        return _done_turn()

    def note_session_id(self, conversation_id: str) -> None:
        self.noted.append(conversation_id)

    async def reset(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def interrupt(self) -> bool:
        return False


def _ask_user_from_config(config: Any) -> Any:
    policies = getattr(config, "policies", None)
    if policies is None and hasattr(config, "kwargs"):
        policies = config.kwargs.get("policies")
    if not policies:
        return None
    for item in policies:
        handler = getattr(item, "ask_user", None)
        if callable(handler):
            return handler
    return None


def _fake_antigravity_modules(
    state: dict[str, Any],
    *,
    policies_field: bool = True,
    policy_api: bool = True,
) -> dict[str, Any]:
    module = ModuleType("google.antigravity")
    types_mod = ModuleType("google.antigravity.types")
    hooks_mod = ModuleType("google.antigravity.hooks")
    policy_mod = ModuleType("google.antigravity.hooks.policy")

    class SessionContinuationMode:
        RESUME = "resume"

    types_mod.SessionContinuationMode = SessionContinuationMode

    def ask_user(tool: str, *, handler: Any, when: Any = None, name: str = "") -> Any:
        del when
        state.setdefault("ask_user_calls", []).append(tool)
        return SimpleNamespace(tool=tool, decision="ASK_USER", ask_user=handler, name=name)

    def allow_all() -> Any:
        state.setdefault("allow_all_calls", []).append(True)
        return SimpleNamespace(tool="*", decision="APPROVE", ask_user=None, name="allow_all")

    policy_mod.ask_user = ask_user
    policy_mod.allow_all = allow_all
    hooks_mod.policy = policy_mod

    fields = {
        "conversation_id": object(),
        "save_dir": object(),
        "session_continuation_mode": object(),
        "app_data_dir": object(),
        "capabilities": object(),
    }
    if policies_field:
        fields["policies"] = object()

    class LocalAgentConfig:
        model_fields = fields

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)
            state.setdefault("configs", []).append(kwargs)

    class FakeAgent:
        def __init__(self, config: Any) -> None:
            self._config = config
            self.conversation_id = None
            state.setdefault("agents", []).append(self)

        async def __aenter__(self) -> FakeAgent:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            del exc_type, exc, tb
            return None

        async def chat(self, prompt: str) -> Any:
            del prompt
            handler = _ask_user_from_config(self._config)
            if handler is None:
                if state.get("require_gate"):
                    raise AssertionError("ask_user was not registered; this test must park")
                self.conversation_id = "conv-gate"
                return _DoneResponse()
            mode = state.get("gate_mode", "park")
            command = _tool_call("run_command", {"CommandLine": "true"})
            file_change = _tool_call("edit_file", {"TargetFile": "a.py"})

            async def invoke(tool_call: Any) -> bool:
                result = handler(tool_call)
                if asyncio.iscoroutine(result):
                    result = await result
                return bool(result)

            if mode == "overlap":
                results = await asyncio.gather(invoke(command), invoke(file_change))
            elif mode == "abandon":
                park = asyncio.create_task(invoke(command))
                state["park_task"] = park
                await state["release_result"].wait()
                results = []
            else:
                results = [await invoke(command)]
            state.setdefault("approval_results", []).extend(results)
            names = ("run_command", "edit_file") if mode == "overlap" else ("run_command",)
            for result, name in zip(results, names):
                if result is True:
                    state.setdefault("executed", []).append(name)
            if mode != "abandon" and state.get("hang_after_decision"):
                await asyncio.Event().wait()
            self.conversation_id = "conv-gate"
            return _DoneResponse()

    module.Agent = FakeAgent
    module.LocalAgentConfig = LocalAgentConfig
    module.hooks = hooks_mod
    mapping: dict[str, Any] = {
        "google.antigravity": module,
        "google.antigravity.types": types_mod,
    }
    if policy_api:
        mapping["google.antigravity.hooks"] = hooks_mod
        mapping["google.antigravity.hooks.policy"] = policy_mod
    else:
        mapping["google.antigravity.hooks"] = None
        mapping["google.antigravity.hooks.policy"] = None
    return mapping


class PermissionHelperTests(unittest.TestCase):
    def test_pinned_handler_survives_deepcopy(self) -> None:
        from copy import deepcopy

        calls: list[str] = []

        async def impl(tool_call: Any) -> bool:
            calls.append(getattr(tool_call, "name", ""))
            return True

        pinned = PinnedAskUserHandler(impl)
        copied = deepcopy(pinned)
        self.assertIs(copied, pinned)

    def test_decision_mapping_is_fail_closed(self) -> None:
        self.assertTrue(approval_result_from_decision({"decision": "approve"}))
        self.assertFalse(approval_result_from_decision({"decision": "deny"}))
        self.assertFalse(approval_result_from_decision({"decision": "maybe"}))
        self.assertFalse(approval_result_from_decision(None))

    def test_tool_name_uses_enum_value(self) -> None:
        import enum

        class Builtin(enum.Enum):
            RUN_COMMAND = "run_command"

        self.assertEqual(
            tool_name_from_call(SimpleNamespace(name=Builtin.RUN_COMMAND)), "run_command"
        )
        self.assertEqual(tool_name_from_call(SimpleNamespace(name="edit_file")), "edit_file")


class PermissionHelperAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbound_park_helper_denies_immediately(self) -> None:
        result = await park_antigravity_tool_approval(
            request_approval=None,
            tool_call=_tool_call("run_command", {"CommandLine": "true"}),
        )
        self.assertFalse(result)


class AntigravitySdkWorkerToolGateTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _drive(self, backend: AntigravitySdkWorkerBackend, fake_conv):
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            traj = root / "traj"
            app = root / "app"
            traj.mkdir()
            app.mkdir()
            with (
                mock.patch.object(
                    sdk_worker, "_registry", return_value={"antigravity_sdk": lambda: backend}
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.worker._default_conversation",
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
                                "backend": "antigravity_sdk",
                                "workspace": str(root / "workspace"),
                                "options": {},
                                "save_dir": str(traj),
                                "app_data_dir": str(app),
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
        backend = AntigravitySdkWorkerBackend()
        holder: dict[str, Any] = {}

        def fake_conv(*_args: Any, **kwargs: Any):
            holder["ask_user_handler"] = kwargs.get("ask_user_handler")
            holder["allow_all_policy"] = kwargs.get("allow_all_policy")
            conv = _ParkingConversation(backend)
            holder["conv"] = conv
            return conv

        async with self._drive(backend, fake_conv) as (reader, writer):
            handler = holder["ask_user_handler"]
            self.assertIsInstance(handler, PinnedAskUserHandler)
            self.assertIs(handler._impl.__func__, backend._ask_user.__func__)
            self.assertNotEqual(holder["allow_all_policy"], True)
            await send_frame(writer, make_frame("run", run_id="run-1", prompt="gate"))
            request = await self._recv_until(reader, "approval_request")
            self.assertEqual(request["tool_name"], "run_command")
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
            self.assertEqual(holder["conv"].executed, ["run_command"])
            self.assertTrue(holder["conv"].results[0])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            closed = await self._recv_until(reader, "closed")
            self.assertEqual(closed["type"], "closed")

    async def test_worker_deny_does_not_execute_tool(self) -> None:
        backend = AntigravitySdkWorkerBackend()
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
            self.assertFalse(holder["conv"].results[0])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            await self._recv_until(reader, "closed")

    async def test_worker_overlapping_parks(self) -> None:
        backend = AntigravitySdkWorkerBackend()
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
                self.assertNotIn("tool_input", frame)
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
            self.assertEqual(sorted(holder["conv"].executed), ["edit_file", "run_command"])
            await send_frame(writer, make_frame("close", request_id="close-1"))
            await self._recv_until(reader, "closed")

    async def test_worker_unknown_decision_during_park_is_noop(self) -> None:
        backend = AntigravitySdkWorkerBackend()
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
        backend = AntigravitySdkWorkerBackend()
        conv = _ParkingConversation(backend)

        def fake_conv(*_args: Any, **kwargs: Any):
            return conv

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            traj = root / "traj"
            app = root / "app"
            traj.mkdir()
            app.mkdir()
            with mock.patch(
                "agent_collab.backends.antigravity_sdk.worker._default_conversation",
                fake_conv,
            ):
                await backend.open(
                    {
                        "workspace": str(root / "workspace"),
                        "options": {},
                        "verbose": False,
                        "agent_env": {},
                        "save_dir": str(traj),
                        "app_data_dir": str(app),
                    }
                )
        self.assertIsNone(backend._request_approval)
        outcome = await asyncio.wait_for(backend.run("gate", run_id="run-1"), timeout=2.0)
        _events, result = outcome
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(conv.executed, [])
        self.assertFalse(conv.results[0])
        await backend.close()


class AntigravitySdkInProcessToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_process_approve_parks_and_executes(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park", "require_gate": True}
        held: dict[str, Any] = {}

        def callback(payload: Mapping[str, Any]) -> None:
            held["payload"] = payload

        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        _attach_durable_plan(runner)
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state)):
            events: list[Event] = []

            async def emit(event: Event) -> None:
                events.append(event)

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if "payload" in held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("in-process ask_user never parked")
            self.assertNotIn("tool_input", held["payload"])
            policies = state["configs"][0].get("policies") or []
            self.assertEqual(len(policies), 1)
            self.assertEqual(policies[0].decision, "ASK_USER")
            self.assertEqual(state.get("allow_all_calls", []), [])
            await held["payload"]["send_decision"]("approve")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed"), ["run_command"])
        self.assertTrue(state["approval_results"][0])

    async def test_in_process_deny_does_not_execute_tool(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park", "require_gate": True}
        held: dict[str, Any] = {}

        def callback(payload: Mapping[str, Any]) -> None:
            held["payload"] = payload

        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        _attach_durable_plan(runner)
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state)):

            async def emit(_event: Event) -> None:
                return None

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if "payload" in held:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("in-process ask_user never parked")
            await held["payload"]["send_decision"]("deny")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed", []), [])
        self.assertFalse(state["approval_results"][0])

    async def test_in_process_overlapping_parks(self) -> None:
        state: dict[str, Any] = {"gate_mode": "overlap", "require_gate": True}
        held: list[Mapping[str, Any]] = []

        def callback(payload: Mapping[str, Any]) -> None:
            held.append(payload)

        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(callback)
        runner.bind_turn(turn_id="turn-1", agent_id="claude_cli")
        _attach_durable_plan(runner)
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state)):

            async def emit(_event: Event) -> None:
                return None

            turn = asyncio.create_task(runner.run_turn("gate", Path("/workspace"), emit))
            for _ in range(200):
                if len(held) >= 2:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("overlapping ask_user parks never both registered")
            self.assertEqual(len({item["request_id"] for item in held}), 2)
            for payload in held:
                await payload["send_decision"]("approve")
            outcome = await asyncio.wait_for(turn, timeout=2.0)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(sorted(state.get("executed", [])), ["edit_file", "run_command"])

    async def test_ungated_in_process_does_not_install_ask_user(self) -> None:
        state: dict[str, Any] = {}
        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        _attach_durable_plan(runner)
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state)):

            async def emit(_event: Event) -> None:
                return None

            outcome = await runner.run_turn("plain", Path("/workspace"), emit)
            await runner.close()
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(state.get("executed", []), [])
        self.assertEqual(state.get("ask_user_calls", []), [])
        self.assertNotIn("policies", state["configs"][0])

    async def test_gated_connect_fails_closed_when_policies_field_missing(self) -> None:
        state: dict[str, Any] = {"require_gate": True}
        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(lambda payload: None)
        _attach_durable_plan(runner)
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state, policies_field=False)):
            events: list[Event] = []

            async def emit(event: Event) -> None:
                events.append(event)

            outcome = await runner.run_turn("gate", Path("/workspace"), emit)
            await runner.close()
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(outcome.code, "provider_transport_failed")
        self.assertTrue(any("policies field" in event.text for event in events), events)

    async def test_gated_connect_fails_closed_when_policy_api_missing(self) -> None:
        state: dict[str, Any] = {"require_gate": True}
        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        runner.set_approval_callback(lambda payload: None)
        _attach_durable_plan(runner)
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state, policy_api=False)):
            events: list[Event] = []

            async def emit(event: Event) -> None:
                events.append(event)

            outcome = await runner.run_turn("gate", Path("/workspace"), emit)
            await runner.close()
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(outcome.code, "provider_transport_failed")
        self.assertTrue(any("tool policy API" in event.text for event in events), events)


class AntigravitySdkFactoryToolGateTests(unittest.TestCase):
    def test_gated_factory_installs_ask_user_not_allow_all(self) -> None:
        state: dict[str, Any] = {}

        async def handler(tool_call: Any) -> bool:
            del tool_call
            return False

        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state)):
            _default_agent_factory(
                AGENT,
                {},
                Path("/workspace"),
                ask_user_handler=handler,
            )
        policies = state["configs"][0]["policies"]
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0].tool, "*")
        self.assertEqual(policies[0].decision, "ASK_USER")
        self.assertIs(policies[0].ask_user, handler)
        self.assertEqual(state.get("allow_all_calls", []), [])
        self.assertEqual(state.get("ask_user_calls"), ["*"])

    def test_ungated_factory_does_not_install_ask_user(self) -> None:
        state: dict[str, Any] = {}
        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state)):
            _default_agent_factory(AGENT, {}, Path("/workspace"))
        self.assertNotIn("policies", state["configs"][0])
        self.assertEqual(state.get("ask_user_calls", []), [])

    def test_gated_factory_fails_closed_when_policies_field_missing(self) -> None:
        state: dict[str, Any] = {}

        async def handler(tool_call: Any) -> bool:
            del tool_call
            return False

        with mock.patch.dict(sys.modules, _fake_antigravity_modules(state, policies_field=False)):
            with self.assertRaises(BackendUnavailable) as raised:
                _default_agent_factory(
                    AGENT,
                    {},
                    Path("/workspace"),
                    ask_user_handler=handler,
                )
        self.assertIn("policies field", str(raised.exception))
        self.assertEqual(state.get("configs", []), [])


class AntigravitySdkSessionToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def _start_gated_session(
        self,
        state: dict[str, Any],
        root: Path,
        *,
        approval_deadline: float = 2.0,
    ):
        runner = AntigravitySdkRunner(AGENT, False, {}, conversation_factory=_default_conversation)
        _attach_durable_plan(runner, root)

        def _runners(self: Referee):
            if self.config.approval_callback is not None:
                runner.set_approval_callback(self.config.approval_callback)
            return {"claude_cli": runner}

        manager = SessionManager()
        module_patch = mock.patch.dict(sys.modules, _fake_antigravity_modules(state))
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
                self.fail("session settled without parking ask_user")
            await asyncio.sleep(0.02)
        self.fail("timed out waiting for ask_user park")

    async def test_session_approve_emits_events_and_continues(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park", "require_gate": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                parked = await self._wait_parked(manager, session.session_id)
                self.assertEqual(parked.pending_approvals[0]["tool_name"], "run_command")
                decided = await manager.resolve_approval(
                    session.session_id, parked.pending_approvals[0]["request_id"], "approve"
                )
                self.assertEqual(decided["outcome"], "approved")
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(state.get("executed"), ["run_command"])
                managed = manager._sessions[session.session_id]
                types = [event.get("type") for event in managed.events]
                self.assertIn("approval_request", types)
                self.assertIn("approval_resolved", types)
                await runner.close()

    async def test_session_deny_does_not_execute_tool(self) -> None:
        state: dict[str, Any] = {"gate_mode": "park", "require_gate": True}
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
        state: dict[str, Any] = {"gate_mode": "overlap", "require_gate": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager, session, runner = await self._start_gated_session(state, root)
                parked = await self._wait_parked(manager, session.session_id, count=2)
                self.assertEqual(len(parked.pending_approvals), 2)
                for item in parked.pending_approvals:
                    await manager.resolve_approval(
                        session.session_id, item["request_id"], "approve"
                    )
                result = await self._wait_status(manager, session.session_id, "done")
                self.assertEqual(result.status, "done")
                self.assertEqual(sorted(state.get("executed", [])), ["edit_file", "run_command"])
                await runner.close()


class AntigravitySdkToolGateCapabilityTests(unittest.TestCase):
    def test_production_tool_gate_is_true(self) -> None:
        from agent_collab import backends

        caps = backends.capabilities_for("antigravity", "sdk")
        self.assertEqual(
            caps.to_dict(),
            {"resume": True, "interrupt": True, "tool_gate": True, "continuity": True},
        )


if __name__ == "__main__":
    unittest.main()
