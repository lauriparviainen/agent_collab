"""Parser for Grok Build ``streaming-json`` output."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from ...events import Event, compact_json, parse_json_line
from ...outcomes import TerminalEvidence, TurnOutcomeKind
from ..common.sdk import provider_session_event


# Current Grok streaming-json / ACP tokens are snake_case. Legacy PascalCase
# values (Grok <=0.2.x fixtures) remain accepted so old captures still resolve.
SUCCESS_STOP_REASONS = frozenset({"end_turn", "EndTurn"})
CANCELLED_STOP_REASONS = frozenset({"cancelled", "Cancelled"})
INCOMPLETE_STOP_REASONS = frozenset({"max_tokens", "max_turn_requests"})
REFUSAL_STOP_REASONS = frozenset({"refusal"})

# Backward-compatible name used by older docs/tests; prefer SUCCESS_STOP_REASONS.
SUCCESS_STOP_REASON = "end_turn"


def _event_text(raw: Dict[str, Any]) -> str:
    for key in ("message", "data", "error"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return compact_json(raw)


def _normalize_stop_reason(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _classify_end_reason(
    stop_reason: Any,
) -> Tuple[TurnOutcomeKind, Optional[str], Optional[str], str]:
    """Map a Grok ``end.stopReason`` to outcome evidence plus error copy.

    Returns ``(outcome, code, provider_stop_reason, error_text)``. Success uses
    ``code=None`` and empty ``error_text``.
    """

    reason = _normalize_stop_reason(stop_reason)
    if reason in SUCCESS_STOP_REASONS:
        return ("completed", None, reason, "")
    if reason in CANCELLED_STOP_REASONS:
        return (
            "cancelled",
            "provider_turn_cancelled",
            reason,
            "Grok ended the turn before producing a response",
        )
    if reason in INCOMPLETE_STOP_REASONS:
        return (
            "failed",
            "provider_output_incomplete",
            reason,
            f"Grok turn ended incompletely with stop reason {reason!r}",
        )
    if reason in REFUSAL_STOP_REASONS:
        return (
            "refused",
            "provider_turn_refused",
            reason,
            f"Grok refused the turn ({reason})",
        )
    label = reason if reason is not None else "unknown"
    return (
        "failed",
        "provider_terminal_failure",
        reason,
        f"Grok turn ended with unsuccessful stop reason {label!r}",
    )


def parse_xai_line(
    line: str,
    verbose: bool = False,
    *,
    agent_id: str = "xai",
) -> Optional[Event]:
    """Map one observed Grok NDJSON record without guessing action shapes."""

    stripped = line.strip()
    if not stripped:
        return None
    raw = parse_json_line(line)
    if raw is None:
        return Event.create("xai", "status", stripped, {"line": line}) if verbose else None
    if not isinstance(raw, dict):
        return Event.create("xai", "status", compact_json(raw), raw) if verbose else None

    event_type = raw.get("type")
    data = raw.get("data")
    if event_type == "text" and isinstance(data, str):
        return Event.create("xai", "message", data, raw)
    if event_type == "thought" and isinstance(data, str):
        return Event.create("xai", "status", data, raw) if verbose else None
    if event_type == "error":
        return Event.create("error", "error", _event_text(raw), raw)
    if event_type == "end":
        outcome, code, reason, text = _classify_end_reason(raw.get("stopReason"))
        if outcome != "completed":
            return Event.create(
                "error",
                "error",
                text,
                {
                    **raw,
                    "code": code,
                    "fatal": True,
                    "provider_stop_reason": reason if reason is not None else "unknown",
                },
            )
        session_id = raw.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return provider_session_event("xai", agent_id, session_id, "session", raw=raw)
        return Event.create("xai", "status", _event_text(raw), raw) if verbose else None
    return Event.create("xai", "status", compact_json(raw), raw) if verbose else None


TEXT_FLUSH_CHARS = 200
THOUGHT_HEARTBEAT = "thinking…"
OPEN_TOOL_STATUSES = frozenset({"pending", "in_progress"})
CLOSE_TOOL_STATUSES = frozenset({"completed", "failed"})
TOOL_KIND_EVENT_TYPES = {
    "execute": "command",
    "edit": "file_change",
    "delete": "file_change",
    "move": "file_change",
}


def _tool_status(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _nonempty_tool_input(value: Any) -> bool:
    return value not in (None, "", {}, [])


def _tool_event_type(kind: Any) -> str:
    if isinstance(kind, str) and kind.strip():
        return TOOL_KIND_EVENT_TYPES.get(kind.strip().lower(), "tool_call")
    return "tool_call"


def _tool_display_name(state: Dict[str, Any]) -> str:
    title = state.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    tool_name = state.get("toolName")
    if isinstance(tool_name, str) and tool_name.strip():
        return tool_name.strip()
    return "tool"


def _tool_has_identity(state: Dict[str, Any]) -> bool:
    title = state.get("title")
    tool_name = state.get("toolName")
    if isinstance(title, str) and title.strip():
        return True
    if isinstance(tool_name, str) and tool_name.strip():
        return True
    return _nonempty_tool_input(state.get("rawInput"))


def _tool_line_text(state: Dict[str, Any], *, closing: bool) -> str:
    name = _tool_display_name(state)
    raw_input = state.get("rawInput")
    text = name
    if _nonempty_tool_input(raw_input):
        text = f"{name} {compact_json(raw_input, limit=120)}"
    if closing:
        status = _tool_status(state.get("status"))
        if status in CLOSE_TOOL_STATUSES:
            text = f"{text} · {status}"
    return text


class XaiStreamingParser:
    """Stream Grok ``streaming-json`` into live messages and dim tool rows.

    ``parse_xai_line`` remains the stateless, fixture-level record mapper. The
    subprocess runner uses this stateful callable so token-sized ``text``
    records do not become thousands of transcript messages, while still
    flushing new-text chunks at ~200 characters or a semantic boundary.
    Documented ``tool_call`` / ``tool_call_update`` records become
    ``source="tool"`` rows. On ``end`` or ``error`` it returns leftover prose
    (when any) plus the normal terminal/session event; ``SubprocessRunner``
    accepts this multi-event result.
    """

    def __init__(self, agent_id: str = "xai") -> None:
        self.agent_id = agent_id
        self._pending_parts: List[str] = []
        self._pending_delta_count = 0
        self._full_text = ""
        self._emitted_messages = 0
        self._in_text = False
        self._thought_seen = False
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._terminal_evidence: List[TerminalEvidence] = []

    def reset(self) -> None:
        """Discard leftover turn state without emitting.

        ``SubprocessRunner`` keeps one parser for the session and calls this
        at the start of every turn. Without it, ``raw.full_text`` concatenates
        prior-turn answers into the next harvest.
        """

        self._pending_parts = []
        self._pending_delta_count = 0
        self._full_text = ""
        self._emitted_messages = 0
        self._in_text = False
        self._thought_seen = False
        self._tools = {}
        self._terminal_evidence = []

    def __call__(
        self,
        line: str,
        verbose: bool = False,
    ) -> Optional[Union[Event, List[Event]]]:
        raw = parse_json_line(line)
        if raw is None and line.strip():
            raise ValueError("invalid xAI streaming JSON")
        if not isinstance(raw, dict):
            return parse_xai_line(line, verbose, agent_id=self.agent_id)

        event_type = raw.get("type")
        if event_type == "text" and isinstance(raw.get("data"), str):
            return self._on_text(raw)
        if event_type == "thought":
            return self._on_thought(raw, verbose)
        if event_type in {"tool_call", "tool_call_update"}:
            return self._on_tool(raw)
        if event_type == "usage":
            return self._on_usage(line, verbose)

        event = parse_xai_line(line, verbose, agent_id=self.agent_id)
        if event_type == "end":
            outcome, code, reason, _text = _classify_end_reason(raw.get("stopReason"))
            self._terminal_evidence.append(
                TerminalEvidence(outcome, code, provider_stop_reason=reason)
            )
        elif event_type == "error":
            self._terminal_evidence.append(TerminalEvidence("failed", "provider_terminal_failure"))
        if event_type in {"end", "error"}:
            self._in_text = False
            events = self._flush_pending(final=True)
            if event is not None:
                events.append(event)
            if event_type == "end":
                outcome, _code, _reason, _text = _classify_end_reason(raw.get("stopReason"))
                if outcome != "completed":
                    session_id = raw.get("sessionId")
                    if isinstance(session_id, str) and session_id:
                        events.append(
                            provider_session_event(
                                "xai", self.agent_id, session_id, "session", raw=raw
                            )
                        )
            return events or None
        return event

    def finish(self) -> Optional[Union[Event, List[Event]]]:
        """Flush partial prose if the process ends without a terminal record."""

        events = self._flush_pending(final=True)
        return events or None

    def take_terminal_evidence(self) -> List[TerminalEvidence]:
        evidence = self._terminal_evidence
        self._terminal_evidence = []
        return evidence

    def _on_text(self, raw: Dict[str, Any]) -> Optional[Union[Event, List[Event]]]:
        events: List[Event] = []
        if not self._in_text:
            events.extend(self._flush_pending(final=False))
        self._in_text = True
        self._pending_parts.append(raw["data"])
        self._pending_delta_count += 1
        if sum(len(part) for part in self._pending_parts) >= TEXT_FLUSH_CHARS:
            events.extend(self._flush_pending(final=False))
        return events or None

    def _on_thought(
        self, raw: Dict[str, Any], verbose: bool
    ) -> Optional[Union[Event, List[Event]]]:
        self._in_text = False
        data = raw.get("data")
        if verbose:
            if isinstance(data, str):
                return Event.create("xai", "status", data, raw, agent_id=self.agent_id)
            return Event.create("xai", "status", compact_json(raw), raw, agent_id=self.agent_id)
        if self._thought_seen:
            return None
        self._thought_seen = True
        return Event.create(
            "xai",
            "status",
            THOUGHT_HEARTBEAT,
            {"type": "thought", "heartbeat": True},
            agent_id=self.agent_id,
        )

    def _on_usage(self, line: str, verbose: bool) -> Optional[Union[Event, List[Event]]]:
        self._in_text = False
        events = self._flush_pending(final=False)
        if verbose:
            event = parse_xai_line(line, True, agent_id=self.agent_id)
            if event is not None:
                events.append(event)
        return events or None

    def _on_tool(self, raw: Dict[str, Any]) -> Optional[Union[Event, List[Event]]]:
        self._in_text = False
        events = self._flush_pending(final=False)
        tool_event = self._map_tool(raw)
        if tool_event is not None:
            events.append(tool_event)
        return events or None

    def _merge_tool_state(self, state: Dict[str, Any], raw: Dict[str, Any]) -> None:
        kind = raw.get("kind")
        if not state.get("kind") and isinstance(kind, str) and kind.strip():
            state["kind"] = kind
        title = raw.get("title")
        if not state.get("title") and isinstance(title, str) and title.strip():
            state["title"] = title
        tool_name = raw.get("toolName")
        if not state.get("toolName") and isinstance(tool_name, str) and tool_name.strip():
            state["toolName"] = tool_name
        if not _nonempty_tool_input(state.get("rawInput")) and _nonempty_tool_input(
            raw.get("rawInput")
        ):
            state["rawInput"] = raw["rawInput"]
        status = _tool_status(raw.get("status"))
        if status is not None:
            state["status"] = status

    def _tool_event(self, raw: Dict[str, Any], state: Dict[str, Any], *, closing: bool) -> Event:
        event_raw = dict(raw)
        name = state.get("toolName") or state.get("title")
        if isinstance(name, str) and name.strip():
            event_raw.setdefault("name", name.strip())
        if _nonempty_tool_input(state.get("rawInput")):
            event_raw.setdefault("input", state["rawInput"])
        return Event.create(
            "tool",
            _tool_event_type(state.get("kind")),
            _tool_line_text(state, closing=closing),
            event_raw,
            agent_id=self.agent_id,
        )

    def _map_tool(self, raw: Dict[str, Any]) -> Optional[Event]:
        call_id = raw.get("toolCallId")
        status = _tool_status(raw.get("status"))
        closing = status in CLOSE_TOOL_STATUSES
        if not isinstance(call_id, str) or not call_id:
            state: Dict[str, Any] = {}
            self._merge_tool_state(state, raw)
            return Event.create(
                "tool",
                "tool_call",
                _tool_line_text(state, closing=closing),
                dict(raw),
                agent_id=self.agent_id,
            )

        state = self._tools.setdefault(call_id, {})
        self._merge_tool_state(state, raw)
        last_status = state.get("last_emitted_status")
        if status is not None and status == last_status:
            return None

        if closing:
            event = self._tool_event(raw, state, closing=True)
            state["last_emitted_status"] = status
            state["open_emitted"] = False
            return event

        has_identity = _tool_has_identity(state)
        if not state.get("open_emitted"):
            event = self._tool_event(raw, state, closing=False)
            state["open_emitted"] = True
            state["open_has_identity"] = has_identity
            state["last_emitted_status"] = status
            return event
        if not state.get("open_has_identity") and has_identity:
            event = self._tool_event(raw, state, closing=False)
            state["open_has_identity"] = True
            state["last_emitted_status"] = status
            return event
        if last_status in OPEN_TOOL_STATUSES and (status in OPEN_TOOL_STATUSES or status is None):
            return None
        if status is None and last_status is not None:
            return None
        event = self._tool_event(raw, state, closing=False)
        state["last_emitted_status"] = status
        return event

    def _flush_pending(self, *, final: bool) -> List[Event]:
        if not self._pending_parts:
            return []
        parts = self._pending_parts
        count = self._pending_delta_count
        self._pending_parts = []
        self._pending_delta_count = 0
        new_text = "".join(parts)
        if not new_text.strip():
            return []
        self._full_text += new_text
        self._emitted_messages += 1
        raw: Dict[str, Any] = {
            "type": "text",
            "data": new_text,
            "delta_count": count,
        }
        one_chunk = final and self._emitted_messages == 1 and self._full_text == new_text
        if not one_chunk:
            raw["full_text"] = self._full_text
        if final:
            raw["final"] = True
        return [
            Event.create(
                "xai",
                "message",
                new_text,
                raw,
                agent_id=self.agent_id,
            )
        ]
