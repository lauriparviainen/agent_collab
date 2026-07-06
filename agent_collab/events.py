from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterable, Optional


VALID_SOURCES = {"human", "referee", "claude", "codex", "tool", "error"}
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
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def parse_json_line(line: str) -> Optional[Any]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


TEXT_KEYS = ("text", "content", "message", "summary", "output")
SKIP_TEXT_KEYS = {"id", "role", "session_id", "status", "subtype", "thread_id", "type", "uuid"}


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key in TEXT_KEYS:
            if key in value:
                yield from _walk_strings(value[key])
        for key, nested in value.items():
            if key in TEXT_KEYS or key in SKIP_TEXT_KEYS:
                continue
            yield from _walk_strings(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _first_text(raw: Any) -> str:
    for text in _walk_strings(raw):
        cleaned = text.strip()
        if cleaned:
            return cleaned
    return ""


def _classifier_tokens(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"type", "subtype", "name", "event"} and isinstance(nested, str):
                yield nested
            elif isinstance(nested, (dict, list)):
                yield from _classifier_tokens(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _classifier_tokens(item)


def _classifier_haystack(raw: Dict[str, Any]) -> str:
    return " ".join(_classifier_tokens(raw)).lower()


def _looks_like_command(raw: Dict[str, Any]) -> bool:
    haystack = _classifier_haystack(raw)
    return any(token in haystack for token in ("command", "exec"))


def _looks_like_tool(raw: Dict[str, Any]) -> bool:
    haystack = _classifier_haystack(raw)
    return any(token in haystack for token in ("tool", "function_call"))


def _looks_like_file_change(raw: Dict[str, Any]) -> bool:
    haystack = _classifier_haystack(raw)
    return any(token in haystack.lower() for token in ("patch", "edit", "file_change", "diff"))


def parse_claude_line(line: str, verbose: bool = False) -> Optional[Event]:
    raw = parse_json_line(line)
    if raw is None:
        text = line.strip()
        return Event.create("claude", "message", text, {"line": line}) if text else None
    if not isinstance(raw, dict):
        return Event.create("claude", "status", compact_json(raw), raw) if verbose else None

    if raw.get("type") in {"error", "fatal_error"} or "error" in raw:
        return Event.create("error", "error", _first_text(raw) or compact_json(raw), raw)
    if raw.get("type") in {"system", "rate_limit_event", "result"}:
        text = str(raw.get("subtype") or raw.get("status") or raw.get("type"))
        return Event.create("claude", "status", text, raw) if verbose else None
    if _looks_like_file_change(raw):
        return Event.create("tool", "file_change", _first_text(raw) or compact_json(raw), raw)
    if _looks_like_command(raw):
        return Event.create("tool", "command", _first_text(raw) or compact_json(raw), raw)
    if _looks_like_tool(raw):
        return Event.create("tool", "tool_call", _first_text(raw) or compact_json(raw), raw)

    text = _first_text(raw)
    if text:
        return Event.create("claude", "message", text, raw)
    return Event.create("claude", "status", compact_json(raw), raw) if verbose else None


def parse_codex_line(line: str, verbose: bool = False) -> Optional[Event]:
    raw = parse_json_line(line)
    if raw is None:
        text = line.strip()
        return Event.create("codex", "message", text, {"line": line}) if text else None
    if not isinstance(raw, dict):
        return Event.create("codex", "status", compact_json(raw), raw) if verbose else None

    if raw.get("type") in {"error", "fatal_error"} or "error" in raw:
        return Event.create("error", "error", _first_text(raw) or compact_json(raw), raw)
    if _looks_like_file_change(raw):
        return Event.create("tool", "file_change", _first_text(raw) or compact_json(raw), raw)
    if _looks_like_command(raw):
        return Event.create("tool", "command", _first_text(raw) or compact_json(raw), raw)
    if _looks_like_tool(raw):
        return Event.create("tool", "tool_call", _first_text(raw) or compact_json(raw), raw)

    text = _first_text(raw)
    if text:
        return Event.create("codex", "message", text, raw)
    return Event.create("codex", "status", compact_json(raw), raw) if verbose else None
