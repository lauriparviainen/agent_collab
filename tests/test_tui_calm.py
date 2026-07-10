"""Stage 5.2 calm-TUI cleanup: focused tests for the new formatting helpers,
the shared overlay, hint precedence, the spinner, directed argument-entry mode,
Esc-closes-/details, the /details clip marker, narrow rendering, the one-row
tool summary, and the hex -> xterm-256 / 8-color brand mapping.

Pure-function tests plus two behaviour checks on ``TuiApp._handle_key`` that do
not touch a real curses screen (the Esc key paths render nothing).
"""

import unittest

from agent_collab.api_schema import SessionStateModel
from agent_collab.events import Event
from agent_collab.tui import TuiApp
from agent_collab.tui_core import (
    AgentInfo,
    DirectedEntryState,
    ScrollState,
    ansi8_from_hex,
    ascii_fallback,
    build_info_line_segments,
    classify_message,
    clip_with_marker,
    compose_status_right,
    directed_entry_state,
    format_activity_indicator,
    format_context_line,
    format_details_overlay_lines,
    format_session_picker_lines,
    format_slash_completion_lines,
    GUTTER_WIDTH,
    format_transcript_event,
    gutter_label,
    input_mode_chip,
    info_agents_from_session,
    make_session_picker,
    make_slash_completion,
    overlay_body_lines,
    render_transcript_lines,
    select_hint,
    spinner_frame,
    xterm256_from_hex,
)


CLAUDE = AgentInfo("claude", "claude", "opus-4.8", "cli", "#D97757")
CODEX = AgentInfo("codex", "codex", "gpt-5", "cli", "#10A37F")
CODEX_SDK = AgentInfo("codex", "codex", "gpt-5", "sdk", "#10A37F")


def _info_text(segments):
    return "".join(segment.text for segment in segments)


class BrandColorMappingTests(unittest.TestCase):
    def test_three_known_brand_hexes_pin_to_expected_xterm256_cells(self):
        self.assertEqual(xterm256_from_hex("#D97757"), 173)  # claude coral
        self.assertEqual(xterm256_from_hex("#10A37F"), 36)   # codex green
        self.assertEqual(xterm256_from_hex("#4285F4"), 69)   # antigravity blue

    def test_hex_parsing_tolerates_missing_hash_and_rejects_bad_input(self):
        self.assertEqual(xterm256_from_hex("D97757"), 173)
        with self.assertRaises(ValueError):
            xterm256_from_hex("nope")

    def test_known_brand_hexes_map_to_ansi8_table_cells(self):
        self.assertEqual(ansi8_from_hex("#D97757"), 1)  # COLOR_RED
        self.assertEqual(ansi8_from_hex("#10A37F"), 2)  # COLOR_GREEN
        self.assertEqual(ansi8_from_hex("#4285F4"), 4)  # COLOR_BLUE
        self.assertEqual(ansi8_from_hex("#30AB92"), 6)  # accent teal -> COLOR_CYAN

    def test_very_dark_hex_falls_back_to_white_so_it_stays_visible(self):
        self.assertEqual(ansi8_from_hex("#000000"), 7)


class InfoLineTests(unittest.TestCase):
    def test_full_line_lists_task_agents_and_workflow(self):
        segments = build_info_line_segments(
            "review the poller race", [CLAUDE, CODEX], "cross-review", 100
        )
        self.assertEqual(
            _info_text(segments),
            "review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review",
        )
        roles = {(seg.role, seg.text) for seg in segments}
        self.assertIn(("agent", "claude"), roles)
        self.assertIn(("model", ":opus-4.8"), roles)
        self.assertIn(("workflow", "cross-review"), roles)

    def test_agent_segment_carries_brand_color(self):
        segments = build_info_line_segments("t", [CLAUDE], "wf", 100)
        agent = next(seg for seg in segments if seg.role == "agent")
        self.assertEqual(agent.brand_color, "#D97757")

    def test_inline_backend_shown_only_when_it_differs_from_default_cli(self):
        default = build_info_line_segments("t", [CODEX], "wf", 100)
        self.assertNotIn("sdk", _info_text(default))

        sdk = build_info_line_segments("t", [CODEX_SDK], "wf", 100)
        self.assertIn("codex:gpt-5 sdk", _info_text(sdk))
        backend_seg = next(seg for seg in sdk if seg.role == "backend")
        self.assertEqual(backend_seg.text, " sdk")

    def test_truncation_priority_drops_workflow_then_secondary_agent_then_task(self):
        task = "review the poller race"
        agents = [CLAUDE, CODEX]

        # 1. Workflow drops first.
        self.assertEqual(
            _info_text(build_info_line_segments(task, agents, "cross-review", 60)),
            "review the poller race · claude:opus-4.8 · codex:gpt-5",
        )
        # 2. Then the secondary agent (lead + task survive).
        self.assertEqual(
            _info_text(build_info_line_segments(task, agents, "cross-review", 48)),
            "review the poller race · claude:opus-4.8",
        )
        # 3. Finally the task ellipsizes but is never dropped; the lead survives.
        narrow = _info_text(build_info_line_segments(task, agents, "cross-review", 30))
        self.assertIn("claude:opus-4.8", narrow)
        self.assertIn("…", narrow)
        self.assertLessEqual(len(narrow), 30)

    def test_sdk_backend_chip_travels_with_its_agent_on_overflow(self):
        # When workflow has dropped, the lead's backend chip stays attached.
        text = _info_text(
            build_info_line_segments(
                "review the poller race",
                [CLAUDE, CODEX_SDK],
                "cross-review",
                60,
            )
        )
        self.assertEqual(text, "review the poller race · claude:opus-4.8 · codex:gpt-5 sdk")

    def test_no_session_placeholder(self):
        segments = build_info_line_segments("", [], "", 40)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].role, "placeholder")
        self.assertEqual(segments[0].text, "no active session")

    def test_info_agents_read_from_session_settings(self):
        session = {
            "settings": {
                "agents": {
                    "claude": {"type": "claude", "model": "opus-4.8", "backend": "cli", "brand_color": "#D97757"},
                    "codex": {"type": "codex", "model": "gpt-5", "backend": "sdk", "brand_color": "#10A37F"},
                }
            }
        }
        agents = info_agents_from_session(session)
        self.assertEqual([a.name for a in agents], ["claude", "codex"])
        self.assertEqual(agents[1].backend, "sdk")
        self.assertEqual(agents[0].brand_color, "#D97757")


class ContextLineTests(unittest.TestCase):
    def test_branch_and_abbreviated_path(self):
        import os

        home = os.path.expanduser("~")
        self.assertEqual(format_context_line(f"{home}/projects/x", "main"), "main  ~/projects/x")

    def test_no_workdir_yields_app_name(self):
        self.assertEqual(format_context_line("", None), "agent-collab")

    def test_path_without_branch(self):
        self.assertEqual(format_context_line("/srv/repo", None), "/srv/repo")


class PaletteFormatterTests(unittest.TestCase):
    def test_headerless_menu_with_marker_and_windowing(self):
        state = make_slash_completion("/")
        assert state is not None
        lines = format_slash_completion_lines(state, max_items=7)
        # No "commands  Tab/Enter accepts" header anymore.
        self.assertFalse(any("Tab/Enter accepts" in line for line in lines))
        self.assertEqual(len(lines), 7)  # 10 commands, window of 7
        self.assertTrue(lines[0].startswith("▸"))  # selected row marked ▸
        self.assertIn("/help", lines[0])

    def test_no_match_state_has_no_header(self):
        state = make_slash_completion("/zzz")
        assert state is not None
        lines = format_slash_completion_lines(state)
        self.assertEqual(lines, ("  no matches",))


class SharedOverlayTests(unittest.TestCase):
    """One shared scrollable overlay backs /help, the picker and narrow /details."""

    def test_selects_picker_when_open(self):
        picker = make_session_picker([{"session_id": "a", "status": "running"}])
        lines = overlay_body_lines(picker=picker, overlay_lines=("help",), details_overlay=("d",))
        self.assertEqual(lines, format_session_picker_lines(picker))

    def test_selects_help_overlay_when_no_picker(self):
        lines = overlay_body_lines(picker=None, overlay_lines=("commands", "row"), details_overlay=("d",))
        self.assertEqual(lines, ("commands", "row"))

    def test_selects_details_overlay_last(self):
        details = format_details_overlay_lines({"session_id": "s1", "status": "running"})
        lines = overlay_body_lines(picker=None, overlay_lines=None, details_overlay=details)
        self.assertEqual(lines[0], "details · ↑↓ scroll · Esc close")
        self.assertIn("session_id: s1", "\n".join(lines))

    def test_returns_none_for_transcript(self):
        self.assertIsNone(overlay_body_lines())


class HintPrecedenceTests(unittest.TestCase):
    def test_first_match_precedence(self):
        # Wizard beats everything.
        self.assertEqual(
            select_hint(new_wizard_step="task", picker_open=True, palette_open=True),
            "Enter next · Esc cancel",
        )
        self.assertEqual(select_hint(new_wizard_step="workdir"), "Enter start · Esc cancel")
        self.assertEqual(select_hint(picker_open=True, palette_open=True), "↑↓ move · Enter open · Esc close")
        self.assertEqual(select_hint(palette_open=True), "Enter send · Tab complete · Esc close")
        self.assertEqual(select_hint(details_mode="narrow"), "↑↓ scroll · Esc close")
        self.assertEqual(select_hint(details_mode="wide"), "Enter send · / cmds · Esc close")
        self.assertEqual(select_hint(overlay_open=True), "↑↓ scroll · Esc close")
        self.assertEqual(select_hint(has_session=False), "/new start · /help commands · q quit")
        self.assertEqual(select_hint(read_only=True), "↑↓ scroll · q quit")
        self.assertEqual(select_hint(following=False), "↑↓ scroll · End follow")
        self.assertEqual(select_hint(), "Enter send · / cmds")


class StatusCompositionTests(unittest.TestCase):
    def test_activity_then_hint_joined(self):
        self.assertEqual(
            compose_status_right("⠹ running", "Enter send · / cmds"),
            "⠹ running · Enter send · / cmds",
        )

    def test_empty_activity_leaves_hint_alone(self):
        self.assertEqual(
            compose_status_right("", "/new start · /help commands · q quit"),
            "/new start · /help commands · q quit",
        )

    def test_message_classification_drives_left_color(self):
        self.assertEqual(classify_message("sent note"), "success")
        self.assertEqual(classify_message("asked codex"), "success")
        self.assertEqual(classify_message("queued for codex"), "success")
        self.assertEqual(classify_message("session is read-only (failed)"), "error")
        self.assertEqual(classify_message("unknown target 'reviewer'"), "error")
        self.assertEqual(classify_message(""), "neutral")


class SpinnerTests(unittest.TestCase):
    def test_braille_orbit_when_utf8(self):
        self.assertEqual(spinner_frame(0), "⠋")
        self.assertEqual(spinner_frame(1), "⠙")
        self.assertEqual(spinner_frame(10), "⠋")  # wraps at 10 frames

    def test_ascii_dot_pulse_fallback(self):
        self.assertEqual(spinner_frame(0, utf8=False), ".")
        self.assertEqual(spinner_frame(1, utf8=False), "..")
        self.assertEqual(spinner_frame(2, utf8=False), "...")

    def test_activity_indicator_uses_selected_spinner(self):
        self.assertEqual(format_activity_indicator({"status": "running"}, 0), "⠋ running")
        self.assertEqual(format_activity_indicator({"status": "running"}, 0, utf8=False), ". running")
        self.assertEqual(format_activity_indicator({"status": "awaiting_input"}), "awaiting input")


class DirectedEntryTests(unittest.TestCase):
    def test_hash_agent_awaiting_message(self):
        state = directed_entry_state("#codex ")
        self.assertEqual(state, DirectedEntryState("codex", "message codex directly", True))

    def test_hash_agent_with_message_is_not_awaiting(self):
        state = directed_entry_state("#codex fix the poller")
        self.assertIsNotNone(state)
        self.assertFalse(state.awaiting_arg)

    def test_ask_form_without_agent(self):
        state = directed_entry_state("/ask ")
        self.assertEqual(state, DirectedEntryState("ask AGENT", "usage: /ask AGENT message", True))

    def test_ask_form_with_agent_awaiting_message(self):
        state = directed_entry_state("/ask codex ")
        self.assertEqual(state.target_label, "codex")
        self.assertTrue(state.awaiting_arg)

    def test_plain_text_is_not_directed(self):
        self.assertIsNone(directed_entry_state("plain note"))
        self.assertIsNone(directed_entry_state("/help"))

    def test_mode_chip_reflects_directed_target(self):
        self.assertEqual(input_mode_chip("#codex "), "-> codex")
        self.assertEqual(input_mode_chip("/ask "), "-> ask AGENT")
        self.assertEqual(input_mode_chip("hello"), "referee note")
        self.assertEqual(input_mode_chip("hi", new_wizard=True), "new session")
        self.assertEqual(input_mode_chip("hi", picker_open=True), "picking")
        self.assertEqual(input_mode_chip("hi", has_session=False), "no session")
        self.assertEqual(input_mode_chip("hi", accepts_input=False), "read-only")


class ToolSummaryTests(unittest.TestCase):
    def test_multiline_tool_event_collapses_to_one_summary_row(self):
        event = Event.create("tool", "command", "Read options.py:281\n" + "\n".join(f"line {i}" for i in range(50)))
        lines = format_transcript_event(event)
        self.assertEqual(len(lines), 1)
        rendered = render_transcript_lines(lines)[0]
        self.assertIn("Read options.py:281", rendered)
        self.assertIn("+50 lines", rendered)
        self.assertTrue(rendered.startswith("tool"))

    def test_single_line_tool_event_has_no_size_suffix(self):
        event = Event.create("tool", "command", "Read options.py:281")
        rendered = render_transcript_lines(format_transcript_event(event))[0]
        self.assertEqual(rendered, "tool    Read options.py:281")


class DetailsClipMarkerTests(unittest.TestCase):
    def test_clip_marks_last_visible_row(self):
        lines = [f"row {i}" for i in range(10)]
        clipped = clip_with_marker(lines, 4)
        self.assertEqual(len(clipped), 4)
        self.assertEqual(clipped[-1], "…")
        self.assertEqual(clipped[0], "row 0")

    def test_no_marker_when_it_fits(self):
        lines = ("a", "b")
        self.assertEqual(clip_with_marker(lines, 5), ("a", "b"))

    def test_zero_height_returns_empty(self):
        self.assertEqual(clip_with_marker(("a",), 0), ())


class _DummyScreen:
    def getmaxyx(self):
        return (24, 80)


class _DummyClient:
    pass


class _FakeScreen:
    """Records what would be drawn so ``_render`` can be exercised headlessly.

    Not a real curses screen (the contract's "no real curses screen" rule) — it
    just captures ``addnstr`` into a character grid.
    """

    def __init__(self, height, width):
        self.height = height
        self.width = width
        self._reset()

    def _reset(self):
        self.grid = [[" "] * self.width for _ in range(self.height)]

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        self._reset()

    def refresh(self):
        pass

    def move(self, y, x):
        pass

    def addnstr(self, y, x, text, width, attr=0):
        if y < 0 or y >= self.height:
            return
        for i, ch in enumerate(str(text)[:width]):
            if 0 <= x + i < self.width:
                self.grid[y][x + i] = ch

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)


def _session():
    from agent_collab.api_schema import SessionStateModel

    return SessionStateModel.from_dict(
        {
            "session_id": "daemon-1",
            "status": "running",
            "task": "review the poller race",
            "workflow": "cross-review",
            "workdir": "/home/dev/agent_collab",
            "interactive": True,
            "settings": {
                "workflow": {"name": "cross-review", "sequence": ["claude", "codex"]},
                "interactive": True,
                "agents": {
                    "claude": {"type": "claude", "model": "opus-4.8", "backend": "cli", "brand_color": "#D97757"},
                    "codex": {"type": "codex", "model": "gpt-5", "backend": "sdk", "brand_color": "#10A37F"},
                },
            },
        }
    )


def _app_with_transcript(height, width):
    from agent_collab.tui_core import follow_scroll

    screen = _FakeScreen(height, width)
    app = TuiApp(screen, _DummyClient(), initial_session_id=None)
    app.session = _session()
    app.session_id = "daemon-1"
    app.branch = "main"
    app.styles = {}
    app.utf8 = True
    app.transcript_lines = format_transcript_event(
        Event.create("claude", "message", "the epoch guard drops stale batches")
    ) + format_transcript_event(Event.create("referee", "message", "ship it"))
    app.scroll = follow_scroll(len(app.transcript_lines), app._body_height())
    return app, screen


class RenderIntegrationTests(unittest.TestCase):
    """Headless end-to-end render smoke tests over a FakeScreen (not curses)."""

    def test_main_layout_regions_render(self):
        app, screen = _app_with_transcript(24, 80)
        app._render()
        out = screen.text()
        context = out.splitlines()[0]
        self.assertIn("main", context)  # branch on the context line
        self.assertIn("agent_collab", context)  # project/workdir
        # Info line: inline sdk backend chip on codex, workflow present.
        self.assertIn("claude:opus-4.8 · codex:gpt-5 sdk · cross-review", out)
        self.assertIn("╭", out)  # bordered input box
        self.assertIn("> ", out)
        self.assertIn("referee note", out)  # mode chip
        self.assertIn("running · Enter send · / cmds", out)  # status/hint

    def test_wide_details_keeps_transcript_left_and_panel_right(self):
        app, screen = _app_with_transcript(24, 120)
        app.details_visible = True
        app._render()
        out = screen.text()
        # Transcript stays on the left (regression guard: details mode must read
        # the screen width, not the transcript width).
        self.assertIn("claude  the epoch guard drops stale batches", out)
        self.assertIn("│", out)  # hairline separator column
        self.assertIn("…", out)  # clip marker on overflow

    def test_narrow_details_becomes_scrollable_overlay(self):
        app, screen = _app_with_transcript(20, 48)
        app.details_visible = True
        # Show the top of the overlay (the title row) rather than following the tail.
        app.scroll = ScrollState(top=0, follow=False)
        app._render()
        out = screen.text()
        self.assertIn("details · ↑↓ scroll · Esc close", out)  # overlay title
        self.assertIn("↑↓ scroll · Esc close", out)  # narrow-details hint

    def test_narrow_main_truncates_info_line_to_lead_agent(self):
        app, screen = _app_with_transcript(20, 48)
        app._render()
        lines = screen.text().splitlines()
        info = lines[1]
        self.assertIn("claude:opus-4.8", info)
        self.assertNotIn("cross-review", info)  # workflow dropped first

    def test_sub_minimum_frame_shows_too_small(self):
        app, screen = _app_with_transcript(4, 12)
        app._render()
        self.assertIn("terminal", screen.text())


class EscBehaviourTests(unittest.TestCase):
    """Esc key paths that render nothing and so need no curses screen."""

    def _app(self):
        return TuiApp(_DummyScreen(), _DummyClient(), initial_session_id=None)

    def test_esc_closes_details(self):
        app = self._app()
        app.details_visible = True
        app._handle_key(27)
        self.assertFalse(app.details_visible)

    def test_esc_cancels_directed_argument_entry_and_clears_rail(self):
        app = self._app()
        app.input_text = "#codex "
        app._handle_key(27)
        self.assertEqual(app.input_text, "")

    def test_esc_does_not_clear_a_complete_directed_turn(self):
        app = self._app()
        app.input_text = "#codex fix it"
        app._handle_key(27)
        # A complete turn is not argument-entry, so Esc leaves the rail as-is.
        self.assertEqual(app.input_text, "#codex fix it")


class EscPopsTopmostTests(unittest.TestCase):
    """Esc pops only the topmost open state per press (review finding)."""

    def _app(self):
        return TuiApp(_DummyScreen(), _DummyClient(), initial_session_id=None)

    def test_esc_closes_picker_before_details(self):
        app = self._app()
        app.details_visible = True
        app.picker = make_session_picker([])
        app._handle_key(27)
        self.assertIsNone(app.picker)
        self.assertTrue(app.details_visible)  # untouched by the first press
        app._handle_key(27)
        self.assertFalse(app.details_visible)

    def test_esc_closes_overlay_before_details(self):
        app = self._app()
        app.details_visible = True
        app.overlay_lines = ("help",)
        app._handle_key(27)
        self.assertIsNone(app.overlay_lines)
        self.assertTrue(app.details_visible)

    def test_esc_cancels_wizard_with_its_overlay_in_one_press(self):
        app = self._app()
        app.new_wizard = {"step": "task", "task": "", "workflow": "", "workdir": ""}
        app.overlay_lines = ("new session", "enter task")
        app._handle_key(27)
        self.assertIsNone(app.new_wizard)
        self.assertIsNone(app.overlay_lines)
        self.assertEqual(app.message, "new session cancelled")


class GutterLabelTests(unittest.TestCase):
    def test_long_source_is_ellipsized_into_the_fixed_gutter(self):
        self.assertEqual(gutter_label("antigravity"), "antigr…")
        self.assertEqual(len(gutter_label("antigravity_sdk")), GUTTER_WIDTH)
        self.assertEqual(gutter_label("referee"), "referee")  # exactly 7 fits
        self.assertEqual(gutter_label("claude"), "claude")

    def test_long_source_rows_stay_column_aligned(self):
        event = Event.create("antigravity", "message", "first line\nsecond line")
        lines = format_transcript_event(event)
        self.assertTrue(lines[0].text.startswith("antigr… "))
        self.assertEqual(lines[0].text.index("first"), GUTTER_WIDTH + 1)
        self.assertEqual(lines[1].text.index("second"), GUTTER_WIDTH + 1)
        # The full source survives for brand-color lookup.
        self.assertEqual(lines[0].source, "antigravity")


class AsciiFallbackTests(unittest.TestCase):
    def test_substitutions_are_one_to_one(self):
        for glyph in "▸▏◆│─╭╮╰╯·…↑↓":
            self.assertEqual(len(ascii_fallback(glyph)), 1, glyph)

    def test_chrome_strings_become_ascii(self):
        self.assertEqual(ascii_fallback("▸ /help"), "> /help")
        self.assertEqual(ascii_fallback("↑↓ scroll · q quit"), "^v scroll | q quit")
        self.assertTrue(ascii_fallback("╭─╮│╰─╯▏…").isascii())

    def test_non_utf8_render_draws_only_ascii_chrome(self):
        app, screen = _app_with_transcript(24, 80)
        app.utf8 = False
        app._render()
        for line in screen.text().splitlines():
            self.assertTrue(line.isascii(), line)


class InfoLineWidthTests(unittest.TestCase):
    def test_ellipsized_info_line_keeps_its_last_character(self):
        # At narrow widths the task ellipsizes; the trailing char (often the
        # ellipsis itself) must survive the x=1 draw offset (review finding).
        app, screen = _app_with_transcript(24, 30)
        app._render()
        info_row = screen.text().splitlines()[1]
        self.assertIn("…", info_row)


class QKeyBehaviourTests(unittest.TestCase):
    """``q`` quits only in viewer states; in a live interactive session it types."""

    def _app(self):
        return TuiApp(_DummyScreen(), _DummyClient(), initial_session_id=None)

    def test_q_types_into_the_rail_in_a_live_interactive_session(self):
        app = self._app()
        app.session = _session()  # running + interactive
        app.session_id = "daemon-1"
        app._handle_key(ord("q"))
        self.assertFalse(app.done)
        self.assertEqual(app.input_text, "q")

    def test_q_quits_with_no_session(self):
        app = self._app()
        app._handle_key(ord("q"))
        self.assertTrue(app.done)

    def test_q_quits_a_read_only_terminal_session(self):
        app = self._app()
        session = _session().to_dict()
        session["status"] = "done"
        app.session = SessionStateModel.from_dict(session)
        app.session_id = "daemon-1"
        app._handle_key(ord("q"))
        self.assertTrue(app.done)

    def test_q_quits_a_non_interactive_session(self):
        app = self._app()
        session = _session().to_dict()
        session["interactive"] = False
        session["settings"] = {}
        app.session = SessionStateModel.from_dict(session)
        app.session_id = "daemon-1"
        app._handle_key(ord("q"))
        self.assertTrue(app.done)

    def test_q_never_quits_while_the_rail_holds_text(self):
        app = self._app()  # even with no session, mid-word q must type
        app.input_text = "status "
        app._handle_key(ord("q"))
        self.assertFalse(app.done)
        self.assertEqual(app.input_text, "status q")


if __name__ == "__main__":
    unittest.main()
