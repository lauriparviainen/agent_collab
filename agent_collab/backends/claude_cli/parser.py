"""Claude CLI stream-JSON event parser."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Union
import json

from ...events import Event, compact_json, parse_json_line
from ...outcomes import TerminalEvidence
from ..common.parse import first_text, looks_like_command, looks_like_file_change, looks_like_tool
from ..common.sdk import provider_session_event

_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}


def _content_blocks(raw: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    message = raw.get("message")
    payload = message if isinstance(message, dict) else raw
    content = payload.get("content")
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        yield from (block for block in content if isinstance(block, dict))


def _visible_text(blocks: Iterable[Dict[str, Any]]) -> str:
    return "\n".join(
        str(block["text"]).strip()
        for block in blocks
        if block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    )


def _thinking_text(blocks: Iterable[Dict[str, Any]]) -> str:
    return "\n".join(
        str(block["thinking"]).strip()
        for block in blocks
        if block.get("type") in _THINKING_BLOCK_TYPES
        and isinstance(block.get("thinking"), str)
        and block["thinking"].strip()
    )


def _tool_kind(block: Dict[str, Any]) -> str:
    name = str(block.get("name") or "").lower()
    if any(token in name for token in ("edit", "write", "patch")):
        return "file_change"
    if any(token in name for token in ("bash", "command", "exec", "shell")):
        return "command"
    return "tool_call"


def _tool_text(block: Dict[str, Any]) -> str:
    name = block.get("name")
    if isinstance(name, str) and name:
        return f"{name} {compact_json(block['input'])}" if block.get("input") else name
    return first_text(block.get("content")) or compact_json(block)


def _parse_message(raw: Dict[str, Any], verbose: bool) -> Optional[Event]:
    blocks: List[Dict[str, Any]] = list(_content_blocks(raw))
    tools = [
        block for block in blocks if isinstance(block.get("type"), str) and "tool" in block["type"]
    ]
    text = _visible_text(blocks)
    if tools:
        kinds = {_tool_kind(block) for block in tools}
        event_type = (
            "file_change"
            if "file_change" in kinds
            else "command"
            if "command" in kinds
            else "tool_call"
        )
        return Event.create(
            "tool", event_type, text or "\n".join(_tool_text(block) for block in tools), raw
        )
    if text:
        return Event.create("claude", "message", text, raw)
    if verbose:
        thinking = _thinking_text(blocks)
        if thinking:
            return Event.create("claude", "status", thinking, raw)
        if any(block.get("type") in _THINKING_BLOCK_TYPES for block in blocks):
            return Event.create("claude", "status", "thinking", raw)
        return Event.create("claude", "status", str(raw.get("type") or "message"), raw)
    return None


def parse_claude_line(
    line: str,
    verbose: bool = False,
    *,
    agent_id: str = "claude",
) -> Optional[Union[Event, List[Event]]]:
    raw = parse_json_line(line)
    if raw is None:
        text = line.strip()
        return Event.create("claude", "message", text, {"line": line}) if text else None
    if not isinstance(raw, dict):
        return Event.create("claude", "status", compact_json(raw), raw) if verbose else None
    identity = None
    session_id = raw.get("session_id")
    if raw.get("type") in {"system", "result"} and isinstance(session_id, str) and session_id:
        identity = provider_session_event("claude", agent_id, session_id, "session", raw=raw)
    if raw.get("type") in {"error", "fatal_error"} or "error" in raw:
        event = Event.create(
            "error", "error", first_text(raw) or compact_json(raw), {**raw, "fatal": True}
        )
        return [identity, event] if identity is not None else event
    if raw.get("type") in {"system", "rate_limit_event", "result"}:
        text = str(raw.get("subtype") or raw.get("status") or raw.get("type"))
        event = Event.create("claude", "status", text, raw) if verbose else None
        if identity is None:
            return event
        return [identity, event] if event is not None else identity
    if raw.get("type") in {"assistant", "user"} or isinstance(raw.get("message"), dict):
        return _parse_message(raw, verbose)
    for predicate, event_type in (
        (looks_like_file_change, "file_change"),
        (looks_like_command, "command"),
        (looks_like_tool, "tool_call"),
    ):
        if predicate(raw):
            return Event.create("tool", event_type, first_text(raw) or compact_json(raw), raw)
    text = first_text(raw)
    if text:
        return Event.create("claude", "message", text, raw)
    return Event.create("claude", "status", compact_json(raw), raw) if verbose else None


class ClaudeStreamingParser:
    """Stateful CLI parser that emits each provider session identity once."""

    def __init__(self, agent_id: str = "claude") -> None:
        self.agent_id = agent_id
        self._seen_session_ids: set[str] = set()
        self._terminal_evidence: List[TerminalEvidence] = []

    def __call__(self, line: str, verbose: bool = False):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid Claude stream JSON") from exc
        if isinstance(raw, dict):
            record_type = raw.get("type")
            if record_type == "result":
                if raw.get("subtype") == "success" and raw.get("is_error") is not True:
                    self._terminal_evidence.append(TerminalEvidence("completed"))
                else:
                    self._terminal_evidence.append(
                        TerminalEvidence("failed", "provider_terminal_failure")
                    )
            elif record_type in {"error", "fatal_error"}:
                self._terminal_evidence.append(
                    TerminalEvidence("failed", "provider_terminal_failure")
                )
        parsed = parse_claude_line(line, verbose, agent_id=self.agent_id)
        events = parsed if isinstance(parsed, list) else [parsed]
        kept = []
        for event in events:
            if event is None:
                continue
            identity = event.provider_session
            if identity is not None:
                session_id = identity.get("provider_session_id")
            else:
                session_id = None
            if isinstance(session_id, str):
                if session_id in self._seen_session_ids:
                    continue
                self._seen_session_ids.add(session_id)
            kept.append(event)
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else kept

    def take_terminal_evidence(self) -> List[TerminalEvidence]:
        evidence = self._terminal_evidence
        self._terminal_evidence = []
        return evidence
