"""Antigravity CLI plain-text parser."""

from __future__ import annotations

from typing import List, Optional

from ...events import Event
from ...outcomes import TerminalEvidence


_TOOL_FAILURE_PREFIXES = (
    "TOOL_ERROR",
    "Tool execution failed:",
    "Tool action failed:",
    "Failed to execute tool:",
    "Error executing tool:",
)


def parse_antigravity_line(line: str, verbose: bool = False) -> Optional[Event]:
    text = line.strip()
    return Event.create("antigravity", "message", text, {"line": line}) if text else None


class AntigravityParser:
    """Retain the CLI's explicit tool-failure markers as terminal evidence."""

    def __init__(self) -> None:
        self._terminal_evidence: List[TerminalEvidence] = []

    def __call__(self, line: str, verbose: bool = False) -> Optional[Event]:
        del verbose
        text = line.strip()
        if not text:
            return None
        if text.startswith(_TOOL_FAILURE_PREFIXES):
            self._terminal_evidence.append(TerminalEvidence("failed", "provider_terminal_failure"))
            return Event.create(
                "error",
                "error",
                "Antigravity reported a tool/action failure",
                {
                    "code": "provider_terminal_failure",
                    "fatal": True,
                },
            )
        return Event.create("antigravity", "message", text, {"line": line})

    def take_terminal_evidence(self) -> List[TerminalEvidence]:
        evidence = self._terminal_evidence
        self._terminal_evidence = []
        return evidence
