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
from typing import Any, Callable, Dict, Mapping, Optional, Protocol

from .worker_codec import (
    event_to_payload,
    make_frame,
    outcome_to_payload,
    recv_frame_sync,
    sanitize_error_text,
    send_frame_sync,
    validate_envelope,
)


class WorkerBackend(Protocol):
    async def open(self, payload: Mapping[str, Any]) -> None: ...

    async def run(self, prompt: str, *, run_id: str) -> tuple[list[Any], Any]: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


BackendFactory = Callable[[], WorkerBackend]


def _registry() -> Dict[str, BackendFactory]:
    # Lazy imports keep unsupported backends out of the worker until requested.
    from ..backends.codex_sdk.worker import CodexSdkWorkerBackend

    return {
        "codex_sdk": CodexSdkWorkerBackend,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-collab-sdk-worker")
    parser.add_argument("--worker-fd", type=int, required=True)
    args = parser.parse_args(argv)
    channel = args.worker_fd
    if channel <= 2:
        print("worker-fd must be greater than 2", file=sys.stderr)
        return 2
    try:
        os.set_inheritable(channel, True)
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
    active_run: Optional[str] = None
    run_task: Optional[asyncio.Task[Any]] = None
    seen_requests: set[str] = set()
    sequence = 0
    closed = False

    while not closed:
        if run_task is not None and active_run is not None:
            # While a provider run is in flight, poll for cancel/close without
            # blocking the event loop on a long SDK turn.
            envelope = await _recv_while_running(loop, channel, run_task)
            if envelope is None:
                # Run finished first; harvest its result below via run_task.
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
                # sequence may already be 1 from the run-started status frame.
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
            # Emit an immediate status so the daemon sees progress while the
            # collected SDK turn is still in flight (Codex returns events only
            # after thread.run settles).
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
                        "source": "codex",
                        "type": "status",
                        "text": "sdk worker run started",
                        "raw": {"phase": "run_started"},
                        "agent_id": None,
                    },
                ),
            )
            sequence = 1
            run_task = asyncio.create_task(backend.run(prompt, run_id=run_id))
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


async def _recv_while_running(
    loop: asyncio.AbstractEventLoop,
    channel: int,
    run_task: asyncio.Task[Any],
) -> Optional[dict[str, Any]]:
    """Return a control frame if one arrives before the run task finishes."""

    while not run_task.done():
        ready = await loop.run_in_executor(
            None,
            select.select,
            [channel],
            [],
            [],
            0.1,
        )
        if ready[0]:
            payload = await loop.run_in_executor(None, recv_frame_sync, channel)
            return validate_envelope(payload, expected_direction="daemon")
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
