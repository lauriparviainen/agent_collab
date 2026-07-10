from __future__ import annotations

import curses
import locale
import queue
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .api_schema import SessionStateModel
from .client import AgentCollabClient
from .config import DEFAULT_WORKFLOW
from .tui_core import (
    ACCENT_ANSI8,
    ACCENT_XTERM256,
    READ_ONLY_INPUT_MESSAGE,
    CursorState,
    ParsedInput,
    ScrollState,
    SessionPickerState,
    accept_slash_completion,
    advance_cursor_state,
    ansi8_from_hex,
    build_info_line_segments,
    build_new_session_payload,
    clamp_scroll,
    classify_message,
    clip_with_marker,
    compose_status_right,
    directed_entry_state,
    format_activity_indicator,
    format_context_line,
    format_details_overlay_lines,
    follow_scroll,
    format_session_details,
    format_session_picker_lines,
    format_slash_completion_lines,
    format_transcript_events,
    git_branch,
    info_agents_from_session,
    input_mode_chip,
    make_slash_completion,
    make_session_picker,
    move_session_picker,
    move_slash_completion,
    overlay_body_lines,
    parse_input,
    reset_cursor_state,
    scroll_by,
    select_hint,
    select_latest_session_id,
    selected_picker_session_id,
    selected_slash_command,
    session_is_terminal,
    session_workflow_name,
    should_start_poller,
    slash_completion_matches_input,
    visible_scroll_top,
    workflow_ids_from_options,
    wrap_plain_lines,
    wrap_transcript_lines,
    xterm256_from_hex,
)


HELP_LINES = (
    "commands · ↑↓ scroll · Esc close",
    "/help                    show this help",
    "/sessions                pick from daemon sessions",
    "/session SESSION_ID      switch active session",
    "/new                     start a daemon session",
    "/details                 toggle session details",
    "/follow                  jump to tail and resume follow",
    "/refresh                 re-read active session from cursor 0",
    "/stop                    stop active daemon session",
    "/ask AGENT message       ask one directed question",
    "/quit                    exit",
    "",
    "input",
    "#AGENT message           ask one directed question",
    "plain text               append a referee note",
    "",
    "keys",
    "/quit or Ctrl-C exit  q quits read-only views  arrows/page keys scroll",
    "End follow  Esc closes overlays",
)

SOURCE_LABEL_WIDTH = 8
SLASH_MENU_MAX_ROWS = 8

# Box-drawing chrome (rounded when UTF-8 capable, ASCII fallback otherwise).
_BOX_UTF8 = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
_BOX_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}

# David AI xterm-256 cells for the calm palette (foreground unless noted).
_CELL_TEXT = 255
_CELL_MUTED = 250
_CELL_DIM = 245
_CELL_HAIRLINE = 236
_CELL_ERROR = 203
_CELL_BORDER = 59        # warm --border #635441
_CELL_MENU_FILL = 234    # --color-gray-floor
_CELL_MENU_SELECTED = 236  # --color-gray-panel
_CELL_BAND = 237         # --raised


class TuiApp:
    def __init__(
        self,
        stdscr,
        client: AgentCollabClient,
        initial_session_id: Optional[str],
        initial_message: str = "",
    ) -> None:
        self.stdscr = stdscr
        self.client = client
        self.session_id: Optional[str] = None
        self.session: Optional[SessionStateModel] = None
        self.transcript_lines = ()
        self.scroll = ScrollState()
        self.overlay_lines: Optional[Sequence[str]] = None
        self.picker: Optional[SessionPickerState] = None
        self.details_visible = False
        self.input_text = ""
        self.message = initial_message
        self.done = False
        self.cursor_state = CursorState()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.poll_stop: Optional[threading.Event] = None
        self.poll_thread: Optional[threading.Thread] = None
        self.new_wizard: Optional[dict[str, Any]] = None
        self.styles: dict[str, int] = {}
        self.slash_completion_index = 0
        self.slash_completion_dismissed_for: Optional[str] = None
        self._last_status_refresh = 0.0
        self._initial_session_id = initial_session_id
        self.branch: Optional[str] = None
        self.colors_256 = False
        self.utf8 = True
        self._pair_cache: dict[tuple, int] = {}
        self._next_pair = 1
        self._brand_cache: dict[str, int] = {}

    def run(self) -> int:
        self._setup_curses()
        if self._initial_session_id:
            try:
                self.activate_session(self._initial_session_id)
            except Exception as exc:
                self.message = str(exc)
        try:
            while not self.done:
                self._drain_events()
                self._maybe_refresh_status()
                self._render()
                key = self.stdscr.getch()
                if key != -1:
                    self._handle_key(key)
                else:
                    time.sleep(0.03)
        finally:
            self._stop_poller()
        return 0

    def activate_session(self, session_id: str) -> None:
        self._stop_poller()
        session = self.client.get_session(session_id)
        cursor = reset_cursor_state(self.cursor_state, session_id)
        batch = self.client.read_events(session_id, 0)
        cursor, _ = advance_cursor_state(
            cursor,
            session_id=session_id,
            cursor=batch.cursor,
            epoch=cursor.epoch,
        )
        self.cursor_state = cursor
        self.session_id = session_id
        self.session = session
        self.branch = git_branch(session.workdir) if session.workdir else None
        self.transcript_lines = format_transcript_events(batch.events)
        self.scroll = follow_scroll(len(self.transcript_lines), self._body_height())
        self.overlay_lines = None
        self.picker = None
        self.slash_completion_dismissed_for = None
        if session_is_terminal(session):
            self.message = f"session is read-only ({session.status})"
        else:
            self.message = f"opened {session_id}" if _session_accepts_input(session) else "session is read-only (not interactive)"
            self._start_poller()

    def _setup_curses(self) -> None:
        curses.noecho()
        curses.cbreak()
        self.stdscr.keypad(True)
        self.stdscr.timeout(100)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self.utf8 = _terminal_is_utf8()
        has_colors = False
        if curses.has_colors():
            has_colors = True
            try:
                curses.use_default_colors()
            except curses.error:
                pass
        self.colors_256 = has_colors and curses.COLORS >= 256
        self._build_styles(has_colors)

    def _build_styles(self, has_colors: bool) -> None:
        A_BOLD = curses.A_BOLD
        A_DIM = curses.A_DIM
        A_REVERSE = curses.A_REVERSE
        styles: dict[str, int] = {}
        if self.colors_256:
            styles["text"] = self._pair(_CELL_TEXT)
            styles["muted"] = self._pair(_CELL_MUTED)
            styles["dim"] = self._pair(_CELL_DIM)
            styles["accent"] = self._pair(ACCENT_XTERM256)
            styles["hairline"] = self._pair(_CELL_HAIRLINE) | A_DIM
            styles["error"] = self._pair(_CELL_ERROR) | A_BOLD
            styles["border"] = self._pair(_CELL_BORDER)
            styles["band"] = self._pair(_CELL_MUTED, _CELL_BAND)
            styles["menu"] = self._pair(_CELL_TEXT, _CELL_MENU_FILL)
            styles["menu_name"] = self._pair(ACCENT_XTERM256, _CELL_MENU_FILL)
            styles["menu_desc"] = self._pair(_CELL_MUTED, _CELL_MENU_FILL)
            styles["menu_selected"] = self._pair(_CELL_TEXT, _CELL_MENU_SELECTED) | A_BOLD
        elif has_colors:
            styles["text"] = 0
            styles["muted"] = 0
            styles["dim"] = A_DIM
            styles["accent"] = self._pair(ACCENT_ANSI8)
            styles["hairline"] = A_DIM
            styles["error"] = self._pair(curses.COLOR_RED) | A_BOLD
            styles["border"] = A_DIM
            styles["band"] = A_REVERSE
            styles["menu"] = 0
            styles["menu_name"] = self._pair(ACCENT_ANSI8)
            styles["menu_desc"] = 0
            styles["menu_selected"] = A_REVERSE
        else:
            styles["text"] = 0
            styles["muted"] = 0
            styles["dim"] = A_DIM
            styles["accent"] = A_BOLD
            styles["hairline"] = A_DIM
            styles["error"] = A_BOLD
            styles["border"] = A_DIM
            styles["band"] = A_REVERSE
            styles["menu"] = 0
            styles["menu_name"] = A_BOLD
            styles["menu_desc"] = 0
            styles["menu_selected"] = A_REVERSE
        self.styles = styles

    @staticmethod
    def _has_colors() -> bool:
        try:
            return bool(curses.has_colors())
        except curses.error:
            return False

    def _pair(self, fg: int, bg: int = -1) -> int:
        if not self._has_colors():
            return 0
        key = (fg, bg)
        cached = self._pair_cache.get(key)
        if cached is not None:
            return cached
        index = self._next_pair
        if index >= curses.COLOR_PAIRS:
            return 0
        try:
            curses.init_pair(index, fg, bg)
        except curses.error:
            return 0
        self._next_pair += 1
        attr = curses.color_pair(index)
        self._pair_cache[key] = attr
        return attr

    def _brand_style(self, brand_color: Optional[str]) -> int:
        if not brand_color:
            return self._style("accent") | curses.A_BOLD
        cached = self._brand_cache.get(brand_color)
        if cached is not None:
            return cached
        attr = curses.A_BOLD
        try:
            if self.colors_256:
                attr |= self._pair(xterm256_from_hex(brand_color))
            elif self._has_colors():
                attr |= self._pair(ansi8_from_hex(brand_color))
        except ValueError:
            attr = self._style("accent") | curses.A_BOLD
        self._brand_cache[brand_color] = attr
        return attr

    def _start_poller(self) -> None:
        if not should_start_poller(self.session) or not self.session_id:
            return
        self._stop_poller()
        stop = threading.Event()
        state = self.cursor_state
        thread = threading.Thread(
            target=self._poll_loop,
            args=(state.session_id, state.cursor, state.epoch, stop),
            daemon=True,
            name=f"agent-collab-tui-poller-{state.session_id}",
        )
        self.poll_stop = stop
        self.poll_thread = thread
        thread.start()

    def _stop_poller(self) -> None:
        if self.poll_stop is not None:
            self.poll_stop.set()
        thread = self.poll_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.2)
        self.poll_stop = None
        self.poll_thread = None

    def _poll_loop(self, session_id: Optional[str], cursor: int, epoch: int, stop: threading.Event) -> None:
        if not session_id:
            return
        current = cursor
        while not stop.is_set():
            try:
                batch = self.client.wait_events(session_id, current, 750)
            except Exception as exc:
                self.events.put(("error", epoch, session_id, str(exc)))
                return
            if stop.is_set():
                return
            self.events.put(("batch", epoch, session_id, batch))
            current = int(batch.cursor)
            if not batch.events:
                try:
                    session = self.client.get_session(session_id)
                except Exception as exc:
                    self.events.put(("error", epoch, session_id, str(exc)))
                    return
                self.events.put(("status", epoch, session_id, session))
                if session_is_terminal(session):
                    return

    def _drain_events(self) -> None:
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            if kind == "batch":
                _, epoch, session_id, batch = item
                if not self._current_epoch(session_id, epoch):
                    continue
                self.cursor_state, accepted = advance_cursor_state(
                    self.cursor_state,
                    session_id=session_id,
                    cursor=batch.cursor,
                    epoch=epoch,
                )
                if not accepted:
                    continue
                events = batch.events
                if events:
                    self.transcript_lines = self.transcript_lines + format_transcript_events(events)
                    self.scroll = clamp_scroll(self.scroll, len(self.transcript_lines), self._body_height())
                    self._refresh_session_status()
            elif kind == "status":
                _, epoch, session_id, session = item
                if not self._current_epoch(session_id, epoch):
                    continue
                self.session = session
                if session_is_terminal(session):
                    self.message = f"session is read-only ({session.status})"
                    self._stop_poller()
            elif kind == "error":
                _, epoch, session_id, message = item
                if not self._current_epoch(session_id, epoch):
                    continue
                self.message = message
                self._stop_poller()

    def _current_epoch(self, session_id: str, epoch: int) -> bool:
        return self.cursor_state.session_id == session_id and self.cursor_state.epoch == epoch

    def _maybe_refresh_status(self) -> None:
        if not self.session_id:
            return
        if self.poll_thread is not None and self.poll_thread.is_alive():
            return
        now = time.monotonic()
        if now - self._last_status_refresh < 3.0:
            return
        self._last_status_refresh = now
        self._refresh_session_status()

    def _refresh_session_status(self) -> None:
        if not self.session_id:
            return
        try:
            self.session = self.client.get_session(self.session_id)
        except Exception as exc:
            self.message = str(exc)
            return
        if self.session and session_is_terminal(self.session):
            self._stop_poller()

    def _handle_key(self, key: int) -> None:
        if key in (3,):
            self.done = True
            return
        if key == 27:
            if self._current_slash_completion() is not None:
                self.slash_completion_dismissed_for = self.input_text
                return
            entry = directed_entry_state(self.input_text)
            if entry is not None and entry.awaiting_arg:
                # Approved interaction change: cancel directed argument-entry mode
                # back to referee mode, clearing the rail.
                self._set_input_text("")
                return
            self.overlay_lines = None
            self.picker = None
            if self.details_visible:
                # Approved interaction change: Esc dismisses /details, matching
                # how it already closes the palette and picker.
                self.details_visible = False
            if self.new_wizard:
                self.new_wizard = None
                self.message = "new session cancelled"
            return
        if self.picker is not None:
            if key in (curses.KEY_UP, ord("k")):
                self.picker = move_session_picker(self.picker, -1)
                return
            if key in (curses.KEY_DOWN, ord("j")):
                self.picker = move_session_picker(self.picker, 1)
                return
            if key in (curses.KEY_NPAGE,):
                self.picker = move_session_picker(self.picker, max(1, self._body_height()))
                return
            if key in (curses.KEY_PPAGE,):
                self.picker = move_session_picker(self.picker, -max(1, self._body_height()))
                return
            if key in (10, 13):
                selected = selected_picker_session_id(self.picker)
                if selected:
                    try:
                        self.activate_session(selected)
                    except Exception as exc:
                        self.message = str(exc)
                return

        completion = self._current_slash_completion()
        if completion is not None and completion.matches:
            if key in (curses.KEY_UP,):
                self._set_slash_completion(move_slash_completion(completion, -1))
                return
            if key in (curses.KEY_DOWN,):
                self._set_slash_completion(move_slash_completion(completion, 1))
                return
            if key in (10, 13) and slash_completion_matches_input(self.input_text, completion):
                self._submit_input()
                return
            if key in (9, 10, 13):
                self._accept_slash_completion(completion)
                return

        if key == ord("q") and self._q_quits():
            self.done = True
            return
        if key in (curses.KEY_UP,):
            self.scroll = scroll_by(self.scroll, len(self._active_body_lines()), self._body_height(), -1)
            return
        if key in (curses.KEY_DOWN,):
            self.scroll = scroll_by(self.scroll, len(self._active_body_lines()), self._body_height(), 1)
            return
        if key in (curses.KEY_PPAGE,):
            self.scroll = scroll_by(self.scroll, len(self._active_body_lines()), self._body_height(), -max(1, self._body_height()))
            return
        if key in (curses.KEY_NPAGE,):
            self.scroll = scroll_by(self.scroll, len(self._active_body_lines()), self._body_height(), max(1, self._body_height()))
            return
        if key in (curses.KEY_END,):
            self.scroll = follow_scroll(len(self._active_body_lines()), self._body_height())
            return
        if key in (10, 13):
            self._submit_input()
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self._set_input_text(self.input_text[:-1])
            return
        if key == 21:
            self._set_input_text("")
            return
        if 0 <= key <= 255:
            char = chr(key)
            if char.isprintable():
                self._set_input_text(self.input_text + char)

    def _q_quits(self) -> bool:
        """Single-key ``q`` quits only where typing is meaningless.

        In a live interactive session every printable key — including ``q`` —
        belongs to the input rail (quit via /quit or Ctrl-C). ``q`` stays a
        one-key exit for viewer states: no session, or a session that cannot
        accept referee input (terminal or non-interactive).
        """
        if self.input_text or self.new_wizard:
            return False
        if not self.session:
            return True
        return not _session_accepts_input(self.session)

    def _submit_input(self) -> None:
        raw = self.input_text
        self._set_input_text("")
        if self.new_wizard:
            self._advance_new_wizard(raw)
            return
        parsed = parse_input(raw)
        self._dispatch(parsed)

    def _set_input_text(self, value: str) -> None:
        if value == self.input_text:
            return
        self.input_text = value
        self.slash_completion_dismissed_for = None
        if not value.startswith("/"):
            self.slash_completion_index = 0

    def _current_slash_completion(self):
        if self.new_wizard or self.picker is not None:
            return None
        if self.input_text and self.input_text == self.slash_completion_dismissed_for:
            return None
        return make_slash_completion(self.input_text, self.slash_completion_index)

    def _set_slash_completion(self, completion) -> None:
        self.slash_completion_index = completion.index

    def _accept_slash_completion(self, completion) -> None:
        selected = selected_slash_command(completion)
        if not selected:
            return
        self._set_input_text(accept_slash_completion(self.input_text, completion))
        self.slash_completion_index = 0
        self.message = f"inserted {selected}"

    def _dispatch(self, parsed: ParsedInput) -> None:
        if parsed.kind == "empty":
            return
        self.overlay_lines = None
        self.picker = None
        if parsed.kind == "invalid":
            self.message = parsed.error or "invalid input"
            return
        if parsed.kind == "text":
            self._post_referee_message(parsed.text)
            return
        if parsed.kind == "directed":
            self._handle_directed_input(parsed)
            return
        if parsed.kind != "slash":
            return
        command = parsed.command
        if command == "help":
            self.overlay_lines = HELP_LINES
            self.scroll = follow_scroll(len(HELP_LINES), self._body_height())
        elif command == "sessions":
            self._open_session_picker()
        elif command == "session":
            if not parsed.args:
                self.message = "usage: /session SESSION_ID"
                return
            try:
                self.activate_session(parsed.args[0])
            except Exception as exc:
                self.message = str(exc)
        elif command == "new":
            self._start_new_wizard()
        elif command == "details":
            self.details_visible = not self.details_visible
            self._refresh_session_status()
        elif command == "follow":
            self.scroll = follow_scroll(len(self._active_body_lines()), self._body_height())
            self.message = "following"
        elif command == "refresh":
            if not self.session_id:
                self.message = "no active session"
                return
            try:
                self.activate_session(self.session_id)
                self.message = "refreshed"
            except Exception as exc:
                self.message = str(exc)
        elif command == "stop":
            if not self.session_id:
                self.message = "no active session"
                return
            if self.session and session_is_terminal(self.session):
                self.message = f"session already {self.session.status}"
                return
            try:
                self.session = self.client.stop_session(self.session_id)
                self._stop_poller()
                self.message = f"stopped {self.session_id}"
            except Exception as exc:
                self.message = str(exc)
        elif command == "quit":
            self.done = True

    def _handle_directed_input(self, parsed: ParsedInput) -> None:
        self._post_referee_message(parsed.message, target=parsed.agent)

    def _post_referee_message(self, text: str, target: Optional[str] = None) -> None:
        if not self.session_id or not self.session:
            self.message = "no active session"
            return
        if not _session_accepts_input(self.session):
            status = self.session.status
            self.message = f"session is read-only ({status})" if session_is_terminal(self.session) else READ_ONLY_INPUT_MESSAGE
            return
        self._stop_poller()
        try:
            batch = self.client.post_message(self.session_id, text, source="referee", target=target)
            self.cursor_state = CursorState(
                session_id=self.session_id,
                cursor=max(0, int(batch.cursor)),
                epoch=self.cursor_state.epoch + 1,
            )
            events = batch.events
            if events:
                self.transcript_lines = self.transcript_lines + format_transcript_events(events)
                self.scroll = follow_scroll(len(self.transcript_lines), self._body_height())
            raw = events[0].raw if events else None
            queued = isinstance(raw, Mapping) and bool(raw.get("queued"))
            resolved = raw.get("resolved_target") if isinstance(raw, Mapping) else None
            if resolved:
                self.message = f"queued for {resolved}" if queued else f"asked {resolved}"
            else:
                self.message = "queued note" if queued else "sent note"
            self._refresh_session_status()
        except Exception as exc:
            self.message = str(exc)
        finally:
            if should_start_poller(self.session):
                self._start_poller()

    def _open_session_picker(self) -> None:
        try:
            sessions = self.client.list_sessions().sessions
        except Exception as exc:
            self.message = str(exc)
            return
        self.picker = make_session_picker(sessions, self.session_id)
        self.overlay_lines = None
        self.scroll = follow_scroll(len(format_session_picker_lines(self.picker)), self._body_height())

    def _start_new_wizard(self) -> None:
        self.new_wizard = {"step": "task", "task": "", "workflow": "", "workdir": ""}
        self._set_input_text("")
        self.overlay_lines = ("new session", "enter task")
        self.picker = None
        self.message = "new session task"

    def _advance_new_wizard(self, value: str) -> None:
        wizard = self.new_wizard
        if wizard is None:
            return
        value = value.strip()
        step = wizard["step"]
        if step == "task":
            if not value:
                self.message = "task is required"
                return
            wizard["task"] = value
            workdir = str(self._default_workdir())
            options = self._describe_options_for_workdir(workdir)
            workflows = workflow_ids_from_options(options)
            default_workflow = self._default_workflow(workflows)
            wizard["step"] = "workflow"
            wizard["default_workflow"] = default_workflow
            self.overlay_lines = (
                "new session",
                f"task: {wizard['task']}",
                f"workflow [{default_workflow}]",
                f"choices: {', '.join(workflows) if workflows else '(daemon default)'}",
            )
            self.message = f"workflow [{default_workflow}]"
            return
        if step == "workflow":
            workflow = value or str(wizard.get("default_workflow") or DEFAULT_WORKFLOW)
            wizard["workflow"] = workflow
            default_workdir = str(self._default_workdir())
            wizard["default_workdir"] = default_workdir
            wizard["step"] = "workdir"
            self.overlay_lines = (
                "new session",
                f"task: {wizard['task']}",
                f"workflow: {workflow}",
                f"workdir [{default_workdir}]",
            )
            self.message = f"workdir [{default_workdir}]"
            return
        if step == "workdir":
            workdir = value or str(wizard.get("default_workdir") or self._default_workdir())
            try:
                options = self._describe_options_for_workdir(workdir)
                workflows = workflow_ids_from_options(options)
                if workflows and wizard["workflow"] not in workflows:
                    wizard["step"] = "workflow"
                    wizard["default_workflow"] = workflows[0]
                    self.message = f"unknown workflow {wizard['workflow']!r}; choices: {', '.join(workflows)}"
                    return
                payload = build_new_session_payload(
                    task=wizard["task"],
                    workflow=wizard["workflow"],
                    workdir=workdir,
                    interactive=True,
                )
                result = self.client.start_session(payload)
                self.new_wizard = None
                self.overlay_lines = None
                self.activate_session(result.session_id)
            except Exception as exc:
                self.message = str(exc)

    def _describe_options_for_workdir(self, workdir: str) -> Mapping[str, Any]:
        return self.client.describe_options({"workdir": str(Path(workdir).expanduser().resolve())})

    def _default_workflow(self, workflows: Sequence[str]) -> str:
        current = session_workflow_name(self.session) if self.session else ""
        if current and (not workflows or current in workflows):
            return current
        if DEFAULT_WORKFLOW in workflows:
            return DEFAULT_WORKFLOW
        return workflows[0] if workflows else DEFAULT_WORKFLOW

    def _default_workdir(self) -> Path:
        if self.session and self.session.workdir:
            return Path(self.session.workdir).expanduser().resolve()
        return Path(".").expanduser().resolve()

    def _render(self) -> None:
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()
        if height < 5 or width < 20:
            self._add(0, 0, "terminal too small", max(0, width - 1), self._style("dim"))
            self.stdscr.refresh()
            return

        details_width = self._details_width(width)
        transcript_width = width - details_width - (1 if details_width else 0)

        # Region 1-2: quiet context line + legible session-info line, hairline.
        self._render_context_line(0, width)
        self._render_info_line(1, width)
        self._add(2, 0, self._hairline(width), width, self._style("hairline"))

        body_top = 3
        body_height = self._body_height()
        body_lines = self._active_body_lines(transcript_width)
        self.scroll = clamp_scroll(self.scroll, len(body_lines), body_height)
        start = visible_scroll_top(self.scroll, len(body_lines), body_height)
        for row, line in enumerate(body_lines[start : start + body_height], start=body_top):
            self._render_body_line(row, line, transcript_width)

        if details_width:
            self._render_details_panel(body_top, body_height, transcript_width, details_width)

        self._render_slash_completion(body_top, body_height, transcript_width)

        # Region 5-6: bordered input box (3 rows) + status/hint line.
        box_top = height - 4
        self._render_input_box(box_top, width)
        self._render_status_line(height - 1, width)
        self.stdscr.refresh()

    def _hairline(self, width: int) -> str:
        return (_BOX_UTF8["h"] if self.utf8 else _BOX_ASCII["h"]) * width

    def _render_details_panel(self, body_top: int, body_height: int, transcript_width: int, details_width: int) -> None:
        separator_x = transcript_width
        vertical = _BOX_UTF8["v"] if self.utf8 else _BOX_ASCII["v"]
        for row in range(body_top, body_top + body_height):
            self._add(row, separator_x, vertical, 1, self._style("hairline"))
        detail_lines = wrap_plain_lines(
            format_session_details(self.session) if self.session else (),
            details_width - 2,
        )
        visible = clip_with_marker(detail_lines, body_height)
        for index, line in enumerate(visible):
            self._add(body_top + index, separator_x + 1, line, details_width - 2, self._style("muted"))

    def _render_body_line(self, row: int, line, width: int) -> None:
        if line.source == "ui":
            # Shared scrollable overlay (picker / help / narrow details): the
            # first line is a title/hint, the rest is body content.
            style = self._style("dim") if row == 3 else self._style("text")
            self._add(row, 0, line.text, width, style)
            return
        if line.continuation:
            self._add(row, 0, line.text, width, self._style("text"))
            return

        band = line.source in ("referee", "human")
        attr = self._style_for_source(line.source)
        if band:
            # Raised band for referee/human, right-aligned timestamp.
            self._add(row, 0, " " * width, width, self._style("band"))
            self._add(row, 0, line.text, width, self._style("band"))
            if line.timestamp and width > len(line.timestamp) + 1:
                self._add(row, width - len(line.timestamp), line.timestamp, len(line.timestamp), self._style("band"))
            return
        if len(line.text) <= SOURCE_LABEL_WIDTH:
            self._add(row, 0, line.text, width, attr)
            return
        label_width = min(width, SOURCE_LABEL_WIDTH)
        self._add(row, 0, line.text[:label_width], label_width, attr)
        if width > SOURCE_LABEL_WIDTH:
            # Tool summaries stay dim across the whole row; other bodies read in text.
            body_attr = self._style("dim") if line.source == "tool" else self._style("text")
            self._add(row, SOURCE_LABEL_WIDTH, line.text[SOURCE_LABEL_WIDTH:], width - SOURCE_LABEL_WIDTH, body_attr)

    def _render_slash_completion(self, body_top: int, body_height: int, width: int) -> None:
        completion = self._current_slash_completion()
        if completion is None:
            return
        max_rows = max(1, min(SLASH_MENU_MAX_ROWS, body_height))
        lines = format_slash_completion_lines(completion, max_items=max_rows)[:max_rows]
        start_y = body_top + body_height - len(lines)
        for offset, line in enumerate(lines):
            y = start_y + offset
            selected = line.startswith("▸")
            fill = self._style("menu_selected") if selected else self._style("menu")
            self._add(y, 0, " " * width, width, fill)
            if selected:
                self._add(y, 0, line, width, fill)
            else:
                # marker + name in accent, description in muted, on the gray fill.
                self._add(y, 0, line, width, self._style("menu_name"))

    def _render_context_line(self, y: int, width: int) -> None:
        workdir = self.session.workdir if self.session else ""
        text = format_context_line(workdir, self.branch)
        self._add(y, 1, text, max(0, width - 1), self._style("dim"))

    def _render_info_line(self, y: int, width: int) -> None:
        if self.session:
            task = self.session.task
            agents = info_agents_from_session(self.session)
            workflow = session_workflow_name(self.session)
        else:
            task = ""
            agents = ()
            workflow = ""
        segments = build_info_line_segments(task, agents, workflow, max(1, width - 1))
        x = 1
        for segment in segments:
            if x >= width - 1:
                break
            attr = self._info_segment_style(segment.role, segment.brand_color)
            self._add(y, x, segment.text, width - 1 - x, attr)
            x += len(segment.text)

    def _info_segment_style(self, role: str, brand_color: Optional[str]) -> int:
        if role == "agent":
            return self._brand_style(brand_color)
        if role in ("model", "task", "workflow"):
            return self._style("muted")
        if role in ("backend", "sep", "placeholder"):
            return self._style("dim")
        return self._style("muted")

    def _render_input_box(self, box_top: int, width: int) -> None:
        box = _BOX_UTF8 if self.utf8 else _BOX_ASCII
        border = self._style("border")
        inner = max(0, width - 2)
        self._add(box_top, 0, box["tl"] + box["h"] * inner + box["tr"], width, border)
        self._add(box_top + 2, 0, box["bl"] + box["h"] * inner + box["br"], width, border)

        mid = box_top + 1
        self._add(mid, 0, box["v"], 1, border)
        self._add(mid, width - 1, box["v"], 1, border)

        content_x = 2
        content_width = max(1, width - 4)
        chip = self._input_chip()
        chip_style = self._chip_style(chip)
        prompt = "> "
        typed = prompt + self.input_text
        # Right-aligned chip inside the box; keep room between text and chip.
        chip_x = width - 2 - len(chip)
        if chip and chip_x > content_x + len(typed):
            self._add(mid, chip_x, chip, len(chip), chip_style)
            text_width = chip_x - content_x - 1
        else:
            text_width = content_width
        self._add(mid, content_x, typed, max(0, text_width), self._style("text"))
        cursor_x = min(content_x + max(0, text_width), content_x + len(typed))
        try:
            self.stdscr.move(mid, max(content_x, min(width - 2, cursor_x)))
        except curses.error:
            pass

    def _input_chip(self) -> str:
        return input_mode_chip(
            self.input_text,
            new_wizard=bool(self.new_wizard),
            picker_open=self.picker is not None,
            has_session=self.session is not None,
            accepts_input=bool(self.session and _session_accepts_input(self.session)),
        )

    def _chip_style(self, chip: str) -> int:
        if chip in ("read-only", "no session", "picking"):
            return self._style("dim")
        return self._style("accent")

    def _render_status_line(self, y: int, width: int) -> None:
        entry = directed_entry_state(self.input_text)
        if entry is not None and entry.awaiting_arg:
            # Live guidance while entering a directed argument (not an error).
            message = entry.usage_hint
            message_style = self._style("muted")
        else:
            message = self.message
            message_style = self._message_style(message)

        activity = format_activity_indicator(
            self.session, int(time.monotonic() * 4), utf8=self.utf8
        ) if self.session else ""
        hint = self._select_hint(width)
        right = compose_status_right(activity, hint)

        right_x = max(1, width - len(right) - 1)
        self._add(y, right_x, right, width - right_x, self._style("dim"))
        self._add(y, 1, message, max(0, right_x - 2), message_style)

    def _message_style(self, message: str) -> int:
        classification = classify_message(message)
        if classification == "error":
            return self._style("error")
        if classification == "success":
            return self._style("accent")
        return self._style("muted")

    def _select_hint(self, width: int) -> str:
        step = self.new_wizard.get("step") if self.new_wizard else None
        palette_open = self._current_slash_completion() is not None
        details_mode = self._details_mode(width)
        read_only = bool(self.session) and not _session_accepts_input(self.session)
        overlay_open = self.overlay_lines is not None
        return select_hint(
            new_wizard_step=step,
            picker_open=self.picker is not None,
            palette_open=palette_open,
            details_mode=details_mode,
            overlay_open=overlay_open,
            has_session=self.session is not None,
            read_only=read_only,
            following=self.scroll.follow,
        )

    def _active_body_lines(self, width: Optional[int] = None):
        if width is None:
            width = self._transcript_width()
        _, screen_width = self.stdscr.getmaxyx()
        details_overlay = None
        if self._details_mode(screen_width) == "narrow" and self.session:
            details_overlay = format_details_overlay_lines(self.session)
        overlay = overlay_body_lines(
            picker=self.picker,
            overlay_lines=self.overlay_lines,
            details_overlay=details_overlay,
        )
        if overlay is not None:
            return tuple(_ui_line(line) for line in wrap_plain_lines(overlay, width))
        return wrap_transcript_lines(self.transcript_lines, width)

    def _body_height(self) -> int:
        # Row map: 0 context, 1 info, 2 hairline, body, box top, input, box
        # bottom, status = 3 top rows + 4 bottom rows of chrome.
        height, _ = self.stdscr.getmaxyx()
        return max(1, height - 7)

    def _transcript_width(self) -> int:
        _, width = self.stdscr.getmaxyx()
        details_width = self._details_width(width)
        return max(1, width - details_width - (1 if details_width else 0))

    def _details_mode(self, width: int) -> Optional[str]:
        if not self.details_visible or not self.session:
            return None
        return "wide" if width >= 100 else "narrow"

    def _details_width(self, width: int) -> int:
        if self._details_mode(width) != "wide":
            return 0
        return min(48, max(32, width // 3))

    def _style_for_source(self, source: str) -> int:
        if source in ("referee", "human"):
            return self._style("band")
        if source == "error":
            return self._style("error")
        if source in ("tool", "status"):
            return self._style("dim")
        brand = self._source_brand_color(source)
        if brand is not None:
            return self._brand_style(brand)
        return self._style("text")

    def _source_brand_color(self, source: str) -> Optional[str]:
        for agent in info_agents_from_session(self.session):
            if source in (agent.name, agent.type) and agent.brand_color:
                return agent.brand_color
        return None

    def _style(self, name: str) -> int:
        return self.styles.get(name, 0)

    def _add(self, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        if width <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, width, attr)
        except curses.error:
            pass


def _terminal_is_utf8() -> bool:
    """Best-effort UTF-8 capability check (drives braille spinner + box chars)."""
    try:
        encoding = locale.getpreferredencoding(False) or ""
    except Exception:
        encoding = ""
    return "utf" in encoding.lower()


def _ui_line(text: str):
    from .tui_core import TranscriptLine

    return TranscriptLine(source="ui", text=text)


def _session_accepts_input(session: SessionStateModel) -> bool:
    settings = session.settings if isinstance(session.settings, Mapping) else {}
    interactive = bool(session.interactive or settings.get("interactive"))
    return interactive and not session_is_terminal(session)


def run_tui(session_id: Optional[str] = None, server_url: Optional[str] = None) -> int:
    client = AgentCollabClient(server_url)
    initial_message = ""
    if session_id is None:
        sessions = client.list_sessions().sessions
        if sessions:
            session_id = select_latest_session_id(sessions)
        else:
            initial_message = "no daemon sessions found; use /new"

    def _run(stdscr) -> int:
        return TuiApp(stdscr, client, session_id, initial_message).run()

    return int(curses.wrapper(_run))
