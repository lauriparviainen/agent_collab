from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterable, Optional


VALID_SOURCES = {"human", "referee", "claude", "codex", "antigravity", "tool", "error"}
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
SKIP_TEXT_KEYS = {
    "id",
    "model",
    "parent_tool_use_id",
    "request_id",
    "role",
    "service_tier",
    "session_id",
    "signature",
    "status",
    "stop_reason",
    "stop_sequence",
    "subtype",
    "thread_id",
    "type",
    "usage",
    "uuid",
}


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


# Claude Code stream-json events wrap an API message under "message". Only
# "text" content blocks carry transcript prose; thinking blocks hold opaque
# provider metadata (verification signatures) that must never be displayed.
_CLAUDE_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}


def _claude_content_blocks(raw: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    message = raw.get("message")
    payload = message if isinstance(message, dict) else raw
    content = payload.get("content")
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _claude_visible_text(blocks: Iterable[Dict[str, Any]]) -> str:
    parts = []
    for block in blocks:
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _claude_thinking_text(blocks: Iterable[Dict[str, Any]]) -> str:
    parts = []
    for block in blocks:
        if block.get("type") in _CLAUDE_THINKING_BLOCK_TYPES:
            text = block.get("thinking")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _classify_claude_tool_block(block: Dict[str, Any]) -> str:
    name = str(block.get("name") or "").lower()
    if any(token in name for token in ("edit", "write", "patch")):
        return "file_change"
    if any(token in name for token in ("bash", "command", "exec", "shell")):
        return "command"
    return "tool_call"


def _claude_tool_text(block: Dict[str, Any]) -> str:
    name = block.get("name")
    if isinstance(name, str) and name:
        input_value = block.get("input")
        if input_value:
            return f"{name} {compact_json(input_value)}"
        return name
    return _first_text(block.get("content")) or compact_json(block)


def _parse_claude_message(raw: Dict[str, Any], verbose: bool) -> Optional[Event]:
    blocks = list(_claude_content_blocks(raw))
    tool_blocks = [
        block for block in blocks if isinstance(block.get("type"), str) and "tool" in block["type"]
    ]
    text = _claude_visible_text(blocks)
    if tool_blocks:
        kinds = {_classify_claude_tool_block(block) for block in tool_blocks}
        if "file_change" in kinds:
            event_type = "file_change"
        elif "command" in kinds:
            event_type = "command"
        else:
            event_type = "tool_call"
        tool_text = text or "\n".join(
            _claude_tool_text(block) for block in tool_blocks
        )
        return Event.create("tool", event_type, tool_text, raw)
    if text:
        return Event.create("claude", "message", text, raw)
    if verbose:
        thinking = _claude_thinking_text(blocks)
        if thinking:
            return Event.create("claude", "status", thinking, raw)
        if any(block.get("type") in _CLAUDE_THINKING_BLOCK_TYPES for block in blocks):
            return Event.create("claude", "status", "thinking", raw)
        return Event.create("claude", "status", str(raw.get("type") or "message"), raw)
    return None


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
    if raw.get("type") in {"assistant", "user"} or isinstance(raw.get("message"), dict):
        return _parse_claude_message(raw, verbose)
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


def parse_antigravity_line(line: str, verbose: bool = False) -> Optional[Event]:
    """Parse one line of `agy -p` plain-text output into an event.

    The Antigravity CLI print mode emits free-form plain text / Markdown prose:
    no JSON, no NDJSON, and no stable per-line event marker (confirmed against
    tests/fixtures/antigravity/agy-print-sample.stdout.txt, agy 1.1.0). There is
    therefore no tool/command/file-change structure to recover, so each non-empty
    line becomes an `antigravity` `message` event (message-only, low fidelity).
    """

    text = line.strip()
    if not text:
        return None
    return Event.create("antigravity", "message", text, {"line": line})


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
