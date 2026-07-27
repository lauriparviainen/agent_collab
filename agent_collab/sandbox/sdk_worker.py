"""Generic SDK worker entrypoint launched inside the Bubblewrap namespace.

The daemon never imports provider SDKs for a worker-backed backend. This process
owns SDK clients, callbacks, and provider runtime children after establishment.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
import secrets
import select
import sys
import traceback
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol

from .worker_codec import (
    FRAME_LIMIT,
    MAX_EVENT_BYTES_PER_RUN,
    MAX_EVENTS_PER_RUN,
    WorkerProtocolError,
    encode_frame,
    event_to_payload,
    make_frame,
    outcome_to_payload,
    recv_frame_sync,
    sanitize_error_text,
    send_frame_sync,
    validate_envelope,
)

EventEmit = Callable[[Any], Awaitable[None]]


class WorkerBackend(Protocol):
    async def open(self, payload: Mapping[str, Any]) -> None: ...

    async def run(
        self,
        prompt: str,
        *,
        run_id: str,
        emit: Optional[EventEmit] = None,
    ) -> tuple[list[Any], Any]: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


BackendFactory = Callable[[], WorkerBackend]


def _registry() -> Dict[str, BackendFactory]:
    # Lazy imports keep unsupported backends out of the worker until requested.
    from ..backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend
    from ..backends.claude_sdk.worker import ClaudeSdkWorkerBackend
    from ..backends.codex_sdk.worker import CodexSdkWorkerBackend

    return {
        "antigravity_sdk": AntigravitySdkWorkerBackend,
        "claude_sdk": ClaudeSdkWorkerBackend,
        "codex_sdk": CodexSdkWorkerBackend,
    }


def event_source_for_backend(backend_id: str) -> str:
    """Map a registered worker backend id to the Event.source provider name."""

    if backend_id.endswith("_sdk") or backend_id.endswith("_cli"):
        return backend_id.rsplit("_", 1)[0]
    return backend_id


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-collab-sdk-worker")
    parser.add_argument("--worker-fd", type=int, required=True)
    args = parser.parse_args(argv)
    channel = args.worker_fd
    if channel <= 2:
        print("worker-fd must be greater than 2", file=sys.stderr)
        return 2
    # The control socket was inherited into this process only. Clear the
    # inheritable flag so allow-all provider shells / localharness children
    # cannot retain the daemon full-duplex channel.
    try:
        os.set_inheritable(channel, False)
    except OSError:
        pass
    try:
        return asyncio.run(_serve(channel))
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException:
        try:
            traceback.print_exc(file=sys.stderr)
        except BaseException:
            pass
        return 1


async def _serve(channel: int) -> int:
    loop = asyncio.get_running_loop()
    instance = secrets.token_hex(16)
    await loop.run_in_executor(
        None,
        send_frame_sync,
        channel,
        make_frame("hello", instance=instance, worker_pid=os.getpid()),
    )
    backend: Optional[WorkerBackend] = None
    event_source = "sdk"
    active_run: Optional[str] = None
    run_task: Optional[asyncio.Task[Any]] = None
    # Bound mid-turn streaming so a chatty provider cannot exhaust memory while
    # the daemon sink is slow; full put() blocks and applies backpressure.
    event_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=MAX_EVENTS_PER_RUN)
    queued_event_bytes = 0
    run_event_bytes = 0
    seen_requests: set[str] = set()
    sequence = 0
    closed = False

    while not closed:
        if run_task is not None and active_run is not None:
            # Stream any mid-turn events before polling cancel/close so Claude
            # assistant/tool output is not lost if the turn is later killed.
            sequence, queued_event_bytes = await _drain_event_queue(
                loop,
                channel,
                event_queue,
                run_id=active_run,
                sequence=sequence,
                queued_event_bytes=queued_event_bytes,
            )
            envelope = await _recv_while_running(loop, channel, run_task, event_queue)
            if envelope is None:
                # Either an event is ready or the run finished; drain again.
                if not event_queue.empty():
                    continue
                # Run finished first; harvest residual events plus the result.
                try:
                    events, outcome = await run_task
                except asyncio.CancelledError:
                    await _send_error(loop, channel, "worker run cancelled", run_id=active_run)
                    return 1
                except Exception as exc:
                    await _send_error(
                        loop,
                        channel,
                        sanitize_error_text(f"worker run failed: {exc}"),
                        run_id=active_run,
                    )
                    return 1
                finally:
                    run_task = None
                    finished_run = active_run
                    active_run = None
                sequence, queued_event_bytes = await _drain_event_queue(
                    loop,
                    channel,
                    event_queue,
                    run_id=finished_run,
                    sequence=sequence,
                    queued_event_bytes=queued_event_bytes,
                )
                # Residual return-list events (Codex collects after thread.run).
                for event in events:
                    sequence += 1
                    await loop.run_in_executor(
                        None,
                        send_frame_sync,
                        channel,
                        make_frame(
                            "event",
                            run_id=finished_run,
                            sequence=sequence,
                            event=event_to_payload(event),
                        ),
                    )
                await loop.run_in_executor(
                    None,
                    send_frame_sync,
                    channel,
                    make_frame(
                        "result",
                        run_id=finished_run,
                        sequence=sequence + 1,
                        outcome=outcome_to_payload(outcome),
                    ),
                )
                sequence = 0
                queued_event_bytes = 0
                run_event_bytes = 0
                continue
        else:
            payload = await loop.run_in_executor(None, recv_frame_sync, channel)
            envelope = validate_envelope(payload, expected_direction="daemon")

        frame_type = envelope["type"]
        request_id = envelope.get("request_id")
        if isinstance(request_id, str):
            if request_id in seen_requests:
                await _send_error(loop, channel, "duplicate request_id")
                return 1
            seen_requests.add(request_id)

        if frame_type == "open":
            if backend is not None:
                await _send_error(loop, channel, "worker already opened")
                return 1
            open_payload = envelope.get("payload")
            if not isinstance(open_payload, dict):
                await _send_error(loop, channel, "open payload must be an object")
                return 1
            backend_id = open_payload.get("backend")
            factories = _registry()
            if backend_id not in factories:
                await _send_error(loop, channel, f"unsupported worker backend {backend_id!r}")
                return 1
            try:
                backend = factories[backend_id]()
                await backend.open(open_payload)
            except Exception as exc:
                await _send_error(
                    loop,
                    channel,
                    sanitize_error_text(f"worker open failed: {exc}"),
                )
                return 1
            if isinstance(backend_id, str) and backend_id:
                event_source = event_source_for_backend(backend_id)
            await loop.run_in_executor(
                None,
                send_frame_sync,
                channel,
                make_frame("ready", request_id=request_id, instance=instance),
            )
            continue

        if backend is None:
            await _send_error(loop, channel, "worker is not open")
            return 1

        if frame_type == "run":
            run_id = envelope.get("run_id")
            prompt = envelope.get("prompt")
            if not isinstance(run_id, str) or not run_id:
                await _send_error(loop, channel, "run_id is required")
                return 1
            if not isinstance(prompt, str):
                await _send_error(loop, channel, "run prompt is required")
                return 1
            if active_run is not None:
                await _send_error(loop, channel, "a run is already active")
                return 1
            active_run = run_id
            # Immediate status so the daemon sees progress while an SDK turn
            # that only settles at the end (Codex) is still in flight.
            await loop.run_in_executor(
                None,
                send_frame_sync,
                channel,
                make_frame(
                    "event",
                    run_id=run_id,
                    sequence=1,
                    event={
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": event_source,
                        "type": "status",
                        "text": "sdk worker run started",
                        "raw": {"phase": "run_started"},
                        "agent_id": None,
                    },
                ),
            )
            sequence = 1
            queued_event_bytes = 0
            run_event_bytes = 0

            async def _emit(event: Any) -> None:
                nonlocal queued_event_bytes, run_event_bytes
                payload = event_to_payload(event)
                # Measure the actual UTF-8 frame that will cross the socket, not
                # Python repr length (which undercounts multibyte text).
                try:
                    frame_bytes = encode_frame(
                        make_frame(
                            "event",
                            run_id=run_id,
                            sequence=1,
                            event=payload,
                        )
                    )
                except WorkerProtocolError as exc:
                    raise RuntimeError(
                        "worker event exceeds the transport frame size limit"
                    ) from exc
                encoded_size = len(frame_bytes)
                if encoded_size > FRAME_LIMIT:
                    raise RuntimeError("worker event exceeds the transport frame size limit")
                # Cumulative per-run wire budget (does not reset when drained).
                if run_event_bytes + encoded_size > MAX_EVENT_BYTES_PER_RUN:
                    raise RuntimeError("worker run exceeded the per-run byte budget")
                # In-flight queue occupancy: wait if memory would grow too large.
                while queued_event_bytes + encoded_size > MAX_EVENT_BYTES_PER_RUN:
                    await asyncio.sleep(0.01)
                await event_queue.put((payload, encoded_size))
                queued_event_bytes += encoded_size
                run_event_bytes += encoded_size

            run_task = asyncio.create_task(
                backend.run(prompt, run_id=run_id, emit=_emit),
            )
            continue

        if frame_type == "cancel":
            if run_task is not None and not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            await loop.run_in_executor(
                None,
                send_frame_sync,
                channel,
                make_frame(
                    "cancelled",
                    run_id=envelope.get("run_id") or active_run,
                    request_id=request_id,
                ),
            )
            active_run = None
            run_task = None
            # Exit so the process tree is not left mid-turn; daemon reaps.
            return 0

        if frame_type == "reset":
            if active_run is not None:
                await _send_error(loop, channel, "cannot reset while a run is active")
                return 1
            try:
                await backend.reset()
                ok = True
                message = None
            except Exception as exc:
                ok = False
                message = sanitize_error_text(str(exc))
            await loop.run_in_executor(
                None,
                send_frame_sync,
                channel,
                make_frame(
                    "reset_result",
                    request_id=request_id,
                    ok=ok,
                    message=message,
                ),
            )
            continue

        if frame_type == "close":
            if run_task is not None and not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            try:
                await backend.close()
            except Exception:
                pass
            await loop.run_in_executor(
                None,
                send_frame_sync,
                channel,
                make_frame("closed", request_id=request_id),
            )
            closed = True
            continue

        await _send_error(loop, channel, f"unsupported frame type {frame_type!r}")
        return 1

    return 0


async def _drain_event_queue(
    loop: asyncio.AbstractEventLoop,
    channel: int,
    event_queue: asyncio.Queue[Any],
    *,
    run_id: str,
    sequence: int,
    queued_event_bytes: int,
) -> tuple[int, int]:
    while True:
        try:
            item = event_queue.get_nowait()
        except asyncio.QueueEmpty:
            return sequence, queued_event_bytes
        if isinstance(item, tuple) and len(item) == 2:
            payload, encoded_size = item
            queued_event_bytes = max(0, queued_event_bytes - int(encoded_size))
        else:
            # Back-compat for residual object puts (should not occur in production).
            payload = event_to_payload(item)
            encoded_size = 0
        sequence += 1
        await loop.run_in_executor(
            None,
            send_frame_sync,
            channel,
            make_frame(
                "event",
                run_id=run_id,
                sequence=sequence,
                event=payload,
            ),
        )


async def _recv_while_running(
    loop: asyncio.AbstractEventLoop,
    channel: int,
    run_task: asyncio.Task[Any],
    event_queue: asyncio.Queue[Any],
) -> Optional[dict[str, Any]]:
    """Return a control frame if one arrives before the run task finishes.

    Also returns ``None`` early when the event queue has mid-turn events so
    the serve loop can stream them without waiting out the full select timeout.
    """

    while not run_task.done():
        if not event_queue.empty():
            return None
        ready = await loop.run_in_executor(
            None,
            select.select,
            [channel],
            [],
            [],
            0.05,
        )
        if ready[0]:
            payload = await loop.run_in_executor(None, recv_frame_sync, channel)
            return validate_envelope(payload, expected_direction="daemon")
        if not event_queue.empty():
            return None
    return None


async def _send_error(
    loop: asyncio.AbstractEventLoop,
    channel: int,
    message: str,
    *,
    run_id: Optional[str] = None,
) -> None:
    await loop.run_in_executor(
        None,
        send_frame_sync,
        channel,
        make_frame("error", message=sanitize_error_text(message), run_id=run_id),
    )


if __name__ == "__main__":
    raise SystemExit(main())
