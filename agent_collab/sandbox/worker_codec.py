"""Length-prefixed UTF-8 JSON frames for the outer-sandbox SDK worker."""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path
from typing import Any, Mapping

from .specs import SandboxFailure

PROTOCOL_NAME = "agent-collab-sdk-worker"
PROTOCOL_VERSION = 1
# Match the provider event transport bound so a single message cannot exceed it.
FRAME_LIMIT = 8 * 1024 * 1024
MAX_EVENTS_PER_RUN = 10_000
MAX_EVENT_BYTES_PER_RUN = 8 * 1024 * 1024
HANDSHAKE_TIMEOUT_SECONDS = 15.0
OPEN_TIMEOUT_SECONDS = 30.0
EMIT_BACKPRESSURE_TIMEOUT_SECONDS = 5.0
WORKER_TERMINATE_GRACE_SECONDS = 1.0
WORKER_KILL_GRACE_SECONDS = 1.0

DAEMON_TO_WORKER = frozenset({"open", "run", "cancel", "reset", "close"})
WORKER_TO_DAEMON = frozenset(
    {
        "hello",
        "ready",
        "event",
        "result",
        "cancelled",
        "reset_result",
        "closed",
        "error",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)\bxai-[A-Za-z0-9]{10,}"),
)


class WorkerProtocolError(SandboxFailure):
    def __init__(self, message: str, *, phase: str = "worker") -> None:
        super().__init__(
            "outer_sandbox_worker_protocol_invalid",
            message,
            phase=phase,
        )


def encode_frame(payload: Mapping[str, Any]) -> bytes:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    encoded = raw.encode("utf-8")
    if not encoded or len(encoded) > FRAME_LIMIT:
        raise WorkerProtocolError("worker frame length is invalid")
    return struct.pack(">I", len(encoded)) + encoded


def decode_frame(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > FRAME_LIMIT:
        raise WorkerProtocolError("worker frame length is invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("worker frame is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker frame must be a JSON object")
    return value


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerProtocolError("worker frame has duplicate JSON fields")
        result[key] = value
    return result


def validate_envelope(
    payload: Mapping[str, Any],
    *,
    expected_direction: str,
) -> dict[str, Any]:
    if payload.get("protocol") != PROTOCOL_NAME:
        raise WorkerProtocolError("worker frame protocol name is invalid")
    if payload.get("version") != PROTOCOL_VERSION:
        raise WorkerProtocolError("worker frame protocol version is invalid")
    frame_type = payload.get("type")
    if not isinstance(frame_type, str):
        raise WorkerProtocolError("worker frame type is invalid")
    allowed = WORKER_TO_DAEMON if expected_direction == "worker" else DAEMON_TO_WORKER
    if frame_type not in allowed:
        raise WorkerProtocolError(f"worker frame type {frame_type!r} is not allowed")
    return dict(payload)


def make_frame(frame_type: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "type": frame_type,
    }
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


async def send_frame(writer: Any, payload: Mapping[str, Any]) -> None:
    data = encode_frame(payload)
    writer.write(data)
    await writer.drain()


async def recv_frame(reader: Any) -> dict[str, Any]:
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > FRAME_LIMIT:
        raise WorkerProtocolError("worker frame length is invalid")
    raw = await reader.readexactly(length)
    return decode_frame(raw)


def recv_frame_sync(channel: int) -> dict[str, Any]:
    header = _recv_exact(channel, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > FRAME_LIMIT:
        raise WorkerProtocolError("worker frame length is invalid")
    return decode_frame(_recv_exact(channel, length))


def send_frame_sync(channel: int, payload: Mapping[str, Any]) -> None:
    data = encode_frame(payload)
    view = memoryview(data)
    while view:
        written = os.write(channel, view)
        view = view[written:]


def _recv_exact(channel: int, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        value = os.read(channel, length - len(chunks))
        if not value:
            raise WorkerProtocolError("worker control connection closed")
        chunks.extend(value)
    return bytes(chunks)


def sanitize_error_text(text: str, *, limit: int = 240) -> str:
    cleaned = " ".join(str(text).split())
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("<redacted>", cleaned)
    home = str(Path.home())
    if home and home in cleaned:
        cleaned = cleaned.replace(home, "~")
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME", "HOME", "AGENT_COLLAB_HOME"):
        value = os.environ.get(key)
        if value and value in cleaned:
            cleaned = cleaned.replace(value, f"${key}")
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def outcome_to_payload(outcome: Any) -> dict[str, Any]:
    if hasattr(outcome, "to_dict"):
        payload = dict(outcome.to_dict())
    elif isinstance(outcome, Mapping):
        payload = dict(outcome)
    else:
        raise WorkerProtocolError("worker result outcome is not serializable")
    kind = payload.get("outcome")
    if kind not in {
        "completed",
        "cancelled",
        "interrupted",
        "timed_out",
        "refused",
        "failed",
    }:
        raise WorkerProtocolError("worker result outcome kind is invalid")
    return payload


def event_to_payload(event: Any) -> dict[str, Any]:
    if not hasattr(event, "to_dict"):
        raise WorkerProtocolError("worker event is not serializable")
    payload = event.to_dict()
    allowed = {
        "timestamp",
        "source",
        "type",
        "text",
        "raw",
        "agent_id",
    }
    result = {key: value for key, value in payload.items() if key in allowed}
    # Trusted in-process marker is not in to_dict(); re-attach explicitly.
    session = getattr(event, "provider_session", None)
    if isinstance(session, Mapping):
        result["provider_session"] = {
            "agent_id": session.get("agent_id"),
            "provider_session_id": session.get("provider_session_id"),
            "provider_session_kind": session.get("provider_session_kind"),
        }
    # Never ship raw exception strings that may include credentials.
    raw = result.get("raw")
    if isinstance(raw, Mapping) and "error" in raw:
        scrubbed = dict(raw)
        scrubbed["error"] = sanitize_error_text(str(scrubbed.get("error") or ""))
        result["raw"] = scrubbed
        result["text"] = sanitize_error_text(str(result.get("text") or ""))
    return result


def event_from_payload(payload: Mapping[str, Any]) -> Any:
    from ..events import Event, VALID_SOURCES, VALID_TYPES

    source = payload.get("source")
    event_type = payload.get("type")
    text = payload.get("text")
    if source not in VALID_SOURCES or event_type not in VALID_TYPES:
        raise WorkerProtocolError("worker event source or type is not allow-listed")
    if not isinstance(text, str):
        raise WorkerProtocolError("worker event text is invalid")
    raw = payload.get("raw")
    agent_id = payload.get("agent_id")
    if agent_id is not None and not isinstance(agent_id, str):
        raise WorkerProtocolError("worker event agent_id is invalid")
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        raise WorkerProtocolError("worker event timestamp is invalid")
    event = Event(timestamp, source, event_type, text, raw, agent_id)
    session = payload.get("provider_session")
    if isinstance(session, Mapping):
        sid = session.get("provider_session_id")
        kind = session.get("provider_session_kind")
        session_agent = session.get("agent_id")
        if (
            isinstance(sid, str)
            and sid
            and isinstance(kind, str)
            and kind
            and isinstance(session_agent, str)
            and session_agent
        ):
            event.mark_provider_session(
                agent_id=session_agent,
                session_id=sid,
                kind=kind,
            )
    return event


def parse_outcome_payload(payload: Mapping[str, Any]) -> Any:
    from ..outcomes import TurnOutcome

    try:
        return TurnOutcome(
            outcome=payload["outcome"],
            code=payload.get("code"),
            message=payload.get("message"),
            provider_stop_reason=payload.get("provider_stop_reason"),
            process_exit_code=payload.get("process_exit_code"),
            retry_after_seconds=payload.get("retry_after_seconds"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkerProtocolError("worker result outcome is invalid") from exc
