"""Shared JSONL parser heuristics for subprocess backends."""

from __future__ import annotations

from typing import Any, Dict, Iterable


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


def walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key in TEXT_KEYS:
            if key in value:
                yield from walk_strings(value[key])
        for key, nested in value.items():
            if key not in TEXT_KEYS and key not in SKIP_TEXT_KEYS:
                yield from walk_strings(nested)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)


def first_text(raw: Any) -> str:
    return next((text.strip() for text in walk_strings(raw) if text.strip()), "")


def classifier_tokens(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"type", "subtype", "name", "event"} and isinstance(nested, str):
                yield nested
            elif isinstance(nested, (dict, list)):
                yield from classifier_tokens(nested)
    elif isinstance(value, list):
        for item in value:
            yield from classifier_tokens(item)


def classifier_haystack(raw: Dict[str, Any]) -> str:
    return " ".join(classifier_tokens(raw)).lower()


def looks_like_command(raw: Dict[str, Any]) -> bool:
    return any(token in classifier_haystack(raw) for token in ("command", "exec"))


def looks_like_tool(raw: Dict[str, Any]) -> bool:
    return any(token in classifier_haystack(raw) for token in ("tool", "function_call"))


def looks_like_file_change(raw: Dict[str, Any]) -> bool:
    return any(
        token in classifier_haystack(raw) for token in ("patch", "edit", "file_change", "diff")
    )
