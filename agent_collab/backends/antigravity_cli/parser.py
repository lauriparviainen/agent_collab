"""Antigravity CLI plain-text parser."""

from __future__ import annotations

from typing import Optional

from ...events import Event


def parse_antigravity_line(line: str, verbose: bool = False) -> Optional[Event]:
    text = line.strip()
    return Event.create("antigravity", "message", text, {"line": line}) if text else None
