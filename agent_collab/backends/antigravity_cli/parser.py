"""Antigravity CLI plain-text parser."""

from __future__ import annotations

from typing import List, Optional

from ...events import Event
from ...outcomes import TerminalEvidence


_TOOL_FAILURE_PREFIXES = ("TOOL_ERROR:",)


def _failure_event() -> Event:
    return Event.create(
        "error",
        "error",
        "Antigravity reported a tool/action failure",
        {
            "code": "provider_terminal_failure",
            "fatal": True,
        },
    )


def _substantive_text(line: str) -> Optional[str]:
    """Return message text only when stdout contains actual answer content.

    ``agy -p`` is message-only and exposes no structured success marker, so a
    clean exit plus output remains provisional success. Structural fragments
    such as a lone ``}`` must not satisfy that contract.
    """

    text = line.strip()
    if not text or not any(character.isalnum() for character in text):
        return None
    return text


def parse_antigravity_line(line: str, verbose: bool = False) -> Optional[Event]:
    """Map one plain-text record without retaining turn-level evidence.

    The production runner uses :class:`AntigravityParser`; this stateless
    helper remains the fixture-level event mapper.
    """

    del verbose
    text = line.strip()
    if not text:
        return None
    if text.startswith(_TOOL_FAILURE_PREFIXES):
        return _failure_event()
    substantive = _substantive_text(line)
    if substantive is None:
        return None
    return Event.create("antigravity", "message", substantive, {"line": line})


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
            return _failure_event()
        substantive = _substantive_text(line)
        if substantive is None:
            return None
        return Event.create("antigravity", "message", substantive, {"line": line})

    def take_terminal_evidence(self) -> List[TerminalEvidence]:
        evidence = self._terminal_evidence
        self._terminal_evidence = []
        return evidence
