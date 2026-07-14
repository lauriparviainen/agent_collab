from __future__ import annotations

import curses
import locale
import queue
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .api_schema import SessionStateModel
from .client import AgentCollabClient
from .tui_core import (
    ACCENT_ANSI8,
    ACCENT_XTERM256,
    GUTTER_WIDTH,
    READ_ONLY_INPUT_MESSAGE,
    CursorState,
    ParsedInput,
    ScrollState,
    SessionPickerState,
    accept_slash_completion,
    advance_cursor_state,
    ansi8_from_hex,
    ascii_fallback,
    build_context_agent_segments,
    build_info_line_segments,
    build_new_session_payload,
    clamp_scroll,
    classify_message,
    clip_with_marker,
    compose_status_right,
    format_activity_indicator,
    format_context_line,
    format_details_overlay_lines,
    InfoSegment,
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
    MENU_ACCENT_TITLE_SOURCE,
    MENU_ROW_SOURCE,
    MENU_SELECTED_SOURCE,
    MENU_TEXT_ROW_SOURCE,
    MENU_TITLE_SOURCE,
    overlay_body_lines,
    parallel_workflow_ids_from_options,
    parse_input,
    picker_menu_lines,
    picker_scroll,
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
    wizard_menu_lines,
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
    "/quit                    exit",
    "",
    "input",
    "plain text               append a referee note",
    "",
    "keys",
    "/quit or Ctrl-C exit  arrows/page keys scroll  End follow  Esc closes overlays",
)

SOURCE_LABEL_WIDTH = GUTTER_WIDTH + 1  # gutter column + separating space

# Box-drawing chrome (rounded when UTF-8 capable, ASCII fallback otherwise).
_BOX_UTF8 = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
_BOX_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}

# David AI xterm-256 cells for the calm palette (foreground unless noted).
_CELL_TEXT = 255
_CELL_MUTED = 250
_CELL_DIM = 245
_CELL_HAIRLINE = 236
_CELL_ERROR = 203
_CELL_BORDER = 59  # warm --border #635441
_CELL_MENU_FILL = 234  # --color-gray-floor
_CELL_MENU_SELECTED = 236  # --color-gray-panel
_CELL_BAND = 237  # --raised


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
            # Plain confirmation — the chip and status line carry the
            # read-only mode; repeating it here (in red) reads as a failure.
            self.message = f"opened {session_id} ({session.status})"
        else:
            self.message = (
                f"opened {session_id}"
                if _session_accepts_input(session)
                else "session is read-only (not interactive)"
            )
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
            styles["band_accent"] = self._pair(ACCENT_XTERM256, _CELL_BAND)
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
            styles["band_accent"] = self._pair(ACCENT_ANSI8) | A_REVERSE
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
            styles["band_accent"] = A_REVERSE | A_BOLD
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

    def _poll_loop(
        self, session_id: Optional[str], cursor: int, epoch: int, stop: threading.Event
    ) -> None:
        if not session_id:
            return
        current = cursor
        while not stop.is_set():
            try:
                batch = self.client.wait_events(session_id, current, 750)
            except Exception as exc:
                if stop.is_set():
                    return
                self.events.put(("error", epoch, session_id, str(exc)))
                return
            if stop.is_set():
                return
            self.events.put(("batch", epoch, session_id, current, batch))
            current = int(batch.cursor)
            if not batch.events:
                try:
                    session = self.client.get_session(session_id)
                except Exception as exc:
                    if stop.is_set():
                        return
                    self.events.put(("error", epoch, session_id, str(exc)))
                    return
                if stop.is_set():
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
                _, epoch, session_id, start_cursor, batch = item
                if self._accept_batch(session_id, epoch, start_cursor, batch):
                    self._refresh_session_status()
            elif kind == "status":
                _, epoch, session_id, session = item
                if not self._current_epoch(session_id, epoch):
                    continue
                self.session = session
                if session_is_terminal(session):
                    # Announce the event; mode is visible in chip + status.
                    self.message = f"session ended ({session.status})"
                    self._stop_poller()
            elif kind == "error":
                _, epoch, session_id, message = item
                if not self._current_epoch(session_id, epoch):
                    continue
                self.message = message
                self._stop_poller()

    def _accept_batch(self, session_id: str, epoch: int, start_cursor: int, batch: Any) -> bool:
        """Append only the unseen suffix of a forward, contiguous event batch."""

        if not self._current_epoch(session_id, epoch):
            return False
        current_cursor = self.cursor_state.cursor
        batch_start = max(0, int(start_cursor))
        batch_cursor = max(0, int(batch.cursor))
        events = batch.events
        if batch_start > current_cursor or batch_cursor - batch_start != len(events):
            return False
        next_state, accepted = advance_cursor_state(
            self.cursor_state,
            session_id=session_id,
            cursor=batch_cursor,
            epoch=epoch,
        )
        if not accepted:
            return False
        unseen = events[current_cursor - batch_start :]
        if len(unseen) != batch_cursor - current_cursor:
            return False
        self.cursor_state = next_state
        if unseen:
            self.transcript_lines = self.transcript_lines + format_transcript_events(unseen)
            self.scroll = clamp_scroll(self.scroll, len(self.transcript_lines), self._body_height())
        return True

    def _catch_up_and_rotate_epoch(self) -> None:
        """Read from the accepted cursor, then invalidate the stopped poller."""

        state = self.cursor_state
        if state.session_id:
            try:
                batch = self.client.read_events(state.session_id, state.cursor)
            except Exception:
                # A replacement poller can retry from the unchanged cursor.
                pass
            else:
                self._accept_batch(state.session_id, state.epoch, state.cursor, batch)
        self.cursor_state = CursorState(
            session_id=state.session_id,
            cursor=self.cursor_state.cursor,
            epoch=state.epoch + 1,
        )

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
            # Esc pops only the topmost open state per press. The wizard owns
            # its overlay lines, so cancelling it clears both together.
            if self.new_wizard:
                self.new_wizard = None
                self.overlay_lines = None
                self.scroll = follow_scroll(len(self._active_body_lines()), self._body_height())
                self.message = "new session cancelled"
                return
            if self.picker is not None:
                self.picker = None
                self.scroll = follow_scroll(len(self._active_body_lines()), self._body_height())
                return
            if self.overlay_lines is not None:
                self.overlay_lines = None
                self.scroll = follow_scroll(len(self._active_body_lines()), self._body_height())
                return
            if self.details_visible:
                # Approved interaction change: Esc dismisses /details, matching
                # how it already closes the palette and picker.
                self.details_visible = False
            return
        if (
            self.new_wizard is not None
            and self.new_wizard.get("step") == "workflow"
            and key in (curses.KEY_UP, curses.KEY_DOWN)
        ):
            self._move_wizard_choice(-1 if key == curses.KEY_UP else 1)
            return
        if self.picker is not None:
            if key in (curses.KEY_UP, ord("k")):
                self._move_picker(-1)
                return
            if key in (curses.KEY_DOWN, ord("j")):
                self._move_picker(1)
                return
            if key in (curses.KEY_NPAGE,):
                self._move_picker(max(1, self._body_height()))
                return
            if key in (curses.KEY_PPAGE,):
                self._move_picker(-max(1, self._body_height()))
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

        if key in (curses.KEY_UP,):
            self.scroll = scroll_by(
                self.scroll, len(self._active_body_lines()), self._body_height(), -1
            )
            return
        if key in (curses.KEY_DOWN,):
            self.scroll = scroll_by(
                self.scroll, len(self._active_body_lines()), self._body_height(), 1
            )
            return
        if key in (curses.KEY_PPAGE,):
            self.scroll = scroll_by(
                self.scroll,
                len(self._active_body_lines()),
                self._body_height(),
                -max(1, self._body_height()),
            )
            return
        if key in (curses.KEY_NPAGE,):
            self.scroll = scroll_by(
                self.scroll,
                len(self._active_body_lines()),
                self._body_height(),
                max(1, self._body_height()),
            )
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
        if parsed.kind != "slash":
            return
        command = parsed.command
        if command == "help":
            self.overlay_lines = HELP_LINES
            # Overlays read top-down; tail-follow would open long help at its end.
            self.scroll = ScrollState(top=0, follow=False)
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
                self._catch_up_and_rotate_epoch()
                self.message = f"stopped {self.session_id}"
            except Exception as exc:
                self.message = str(exc)
        elif command == "quit":
            self.done = True

    def _post_referee_message(self, text: str) -> None:
        if not self.session_id or not self.session:
            self.message = "no active session"
            return
        if not _session_accepts_input(self.session):
            status = self.session.status
            self.message = (
                f"session is read-only ({status})"
                if session_is_terminal(self.session)
                else READ_ONLY_INPUT_MESSAGE
            )
            return
        self._stop_poller()
        posted = False
        try:
            batch = self.client.post_message(self.session_id, text, source="referee")
            posted = True
            events = batch.events
            raw = events[0].raw if events else None
            queued = isinstance(raw, Mapping) and bool(raw.get("queued"))
            self.message = "queued note" if queued else "sent note"
            self._refresh_session_status()
        except Exception as exc:
            self.message = str(exc)
        finally:
            self._catch_up_and_rotate_epoch()
            if posted:
                self.scroll = follow_scroll(len(self.transcript_lines), self._body_height())
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
        # Anchor the picker to its top (title + column header + latest-first
        # rows), then bring the pre-selected current session into view.
        self.scroll = picker_scroll(
            self.picker,
            ScrollState(top=0, follow=False),
            self._body_text_width(),
            self._body_height(),
        )

    def _move_picker(self, delta: int) -> None:
        self.picker = move_session_picker(self.picker, delta)
        self.scroll = picker_scroll(
            self.picker, self.scroll, self._body_text_width(), self._body_height()
        )

    def _start_new_wizard(self) -> None:
        self.new_wizard = {"step": "task", "task": "", "workflows": [], "workdir": ""}
        self._set_input_text("")
        self._refresh_wizard_overlay("task")
        self.picker = None
        self.message = "new session task"

    WIZARD_CONTINUE_ROW = "continue"

    def _refresh_wizard_overlay(self, question: str) -> None:
        """Rebuild the wizard menu block: band header, answers, question last.

        Questions ask from the bottom and answered steps stack up above them,
        matching the bottom-anchored read of the palette and picker menus.
        """
        wizard = self.new_wizard or {}
        lines = ["new session"]
        if wizard.get("task"):
            lines.append(f"task: {wizard['task']}")
        for workflow_id in wizard.get("workflows", ()):
            lines.append(f"workflow: {workflow_id}")
        lines.append(question)
        self.overlay_lines = tuple(lines)

    def _enter_workflow_step(self, options: Mapping[str, Any]) -> None:
        wizard = self.new_wizard
        if wizard is None:
            return
        raw = options.get("workflows") if isinstance(options.get("workflows"), Sequence) else []
        members = {}
        for workflow in raw:
            if isinstance(workflow, Mapping) and workflow.get("id"):
                # describe_options fills "sequence" with the member list for
                # parallel workflows too; the "parallel" fallback covers
                # payloads that only carry the parallel shape.
                found = workflow.get("sequence") or workflow.get("parallel") or ()
                members[str(workflow["id"])] = [str(item) for item in found]
        wizard["step"] = "workflow"
        wizard["choices"] = workflow_ids_from_options(options)
        wizard["members"] = members
        wizard["index"] = 0
        # No preselected workflow: the choice is deliberate; continue is
        # rejected until at least one workflow is selected.
        self._refresh_workflow_choices()
        self.message = "↑↓ choose · Enter select"

    def _wizard_choice_rows(self) -> list:
        wizard = self.new_wizard or {}
        return list(wizard.get("choices") or ()) + [self.WIZARD_CONTINUE_ROW]

    def _refresh_workflow_choices(self) -> None:
        """Rebuild the workflow step as a selectable list.

        ↑↓ moves the ▸ highlight, Enter toggles the highlighted workflow
        (marked ✓) or, on the final ``continue`` row, proceeds. Each row shows
        the workflow's configured members so the shape is visible before
        selecting. Typing a workflow id still works.
        """
        wizard = self.new_wizard or {}
        index = int(wizard.get("index") or 0)
        selected = wizard.get("workflows") or []
        members = wizard.get("members") or {}
        lines = ["new session"]
        if wizard.get("task"):
            lines.append(f"task: {wizard['task']}")
        for row_index, label in enumerate(self._wizard_choice_rows()):
            marker = "▸" if row_index == index else " "
            if label == self.WIZARD_CONTINUE_ROW:
                lines.append(f"{marker} {label}")
                continue
            annotation = " + ".join(members.get(label) or ())
            check = " ✓" if label in selected else ""
            suffix = f" · {annotation}" if annotation else ""
            lines.append(f"{marker} {label}{check}{suffix}")
        self.overlay_lines = tuple(lines)

    def _move_wizard_choice(self, delta: int) -> None:
        wizard = self.new_wizard
        if wizard is None:
            return
        rows = self._wizard_choice_rows()
        index = max(0, min(len(rows) - 1, int(wizard.get("index") or 0) + delta))
        wizard["index"] = index
        self._refresh_workflow_choices()

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
            try:
                options = self._describe_options_for_workdir(workdir)
            except Exception as exc:
                # A failed daemon call must not crash the curses app; stay on
                # the task step so submitting again retries.
                wizard["task"] = ""
                self.message = str(exc)
                return
            self._enter_workflow_step(options)
            return
        if step == "workflow":
            choices = wizard.get("choices") or ()
            if not value:
                rows = self._wizard_choice_rows()
                label = rows[min(int(wizard.get("index") or 0), len(rows) - 1)]
                if label != self.WIZARD_CONTINUE_ROW:
                    # Enter toggles the highlighted workflow.
                    if label in wizard["workflows"]:
                        wizard["workflows"].remove(label)
                    else:
                        wizard["workflows"].append(label)
                    self._refresh_workflow_choices()
                    picked = ", ".join(wizard["workflows"]) or "(none)"
                    self.message = f"selected: {picked}"
                    return
                if not wizard["workflows"]:
                    self.message = "workflow is required"
                    return
                default_workdir = str(self._default_workdir())
                wizard["step"] = "workdir"
                wizard["default_workdir"] = default_workdir
                self._refresh_wizard_overlay(f"workdir [{default_workdir}]")
                self.message = f"workdir [{default_workdir}]"
                return
            if choices and value not in choices:
                self.message = f"unknown workflow {value!r}; choices: {', '.join(choices)}"
                return
            if value in wizard["workflows"]:
                self.message = f"workflow {value!r} already added"
                return
            wizard["workflows"].append(value)
            self._refresh_workflow_choices()
            self.message = f"selected: {', '.join(wizard['workflows'])}"
            return
        if step == "workdir":
            workdir = value or str(wizard.get("default_workdir") or self._default_workdir())
            try:
                options = self._describe_options_for_workdir(workdir)
                workflows = workflow_ids_from_options(options)
                selected = list(wizard["workflows"])
                unknown = [item for item in selected if workflows and item not in workflows]
                if unknown:
                    # The final workdir's config may differ from the default
                    # workdir the choices came from: re-ask with its list, and
                    # keep the typed workdir as the next default so it is not
                    # silently discarded on the way back.
                    wizard["workflows"] = [item for item in selected if item in workflows]
                    wizard["default_workdir"] = workdir
                    self._enter_workflow_step(options)
                    self.message = (
                        f"unknown workflow {unknown[0]!r}; choices: {', '.join(workflows)}"
                    )
                    return
                parallel = set(parallel_workflow_ids_from_options(options))
                started = []
                for workflow_id in selected:
                    payload = build_new_session_payload(
                        task=wizard["task"],
                        workflow=workflow_id,
                        workdir=workdir,
                        # A parallel workflow is non-interactive by contract,
                        # and a multi-session start is watched, not driven.
                        interactive=len(selected) == 1 and workflow_id not in parallel,
                    )
                    started.append(self.client.start_session(payload).session_id)
                self.new_wizard = None
                self.overlay_lines = None
                self.activate_session(started[0])
                if len(started) > 1:
                    self.message = f"started {len(started)} sessions · watching {started[0]}"
            except Exception as exc:
                self.message = str(exc)

    def _describe_options_for_workdir(self, workdir: str) -> Mapping[str, Any]:
        return self.client.describe_options({"workdir": str(Path(workdir).expanduser().resolve())})

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
        body_lines = self._active_body_lines(max(1, transcript_width - 1))
        self.scroll = clamp_scroll(self.scroll, len(body_lines), body_height)
        start = visible_scroll_top(self.scroll, len(body_lines), body_height)
        first_row = body_top
        menu_open = self.picker is not None or self.new_wizard is not None
        if menu_open and len(body_lines) < body_height:
            # A short menu block (session list, /new wizard) reads bottom-up
            # next to the input box, like the slash palette, instead of
            # floating at the top.
            first_row = body_top + body_height - len(body_lines)
        for row, line in enumerate(body_lines[start : start + body_height], start=first_row):
            self._render_body_line(row, line, transcript_width)

        if details_width:
            self._render_details_panel(body_top, body_height, transcript_width, details_width)

        self._render_slash_completion(body_top, body_height, transcript_width)

        # Region 5-6: bordered input box (3 rows) + status/hint line.
        box_top = height - 4
        input_cursor = self._render_input_box(box_top, width)
        self._render_status_line(height - 1, width)
        # Park the hardware cursor in the input field as the last move before
        # refresh: curses shows (and the terminal blinks) the cursor wherever
        # the final draw leaves it, so any draw after this would strand it.
        try:
            self.stdscr.move(*input_cursor)
        except curses.error:
            pass
        self.stdscr.refresh()

    def _hairline(self, width: int) -> str:
        return (_BOX_UTF8["h"] if self.utf8 else _BOX_ASCII["h"]) * width

    def _render_details_panel(
        self, body_top: int, body_height: int, transcript_width: int, details_width: int
    ) -> None:
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
            self._add(
                body_top + index, separator_x + 1, line, details_width - 2, self._style("muted")
            )

    def _render_body_line(self, row: int, line, width: int) -> None:
        # Body text shares the chrome's 1-column left margin (context/info draw
        # at x=1); background fills still bleed to the terminal edge. Lines are
        # wrapped to width-1 so the margin never clips the last character.
        text_width = max(0, width - 1)
        if line.source in (
            MENU_TITLE_SOURCE,
            MENU_ACCENT_TITLE_SOURCE,
            MENU_ROW_SOURCE,
            MENU_TEXT_ROW_SOURCE,
            MENU_SELECTED_SOURCE,
        ):
            # Menu blocks (picker, /new wizard), like the slash palette.
            if line.source in (MENU_TITLE_SOURCE, MENU_ACCENT_TITLE_SOURCE):
                # Raised band caps the menu: one shade lighter than the row
                # fill, matching the referee/human band tone. The wizard title
                # carries the accent color on that band.
                text_style = (
                    self._style("band_accent")
                    if line.source == MENU_ACCENT_TITLE_SOURCE
                    else self._style("band")
                )
                self._add(row, 0, " " * width, width, self._style("band"))
                self._add(row, 1, line.text, text_width, text_style)
                return
            if line.source == MENU_SELECTED_SOURCE:
                style = self._style("menu_selected")
            elif line.source == MENU_TEXT_ROW_SOURCE:
                # Wizard answers read as plain text on the fill.
                style = self._style("menu")
            else:
                # Unselected rows read accent-on-fill like palette items.
                style = self._style("menu_name")
            self._add(row, 0, " " * width, width, style)
            self._add(row, 1, line.text, text_width, style)
            return
        if line.source == "ui":
            # Shared scrollable overlay (picker / help / narrow details): the
            # first line is a title/hint, the rest is body content.
            style = self._style("dim") if row == 3 else self._style("text")
            self._add(row, 1, line.text, text_width, style)
            return
        if line.source in ("referee", "human"):
            # Raised band for referee/human covering every wrapped row of the
            # message (a first-row-only band reads as two different voices);
            # right-aligned timestamp on the first row.
            self._add(row, 0, " " * width, width, self._style("band"))
            self._add(row, 1, line.text, text_width, self._style("band"))
            if line.timestamp and width > len(line.timestamp) + 1:
                self._add(
                    row,
                    width - len(line.timestamp),
                    line.timestamp,
                    len(line.timestamp),
                    self._style("band"),
                )
            return
        if line.continuation:
            self._add(row, 1, line.text, text_width, self._style("text"))
            return

        attr = self._style_for_source(line.source)
        if len(line.text) <= SOURCE_LABEL_WIDTH:
            self._add(row, 1, line.text, text_width, attr)
            return
        label_width = min(text_width, SOURCE_LABEL_WIDTH)
        self._add(row, 1, line.text[:label_width], label_width, attr)
        if text_width > SOURCE_LABEL_WIDTH:
            # Tool summaries stay dim across the whole row; other bodies read in text.
            body_attr = self._style("dim") if line.source == "tool" else self._style("text")
            self._add(
                row,
                1 + SOURCE_LABEL_WIDTH,
                line.text[SOURCE_LABEL_WIDTH:],
                text_width - SOURCE_LABEL_WIDTH,
                body_attr,
            )

    def _render_slash_completion(self, body_top: int, body_height: int, width: int) -> None:
        completion = self._current_slash_completion()
        if completion is None:
            return
        # No fixed row cap: show every match that fits the body; the window
        # (centred on the selection) only kicks in when the screen is shorter.
        max_rows = max(1, body_height - 1)
        lines = format_slash_completion_lines(completion, max_items=max_rows)[: max_rows + 1]
        start_y = body_top + body_height - len(lines)
        for offset, line in enumerate(lines):
            y = start_y + offset
            if offset == 0:
                # Band header caps the menu, matching the session picker.
                self._add(y, 0, " " * width, width, self._style("band"))
                self._add(y, 1, line, max(0, width - 1), self._style("band"))
                continue
            selected = line.startswith("▸")
            fill = self._style("menu_selected") if selected else self._style("menu")
            self._add(y, 0, " " * width, width, fill)
            if selected:
                self._add(y, 1, line, max(0, width - 1), fill)
            else:
                # marker + name in accent, description in muted, on the gray fill.
                self._add(y, 1, line, max(0, width - 1), self._style("menu_name"))

    def _render_context_line(self, y: int, width: int) -> None:
        workdir = self.session.workdir if self.session else ""
        text = format_context_line(workdir, self.branch)
        self._add(y, 1, text, max(0, width - 1), self._style("dim"))
        # Agent cluster right-aligned into the otherwise-empty context row,
        # with a 2-cell gap so it never crowds the branch/workdir text.
        agents = info_agents_from_session(self.session) if self.session else ()
        available = width - 1 - (1 + len(text) + 2)
        segments = build_context_agent_segments(agents, max(0, available))
        cluster = sum(len(segment.text) for segment in segments)
        self._render_info_segments(y, width - 1 - cluster, width, segments)

    def _render_info_line(self, y: int, width: int) -> None:
        if self.session:
            task = self.session.task
            workflow = session_workflow_name(self.session)
        else:
            task = ""
            workflow = ""
        # Drawable columns run from x=1 to width-2 inclusive: width-2 cells.
        segments = build_info_line_segments(task, workflow, max(1, width - 2))
        self._render_info_segments(y, 1, width, segments)

    def _render_info_segments(
        self, y: int, x: int, width: int, segments: Sequence[InfoSegment]
    ) -> None:
        for segment in segments:
            if x >= width - 1:
                break
            # While the picker menu is open, the background session's chrome
            # recedes to chrome-dim so the menu reads as the focused layer.
            if self.picker is not None:
                attr = self._style("dim")
            else:
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

    def _render_input_box(self, box_top: int, width: int) -> Tuple[int, int]:
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
        return mid, max(content_x, min(width - 2, cursor_x))

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
        message = self.message
        message_style = self._message_style(message)

        activity = (
            format_activity_indicator(self.session, int(time.monotonic() * 4), utf8=self.utf8)
            if self.session
            else ""
        )
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
            width = self._body_text_width()
        _, screen_width = self.stdscr.getmaxyx()
        details_overlay = None
        if self._details_mode(screen_width) == "narrow" and self.session:
            details_overlay = format_details_overlay_lines(self.session)
        if self.picker is not None:
            # The picker renders as a colored menu, not plain overlay text;
            # width lets the header row right-align its key hints.
            return picker_menu_lines(format_session_picker_lines(self.picker, width), width)
        if self.new_wizard is not None and self.overlay_lines is not None:
            # The /new wizard renders as a menu block: accent band header,
            # answered steps as text rows, the current question at the bottom.
            return wizard_menu_lines(self.overlay_lines, width)
        overlay = overlay_body_lines(
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

    def _body_text_width(self) -> int:
        # Wrap width for body text: the transcript region minus the 1-column
        # left margin (_render_body_line draws text at x=1).
        return max(1, self._transcript_width() - 1)

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
        if not self.utf8:
            text = ascii_fallback(text)
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
