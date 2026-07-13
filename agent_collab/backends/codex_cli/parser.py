"""Codex CLI JSONL event parser."""

from __future__ import annotations

import json
from typing import List, Optional

from ...events import Event, compact_json, parse_json_line
from ...outcomes import TerminalEvidence
from ..common.parse import first_text, looks_like_command, looks_like_file_change, looks_like_tool
from ..common.sdk import provider_session_event


def parse_codex_line(
    line: str,
    verbose: bool = False,
    *,
    agent_id: str = "codex",
) -> Optional[Event]:
    raw = parse_json_line(line)
    if raw is None:
        text = line.strip()
        return Event.create("codex", "message", text, {"line": line}) if text else None
    if not isinstance(raw, dict):
        return Event.create("codex", "status", compact_json(raw), raw) if verbose else None
    thread_id = raw.get("thread_id")
    if raw.get("type") == "thread.started" and isinstance(thread_id, str) and thread_id:
        return provider_session_event("codex", agent_id, thread_id, "thread", raw=raw)
    if raw.get("type") in {"error", "fatal_error"} or "error" in raw:
        return Event.create(
            "error", "error", first_text(raw) or compact_json(raw), {**raw, "fatal": True}
        )
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


class CodexStreamingParser:
    """Map Codex JSONL while privately retaining turn terminal evidence."""

    def __init__(self, agent_id: str = "codex") -> None:
        self.agent_id = agent_id
        self._terminal_evidence: List[TerminalEvidence] = []

    def __call__(self, line: str, verbose: bool = False) -> Optional[Event]:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid Codex stream JSON") from exc
        if isinstance(raw, dict):
            record_type = raw.get("type")
            if record_type == "turn.completed":
                self._terminal_evidence.append(TerminalEvidence("completed"))
            elif record_type == "turn.failed":
                self._terminal_evidence.append(
                    TerminalEvidence("failed", "provider_terminal_failure")
                )
            elif record_type in {"error", "fatal_error"}:
                self._terminal_evidence.append(
                    TerminalEvidence("failed", "provider_terminal_failure")
                )
        return parse_codex_line(line, verbose, agent_id=self.agent_id)

    def take_terminal_evidence(self) -> List[TerminalEvidence]:
        evidence = self._terminal_evidence
        self._terminal_evidence = []
        return evidence
