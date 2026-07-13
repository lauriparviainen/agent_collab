"""Parser for Grok Build ``streaming-json`` output."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ...events import Event, compact_json, parse_json_line
from ..common.sdk import provider_session_event


SUCCESS_STOP_REASON = "EndTurn"


def _event_text(raw: Dict[str, Any]) -> str:
    for key in ("message", "data", "error"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return compact_json(raw)


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
        stop_reason = raw.get("stopReason")
        if stop_reason != SUCCESS_STOP_REASON:
            reason = stop_reason if isinstance(stop_reason, str) and stop_reason else "unknown"
            code = "provider_turn_cancelled" if reason == "Cancelled" else "provider_turn_failed"
            text = (
                "Grok ended the turn before producing a response"
                if reason == "Cancelled"
                else f"Grok turn ended with unsuccessful stop reason {reason!r}"
            )
            return Event.create(
                "error",
                "error",
                text,
                {
                    **raw,
                    "code": code,
                    "fatal": True,
                    "provider_stop_reason": reason,
                },
            )
        session_id = raw.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return provider_session_event("xai", agent_id, session_id, "session", raw=raw)
        return Event.create("xai", "status", _event_text(raw), raw) if verbose else None
    return Event.create("xai", "status", compact_json(raw), raw) if verbose else None


class XaiStreamingParser:
    """Coalesce Grok text deltas into one message per completed turn.

    ``parse_xai_line`` remains the stateless, fixture-level record mapper. The
    subprocess runner uses this stateful callable so token-sized ``text``
    records do not become thousands of transcript messages. On ``end`` or
    ``error`` it returns both the collected message (when any) and the normal
    terminal/session event; ``SubprocessRunner`` accepts this multi-event result.
    """

    def __init__(self, agent_id: str = "xai") -> None:
        self.agent_id = agent_id
        self._text_parts: List[str] = []

    def __call__(
        self,
        line: str,
        verbose: bool = False,
    ) -> Optional[Union[Event, List[Event]]]:
        raw = parse_json_line(line)
        if isinstance(raw, dict) and raw.get("type") == "text" and isinstance(raw.get("data"), str):
            self._text_parts.append(raw["data"])
            return None

        event = parse_xai_line(line, verbose, agent_id=self.agent_id)
        if isinstance(raw, dict) and raw.get("type") in {"end", "error"}:
            events = self._flush_text()
            if event is not None:
                events.append(event)
            if raw.get("type") == "end" and raw.get("stopReason") != SUCCESS_STOP_REASON:
                session_id = raw.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    events.append(
                        provider_session_event("xai", self.agent_id, session_id, "session", raw=raw)
                    )
            return events or None
        return event

    def finish(self) -> Optional[Union[Event, List[Event]]]:
        """Flush partial prose if the process ends without a terminal record."""

        events = self._flush_text()
        return events or None

    def _flush_text(self) -> List[Event]:
        if not self._text_parts:
            return []
        parts = self._text_parts
        self._text_parts = []
        text = "".join(parts)
        return [
            Event.create(
                "xai",
                "message",
                text,
                {"type": "text", "data": text, "delta_count": len(parts)},
            )
        ]
