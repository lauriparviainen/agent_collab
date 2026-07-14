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
    "new": "start a daemon session",
    "details": "toggle session details",
    "follow": "jump to tail and follow",
    "refresh": "re-read the active session",
    "stop": "stop the active session",
    "quit": "exit",
}


@dataclass(frozen=True)
class TranscriptLine:
    source: str
    text: str
    continuation: bool = False
    timestamp: str = ""


@dataclass(frozen=True)
class ParsedInput:
    kind: str
    command: Optional[str] = None
    args: Tuple[str, ...] = ()
    text: str = ""
    error: Optional[str] = None


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
            return ParsedInput(
                kind="invalid", command=name, error=f"unknown command /{name}; type /help"
            )
        args = tuple(part for part in rest.split() if part)
        return ParsedInput(kind="slash", command=name, args=args, text=rest.strip())
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


SLASH_SELECTED_MARKER = "▸"

# 1:1 glyph fallbacks for non-UTF-8 terminals, applied at the single draw
# funnel (``TuiApp._add``). One-to-one on purpose: substitution must never
# change column math. The spinner and input-box corners have their own
# dedicated fallbacks; this covers every remaining chrome glyph.
ASCII_FALLBACKS = str.maketrans(
    {
        "▸": ">",
        "▏": "|",
        "◆": "*",
        "│": "|",
        "─": "-",
        "╭": "+",
        "╮": "+",
        "╰": "+",
        "╯": "+",
        "·": "|",
        "…": "~",
        "↑": "^",
        "↓": "v",
    }
)


def ascii_fallback(text: str) -> str:
    """Substitute chrome glyphs for ASCII on non-UTF-8 terminals (1:1)."""
    return str(text).translate(ASCII_FALLBACKS)


def format_slash_completion_lines(
    state: SlashCompletionState, max_items: int = 6
) -> Tuple[str, ...]:
    """Render the palette menu: a band header row, then the windowed rows.

    The header carries the column titles, matching the session picker's
    combined header so every menu reads the same. The typed ``/`` in the
    input box remains the mode label and the keys live on the status/hint
    line. Selected row is marked ``▸``; windowing/filter behaviour is
    unchanged.
    """
    header = f"  {'command':<14} description"
    if not state.matches:
        return (header, "  no matches")

    item_count = max(1, int(max_items))
    start = max(0, min(state.index - item_count // 2, len(state.matches) - item_count))
    end = min(len(state.matches), start + item_count)
    lines = [header]
    for index, match in enumerate(state.matches[start:end], start=start):
        marker = SLASH_SELECTED_MARKER if index == state.index else " "
        lines.append(f"{marker} {match.name:<14} {match.description}")
    return tuple(lines)


BRAILLE_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
ASCII_PULSE_FRAMES = (".", "..", "...")


def spinner_frame(tick: int, *, utf8: bool = True) -> str:
    """Return the running-spinner frame: braille orbit (UTF-8) or dot-pulse."""
    frames = BRAILLE_FRAMES if utf8 else ASCII_PULSE_FRAMES
    return frames[int(tick) % len(frames)]


def format_activity_indicator(session: Any, tick: int = 0, *, utf8: bool = True) -> str:
    if not session:
        return "no session"
    status = str(_value(session, "status", None) or "")
    if status_is_terminal(status):
        # Just the status: the input-box chip already carries "read-only".
        return status
    if status == "awaiting_input":
        return "awaiting input"
    if status == "running":
        return f"{spinner_frame(tick, utf8=utf8)} running"
    return status or "live"


# Wide enough for the longest built-in member tool label ("antigravity-tool");
# longer custom agent ids still ellipsize via gutter_label.
GUTTER_WIDTH = 16
BAND_SOURCES = {"referee", "human"}
# Display-only gutter aliases; the wire source is untouched (band styling,
# brand colors, and JSONL key on it). The "human" event carries the task
# prompt, which an MCP caller rather than a person may have posted —
# "prompt" says what the row is instead of guessing who sent it.
GUTTER_ALIASES = {"human": "prompt"}


def gutter_label(source: str) -> str:
    """Lowercase gutter label, ellipsized into the fixed gutter column.

    Agent ids are arbitrary (``antigravity``, ``claude-a``, …); ``{label:<N}``
    only pads to a *minimum*, so a long source would push its rows out of
    column with everyone else's. Keep the gutter fixed and mark the cut.
    """
    label = str(source or "").lower()
    if len(label) <= GUTTER_WIDTH:
        return label
    return label[: GUTTER_WIDTH - 1] + "…"


def short_time(timestamp: Any) -> str:
    """Render an ISO ``timestamp`` as ``H:MM`` (no leading hour zero).

    Returns ``""`` when a time cannot be extracted; the value is display-only
    and never affects dispatch.
    """
    text = str(timestamp or "")
    if "T" not in text or len(text) < 16:
        return ""
    clock = text[11:16]
    if len(clock) != 5 or clock[2] != ":":
        return ""
    hour = clock[:2].lstrip("0") or "0"
    return f"{hour}:{clock[3:]}"


def format_transcript_event(event: Any) -> Tuple[TranscriptLine, ...]:
    """Project one event into gutter-labelled transcript lines.

    Source labels are lowercase (calm direction). ``tool`` events collapse to a
    single dim summary row (name + first-line digest + ``+N lines`` size) — a
    display-only projection; the JSONL keeps the full payload.
    """
    raw = _value(event, "raw", None)
    if isinstance(raw, Mapping) and raw.get("fatal") is True:
        return ()
    source = str(_value(event, "source", "error"))
    text = str(_value(event, "text", "") or "")
    agent_id = str(_value(event, "agent_id", "") or "")
    # Member attribution follows the Markdown renderers' agent_id-differs rule,
    # rendered in the gutter: `codex-tool` for a member's tool rows, the agent
    # id for a member's stream rows. Band rows are referee-authored prose that
    # already names the member.
    attributed = bool(agent_id) and agent_id != source and source not in BAND_SOURCES
    if source == "tool":
        label = gutter_label(f"{agent_id}-tool" if attributed else source)
    else:
        label = gutter_label(agent_id if attributed else GUTTER_ALIASES.get(source, source))
    stamp = short_time(_value(event, "timestamp", "")) if source in BAND_SOURCES else ""

    if source == "tool":
        raw_lines = text.splitlines()
        first = raw_lines[0].strip() if raw_lines else ""
        extra = max(0, len(raw_lines) - 1)
        digest = first
        if extra > 0:
            digest = f"{first} · +{extra} lines" if first else f"+{extra} lines"
        return (TranscriptLine(source=source, text=f"{label:<{GUTTER_WIDTH}} {digest}".rstrip()),)

    result = []
    for index, line in enumerate(text.splitlines() or [""]):
        prefix = f"{label:<{GUTTER_WIDTH}}" if index == 0 else f"{'':<{GUTTER_WIDTH}}"
        result.append(
            TranscriptLine(
                source=source,
                text=f"{prefix} {line}",
                continuation=index > 0,
                timestamp=stamp if index == 0 else "",
            )
        )
    return tuple(result)


def format_transcript_events(events: Iterable[Any]) -> Tuple[TranscriptLine, ...]:
    lines = []
    for event in events:
        lines.extend(format_transcript_event(event))
    return tuple(lines)


def render_transcript_lines(lines: Iterable[TranscriptLine]) -> Tuple[str, ...]:
    return tuple(line.text for line in lines)


def wrap_transcript_lines(
    lines: Sequence[TranscriptLine], width: int
) -> Tuple[TranscriptLine, ...]:
    if width <= 0:
        return ()
    wrapped = []
    for line in lines:
        for index, chunk in enumerate(
            _wrap_display_text(line.text, width, continuation_indent=" " * (GUTTER_WIDTH + 1))
        ):
            wrapped.append(
                TranscriptLine(
                    source=line.source,
                    text=chunk,
                    continuation=line.continuation or index > 0,
                    timestamp=line.timestamp if index == 0 else "",
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
    workflow_settings = (
        settings.get("workflow") if isinstance(settings.get("workflow"), Mapping) else {}
    )

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
    ):
        _append_present(lines, key, session)

    failure = _value(session, "failure", None)
    if isinstance(failure, Mapping):
        turn = f" {failure.get('turn_id')}" if failure.get("turn_id") else ""
        lines.append(f"failure{turn}: {failure.get('code')} — {failure.get('message')}")
    else:
        _append_present(lines, "error", session)

    outcomes = _value(session, "turn_outcomes", None)
    if isinstance(outcomes, Sequence) and not isinstance(outcomes, (str, bytes)):
        for outcome in outcomes:
            if not isinstance(outcome, Mapping):
                continue
            lines.append(
                "outcome "
                f"{outcome.get('turn_id')}: {outcome.get('agent_id')} "
                f"{outcome.get('outcome')}"
            )

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


def session_backends_summary(session: Any) -> str:
    """The session's effective member backends, deduped in order (`a+b`).

    Read from the settings echo, which reflects start-time member selection —
    with one built-in `solo` shape the workflow id alone no longer says which
    agents ran. Legacy records without settings yield an empty string.
    """
    raw_settings = _value(session, "settings", None)
    settings = raw_settings if isinstance(raw_settings, Mapping) else {}
    workflow = settings.get("workflow") if isinstance(settings.get("workflow"), Mapping) else {}
    # The daemon echo fills "sequence" with the member list for parallel
    # workflows too; the "parallel" fallback covers records that only carry
    # the parallel shape (same tolerance as the wizard's annotations).
    members = workflow.get("sequence") or workflow.get("parallel")
    if isinstance(members, str) or not isinstance(members, Sequence):
        return ""
    return "+".join(dict.fromkeys(str(member) for member in members))


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


def make_session_picker(
    sessions: Sequence[Any], current_session_id: Optional[str] = None
) -> SessionPickerState:
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


# Lines before the first session row in format_session_picker_lines' output:
# one combined title row carrying the column headers and right-aligned hints.
PICKER_HEADER_LINES = 1
PICKER_HEADER_HINTS = "↑↓ choose · Enter switch · Esc close"


def _picker_timestamp(value: Any) -> str:
    """Minute-precision ``YYYY-MM-DD HH:MM`` for picker rows (display-only)."""
    text = str(value or "")
    if len(text) >= 16 and text[10] == "T":
        clock = text[11:16]
        if len(clock) == 5 and clock[2] == ":":
            return f"{text[:10]} {clock}"
    return text


def format_session_picker_lines(picker: SessionPickerState, width: int = 0) -> Tuple[str, ...]:
    """Render the session picker as shared-overlay body lines.

    Behaviour (latest-first sort, pre-selection, activation) is unchanged; the
    selected row is marked ``▸``. One combined header row carries the column
    titles, with the key hints right-aligned into it when ``width`` (> 0)
    leaves room. Column widths fit the widest value over reserved floors so
    long workflow names or custom session ids never push later columns out of
    line; timestamps render at minute precision.
    """
    if not picker.sessions:
        return (
            "    sessions · Esc close",
            "",
            "    no daemon sessions found — /new to start one",
        )
    header = ("session", "status", "workflow", "backends", "updated")
    rows = []
    for session in picker.sessions:
        rows.append(
            (
                str(_value(session, "session_id", None) or ""),
                str(_value(session, "status", None) or ""),
                session_workflow_name(session),
                session_backends_summary(session),
                _picker_timestamp(
                    _value(session, "updated_at", None) or _value(session, "created_at", None)
                ),
                str(_value(session, "workdir", None) or ""),
            )
        )
    # Reserved floors keep the columns from shifting between session lists:
    # the daemon session-id shape, the longest built-in workflow name, and the
    # minute-precision timestamp. Status and backends stay dynamic —
    # statuses are short, and the effective member list varies too much for a
    # useful floor. Wider values (custom ids or workflow names) still grow their
    # column.
    floors = (
        len("daemon-0123456789abcdef"),
        0,
        len("cross-review"),
        0,
        len("2026-07-13 20:38"),
    )
    widths = [
        max(floors[column], len(title), max(len(row[column]) for row in rows))
        for column, title in enumerate(header)
    ]
    header_line = (
        "    " + " ".join(f"{title:<{widths[i]}}" for i, title in enumerate(header)) + " workdir"
    )
    if width > 0 and len(header_line) + 2 + len(PICKER_HEADER_HINTS) <= width:
        padding = width - len(header_line) - len(PICKER_HEADER_HINTS)
        header_line += " " * padding + PICKER_HEADER_HINTS
    lines = [header_line]
    for index, row in enumerate(rows):
        marker = SLASH_SELECTED_MARKER if index == picker.index else " "
        lines.append(
            f"{marker}   "
            + " ".join(f"{row[i]:<{widths[i]}}" for i in range(len(header)))
            + f" {row[5]}"
        )
    return tuple(lines)


def max_scroll_top(total_lines: int, viewport_height: int) -> int:
    return max(0, int(total_lines) - max(0, int(viewport_height)))


def visible_scroll_top(state: ScrollState, total_lines: int, viewport_height: int) -> int:
    if state.follow:
        return max_scroll_top(total_lines, viewport_height)
    return max(0, min(state.top, max_scroll_top(total_lines, viewport_height)))


def clamp_scroll(state: ScrollState, total_lines: int, viewport_height: int) -> ScrollState:
    top = visible_scroll_top(state, total_lines, viewport_height)
    return ScrollState(
        top=top, follow=state.follow and top == max_scroll_top(total_lines, viewport_height)
    )


def scroll_by(
    state: ScrollState, total_lines: int, viewport_height: int, delta: int
) -> ScrollState:
    maximum = max_scroll_top(total_lines, viewport_height)
    current = visible_scroll_top(state, total_lines, viewport_height)
    top = max(0, min(maximum, current + delta))
    return ScrollState(top=top, follow=top >= maximum)


def follow_scroll(total_lines: int, viewport_height: int) -> ScrollState:
    return ScrollState(top=max_scroll_top(total_lines, viewport_height), follow=True)


def ensure_scroll_visible(
    state: ScrollState, row_start: int, row_end: int, total_lines: int, viewport_height: int
) -> ScrollState:
    """Minimally adjust ``state`` so display rows ``[row_start, row_end)`` are
    on screen. Returns a non-following state: selection-anchored views scroll
    with their selection, not the tail."""
    viewport_height = max(1, int(viewport_height))
    top = max(0, min(int(state.top), max_scroll_top(total_lines, viewport_height)))
    if row_start < top:
        top = row_start
    elif row_end > top + viewport_height:
        top = row_end - viewport_height
    top = max(0, min(top, max_scroll_top(total_lines, viewport_height)))
    return ScrollState(top=top, follow=False)


def picker_scroll(
    picker: SessionPickerState, state: ScrollState, width: int, viewport_height: int
) -> ScrollState:
    """Scroll state for the picker overlay that keeps the selected row visible.

    Row spans are computed on the wrapped display lines because picker rows
    (long workdir paths) can wrap on narrow terminals. Tail-follow is never
    right here: it hides the header row and the latest-first top rows —
    including the pre-selected current session. Selecting the first row
    re-pins the top so the header scrolls back into view.
    """
    if not picker.sessions:
        return ScrollState(top=0, follow=False)
    if picker.index == 0:
        return ScrollState(top=0, follow=False)
    lines = format_session_picker_lines(picker, width)
    selected = PICKER_HEADER_LINES + picker.index
    row_start = len(wrap_plain_lines(lines[:selected], width))
    row_end = row_start + len(wrap_plain_lines(lines[selected : selected + 1], width))
    total_lines = row_end + len(wrap_plain_lines(lines[selected + 1 :], width))
    return ensure_scroll_visible(state, row_start, row_end, total_lines, viewport_height)


MENU_TITLE_SOURCE = "menu_title"
MENU_ROW_SOURCE = "menu_row"
MENU_SELECTED_SOURCE = "menu_selected"
# Wizard variant: the rows are answers rather than selectable items, so they
# read as plain text on the fill while the title carries the accent.
MENU_ACCENT_TITLE_SOURCE = "menu_accent_title"
MENU_TEXT_ROW_SOURCE = "menu_text_row"


def picker_menu_lines(lines: Sequence[str], width: int) -> Tuple[TranscriptLine, ...]:
    """Wrap menu-block lines and tag each wrapped row with a menu role.

    Shared by the session picker and the /new wizard: line 0 is the
    band-styled header, a ``▸``-marked row gets the selected bar — wrapped
    continuations included — and every other row reads accent-on-fill like a
    palette item.
    """
    tagged = []
    for index, line in enumerate(lines):
        if index == 0:
            source = MENU_TITLE_SOURCE
        elif line.startswith(SLASH_SELECTED_MARKER):
            source = MENU_SELECTED_SOURCE
        else:
            source = MENU_ROW_SOURCE
        for wrapped_index, text in enumerate(wrap_plain_lines((line,), width)):
            tagged.append(TranscriptLine(source=source, text=text, continuation=wrapped_index > 0))
    return tuple(tagged)


def wizard_menu_lines(lines: Sequence[str], width: int) -> Tuple[TranscriptLine, ...]:
    """Wrap /new wizard lines and tag each row with its menu role.

    Line 0 is the accent band title. A ``▸``-marked row is the highlighted
    choice; a marker-column row (leading space) is an unselected choice and
    reads accent-on-fill like a palette item; everything else (answers,
    questions) reads as plain text on the fill.
    """
    tagged = []
    for index, line in enumerate(lines):
        if index == 0:
            source = MENU_ACCENT_TITLE_SOURCE
        elif line.startswith(SLASH_SELECTED_MARKER):
            source = MENU_SELECTED_SOURCE
        elif line.startswith(" "):
            source = MENU_ROW_SOURCE
        else:
            source = MENU_TEXT_ROW_SOURCE
        for wrapped_index, text in enumerate(wrap_plain_lines((line,), width)):
            tagged.append(TranscriptLine(source=source, text=text, continuation=wrapped_index > 0))
    return tuple(tagged)


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
    next_cursor = max(0, int(cursor))
    if next_cursor <= state.cursor:
        return state, False
    return CursorState(session_id=state.session_id, cursor=next_cursor, epoch=state.epoch), True


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
    members: Optional[Mapping[str, str]] = None,
) -> dict:
    task = task.strip()
    workflow = workflow.strip()
    if not task:
        raise ValueError("task is required")
    if not workflow:
        raise ValueError("workflow is required")
    root = Path(workdir or ".").expanduser().resolve()
    payload = {
        "task": task,
        "workflow": workflow,
        "workdir": str(root),
        "max_turns": int(max_turns),
        "timeout": int(timeout),
        "mock": bool(mock),
        "dry_run": bool(dry_run),
        "interactive": bool(interactive),
        "interactive_idle_timeout": float(interactive_idle_timeout),
        "backend_options": {key: dict(value) for key, value in (backend_options or {}).items()},
    }
    # Only a real substitution goes on the wire; Enter-through-defaults keeps
    # the payload identical to a start without member selection.
    if members:
        payload["members"] = {str(slot): str(agent_id) for slot, agent_id in members.items()}
    return payload


def workflow_ids_from_options(options: Mapping[str, Any]) -> Tuple[str, ...]:
    workflows = options.get("workflows") if isinstance(options.get("workflows"), Sequence) else []
    ids = []
    for workflow in workflows:
        if isinstance(workflow, Mapping) and workflow.get("id"):
            ids.append(str(workflow["id"]))
    return tuple(ids)


def parallel_workflow_ids_from_options(options: Mapping[str, Any]) -> Tuple[str, ...]:
    """Ids of workflows with a ``parallel`` member list (never interactive)."""
    workflows = options.get("workflows") if isinstance(options.get("workflows"), Sequence) else []
    ids = []
    for workflow in workflows:
        if isinstance(workflow, Mapping) and workflow.get("id") and workflow.get("parallel"):
            ids.append(str(workflow["id"]))
    return tuple(ids)


def member_slots_from_options(options: Mapping[str, Any]) -> dict:
    """Per-workflow member-selection slots from a discovery payload.

    Returns ``{workflow_id: [{"slot", "default", "eligible_members"}, ...]}``.
    A daemon without ``member_selection`` support yields an empty map, which
    makes the wizard skip its backends step entirely (today's behavior).
    """

    workflows = options.get("workflows") if isinstance(options.get("workflows"), Sequence) else []
    result: dict = {}
    for workflow in workflows:
        if not isinstance(workflow, Mapping) or not workflow.get("id"):
            continue
        selection = workflow.get("member_selection")
        if not isinstance(selection, Mapping):
            continue
        raw_slots = selection.get("slots")
        if not isinstance(raw_slots, Sequence):
            continue
        slots = []
        for raw in raw_slots:
            if not isinstance(raw, Mapping) or not raw.get("slot"):
                continue
            eligible = raw.get("eligible_members")
            slots.append(
                {
                    "slot": str(raw["slot"]),
                    "default": str(raw.get("default") or raw["slot"]),
                    "eligible_members": [str(item) for item in eligible]
                    if isinstance(eligible, Sequence) and not isinstance(eligible, str)
                    else [],
                }
            )
        if slots and all(slot["eligible_members"] for slot in slots):
            result[str(workflow["id"])] = slots
    return result


# ---------------------------------------------------------------------------
# Calm-TUI color infrastructure (David AI mapping)
#
# ``curses`` cannot do truecolor, so brand hues degrade truecolor -> xterm-256
# -> the 8-color source labels. These helpers are pure so unknown providers
# degrade sanely and the three known brand hexes are pinned by test.
# ---------------------------------------------------------------------------

ACCENT_XTERM256 = 37
ACCENT_ANSI8 = 6  # curses.COLOR_CYAN

_CUBE_STEPS = (0, 95, 135, 175, 215, 255)
INFO_SEPARATOR = " · "


def _hex_to_rgb(hex_str: Any) -> Tuple[int, int, int]:
    text = str(hex_str or "").strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected a '#RRGGBB' hex, got {hex_str!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _nearest_cube_index(value: int) -> int:
    return min(range(len(_CUBE_STEPS)), key=lambda i: abs(_CUBE_STEPS[i] - value))


def xterm256_from_hex(hex_str: Any) -> int:
    """Nearest xterm-256 cell (6x6x6 cube or grayscale ramp) for ``hex_str``.

    The three known brand hexes pin to 173/36/69 (asserted in tests); unknown
    providers still get a sane nearest match.
    """
    r, g, b = _hex_to_rgb(hex_str)
    ri, gi, bi = _nearest_cube_index(r), _nearest_cube_index(g), _nearest_cube_index(b)
    cube_index = 16 + 36 * ri + 6 * gi + bi
    cube_rgb = (_CUBE_STEPS[ri], _CUBE_STEPS[gi], _CUBE_STEPS[bi])

    gray_step = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    gray_value = 8 + 10 * gray_step
    gray_index = 232 + gray_step

    def _dist(a: Tuple[int, int, int], b_: Tuple[int, int, int]) -> int:
        return sum((x - y) ** 2 for x, y in zip(a, b_))

    if _dist(cube_rgb, (r, g, b)) <= _dist((gray_value, gray_value, gray_value), (r, g, b)):
        return cube_index
    return gray_index


def ansi8_from_hex(hex_str: Any) -> int:
    """Nearest of the 8 basic ANSI colors for ``hex_str``.

    Channel bit-threshold reduction; the threshold is tuned so the four design
    brand hues land on their table cells (claude red, codex green, antigravity
    blue, teal cyan). Very dark hues fall back to white so they stay visible.
    """
    r, g, b = _hex_to_rgb(hex_str)
    bits = (1 if r >= 140 else 0) | (2 if g >= 140 else 0) | (4 if b >= 140 else 0)
    return bits or 7  # COLOR_WHITE


# ---------------------------------------------------------------------------
# Session-info line (region 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentInfo:
    name: str
    type: str = ""
    model: str = ""
    backend: str = ""
    brand_color: Optional[str] = None


@dataclass(frozen=True)
class InfoSegment:
    text: str
    role: str
    brand_color: Optional[str] = None


def info_agents_from_session(session: Any) -> Tuple[AgentInfo, ...]:
    """Per-agent info in workflow-sequence order (settings.agents insertion)."""
    raw_settings = _value(session, "settings", None)
    settings = raw_settings if isinstance(raw_settings, Mapping) else {}
    agents = settings.get("agents") if isinstance(settings.get("agents"), Mapping) else {}
    result = []
    for agent_id, agent in agents.items():
        if not isinstance(agent, Mapping):
            result.append(AgentInfo(name=str(agent_id)))
            continue
        brand = agent.get("brand_color")
        result.append(
            AgentInfo(
                name=str(agent_id),
                type=str(agent.get("type") or ""),
                model=str(agent.get("model") or ""),
                backend=str(agent.get("backend") or ""),
                brand_color=brand if isinstance(brand, str) and brand else None,
            )
        )
    return tuple(result)


def _ellipsize(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _segments_width(segments: Sequence[InfoSegment]) -> int:
    return sum(len(segment.text) for segment in segments)


def _agent_chip_segments(agent: AgentInfo) -> Tuple[InfoSegment, ...]:
    """One agent chip: canonical backend name, then model (``codex_cli: gpt-5``).

    ``{type}_{backend}`` is the registry's canonical backend name and doubles
    as the agent label in the common case where the agent id equals its type.
    A custom agent id stays in front so workflow identity is never lost
    (``reviewer codex_cli: gpt-5``) — unless it equals the canonical name
    itself (an ``antigravity_cli`` agent id would print twice); agents without
    a backend (mock) fall back to the bare id.
    """
    segments: list = []
    if agent.type and agent.backend:
        canonical = f"{agent.type}_{agent.backend}"
        if agent.name and agent.name not in (agent.type, canonical):
            segments.append(InfoSegment(agent.name, "agent", agent.brand_color))
            segments.append(InfoSegment(f" {canonical}", "backend"))
        else:
            segments.append(InfoSegment(canonical, "agent", agent.brand_color))
    else:
        segments.append(InfoSegment(agent.name, "agent", agent.brand_color))
    if agent.model:
        segments.append(InfoSegment(f": {agent.model}", "model"))
    return tuple(segments)


def build_context_agent_segments(
    agents: Sequence[AgentInfo], width: int
) -> Tuple[InfoSegment, ...]:
    """Compose the agent cluster shown right-aligned on the context line.

    Each agent renders as ``{type}_{backend}: {model}`` — the canonical
    backend name is always shown so the session's backend is never ambiguous.
    On overflow the
    right-most agents drop first (the lead survives longest); if even the lead
    alone does not fit the cluster disappears rather than ellipsizing, keeping
    the context row calm.
    """
    keep = list(agents)
    while keep:
        segments: list = []
        for agent in keep:
            if segments:
                segments.append(InfoSegment(INFO_SEPARATOR, "sep"))
            segments.extend(_agent_chip_segments(agent))
        if _segments_width(segments) <= width:
            return tuple(segments)
        keep.pop()
    return ()


def build_info_line_segments(
    task: Any,
    workflow: Any,
    width: int,
) -> Tuple[InfoSegment, ...]:
    """Compose the session-info line (task · workflow) with truncation.

    The agent cluster lives on the context line (see
    ``build_context_agent_segments``), leaving this row to the task. On
    overflow the workflow drops first, then the task ellipsizes (never
    dropped). Workdir/project lives on the context line and is truncated there.
    """
    task = str(task or "")
    workflow = str(workflow or "")
    if not task and not workflow:
        return (InfoSegment("no active session", "placeholder"),)

    def _assemble(include_workflow: bool, task_text: str) -> list:
        segments: list = []
        if task_text:
            segments.append(InfoSegment(task_text, "task"))
        if include_workflow and workflow:
            if segments:
                segments.append(InfoSegment(INFO_SEPARATOR, "sep"))
            segments.append(InfoSegment(workflow, "workflow"))
        return segments

    segments = _assemble(True, task)
    if _segments_width(segments) <= width:
        return tuple(segments)

    segments = _assemble(False, task)
    if _segments_width(segments) <= width or not task:
        return tuple(segments)

    return tuple(_assemble(False, _ellipsize(task, width)))


# ---------------------------------------------------------------------------
# Context line (region 1)
# ---------------------------------------------------------------------------


def abbreviate_path(path: Any) -> str:
    text = str(path or "")
    if not text:
        return ""
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~" + text[len(home) :]
    return text


def format_context_line(workdir: Any, branch: Any = None) -> str:
    """``workdir: <path> (<branch>)`` — explicit label; branch only when known."""
    path = abbreviate_path(workdir)
    if not path:
        return "agent-collab"
    branch = str(branch or "").strip()
    return f"workdir: {path} ({branch})" if branch else f"workdir: {path}"


def git_branch(workdir: Any) -> Optional[str]:
    """Best-effort current git branch from ``<workdir>/.git/HEAD`` (no subprocess)."""
    try:
        content = (Path(str(workdir)) / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    prefix = "ref: refs/heads/"
    if content.startswith(prefix):
        return content[len(prefix) :].strip() or None
    return None


# ---------------------------------------------------------------------------
# Input mode chip (region 5)
# ---------------------------------------------------------------------------


def input_mode_chip(
    input_text: Any,
    *,
    new_wizard: bool = False,
    picker_open: bool = False,
    has_session: bool = True,
    accepts_input: bool = True,
) -> str:
    """The right-aligned mode chip inside the input box."""
    if new_wizard:
        return "new session"
    if picker_open:
        return "picking"
    if not has_session:
        return "no session"
    if not accepts_input:
        return "read-only"
    return "referee note"


# ---------------------------------------------------------------------------
# Status/hint line (region 6): hint precedence + overlay selection
# ---------------------------------------------------------------------------


def select_hint(
    *,
    new_wizard_step: Optional[str] = None,
    picker_open: bool = False,
    palette_open: bool = False,
    details_mode: Optional[str] = None,
    overlay_open: bool = False,
    has_session: bool = True,
    read_only: bool = False,
    following: bool = True,
) -> str:
    """Resolve the contextual hint by first-match precedence (region 6)."""
    if new_wizard_step is not None:
        return (
            "Enter start · Esc cancel"
            if new_wizard_step == "workdir"
            else "Enter next · Esc cancel"
        )
    if picker_open:
        return "↑↓ move · Enter open · Esc close"
    if palette_open:
        return "Enter send · Tab complete · Esc close"
    if details_mode == "narrow":
        return "↑↓ scroll · Esc close"
    if details_mode == "wide":
        return "Enter send · / cmds · Esc close"
    if overlay_open:
        return "↑↓ scroll · Esc close"
    if not has_session:
        return "/new start · /help commands · /quit exit"
    if read_only:
        return "↑↓ scroll · /quit exit"
    if not following:
        return "↑↓ scroll · End follow"
    return "Enter send · / cmds"


def format_details_overlay_lines(session: Any) -> Tuple[str, ...]:
    """Details block as shared-overlay body lines (title + detail rows)."""
    return ("details · ↑↓ scroll · Esc close",) + format_session_details(session)


def clip_with_marker(lines: Sequence[str], height: int, marker: str = "…") -> Tuple[str, ...]:
    """Clip ``lines`` to ``height`` rows, marking the last visible row when cut.

    The wide ``/details`` side panel does not scroll (scope simplification): it
    keeps clipping but shows a ``…`` marker so overflow is never silent.
    """
    if height <= 0:
        return ()
    rows = list(lines)
    if len(rows) <= height:
        return tuple(rows)
    visible = rows[:height]
    visible[-1] = marker
    return tuple(visible)


def compose_status_right(activity: str, hint: str) -> str:
    """Compose the right side of the status/hint line (activity then hint).

    Fields join with ``·``; an empty activity (no active session) drops out so
    the hint stands alone.
    """
    return " · ".join(part for part in (str(activity or ""), str(hint or "")) if part)


def overlay_body_lines(
    *,
    picker: Optional[SessionPickerState] = None,
    overlay_lines: Optional[Sequence[str]] = None,
    details_overlay: Optional[Sequence[str]] = None,
) -> Optional[Tuple[str, ...]]:
    """Select the active shared-overlay body content (picker / help / details).

    One component backs all three; this returns whichever overlay is active (or
    ``None`` for the transcript), so the caller renders identical chrome.
    """
    if picker is not None:
        return format_session_picker_lines(picker)
    if overlay_lines is not None:
        return tuple(overlay_lines)
    if details_overlay is not None:
        return tuple(details_overlay)
    return None


_ERROR_MESSAGE_MARKERS = (
    "read-only",
    "unknown",
    "cannot",
    "error",
    "required",
    "invalid",
    "ambiguous",
    "refused",
    "already",
    "no active session",
    "usage:",
    "available only",
)
_SUCCESS_MESSAGE_MARKERS = (
    "sent",
    "asked",
    "queued",
    "opened",
    "refreshed",
    "following",
    "stopped",
    "inserted",
)


def classify_message(message: Any) -> str:
    """Classify the transient message slot as error / success / neutral (color only)."""
    text = str(message or "").lower()
    if not text:
        return "neutral"
    if any(marker in text for marker in _ERROR_MESSAGE_MARKERS):
        return "error"
    if any(text.startswith(marker) for marker in _SUCCESS_MESSAGE_MARKERS):
        return "success"
    return "neutral"


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
