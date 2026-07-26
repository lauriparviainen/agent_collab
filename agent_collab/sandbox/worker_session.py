"""Daemon-side client for a supervised outer-sandbox SDK worker."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Awaitable, Callable, Mapping, Optional, Tuple

from ..outcomes import TurnOutcome
from .specs import SandboxFailure
from .supervisor import SupervisedProcess
from .worker_codec import (
    EMIT_BACKPRESSURE_TIMEOUT_SECONDS,
    HANDSHAKE_TIMEOUT_SECONDS,
    MAX_EVENT_BYTES_PER_RUN,
    MAX_EVENTS_PER_RUN,
    OPEN_TIMEOUT_SECONDS,
    WORKER_KILL_GRACE_SECONDS,
    WORKER_TERMINATE_GRACE_SECONDS,
    WorkerProtocolError,
    encode_frame,
    event_from_payload,
    make_frame,
    parse_outcome_payload,
    recv_frame,
    send_frame,
    validate_envelope,
)

AsyncEventEmit = Callable[[Any], Awaitable[None]]


class SupervisedWorkerSession:
    """Owns one established Bubblewrap worker and its framed control socket."""

    def __init__(
        self,
        process: SupervisedProcess,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        instance: str,
    ) -> None:
        self._process = process
        self._reader = reader
        self._writer = writer
        self.instance = instance
        self._lock = asyncio.Lock()
        self._opened = False
        self._closed = False
        self._terminal = False
        self._request_ids: set[str] = set()
        self._active_run: Optional[str] = None
        self._cancel_run_id: Optional[str] = None
        self._scratch = getattr(process, "_scratch", None)
        # Drain provider/worker stdio so a chatty SDK cannot fill the pipes and
        # deadlock the control socket. These are the bootstrap-transferred fds,
        # not Bubblewrap's diagnostic pipes.
        self._drain_tasks: tuple[asyncio.Task[None], ...] = (
            asyncio.create_task(
                _drain_stream(getattr(process, "stdout", None)),
                name="sdk-worker-stdout-drain",
            ),
            asyncio.create_task(
                _drain_stream(getattr(process, "stderr", None)),
                name="sdk-worker-stderr-drain",
            ),
        )

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    @property
    def terminal(self) -> bool:
        return self._terminal or self._closed

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        return await self._process.wait()

    async def open(self, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            self._ensure_openable()
            request_id = self._request_id()
            await send_frame(
                self._writer,
                make_frame("open", request_id=request_id, payload=dict(payload)),
            )
            try:
                frame = await asyncio.wait_for(
                    self._recv("worker"),
                    OPEN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                await self._force_teardown_locked()
                raise SandboxFailure(
                    "outer_sandbox_bootstrap_failed",
                    "the SDK worker timed out during open",
                    phase="worker",
                ) from exc
            if frame["type"] == "error":
                await self._force_teardown_locked()
                raise SandboxFailure(
                    "outer_sandbox_backend_incompatible",
                    str(frame.get("message") or "worker open failed"),
                    phase="worker",
                )
            if frame["type"] != "ready" or frame.get("request_id") != request_id:
                await self._force_teardown_locked()
                raise WorkerProtocolError("worker open did not produce a matching ready frame")
            self._opened = True

    async def run(
        self,
        prompt: str,
        *,
        emit: Optional[AsyncEventEmit] = None,
    ) -> Tuple[list[Any], TurnOutcome]:
        async with self._lock:
            self._ensure_ready()
            run_id = secrets.token_hex(8)
            self._active_run = run_id
            self._cancel_run_id = run_id
            sequence = 0
            events: list[Any] = []
            event_count = 0
            event_bytes = 0
            try:
                await send_frame(
                    self._writer,
                    make_frame("run", run_id=run_id, prompt=prompt),
                )
                while True:
                    frame = await self._recv("worker")
                    frame_type = frame["type"]
                    if frame_type == "error":
                        raise WorkerProtocolError(str(frame.get("message") or "worker run failed"))
                    if frame.get("run_id") != run_id:
                        raise WorkerProtocolError("worker frame run_id mismatch")
                    seq = frame.get("sequence")
                    if not isinstance(seq, int) or seq <= sequence:
                        raise WorkerProtocolError("worker frame sequence is not increasing")
                    sequence = seq
                    if frame_type == "event":
                        event_payload = frame.get("event")
                        if not isinstance(event_payload, dict):
                            raise WorkerProtocolError("worker event payload is invalid")
                        event_count += 1
                        if event_count > MAX_EVENTS_PER_RUN:
                            raise WorkerProtocolError("worker event count exceeded the run limit")
                        # Cumulative UTF-8 wire size for the framed event, not
                        # Python repr length (which undercounts multibyte text).
                        try:
                            wire = encode_frame(
                                make_frame(
                                    "event",
                                    run_id=run_id,
                                    sequence=seq,
                                    event=event_payload,
                                )
                            )
                        except WorkerProtocolError as exc:
                            raise WorkerProtocolError(
                                "worker event frame exceeds the transport size limit"
                            ) from exc
                        event_bytes += len(wire)
                        if event_bytes > MAX_EVENT_BYTES_PER_RUN:
                            raise WorkerProtocolError("worker event bytes exceeded the run limit")
                        event = event_from_payload(event_payload)
                        # Stream immediately so a slow sink backpressures the
                        # receive loop before the next frame is pulled.
                        if emit is not None:
                            try:
                                await asyncio.wait_for(
                                    emit(event),
                                    EMIT_BACKPRESSURE_TIMEOUT_SECONDS,
                                )
                            except asyncio.TimeoutError as exc:
                                raise SandboxFailure(
                                    "outer_sandbox_worker_backpressure_exceeded",
                                    "the worker event sink exceeded its backpressure deadline",
                                    phase="worker",
                                ) from exc
                        else:
                            events.append(event)
                        continue
                    if frame_type == "result":
                        outcome_payload = frame.get("outcome")
                        if not isinstance(outcome_payload, dict):
                            raise WorkerProtocolError("worker result payload is invalid")
                        return events, parse_outcome_payload(outcome_payload)
                    raise WorkerProtocolError(f"unexpected worker frame {frame_type!r}")
            except asyncio.CancelledError:
                # Sticky cancellation must still deliver SIGKILL before re-raise.
                self._signal_kill_unlocked()
                self._terminal = True
                self._closed = True
                raise
            except WorkerProtocolError:
                await self._force_teardown_locked()
                raise
            except Exception:
                await self._force_teardown_locked()
                raise
            finally:
                self._active_run = None

    async def cancel_active(self) -> None:
        """Hard-stop the worker tree; framed cancel is best-effort only.

        Process-group kill is performed *before* any await so Python 3.11+
        sticky cancellation cannot orphan Bubblewrap descendants. The control
        lock is not required for kill: SIGKILL is safe while ``run`` holds it.
        """

        run_id = self._active_run or getattr(self, "_cancel_run_id", None)
        self._signal_kill_unlocked()
        self._terminal = True
        self._closed = True
        self._active_run = None
        try:
            self._writer.close()
        except Exception:
            pass
        # Best-effort framed cancel is intentionally omitted under sticky
        # cancel: writing can await and re-raise before reap. Hard kill is the
        # ownership contract.
        del run_id
        try:
            await asyncio.shield(asyncio.wait_for(self._process.wait(), WORKER_KILL_GRACE_SECONDS))
        except asyncio.CancelledError:
            asyncio.create_task(self._process.wait())
            raise
        except Exception:
            pass

    async def reset(self) -> None:
        async with self._lock:
            self._ensure_ready()
            request_id = self._request_id()
            await send_frame(self._writer, make_frame("reset", request_id=request_id))
            try:
                frame = await self._recv("worker")
            except WorkerProtocolError:
                await self._force_teardown_locked()
                raise
            if frame["type"] != "reset_result" or frame.get("request_id") != request_id:
                await self._force_teardown_locked()
                raise WorkerProtocolError("worker reset did not produce reset_result")
            if frame.get("ok") is not True:
                await self._force_teardown_locked()
                raise SandboxFailure(
                    "provider_transport_failed",
                    str(frame.get("message") or "worker reset failed"),
                    phase="worker",
                )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._opened and not self._terminal and self._process.returncode is None:
                    request_id = self._request_id()
                    await send_frame(self._writer, make_frame("close", request_id=request_id))
                    try:
                        frame = await asyncio.wait_for(self._recv("worker"), 5.0)
                        if frame.get("type") != "closed":
                            await self._force_teardown_locked()
                            return
                    except Exception:
                        await self._force_teardown_locked()
                        return
            finally:
                await self._close_streams()
                if self._process.returncode is None:
                    await self._reap_process_locked()
                else:
                    await self._process.wait()
                self._terminal = True

    async def force_teardown(self) -> None:
        # Kill without the session lock first so concurrent run() holders cannot
        # block process-group teardown under sticky cancellation.
        self._signal_kill_unlocked()
        self._terminal = True
        self._closed = True
        self._active_run = None
        try:
            self._writer.close()
        except Exception:
            pass
        try:
            await asyncio.shield(asyncio.wait_for(self._process.wait(), WORKER_KILL_GRACE_SECONDS))
        except asyncio.CancelledError:
            asyncio.create_task(self._process.wait())
            raise
        except Exception:
            pass

    def _signal_kill_unlocked(self) -> None:
        """Deliver SIGKILL without awaiting; safe under sticky cancellation."""

        try:
            if self._process.returncode is None:
                self._process.kill()
        except Exception:
            try:
                self._process.terminate()
            except Exception:
                pass

    async def _force_teardown_locked(self) -> None:
        self._terminal = True
        self._closed = True
        self._active_run = None
        self._signal_kill_unlocked()
        try:
            self._writer.close()
        except Exception:
            pass
        try:
            await asyncio.shield(asyncio.wait_for(self._process.wait(), WORKER_KILL_GRACE_SECONDS))
        except asyncio.CancelledError:
            asyncio.create_task(self._process.wait())
            raise
        except Exception:
            pass

    async def _reap_process_locked(self) -> None:
        if self._process.returncode is not None:
            try:
                await self._process.wait()
            except Exception:
                pass
            return
        self.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), WORKER_TERMINATE_GRACE_SECONDS)
            return
        except Exception:
            pass
        if self._process.returncode is None:
            self.kill()
            try:
                await asyncio.wait_for(self._process.wait(), WORKER_KILL_GRACE_SECONDS)
            except Exception:
                pass

    async def _close_streams(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass

    def _ensure_openable(self) -> None:
        if self._closed or self._terminal:
            raise WorkerProtocolError("worker session is closed")
        if self._opened:
            raise WorkerProtocolError("worker session is already open")

    def _ensure_ready(self) -> None:
        if self._closed or self._terminal or not self._opened:
            raise WorkerProtocolError("worker session is not ready")

    def _request_id(self) -> str:
        value = secrets.token_hex(8)
        while value in self._request_ids:
            value = secrets.token_hex(8)
        self._request_ids.add(value)
        return value

    async def _recv(self, direction: str) -> dict[str, Any]:
        try:
            payload = await recv_frame(self._reader)
        except asyncio.IncompleteReadError as exc:
            raise WorkerProtocolError("worker control connection closed") from exc
        return validate_envelope(payload, expected_direction=direction)


async def handshake_worker(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
) -> str:
    try:
        payload = await asyncio.wait_for(recv_frame(reader), timeout)
    except asyncio.TimeoutError as exc:
        raise WorkerProtocolError("worker hello timed out") from exc
    except asyncio.IncompleteReadError as exc:
        raise WorkerProtocolError("worker control connection closed during hello") from exc
    frame = validate_envelope(payload, expected_direction="worker")
    if frame["type"] != "hello":
        raise WorkerProtocolError("worker did not send hello")
    instance = frame.get("instance")
    if not isinstance(instance, str) or not instance:
        raise WorkerProtocolError("worker hello is missing instance")
    return instance


async def _drain_stream(reader: Any) -> None:
    if reader is None:
        return
    try:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                return
    except Exception:
        return
