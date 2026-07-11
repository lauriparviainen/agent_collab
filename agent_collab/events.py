from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional


VALID_SOURCES = {
    "human",
    "referee",
    "claude",
    "codex",
    "antigravity",
    "xai",
    "tool",
    "error",
}
VALID_TYPES = {"message", "tool_call", "command", "file_change", "status", "error"}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    timestamp: str
    source: str
    type: str
    text: str
    raw: Any
    # Trusted in-process bookkeeping. This is deliberately not serialized: raw
    # provider output may contain identically named keys, but only backend code
    # can mark an Event as carrying a provider session identity.
    _provider_session: Optional[Dict[str, str]] = field(default=None, repr=False, compare=False)

    @classmethod
    def create(cls, source: str, event_type: str, text: str, raw: Any = None) -> "Event":
        if source not in VALID_SOURCES:
            source = "error"
        if event_type not in VALID_TYPES:
            event_type = "status"
        return cls(utc_timestamp(), source, event_type, text, raw)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result.pop("_provider_session", None)
        return result

    def mark_provider_session(self, *, agent_id: str, session_id: str, kind: str) -> "Event":
        """Attach trusted, non-wire provider-session metadata to this event."""

        self._provider_session = {
            "agent_id": agent_id,
            "provider_session_id": session_id,
            "provider_session_kind": kind,
        }
        return self

    @property
    def provider_session(self) -> Optional[Dict[str, str]]:
        """Return trusted provider-session metadata, if backend code marked it."""

        return dict(self._provider_session) if self._provider_session is not None else None

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def compact_json(value: Any, limit: int = 220) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return text[: limit - 1] + "..." if len(text) > limit else text


def parse_json_line(line: str) -> Optional[Any]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
