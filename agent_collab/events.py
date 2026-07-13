from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, Optional

_logger = logging.getLogger("agent_collab.events")

# One warning per distinct invalid value is enough to expose a backend
# normalization bug without letting a misbehaving backend flood the daemon log.
# The set is cleared at a small cap so memory stays bounded even if invalid
# values are dynamic; recurrence then logs again, which is acceptable noise.
_WARNED_COERCIONS_CAP = 64
_warned_coercions: set = set()


def _warn_coercion(field: str, value: str, replacement: str, source: str, event_type: str) -> None:
    key = (field, value)
    if key in _warned_coercions:
        return
    if len(_warned_coercions) >= _WARNED_COERCIONS_CAP:
        _warned_coercions.clear()
    _warned_coercions.add(key)
    _logger.warning(
        "coercing invalid event %s %r to %r (source=%r, type=%r)",
        field,
        value,
        replacement,
        source,
        event_type,
    )


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
    agent_id: Optional[str] = None
    # Trusted in-process bookkeeping. This is deliberately not serialized: raw
    # provider output may contain identically named keys, but only backend code
    # can mark an Event as carrying a provider session identity.
    _provider_session: Optional[Dict[str, str]] = field(default=None, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        source: str,
        event_type: str,
        text: str,
        raw: Any = None,
        *,
        agent_id: Optional[str] = None,
    ) -> "Event":
        # Coercion keeps malformed backend output from crashing a live session,
        # but it must never be silent: it hides backend normalization bugs.
        # Warnings carry the original (pre-coercion) source and type.
        original_source, original_type = source, event_type
        if source not in VALID_SOURCES:
            _warn_coercion("source", source, "error", original_source, original_type)
            source = "error"
        if event_type not in VALID_TYPES:
            _warn_coercion("type", event_type, "status", original_source, original_type)
            event_type = "status"
        return cls(utc_timestamp(), source, event_type, text, raw, agent_id)

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
