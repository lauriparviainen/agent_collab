from __future__ import annotations

import curses
import queue
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .client import AgentCollabClient
from .config import DEFAULT_WORKFLOW
from .tui_core import (
    READ_ONLY_INPUT_MESSAGE,
    CursorState,
    ParsedInput,
    ScrollState,
    SessionPickerState,
    accept_slash_completion,
    advance_cursor_state,
    build_new_session_payload,
    clamp_scroll,
    format_activity_indicator,
    follow_scroll,
    format_session_details,
    format_session_picker_lines,
    format_slash_completion_lines,
    format_transcript_events,
    make_slash_completion,
    make_session_picker,
    move_session_picker,
    move_slash_completion,
    parse_input,
    reset_cursor_state,
    scroll_by,
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
)


HELP_LINES = (
    "commands",
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
    "q/Ctrl-C quit  arrows/page keys scroll  End follow  Esc closes overlays",
)

SOURCE_LABEL_WIDTH = 8
SLASH_MENU_MAX_ROWS = 8

SOURCE_STYLE_DEFS = (
    ("human", curses.COLOR_CYAN, -1, 0),
    ("referee", curses.COLOR_MAGENTA, -1, 0),
    ("claude", curses.COLOR_WHITE, -1, curses.A_BOLD),
    ("codex", curses.COLOR_GREEN, -1, curses.A_BOLD),
    ("tool", curses.COLOR_YELLOW, -1, 0),
    ("error", curses.COLOR_RED, -1, curses.A_BOLD),
)

UI_STYLE_DEFS = (
    ("chrome", curses.COLOR_WHITE, -1, curses.A_DIM),
    ("menu", curses.COLOR_WHITE, -1, 0),
    ("selection", curses.COLOR_CYAN, -1, curses.A_REVERSE | curses.A_BOLD),
)


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
        self.session: Optional[Mapping[str, Any]] = None
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
            cursor=batch.get("cursor", 0),
            epoch=cursor.epoch,
        )
        self.cursor_state = cursor
        self.session_id = session_id
        self.session = session
        self.transcript_lines = format_transcript_events(batch.get("events", []))
        self.scroll = follow_scroll(len(self.transcript_lines), self._body_height())
        self.overlay_lines = None
        self.picker = None
        self.slash_completion_dismissed_for = None
        if session_is_terminal(session):
            self.message = f"session is read-only ({session.get('status')})"
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
        style_defs = SOURCE_STYLE_DEFS + UI_STYLE_DEFS
        for name, _, _, attrs in style_defs:
            self.styles[name] = attrs
        if curses.has_colors():
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            for index, (name, foreground, background, attrs) in enumerate(style_defs, start=1):
                try:
                    curses.init_pair(index, foreground, background)
                    self.styles[name] = curses.color_pair(index) | attrs
                except curses.error:
                    pass

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
            current = int(batch.get("cursor", current))
            if not batch.get("events"):
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
                    cursor=batch.get("cursor", self.cursor_state.cursor),
                    epoch=epoch,
                )
                if not accepted:
                    continue
                events = batch.get("events", [])
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
                    self.message = f"session is read-only ({session.get('status')})"
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
            self.overlay_lines = None
            self.picker = None
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

        if key == ord("q") and not self.input_text and not self.new_wizard:
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
                self.message = f"session already {self.session.get('status')}"
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
            status = self.session.get("status")
            self.message = f"session is read-only ({status})" if session_is_terminal(self.session) else READ_ONLY_INPUT_MESSAGE
            return
        self._stop_poller()
        try:
            batch = self.client.post_message(self.session_id, text, source="referee", target=target)
            self.cursor_state = CursorState(
                session_id=self.session_id,
                cursor=max(0, int(batch.get("cursor", self.cursor_state.cursor))),
                epoch=self.cursor_state.epoch + 1,
            )
            events = batch.get("events", [])
            if events:
                self.transcript_lines = self.transcript_lines + format_transcript_events(events)
                self.scroll = follow_scroll(len(self.transcript_lines), self._body_height())
            accepted = events[0] if events else {}
            raw = accepted.get("raw") if isinstance(accepted, Mapping) else {}
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
            sessions = self.client.list_sessions().get("sessions", [])
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
                self.activate_session(str(result["session_id"]))
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
        if self.session and self.session.get("workdir"):
            return Path(str(self.session["workdir"])).expanduser().resolve()
        return Path(".").expanduser().resolve()

    def _render(self) -> None:
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()
        if height < 5 or width < 20:
            self._add(0, 0, "terminal too small", max(0, width - 1))
            self.stdscr.refresh()
            return

        details_width = self._details_width(width)
        transcript_width = width - details_width - (1 if details_width else 0)
        self._render_header(width)
        self._add(1, 0, "-" * width, width, self._style("chrome"))

        body_top = 2
        body_height = self._body_height()
        body_lines = self._active_body_lines(transcript_width)
        self.scroll = clamp_scroll(self.scroll, len(body_lines), body_height)
        start = visible_scroll_top(self.scroll, len(body_lines), body_height)
        for row, line in enumerate(body_lines[start : start + body_height], start=body_top):
            self._render_body_line(row, line, transcript_width)

        if details_width:
            separator_x = transcript_width
            for row in range(body_top, body_top + body_height):
                self._add(row, separator_x, "|", 1, self._style("chrome"))
            detail_lines = wrap_plain_lines(format_session_details(self.session or {}), details_width - 2)
            for index, line in enumerate(detail_lines[:body_height]):
                self._add(body_top + index, separator_x + 1, line, details_width - 2)

        self._render_slash_completion(body_top, body_height, transcript_width)
        footer_y = height - 3
        self._add(footer_y, 0, "-" * width, width, self._style("chrome"))
        self._render_input_line(height - 2, width)
        self._render_status_line(height - 1, width)
        self.stdscr.refresh()

    def _render_body_line(self, row: int, line, width: int) -> None:
        if line.source == "ui":
            self._add(row, 0, line.text, width, self._style("menu"))
            return
        if line.continuation:
            self._add(row, 0, line.text, width)
            return
        attr = self._attr_for_source(line.source)
        if not attr or len(line.text) <= SOURCE_LABEL_WIDTH:
            self._add(row, 0, line.text, width, attr)
            return
        label_width = min(width, SOURCE_LABEL_WIDTH)
        self._add(row, 0, line.text[:label_width], label_width, attr)
        if width > SOURCE_LABEL_WIDTH:
            self._add(row, SOURCE_LABEL_WIDTH, line.text[SOURCE_LABEL_WIDTH:], width - SOURCE_LABEL_WIDTH)

    def _render_slash_completion(self, body_top: int, body_height: int, width: int) -> None:
        completion = self._current_slash_completion()
        if completion is None:
            return
        max_rows = max(1, min(SLASH_MENU_MAX_ROWS, body_height))
        max_items = max(1, max_rows - 1)
        lines = format_slash_completion_lines(completion, max_items=max_items)[:max_rows]
        start_y = body_top + body_height - len(lines)
        for offset, line in enumerate(lines):
            y = start_y + offset
            attr = self._style("selection") if line.startswith(">") else self._style("menu")
            self._add(y, 0, " " * width, width, self._style("menu"))
            self._add(y, 0, line, width, attr)

    def _render_header(self, width: int) -> None:
        if self.session:
            session_id = str(self.session.get("session_id") or self.session_id or "")
            status = str(self.session.get("status") or "")
            workflow = session_workflow_name(self.session)
            workdir = str(self.session.get("workdir") or "")
        else:
            session_id = self.session_id or "no-session"
            status = "read-only"
            workflow = ""
            workdir = ""
        tags = []
        if self.details_visible:
            tags.append("[details]")
        if self.session and session_is_terminal(self.session):
            tags.append("[read-only]")
        header = "  ".join(part for part in ("agent-collab", session_id, status, workflow, workdir, " ".join(tags)) if part)
        self._add(0, 0, header, width)

    def _render_input_line(self, y: int, width: int) -> None:
        prompt = self._input_prompt()
        text = prompt + self.input_text
        self._add(y, 0, text, width)
        cursor_x = min(width - 1, len(text))
        try:
            self.stdscr.move(y, cursor_x)
        except curses.error:
            pass

    def _render_status_line(self, y: int, width: int) -> None:
        scroll_mode = "following" if self.scroll.follow else "scrollback"
        activity = format_activity_indicator(self.session, int(time.monotonic() * 4))
        if not self.session:
            mode = activity
        elif session_is_terminal(self.session):
            mode = activity
        else:
            mode = f"{activity} | {scroll_mode}"
        message = self.message or "q/Ctrl-C quit  arrows/page keys scroll  End follow"
        line = f"{message:<{max(1, width - len(mode) - 1)}} {mode}"
        self._add(y, 0, line, width)

    def _active_body_lines(self, width: Optional[int] = None):
        if width is None:
            width = self._transcript_width()
        if self.picker is not None:
            return tuple(_ui_line(line) for line in wrap_plain_lines(format_session_picker_lines(self.picker), width))
        if self.overlay_lines is not None:
            return tuple(_ui_line(line) for line in wrap_plain_lines(self.overlay_lines, width))
        return wrap_transcript_lines(self.transcript_lines, width)

    def _input_prompt(self) -> str:
        if self.new_wizard:
            step = self.new_wizard.get("step", "task")
            return f"[new {step}] "
        return "[referee] "

    def _body_height(self) -> int:
        height, _ = self.stdscr.getmaxyx()
        return max(1, height - 5)

    def _transcript_width(self) -> int:
        _, width = self.stdscr.getmaxyx()
        details_width = self._details_width(width)
        return max(1, width - details_width - (1 if details_width else 0))

    def _details_width(self, width: int) -> int:
        if not self.details_visible or width < 100 or not self.session:
            return 0
        return min(48, max(32, width // 3))

    def _attr_for_source(self, source: str) -> int:
        return self.styles.get(source, 0)

    def _style(self, name: str) -> int:
        return self.styles.get(name, 0)

    def _add(self, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        if width <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, width, attr)
        except curses.error:
            pass


def _ui_line(text: str):
    from .tui_core import TranscriptLine

    return TranscriptLine(source="ui", text=text)


def _session_accepts_input(session: Mapping[str, Any]) -> bool:
    settings = session.get("settings") if isinstance(session.get("settings"), Mapping) else {}
    interactive = bool(session.get("interactive") or settings.get("interactive"))
    return interactive and not session_is_terminal(session)


def run_tui(session_id: Optional[str] = None, server_url: Optional[str] = None) -> int:
    client = AgentCollabClient(server_url)
    initial_message = ""
    if session_id is None:
        sessions = client.list_sessions().get("sessions", [])
        if sessions:
            session_id = select_latest_session_id(sessions)
        else:
            initial_message = "no daemon sessions found; use /new"

    def _run(stdscr) -> int:
        return TuiApp(stdscr, client, session_id, initial_message).run()

    return int(curses.wrapper(_run))
