"""Antigravity CLI stream-json NDJSON parser."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, Union

from ...events import Event, compact_json
from ...outcomes import TerminalEvidence, TurnOutcomeKind


SUCCESS_STATUSES = frozenset({"SUCCESS"})
CANCELLED_STATUSES = frozenset({"CANCELED", "CANCELLED"})
INCOMPLETE_STATUSES = frozenset({"WAITING", "RUNNING"})
FAILED_STATUSES = frozenset({"ERROR", "INVALID", "INTERRUPTED"})


def _load_record(line: str) -> Optional[Dict[str, Any]]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid Antigravity stream JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("invalid Antigravity stream JSON")
    return raw


def _error_event(text: str, code: str, raw: Dict[str, Any]) -> Event:
    return Event.create(
        "error",
        "error",
        text,
        {**raw, "code": code, "fatal": True},
    )


def _classify_result(
    status: Any,
) -> Tuple[TurnOutcomeKind, Optional[str], Optional[str], str]:
    if not isinstance(status, str) or not status:
        return (
            "failed",
            "provider_terminal_failure",
            None,
            "Antigravity result record is invalid",
        )
    if status in SUCCESS_STATUSES:
        return ("completed", None, status, "")
    if status in CANCELLED_STATUSES:
        return (
            "cancelled",
            "provider_turn_cancelled",
            status,
            "Antigravity cancelled the turn",
        )
    if status in INCOMPLETE_STATUSES:
        return (
            "failed",
            "provider_output_incomplete",
            status,
            "Antigravity turn ended before terminal completion",
        )
    if status in FAILED_STATUSES:
        return (
            "failed",
            "provider_terminal_failure",
            status,
            "Antigravity reported a terminal failure",
        )
    return (
        "failed",
        "provider_terminal_failure",
        status,
        "Antigravity reported an unknown terminal status",
    )


def _map_step_update(raw: Dict[str, Any], verbose: bool) -> Optional[Event]:
    payload = raw.get("step_update")
    if not isinstance(payload, dict):
        return Event.create("antigravity", "status", "step_update", raw) if verbose else None
    text = payload.get("text_delta")
    if isinstance(text, str) and text.strip():
        return Event.create("antigravity", "message", text, raw)
    tool_info = payload.get("tool_info")
    tool_name = payload.get("tool_name")
    if payload.get("step_type") == "tool" or isinstance(tool_info, dict):
        name = tool_name if isinstance(tool_name, str) and tool_name else None
        if name is None and isinstance(tool_info, dict):
            info_name = tool_info.get("name")
            if isinstance(info_name, str) and info_name:
                name = info_name
        return Event.create("tool", "tool_call", name or "tool", raw)
    if verbose:
        label = payload.get("step_type") or payload.get("state") or "step_update"
        return Event.create("antigravity", "status", str(label), raw)
    return None


def _map_result(raw: Dict[str, Any], verbose: bool) -> Optional[Event]:
    payload = raw.get("result")
    if not isinstance(payload, dict):
        return _error_event(
            "Antigravity result record is invalid", "provider_terminal_failure", raw
        )
    outcome, code, _reason, text = _classify_result(payload.get("status"))
    if outcome != "completed":
        return _error_event(text, code or "provider_terminal_failure", raw)
    response = payload.get("response")
    if isinstance(response, str) and response.strip():
        return Event.create("antigravity", "message", response, raw)
    return Event.create("antigravity", "status", "result", raw) if verbose else None


def parse_antigravity_line(line: str, verbose: bool = False) -> Optional[Event]:
    """Map one stream-json record without retaining turn-level evidence.

    The production runner uses :class:`AntigravityStreamingParser`. This
    stateless helper remains the fixture-level event mapper. Malformed NDJSON
    raises; it never treats plain text as a successful message.
    """

    raw = _load_record(line)
    if raw is None:
        return None
    event_type = raw.get("event")
    if event_type == "init":
        payload = raw.get("init")
        if not isinstance(payload, dict):
            return _error_event(
                "Antigravity init record is invalid", "provider_terminal_failure", raw
            )
        return Event.create("antigravity", "status", "init", raw) if verbose else None
    if event_type == "step_update":
        return _map_step_update(raw, verbose)
    if event_type == "result":
        return _map_result(raw, verbose)
    return Event.create("antigravity", "status", compact_json(raw), raw) if verbose else None


class AntigravityStreamingParser:
    """Stateful stream-json parser that requires a typed terminal ``result``."""

    def __init__(self) -> None:
        self._text_parts: List[str] = []
        self._terminal_evidence: List[TerminalEvidence] = []

    def __call__(self, line: str, verbose: bool = False) -> Optional[Union[Event, List[Event]]]:
        raw = _load_record(line)
        if raw is None:
            return None
        event_type = raw.get("event")
        if event_type == "init" and not isinstance(raw.get("init"), dict):
            self._terminal_evidence.append(TerminalEvidence("failed", "provider_terminal_failure"))
            return parse_antigravity_line(line, verbose)
        if event_type == "step_update":
            payload = raw.get("step_update")
            if isinstance(payload, dict) and isinstance(payload.get("text_delta"), str):
                if payload["text_delta"]:
                    self._text_parts.append(payload["text_delta"])
                if payload.get("step_type") == "tool" or isinstance(payload.get("tool_info"), dict):
                    return _map_step_update(raw, verbose)
                if verbose:
                    label = payload.get("step_type") or payload.get("state") or "step_update"
                    return Event.create("antigravity", "status", str(label), raw)
                return None
        if event_type == "result":
            payload = raw.get("result")
            if not isinstance(payload, dict):
                self._terminal_evidence.append(
                    TerminalEvidence("failed", "provider_terminal_failure")
                )
                events = self._flush_text()
                mapped = parse_antigravity_line(line, verbose)
                if mapped is not None:
                    events.append(mapped)
                return events or None
            outcome, code, reason, _text = _classify_result(payload.get("status"))
            self._terminal_evidence.append(
                TerminalEvidence(outcome, code, provider_stop_reason=reason)
            )
            events = self._flush_text()
            if outcome == "completed" and not events:
                response = payload.get("response")
                if isinstance(response, str) and response.strip():
                    events.append(Event.create("antigravity", "message", response, raw))
            mapped = parse_antigravity_line(line, verbose)
            if outcome != "completed" and mapped is not None:
                events.append(mapped)
            elif (
                outcome == "completed"
                and mapped is not None
                and mapped.type != "message"
                and verbose
            ):
                events.append(mapped)
            return events or None
        return parse_antigravity_line(line, verbose)

    def reset(self) -> None:
        """Discard leftover turn state without emitting."""

        self._text_parts = []
        self._terminal_evidence = []

    def finish(self) -> Optional[Union[Event, List[Event]]]:
        events = self._flush_text()
        return events or None

    def take_terminal_evidence(self) -> List[TerminalEvidence]:
        evidence = self._terminal_evidence
        self._terminal_evidence = []
        return evidence

    def _flush_text(self) -> List[Event]:
        if not self._text_parts:
            return []
        parts = self._text_parts
        self._text_parts = []
        text = "".join(parts)
        if not text.strip():
            return []
        return [
            Event.create(
                "antigravity",
                "message",
                text,
                {"event": "step_update", "delta_count": len(parts)},
            )
        ]
