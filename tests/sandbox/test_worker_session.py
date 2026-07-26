"""Hermetic fake-worker lifecycle for the framed SDK protocol."""

from __future__ import annotations

import asyncio
import socket
import unittest

from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.sandbox.worker_codec import (
    WorkerProtocolError,
    make_frame,
    recv_frame,
    sanitize_error_text,
    send_frame,
)
from agent_collab.sandbox.worker_session import SupervisedWorkerSession, handshake_worker
from agent_collab.sandbox.supervisor import _worker_python_executable


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self._done = asyncio.Event()
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return int(self.returncode or 0)


class WorkerSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_run_close_with_fake_worker(self) -> None:
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        worker.setblocking(False)
        worker_task = asyncio.create_task(self._fake_worker(worker))
        try:
            reader, writer = await asyncio.open_connection(sock=daemon)
            instance = await handshake_worker(reader, writer)
            self.assertTrue(instance)
            session = SupervisedWorkerSession(_FakeProcess(), reader, writer, instance=instance)
            await session.open({"backend": "fake", "workspace": "/tmp/ws"})
            streamed: list = []

            async def capture(event) -> None:
                streamed.append(event)

            events, outcome = await session.run("hello", emit=capture)
            self.assertEqual(outcome.outcome, "completed")
            self.assertEqual(events, [])
            self.assertEqual(len(streamed), 1)
            self.assertEqual(streamed[0].text, "worker-hello")
            await session.close()
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    async def test_run_id_mismatch_tears_down_group(self) -> None:
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        worker.setblocking(False)
        process = _FakeProcess()

        async def bad_worker(sock: socket.socket) -> None:
            reader, writer = await asyncio.open_connection(sock=sock)
            await send_frame(writer, make_frame("hello", instance="x", worker_pid=1))
            open_frame = await recv_frame(reader)
            await send_frame(
                writer,
                make_frame("ready", request_id=open_frame["request_id"], instance="x"),
            )
            await recv_frame(reader)
            await send_frame(
                writer,
                make_frame(
                    "result",
                    run_id="not-the-run",
                    sequence=1,
                    outcome=TurnOutcome("completed").to_dict(),
                ),
            )
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()

        worker_task = asyncio.create_task(bad_worker(worker))
        try:
            reader, writer = await asyncio.open_connection(sock=daemon)
            instance = await handshake_worker(reader, writer)
            session = SupervisedWorkerSession(process, reader, writer, instance=instance)
            await session.open({"backend": "fake"})
            with self.assertRaises(WorkerProtocolError):
                await session.run("prompt")
            self.assertTrue(session.terminal)
            self.assertTrue(process.terminated or process.killed)
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    async def test_invalid_outcome_is_protocol_error(self) -> None:
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        worker.setblocking(False)
        process = _FakeProcess()

        async def bad_outcome(sock: socket.socket) -> None:
            reader, writer = await asyncio.open_connection(sock=sock)
            await send_frame(writer, make_frame("hello", instance="x", worker_pid=1))
            open_frame = await recv_frame(reader)
            await send_frame(
                writer,
                make_frame("ready", request_id=open_frame["request_id"], instance="x"),
            )
            run_frame = await recv_frame(reader)
            await send_frame(
                writer,
                make_frame(
                    "result",
                    run_id=run_frame["run_id"],
                    sequence=1,
                    outcome={"outcome": "failed"},  # missing required code
                ),
            )
            writer.close()
            await writer.wait_closed()

        worker_task = asyncio.create_task(bad_outcome(worker))
        try:
            reader, writer = await asyncio.open_connection(sock=daemon)
            instance = await handshake_worker(reader, writer)
            session = SupervisedWorkerSession(process, reader, writer, instance=instance)
            await session.open({"backend": "fake"})
            with self.assertRaises(WorkerProtocolError):
                await session.run("prompt")
            self.assertTrue(session.terminal)
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    async def test_force_teardown_is_idempotent(self) -> None:
        process = _FakeProcess()
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        worker.close()
        reader, writer = await asyncio.open_connection(sock=daemon)
        session = SupervisedWorkerSession(process, reader, writer, instance="x")
        await session.force_teardown()
        await session.force_teardown()
        self.assertTrue(session.terminal)
        self.assertTrue(process.terminated or process.killed)

    async def test_cancel_active_kills_without_framed_cancel(self) -> None:
        process = _FakeProcess()
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        worker.close()
        reader, writer = await asyncio.open_connection(sock=daemon)
        session = SupervisedWorkerSession(process, reader, writer, instance="x")
        session._opened = True
        session._active_run = "run-1"
        session._cancel_run_id = "run-1"
        await session.cancel_active()
        self.assertTrue(session.terminal)
        self.assertTrue(process.killed)

    async def test_run_cancelled_kills_process_group(self) -> None:
        process = _FakeProcess()
        daemon, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        daemon.setblocking(False)
        worker.setblocking(False)

        async def hang_worker(sock: socket.socket) -> None:
            reader, writer = await asyncio.open_connection(sock=sock)
            await send_frame(writer, make_frame("hello", instance="x", worker_pid=1))
            open_frame = await recv_frame(reader)
            await send_frame(
                writer,
                make_frame("ready", request_id=open_frame["request_id"], instance="x"),
            )
            await recv_frame(reader)
            await asyncio.sleep(60)

        worker_task = asyncio.create_task(hang_worker(worker))
        try:
            reader, writer = await asyncio.open_connection(sock=daemon)
            instance = await handshake_worker(reader, writer)
            session = SupervisedWorkerSession(process, reader, writer, instance=instance)
            await session.open({"backend": "fake"})
            run_task = asyncio.create_task(session.run("hang"))
            await asyncio.sleep(0.05)
            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task
            self.assertTrue(process.killed)
            self.assertTrue(session.terminal)
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    async def _fake_worker(self, sock: socket.socket) -> None:
        reader, writer = await asyncio.open_connection(sock=sock)
        await send_frame(writer, make_frame("hello", instance="fake-instance", worker_pid=1))
        open_frame = await recv_frame(reader)
        self.assertEqual(open_frame["type"], "open")
        await send_frame(
            writer,
            make_frame("ready", request_id=open_frame["request_id"], instance="fake-instance"),
        )
        run_frame = await recv_frame(reader)
        self.assertEqual(run_frame["type"], "run")
        event = Event.create("codex", "message", "worker-hello", {"text": "worker-hello"})
        event.mark_provider_session(
            agent_id="reviewer",
            session_id="thread-1",
            kind="thread",
        )
        await send_frame(
            writer,
            make_frame(
                "event",
                run_id=run_frame["run_id"],
                sequence=1,
                event={
                    **event.to_dict(),
                    "provider_session": {
                        "agent_id": "reviewer",
                        "provider_session_id": "thread-1",
                        "provider_session_kind": "thread",
                    },
                },
            ),
        )
        await send_frame(
            writer,
            make_frame(
                "result",
                run_id=run_frame["run_id"],
                sequence=2,
                outcome=TurnOutcome("completed").to_dict(),
            ),
        )
        close_frame = await recv_frame(reader)
        self.assertEqual(close_frame["type"], "close")
        await send_frame(writer, make_frame("closed", request_id=close_frame["request_id"]))
        writer.close()
        await writer.wait_closed()


class WorkerCodecHardeningTests(unittest.TestCase):
    def test_sanitize_redacts_secrets_and_home(self) -> None:
        text = sanitize_error_text(
            f"Authorization: Bearer sk-abc123secret path={__import__('pathlib').Path.home()}/.codex/auth.json"
        )
        self.assertNotIn("sk-abc123", text)
        self.assertNotIn("Bearer sk-", text)
        self.assertNotIn(str(__import__("pathlib").Path.home()), text)

    def test_worker_python_keeps_venv_entrypoint(self) -> None:
        path = _worker_python_executable()
        # Must be absolute and must not fully resolve a venv shim to bare /usr/bin
        # when sys.executable lives under a virtualenv bin directory.
        self.assertTrue(path.startswith("/"))
        from pathlib import Path
        import sys

        executable = Path(sys.executable)
        if "venv" in str(executable) or ".agent-collab" in str(executable):
            self.assertNotEqual(path, str(executable.resolve()))


if __name__ == "__main__":
    unittest.main()
