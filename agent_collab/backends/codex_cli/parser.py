"""Codex CLI JSONL event parser."""

from __future__ import annotations

from typing import Optional

from ...events import Event, compact_json, parse_json_line
from ..common.parse import first_text, looks_like_command, looks_like_file_change, looks_like_tool


def parse_codex_line(line: str, verbose: bool = False) -> Optional[Event]:
    raw = parse_json_line(line)
    if raw is None:
        text = line.strip()
        return Event.create("codex", "message", text, {"line": line}) if text else None
    if not isinstance(raw, dict):
        return Event.create("codex", "status", compact_json(raw), raw) if verbose else None
    if raw.get("type") in {"error", "fatal_error"} or "error" in raw:
        return Event.create("error", "error", first_text(raw) or compact_json(raw), raw)
    for predicate, event_type in (
        (looks_like_file_change, "file_change"),
        (looks_like_command, "command"),
        (looks_like_tool, "tool_call"),
    ):
        if predicate(raw):
            return Event.create("tool", event_type, first_text(raw) or compact_json(raw), raw)
    text = first_text(raw)
    if text:
        return Event.create("codex", "message", text, raw)
    return Event.create("codex", "status", compact_json(raw), raw) if verbose else None
