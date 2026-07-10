from __future__ import annotations

from dataclasses import asdict, dataclass
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

    @classmethod
    def create(cls, source: str, event_type: str, text: str, raw: Any = None) -> "Event":
        if source not in VALID_SOURCES:
            source = "error"
        if event_type not in VALID_TYPES:
            event_type = "status"
        return cls(utc_timestamp(), source, event_type, text, raw)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

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
