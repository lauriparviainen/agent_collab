from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


TERMINAL_STATUSES = {"done", "failed", "stopped", "interrupted"}
READ_ONLY_INPUT_MESSAGE = "referee input is available only for live interactive sessions"
SLASH_COMMANDS = {
    "help": "show help",
    "sessions": "pick from daemon sessions",
    "session": "switch active session",
    "new": "start an interactive daemon session",
    "details": "toggle session details",
    "follow": "jump to tail and follow",
    "refresh": "re-read the active session",
    "stop": "stop the active session",
    "ask": "ask one agent a directed question",
    "quit": "exit",
}


@dataclass(frozen=True)
class TranscriptLine:
    source: str
    text: str
    continuation: bool = False


@dataclass(frozen=True)
class ParsedInput:
    kind: str
    command: Optional[str] = None
    args: Tuple[str, ...] = ()
    text: str = ""
    agent: Optional[str] = None
    message: str = ""
    error: Optional[str] = None


@dataclass(frozen=True)
class AgentRef:
    id: str
    type: str
    enabled: bool = True


@dataclass(frozen=True)
class AgentResolution:
    ok: bool
    agent_id: Optional[str] = None
    error: Optional[str] = None
    valid_agent_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ScrollState:
    top: int = 0
    follow: bool = True


@dataclass(frozen=True)
class CursorState:
    session_id: Optional[str] = None
    cursor: int = 0
    epoch: int = 0


@dataclass(frozen=True)
class SessionPickerState:
    # Session entries are SessionStateModel DTOs from the typed client (plain
    # wire dicts also work — helpers read both via ``_value``).
    sessions: Tuple[Any, ...]
    index: int = 0


@dataclass(frozen=True)
class SlashCommandMatch:
    name: str
    description: str


@dataclass(frozen=True)
class SlashCompletionState:
    matches: Tuple[SlashCommandMatch, ...]
    index: int = 0


def parse_input(raw: str) -> ParsedInput:
    value = raw.strip()
    if not value:
        return ParsedInput(kind="empty")
    if value.startswith("/"):
        body = value[1:].strip()
        if not body:
            return ParsedInput(kind="invalid", error="empty command; type /help")
        name, _, rest = body.partition(" ")
        name = name.lower()
        if name not in SLASH_COMMANDS:
            return ParsedInput(kind="invalid", command=name, error=f"unknown command /{name}; type /help")
        args = tuple(part for part in rest.split() if part)
        if name == "ask":
            agent, sep, message = rest.strip().partition(" ")
            if not sep or not agent.strip() or not message.strip():
                return ParsedInput(kind="invalid", command=name, error="usage: /ask AGENT message")
            return ParsedInput(kind="directed", command=name, args=args, agent=agent.strip(), message=message.strip())
        return ParsedInput(kind="slash", command=name, args=args, text=rest.strip())
    if value.startswith("#"):
        target, _, message = value.partition(" ")
        if len(target) > 1:
            if not message.strip():
                return ParsedInput(kind="invalid", error="usage: #AGENT message")
            return ParsedInput(kind="directed", agent=target[1:], message=message.strip())
    return ParsedInput(kind="text", text=value)


def filter_slash_commands(prefix: str) -> Tuple[SlashCommandMatch, ...]:
    value = str(prefix or "").strip()
    if value.startswith("/"):
        value = value[1:]
    if " " in value:
        return ()
    value = value.lower()
    return tuple(
        SlashCommandMatch(name=f"/{name}", description=description)
        for name, description in SLASH_COMMANDS.items()
        if name.startswith(value)
    )


def make_slash_completion(prefix: str, index: int = 0) -> Optional[SlashCompletionState]:
    value = str(prefix or "")
    if not value.startswith("/"):
        return None
    body = value[1:]
    if any(char.isspace() for char in body):
        return None
    matches = filter_slash_commands(value)
    if not matches:
        return SlashCompletionState(matches=(), index=0)
    return SlashCompletionState(matches=matches, index=max(0, min(len(matches) - 1, int(index))))


def move_slash_completion(state: SlashCompletionState, delta: int) -> SlashCompletionState:
    if not state.matches:
        return SlashCompletionState(matches=state.matches, index=0)
    index = max(0, min(len(state.matches) - 1, state.index + int(delta)))
    return SlashCompletionState(matches=state.matches, index=index)


def selected_slash_command(state: SlashCompletionState) -> Optional[str]:
    if not state.matches:
        return None
    return state.matches[state.index].name


def slash_completion_matches_input(prefix: str, state: SlashCompletionState) -> bool:
    selected = selected_slash_command(state)
    return bool(selected) and str(prefix or "").strip().lower() == selected.lower()


def accept_slash_completion(prefix: str, state: SlashCompletionState) -> str:
    selected = selected_slash_command(state)
    if not selected:
        return str(prefix or "")
    return f"{selected} "


def format_slash_completion_lines(state: SlashCompletionState, max_items: int = 6) -> Tuple[str, ...]:
    header = "commands  Tab/Enter accepts  Esc closes"
    if not state.matches:
        return (header, "  no matches")

    item_count = max(1, int(max_items))
    start = max(0, min(state.index - item_count // 2, len(state.matches) - item_count))
    end = min(len(state.matches), start + item_count)
    lines = [header]
    for index, match in enumerate(state.matches[start:end], start=start):
        marker = ">" if index == state.index else " "
        lines.append(f"{marker} {match.name:<14} {match.description}")
    return tuple(lines)


ACTIVITY_FRAMES = ("-", "\\", "|", "/")


def format_activity_indicator(session: Any, tick: int = 0) -> str:
    if not session:
        return "no session"
    status = str(_value(session, "status", None) or "")
    if status_is_terminal(status):
        return f"read-only {status}"
    if status == "awaiting_input":
        return "awaiting input"
    if status == "running":
        frame = ACTIVITY_FRAMES[int(tick) % len(ACTIVITY_FRAMES)]
        return f"{frame} running"
    return status or "live"


def format_transcript_event(event: Any) -> Tuple[TranscriptLine, ...]:
    source = str(_value(event, "source", "error"))
    label = source.upper()
    text = str(_value(event, "text", "") or "")
    result = []
    for index, line in enumerate(text.splitlines() or [""]):
        prefix = f"{label:<7}" if index == 0 else f"{'':<7}"
        result.append(TranscriptLine(source=source, text=f"{prefix} {line}", continuation=index > 0))
    return tuple(result)


def format_transcript_events(events: Iterable[Any]) -> Tuple[TranscriptLine, ...]:
    lines = []
    for event in events:
        lines.extend(format_transcript_event(event))
    return tuple(lines)


def render_transcript_lines(lines: Iterable[TranscriptLine]) -> Tuple[str, ...]:
    return tuple(line.text for line in lines)


def wrap_transcript_lines(lines: Sequence[TranscriptLine], width: int) -> Tuple[TranscriptLine, ...]:
    if width <= 0:
        return ()
    wrapped = []
    for line in lines:
        for index, chunk in enumerate(_wrap_display_text(line.text, width, continuation_indent="        ")):
            wrapped.append(
                TranscriptLine(
                    source=line.source,
                    text=chunk,
                    continuation=line.continuation or index > 0,
                )
            )
    return tuple(wrapped)


def wrap_plain_lines(lines: Sequence[str], width: int) -> Tuple[str, ...]:
    if width <= 0:
        return ()
    wrapped = []
    for line in lines:
        wrapped.extend(_wrap_display_text(str(line), width, continuation_indent="  "))
    return tuple(wrapped)


def format_session_details(session: Any) -> Tuple[str, ...]:
    """Render the per-session detail block.

    ``session`` is a ``SessionStateModel`` from the typed client (the TUI path)
    or a plain wire dict — ``_value`` reads both.
    """
    raw_settings = _value(session, "settings", None)
    settings = raw_settings if isinstance(raw_settings, Mapping) else {}
    workflow_settings = settings.get("workflow") if isinstance(settings.get("workflow"), Mapping) else {}

    lines = []
    for key in ("session_id", "status"):
        _append_present(lines, key, session)

    workflow = workflow_settings.get("name") or _value(session, "workflow", None)
    if workflow is not None:
        lines.append(f"workflow: {_display_value(workflow)}")
    sequence = workflow_settings.get("sequence")
    if isinstance(sequence, Sequence) and not isinstance(sequence, (str, bytes)):
        lines.append(f"sequence: {' -> '.join(str(item) for item in sequence)}")

    for key in (
        "workdir",
        "created_at",
        "updated_at",
        "ended_at",
        "max_turns",
        "timeout",
        "mock",
        "dry_run",
        "interactive",
        "interactive_idle_timeout",
        "jsonl_path",
        "markdown_path",
        "error",
    ):
        _append_present(lines, key, session)

    agents = settings.get("agents") if isinstance(settings.get("agents"), Mapping) else {}
    if agents:
        lines.append("")
    for agent_id, agent in agents.items():
        if not isinstance(agent, Mapping):
            continue
        details = []
        ordered_fields = (
            "type",
            "model",
            "profile",
            "thinking_level",
            "thinking_budget_tokens",
            "permission_mode",
            "sandbox",
            "approval_policy",
            "search",
        )
        seen = set(ordered_fields) | {"command_preview"}
        for key in ordered_fields:
            if key in agent and agent[key] is not None:
                details.append(f"{key}={_display_value(agent[key])}")
        for key in sorted(k for k in agent if k not in seen):
            if agent[key] is not None:
                details.append(f"{key}={_display_value(agent[key])}")
        suffix = " " + " ".join(details) if details else ""
        lines.append(f"agent {agent_id}:{suffix}")
        preview = agent.get("command_preview")
        if isinstance(preview, Sequence) and not isinstance(preview, (str, bytes)):
            lines.append(f"  command_preview: {' '.join(str(part) for part in preview)}")
        elif preview:
            lines.append(f"  command_preview: {_display_value(preview)}")
    return tuple(lines)


def session_workflow_name(session: Any) -> str:
    raw_settings = _value(session, "settings", None)
    settings = raw_settings if isinstance(raw_settings, Mapping) else {}
    workflow = settings.get("workflow") if isinstance(settings.get("workflow"), Mapping) else {}
    return str(workflow.get("name") or _value(session, "workflow", None) or "")


def status_is_terminal(status: Any) -> bool:
    return str(status or "") in TERMINAL_STATUSES


def session_is_terminal(session: Any) -> bool:
    return status_is_terminal(_value(session, "status", None))


def should_start_poller(session: Any) -> bool:
    return bool(session) and not session_is_terminal(session)


def _session_sort_key(session: Any) -> Tuple[str, str]:
    return (
        str(_value(session, "updated_at", None) or _value(session, "created_at", None) or ""),
        str(_value(session, "session_id", None) or ""),
    )


def select_latest_session_id(sessions: Sequence[Any]) -> str:
    if not sessions:
        raise ValueError("no daemon sessions found")
    latest = max(sessions, key=_session_sort_key)
    session_id = _value(latest, "session_id", None)
    if not session_id:
        raise ValueError("latest daemon session did not include a session_id")
    return str(session_id)


def sort_sessions_latest_first(sessions: Sequence[Any]) -> Tuple[Any, ...]:
    return tuple(sorted(sessions, key=_session_sort_key, reverse=True))


def make_session_picker(sessions: Sequence[Any], current_session_id: Optional[str] = None) -> SessionPickerState:
    ordered = sort_sessions_latest_first(sessions)
    index = 0
    if current_session_id:
        for candidate_index, session in enumerate(ordered):
            if str(_value(session, "session_id", None) or "") == current_session_id:
                index = candidate_index
                break
    return SessionPickerState(sessions=ordered, index=index)


def move_session_picker(picker: SessionPickerState, delta: int) -> SessionPickerState:
    if not picker.sessions:
        return SessionPickerState(sessions=picker.sessions, index=0)
    index = max(0, min(len(picker.sessions) - 1, picker.index + delta))
    return SessionPickerState(sessions=picker.sessions, index=index)


def selected_picker_session_id(picker: SessionPickerState) -> Optional[str]:
    if not picker.sessions:
        return None
    session_id = _value(picker.sessions[picker.index], "session_id", None)
    return str(session_id) if session_id else None


def format_session_picker_lines(picker: SessionPickerState) -> Tuple[str, ...]:
    lines = ["sessions", "enter switches  esc closes", ""]
    if not picker.sessions:
        lines.append("no daemon sessions found")
        return tuple(lines)
    lines.append(f"{'':2} {'SESSION_ID':<24} {'STATUS':<11} {'WORKFLOW':<14} {'UPDATED':<25} WORKDIR")
    for index, session in enumerate(picker.sessions):
        marker = ">" if index == picker.index else " "
        lines.append(
            f"{marker} {str(_value(session, 'session_id', None) or ''):<24} "
            f"{str(_value(session, 'status', None) or ''):<11} "
            f"{session_workflow_name(session):<14} "
            f"{str(_value(session, 'updated_at', None) or _value(session, 'created_at', None) or ''):<25} "
            f"{str(_value(session, 'workdir', None) or '')}"
        )
    return tuple(lines)


def agents_from_session(session: Any) -> Tuple[AgentRef, ...]:
    raw_settings = _value(session, "settings", None)
    settings = raw_settings if isinstance(raw_settings, Mapping) else {}
    agents = settings.get("agents") if isinstance(settings.get("agents"), Mapping) else {}
    refs = []
    for agent_id, agent in agents.items():
        agent_type = ""
        if isinstance(agent, Mapping):
            agent_type = str(agent.get("type") or "")
        refs.append(AgentRef(id=str(agent_id), type=agent_type, enabled=True))
    return tuple(refs)


def agents_from_options(options: Mapping[str, Any]) -> Tuple[AgentRef, ...]:
    refs = []
    agents = options.get("agents") if isinstance(options.get("agents"), Sequence) else []
    for agent in agents:
        if not isinstance(agent, Mapping) or not agent.get("enabled", False):
            continue
        agent_id = agent.get("id")
        if agent_id:
            refs.append(AgentRef(id=str(agent_id), type=str(agent.get("type") or ""), enabled=True))
    return tuple(refs)


def resolve_agent_selector(selector: str, agents: Sequence[AgentRef]) -> AgentResolution:
    enabled = tuple(agent for agent in agents if agent.enabled)
    valid_ids = tuple(agent.id for agent in enabled)
    needle = selector.lower()
    id_matches = [agent for agent in enabled if agent.id.lower() == needle]
    if id_matches:
        return AgentResolution(ok=True, agent_id=id_matches[0].id, valid_agent_ids=valid_ids)
    type_matches = [agent for agent in enabled if agent.type.lower() == needle and agent.type]
    if len(type_matches) == 1:
        return AgentResolution(ok=True, agent_id=type_matches[0].id, valid_agent_ids=valid_ids)
    if len(type_matches) > 1:
        return AgentResolution(
            ok=False,
            error=f"ambiguous agent type {selector!r}; valid agent ids: {', '.join(valid_ids)}",
            valid_agent_ids=valid_ids,
        )
    valid = ", ".join(valid_ids) if valid_ids else "(none)"
    return AgentResolution(
        ok=False,
        error=f"unknown agent {selector!r}; valid agent ids: {valid}",
        valid_agent_ids=valid_ids,
    )


def max_scroll_top(total_lines: int, viewport_height: int) -> int:
    return max(0, int(total_lines) - max(0, int(viewport_height)))


def visible_scroll_top(state: ScrollState, total_lines: int, viewport_height: int) -> int:
    if state.follow:
        return max_scroll_top(total_lines, viewport_height)
    return max(0, min(state.top, max_scroll_top(total_lines, viewport_height)))


def clamp_scroll(state: ScrollState, total_lines: int, viewport_height: int) -> ScrollState:
    top = visible_scroll_top(state, total_lines, viewport_height)
    return ScrollState(top=top, follow=state.follow and top == max_scroll_top(total_lines, viewport_height))


def scroll_by(state: ScrollState, total_lines: int, viewport_height: int, delta: int) -> ScrollState:
    maximum = max_scroll_top(total_lines, viewport_height)
    current = visible_scroll_top(state, total_lines, viewport_height)
    top = max(0, min(maximum, current + delta))
    return ScrollState(top=top, follow=top >= maximum)


def follow_scroll(total_lines: int, viewport_height: int) -> ScrollState:
    return ScrollState(top=max_scroll_top(total_lines, viewport_height), follow=True)


def reset_cursor_state(state: CursorState, session_id: str) -> CursorState:
    return CursorState(session_id=session_id, cursor=0, epoch=state.epoch + 1)


def advance_cursor_state(
    state: CursorState,
    *,
    session_id: str,
    cursor: Any,
    epoch: int,
) -> Tuple[CursorState, bool]:
    if state.session_id != session_id or state.epoch != epoch:
        return state, False
    return CursorState(session_id=state.session_id, cursor=max(0, int(cursor)), epoch=state.epoch), True


def build_new_session_payload(
    *,
    task: str,
    workflow: str,
    workdir: str,
    max_turns: int = 3,
    timeout: int = 900,
    mock: bool = False,
    dry_run: bool = False,
    interactive: bool = False,
    interactive_idle_timeout: float = 600.0,
    backend_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict:
    task = task.strip()
    workflow = workflow.strip()
    if not task:
        raise ValueError("task is required")
    if not workflow:
        raise ValueError("workflow is required")
    root = Path(workdir or ".").expanduser().resolve()
    return {
        "task": task,
        "workflow": workflow,
        "workdir": str(root),
        "max_turns": int(max_turns),
        "timeout": int(timeout),
        "mock": bool(mock),
        "dry_run": bool(dry_run),
        "interactive": bool(interactive),
        "interactive_idle_timeout": float(interactive_idle_timeout),
        "backend_options": {
            key: dict(value) for key, value in (backend_options or {}).items()
        },
    }


def workflow_ids_from_options(options: Mapping[str, Any]) -> Tuple[str, ...]:
    workflows = options.get("workflows") if isinstance(options.get("workflows"), Sequence) else []
    ids = []
    for workflow in workflows:
        if isinstance(workflow, Mapping) and workflow.get("id"):
            ids.append(str(workflow["id"]))
    return tuple(ids)


def _append_present(lines: list[str], key: str, data: Any) -> None:
    value = _value(data, key, None)
    if value is not None:
        lines.append(f"{key}: {_display_value(value)}")


def _display_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _value(event: Any, key: str, default: Any) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _wrap_display_text(text: str, width: int, *, continuation_indent: str) -> Tuple[str, ...]:
    if width <= 0:
        return ()
    text = str(text)
    if text == "":
        return ("",)
    if len(text) <= width:
        return (text,)
    if width <= len(continuation_indent):
        return tuple(text[index : index + width] for index in range(0, len(text), width))

    chunks = []
    current = text
    while len(current) > width:
        chunks.append(current[:width])
        remainder = current[width:].lstrip()
        if not remainder:
            return tuple(chunks)
        current = continuation_indent + remainder
    chunks.append(current)
    return tuple(chunks)
