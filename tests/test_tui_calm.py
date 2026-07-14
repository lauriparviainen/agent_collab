"""Stage 5.2 calm-TUI cleanup: focused tests for the new formatting helpers,
the shared overlay, hint precedence, the spinner, the input mode chip,
Esc-closes-/details, the /details clip marker, narrow rendering, the one-row
tool summary, and the hex -> xterm-256 / 8-color brand mapping.

Pure-function tests plus two behaviour checks on ``TuiApp._handle_key`` that do
not touch a real curses screen (the Esc key paths render nothing).
"""

import unittest
from unittest import mock

from agent_collab.api_schema import SessionStateModel
from agent_collab.events import Event
from agent_collab.tui import TuiApp
from agent_collab.tui_core import (
    AgentInfo,
    ScrollState,
    ansi8_from_hex,
    ascii_fallback,
    build_context_agent_segments,
    build_info_line_segments,
    classify_message,
    clip_with_marker,
    compose_status_right,
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
    def test_known_brand_hexes_pin_to_expected_xterm256_cells(self):
        self.assertEqual(xterm256_from_hex("#D97757"), 173)  # claude coral
        self.assertEqual(xterm256_from_hex("#10A37F"), 36)  # codex green
        self.assertEqual(xterm256_from_hex("#4285F4"), 69)  # antigravity blue
        self.assertEqual(xterm256_from_hex("#A0A0A0"), 247)  # xAI neutral monochrome

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
    def test_full_line_lists_task_and_workflow(self):
        segments = build_info_line_segments("review the poller race", "cross-review", 100)
        self.assertEqual(_info_text(segments), "review the poller race · cross-review")
        roles = {(seg.role, seg.text) for seg in segments}
        self.assertIn(("task", "review the poller race"), roles)
        self.assertIn(("workflow", "cross-review"), roles)

    def test_truncation_drops_workflow_then_ellipsizes_task(self):
        task = "review the poller race"

        # 1. Workflow drops first.
        self.assertEqual(
            _info_text(build_info_line_segments(task, "cross-review", 30)),
            "review the poller race",
        )
        # 2. Then the task ellipsizes but is never dropped.
        narrow = _info_text(build_info_line_segments(task, "cross-review", 12))
        self.assertIn("…", narrow)
        self.assertLessEqual(len(narrow), 12)

    def test_no_session_placeholder(self):
        segments = build_info_line_segments("", "", 40)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].role, "placeholder")
        self.assertEqual(segments[0].text, "no active session")


class ContextAgentClusterTests(unittest.TestCase):
    def test_cluster_shows_canonical_backend_name_then_model(self):
        segments = build_context_agent_segments([CLAUDE, CODEX_SDK], 100)
        self.assertEqual(_info_text(segments), "claude_cli: opus-4.8 · codex_sdk: gpt-5")
        roles = {(seg.role, seg.text) for seg in segments}
        self.assertIn(("agent", "claude_cli"), roles)
        self.assertIn(("model", ": opus-4.8"), roles)

    def test_agent_segment_carries_brand_color(self):
        segments = build_context_agent_segments([CLAUDE], 100)
        agent = next(seg for seg in segments if seg.role == "agent")
        self.assertEqual(agent.brand_color, "#D97757")

    def test_backend_always_shown(self):
        default = build_context_agent_segments([CODEX], 100)
        self.assertIn("codex_cli: gpt-5", _info_text(default))

        sdk = build_context_agent_segments([CODEX_SDK], 100)
        self.assertIn("codex_sdk: gpt-5", _info_text(sdk))

    def test_custom_agent_id_stays_in_front_of_canonical_name(self):
        reviewer = AgentInfo("reviewer", "codex", "gpt-5", "cli", "#10A37F")
        segments = build_context_agent_segments([reviewer], 100)
        self.assertEqual(_info_text(segments), "reviewer codex_cli: gpt-5")

    def test_agent_id_equal_to_canonical_name_is_not_duplicated(self):
        agent = AgentInfo("antigravity_cli", "antigravity", "Gemini 3.1 Pro (High)", "cli")
        segments = build_context_agent_segments([agent], 100)
        self.assertEqual(_info_text(segments), "antigravity_cli: Gemini 3.1 Pro (High)")

    def test_agent_without_backend_falls_back_to_bare_id(self):
        mock = AgentInfo("mock", "mock", "", "")
        segments = build_context_agent_segments([mock], 100)
        self.assertEqual(_info_text(segments), "mock")

    def test_overflow_drops_rightmost_agents_then_disappears(self):
        # Both fit at 40 ("claude_cli: opus-4.8 · codex_sdk: gpt-5" = 39).
        self.assertEqual(
            _info_text(build_context_agent_segments([CLAUDE, CODEX_SDK], 40)),
            "claude_cli: opus-4.8 · codex_sdk: gpt-5",
        )
        # The secondary drops first.
        self.assertEqual(
            _info_text(build_context_agent_segments([CLAUDE, CODEX_SDK], 30)),
            "claude_cli: opus-4.8",
        )
        # No room for even the lead: the cluster disappears, never ellipsizes.
        self.assertEqual(build_context_agent_segments([CLAUDE, CODEX_SDK], 10), ())

    def test_no_agents_yields_empty_cluster(self):
        self.assertEqual(build_context_agent_segments([], 40), ())

    def test_info_agents_read_from_session_settings(self):
        session = {
            "settings": {
                "agents": {
                    "claude": {
                        "type": "claude",
                        "model": "opus-4.8",
                        "backend": "cli",
                        "brand_color": "#D97757",
                    },
                    "codex": {
                        "type": "codex",
                        "model": "gpt-5",
                        "backend": "sdk",
                        "brand_color": "#10A37F",
                    },
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
        self.assertEqual(
            format_context_line(f"{home}/projects/x", "main"), "workdir: ~/projects/x (main)"
        )

    def test_no_workdir_yields_app_name(self):
        self.assertEqual(format_context_line("", None), "agent-collab")

    def test_path_without_branch(self):
        self.assertEqual(format_context_line("/srv/repo", None), "workdir: /srv/repo")


class PaletteFormatterTests(unittest.TestCase):
    def test_menu_has_band_header_with_marker_and_windowing(self):
        state = make_slash_completion("/")
        assert state is not None
        lines = format_slash_completion_lines(state, max_items=7)
        # Band header carries the column titles, matching the session picker.
        self.assertEqual(lines[0], "  command        description")
        self.assertEqual(len(lines), 8)  # header + 10 commands, window of 7
        self.assertTrue(lines[1].startswith("▸"))  # selected row marked ▸
        self.assertIn("/help", lines[1])

    def test_no_match_state_keeps_the_header(self):
        state = make_slash_completion("/zzz")
        assert state is not None
        lines = format_slash_completion_lines(state)
        self.assertEqual(lines, ("  command        description", "  no matches"))


class SharedOverlayTests(unittest.TestCase):
    """One shared scrollable overlay backs /help, the picker and narrow /details."""

    def test_selects_picker_when_open(self):
        picker = make_session_picker([{"session_id": "a", "status": "running"}])
        lines = overlay_body_lines(picker=picker, overlay_lines=("help",), details_overlay=("d",))
        self.assertEqual(lines, format_session_picker_lines(picker))

    def test_selects_help_overlay_when_no_picker(self):
        lines = overlay_body_lines(
            picker=None, overlay_lines=("commands", "row"), details_overlay=("d",)
        )
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
        self.assertEqual(
            select_hint(picker_open=True, palette_open=True), "↑↓ move · Enter open · Esc close"
        )
        self.assertEqual(select_hint(palette_open=True), "Enter send · Tab complete · Esc close")
        self.assertEqual(select_hint(details_mode="narrow"), "↑↓ scroll · Esc close")
        self.assertEqual(select_hint(details_mode="wide"), "Enter send · / cmds · Esc close")
        self.assertEqual(select_hint(overlay_open=True), "↑↓ scroll · Esc close")
        self.assertEqual(select_hint(has_session=False), "/new start · /help commands · /quit exit")
        self.assertEqual(select_hint(read_only=True), "↑↓ scroll · /quit exit")
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
            compose_status_right("", "/new start · /help commands · /quit exit"),
            "/new start · /help commands · /quit exit",
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
        self.assertEqual(
            format_activity_indicator({"status": "running"}, 0, utf8=False), ". running"
        )
        self.assertEqual(format_activity_indicator({"status": "awaiting_input"}), "awaiting input")


class InputModeChipTests(unittest.TestCase):
    def test_mode_chip_reflects_input_state(self):
        self.assertEqual(input_mode_chip("hello"), "referee note")
        self.assertEqual(input_mode_chip("hi", new_wizard=True), "new session")
        self.assertEqual(input_mode_chip("hi", picker_open=True), "picking")
        self.assertEqual(input_mode_chip("hi", has_session=False), "no session")
        self.assertEqual(input_mode_chip("hi", accepts_input=False), "read-only")


class ToolSummaryTests(unittest.TestCase):
    def test_multiline_tool_event_collapses_to_one_summary_row(self):
        event = Event.create(
            "tool", "command", "Read options.py:281\n" + "\n".join(f"line {i}" for i in range(50))
        )
        lines = format_transcript_event(event)
        self.assertEqual(len(lines), 1)
        rendered = render_transcript_lines(lines)[0]
        self.assertIn("Read options.py:281", rendered)
        self.assertIn("+50 lines", rendered)
        self.assertTrue(rendered.startswith("tool"))

    def test_single_line_tool_event_has_no_size_suffix(self):
        event = Event.create("tool", "command", "Read options.py:281")
        rendered = render_transcript_lines(format_transcript_event(event))[0]
        self.assertEqual(rendered, "tool             Read options.py:281")


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
        self.moved = None

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        self._reset()

    def refresh(self):
        pass

    def move(self, y, x):
        self.moved = (y, x)

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
                    "claude": {
                        "type": "claude",
                        "model": "opus-4.8",
                        "backend": "cli",
                        "brand_color": "#D97757",
                    },
                    "codex": {
                        "type": "codex",
                        "model": "gpt-5",
                        "backend": "sdk",
                        "brand_color": "#10A37F",
                    },
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
        app, screen = _app_with_transcript(24, 100)
        app._render()
        out = screen.text()
        context = out.splitlines()[0]
        self.assertIn("workdir: /home/dev/agent_collab (main)", context)
        # Agent cluster right-aligned on the context line, canonical backend names.
        self.assertIn("claude_cli: opus-4.8 · codex_sdk: gpt-5", context)
        # Info line: task and workflow.
        self.assertIn("review the poller race · cross-review", out)
        self.assertIn("╭", out)  # bordered input box
        self.assertIn("> ", out)
        self.assertIn("referee note", out)  # mode chip
        self.assertIn("running · Enter send · / cmds", out)  # status/hint

    def test_hardware_cursor_parks_in_the_input_field(self):
        app, screen = _app_with_transcript(24, 100)
        app._render()
        # Input row is box_top + 1 = height - 3; the cursor sits right after
        # the "> " prompt. It must be the render's final move so the terminal
        # blinks it inside the input field, not on the status line.
        self.assertEqual(screen.moved, (21, 4))
        app.input_text = "hi"
        app._render()
        self.assertEqual(screen.moved, (21, 6))

    def test_palette_shows_every_command_when_it_fits(self):
        app, screen = _app_with_transcript(24, 100)
        app.input_text = "/"
        app._render()
        out = screen.text()
        self.assertIn("  command        description", out)
        for name in (
            "/help",
            "/sessions",
            "/new",
            "/details",
            "/follow",
            "/refresh",
            "/stop",
            "/quit",
        ):
            self.assertIn(name, out)

    def test_short_session_list_bottom_aligns_next_to_the_input_box(self):
        app, screen = _app_with_transcript(24, 100)
        app.picker = make_session_picker(
            [
                {
                    "session_id": "daemon-1",
                    "status": "done",
                    "workflow": "solo-claude-cli",
                    "updated_at": "2026-07-13T20:38:18+00:00",
                    "workdir": "/w",
                }
            ]
        )
        app._render()
        rows = screen.text().splitlines()
        # Body rows are 3..19; header + one session fill the last two so the
        # short list sits next to the input box instead of floating at the top.
        self.assertIn("session", rows[18])
        self.assertIn("daemon-1", rows[19])
        self.assertEqual(rows[3].strip(), "")

    def test_band_covers_every_wrapped_row_of_human_and_referee_messages(self):
        class _AttrScreen(_FakeScreen):
            def _reset(self):
                super()._reset()
                self.attrs = [[0] * self.width for _ in range(self.height)]

            def addnstr(self, y, x, text, width, attr=0):
                super().addnstr(y, x, text, width, attr)
                for i in range(len(str(text)[:width])):
                    if 0 <= x + i < self.width:
                        self.attrs[y][x + i] = attr

        from agent_collab.tui_core import follow_scroll

        screen = _AttrScreen(24, 60)
        app = TuiApp(screen, _DummyClient(), initial_session_id=None)
        app.session = _session()
        app.session_id = "daemon-1"
        app.branch = "main"
        app.styles = {"band": 7}
        app.utf8 = True
        long_text = "verify the worker threads never touch live session state " * 3
        app.transcript_lines = format_transcript_event(Event.create("human", "message", long_text))
        app.scroll = follow_scroll(len(app.transcript_lines), app._body_height())
        app._render()

        rows = [y for y in range(screen.height) if "verify the worker" in "".join(screen.grid[y])]
        self.assertGreater(len(rows), 1)  # the message wraps
        for y in rows:
            # The raised band fills the whole row, wrapped rows included.
            self.assertEqual(set(screen.attrs[y]), {7}, f"row {y} not fully banded")

    def test_wide_details_keeps_transcript_left_and_panel_right(self):
        app, screen = _app_with_transcript(24, 120)
        app.details_visible = True
        app._render()
        out = screen.text()
        # Transcript stays on the left (regression guard: details mode must read
        # the screen width, not the transcript width).
        self.assertIn("claude           the epoch guard drops stale batches", out)
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

    def test_narrow_main_drops_agent_cluster_before_branch_workdir(self):
        app, screen = _app_with_transcript(20, 48)
        app._render()
        lines = screen.text().splitlines()
        # No room next to branch/workdir: the cluster disappears (details
        # panel still carries the full per-agent record).
        self.assertIn("main", lines[0])
        self.assertNotIn("claude:opus-4.8", lines[0])
        # The info line keeps task and workflow.
        self.assertIn("review the poller race · cross-review", lines[1])

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
        app.new_wizard = {"step": "task", "task": "", "workflows": [], "workdir": ""}
        app.overlay_lines = ("new session", "task")
        app._handle_key(27)
        self.assertIsNone(app.new_wizard)
        self.assertIsNone(app.overlay_lines)
        self.assertEqual(app.message, "new session cancelled")


class NewWizardFlowTests(unittest.TestCase):
    """/new renders as a menu block and can start several sessions at once."""

    class _Client:
        def __init__(self):
            self.started = []

        def describe_options(self, payload):
            return {
                "workflows": [
                    {"id": "cross-review", "sequence": ["claude", "codex", "claude"]},
                    {"id": "dual-review", "parallel": ["claude", "codex"]},
                    {"id": "solo-xai-cli", "sequence": ["xai"]},
                ]
            }

        def start_session(self, payload):
            self.started.append(payload)
            from types import SimpleNamespace

            return SimpleNamespace(session_id=f"daemon-{len(self.started)}")

    def _app(self):
        client = self._Client()
        app = TuiApp(_DummyScreen(), client, initial_session_id=None)
        app.activated = []
        app.activate_session = app.activated.append
        return app, client

    def test_wizard_selects_workflows_and_starts_one_session_each(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        # Choice rows: cross-review, dual-review, solo-xai-cli, continue.
        self.assertEqual(app.overlay_lines[0], "new session")
        self.assertEqual(app.overlay_lines[1], "task: review the diff")
        self.assertEqual(app.overlay_lines[2], "▸ cross-review · claude + codex + claude")
        app._move_wizard_choice(1)  # ▸ dual-review
        app._advance_new_wizard("")  # Enter toggles ✓
        app._move_wizard_choice(1)  # ▸ solo-xai-cli
        app._advance_new_wizard("")
        self.assertIn("  dual-review ✓", "\n".join(app.overlay_lines))
        self.assertIn("▸ solo-xai-cli ✓ · xai", "\n".join(app.overlay_lines))
        app._move_wizard_choice(1)  # ▸ continue
        app._advance_new_wizard("")
        self.assertTrue(app.overlay_lines[-1].startswith("workdir ["))
        app._advance_new_wizard("")  # default workdir starts the sessions

        self.assertEqual([p["workflow"] for p in client.started], ["dual-review", "solo-xai-cli"])
        # Multi-session starts are watched, not driven.
        self.assertFalse(any(p["interactive"] for p in client.started))
        self.assertEqual(app.activated, ["daemon-1"])
        self.assertIn("started 2 sessions", app.message)

    def test_wizard_toggle_deselects_and_requires_a_selection(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._advance_new_wizard("")  # toggle cross-review on
        self.assertEqual(app.new_wizard["workflows"], ["cross-review"])
        app._advance_new_wizard("")  # toggle it back off
        self.assertEqual(app.new_wizard["workflows"], [])
        app._move_wizard_choice(99)  # clamp onto continue
        app._advance_new_wizard("")
        self.assertEqual(app.message, "workflow is required")
        self.assertEqual(app.new_wizard["step"], "workflow")

    def test_wizard_single_parallel_workflow_starts_non_interactive(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._move_wizard_choice(1)  # ▸ dual-review
        app._advance_new_wizard("")
        app._move_wizard_choice(99)  # ▸ continue
        app._advance_new_wizard("")
        app._advance_new_wizard("")
        self.assertEqual([p["workflow"] for p in client.started], ["dual-review"])
        self.assertFalse(client.started[0]["interactive"])

    def test_wizard_single_sequential_workflow_stays_interactive(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._advance_new_wizard("")  # ▸ cross-review toggled
        app._move_wizard_choice(99)
        app._advance_new_wizard("")
        app._advance_new_wizard("")
        self.assertEqual([p["workflow"] for p in client.started], ["cross-review"])
        self.assertTrue(client.started[0]["interactive"])

    def test_wizard_survives_describe_options_failure_at_task_step(self):
        app, client = self._app()
        client.describe_options = mock.Mock(side_effect=RuntimeError("daemon unreachable"))
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        # The failure surfaces as a message; the wizard stays on the task
        # step (no crash) so submitting again retries.
        self.assertEqual(app.message, "daemon unreachable")
        self.assertEqual(app.new_wizard["step"], "task")

    def test_wizard_workdir_reentry_keeps_typed_workdir_and_truncates(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._advance_new_wizard("dual-review")
        app._move_wizard_choice(99)
        app._advance_new_wizard("")  # continue → workdir step
        # The custom workdir's config no longer knows dual-review.
        client.describe_options = mock.Mock(
            return_value={"workflows": [{"id": "solo-xai-cli", "sequence": ["xai"]}]}
        )
        app._advance_new_wizard("/custom/workdir")
        self.assertEqual(app.new_wizard["step"], "workflow")
        self.assertEqual(app.new_wizard["workflows"], [])  # unknown truncated
        self.assertIn("unknown workflow", app.message)
        # The typed workdir survives as the next default instead of being
        # silently replaced by the global default.
        self.assertEqual(app.new_wizard["default_workdir"], "/custom/workdir")

    def test_wizard_typed_entry_rejects_unknown_and_duplicate_workflows(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._advance_new_wizard("nope")
        self.assertIn("unknown workflow", app.message)
        app._advance_new_wizard("dual-review")
        app._advance_new_wizard("dual-review")
        self.assertIn("already added", app.message)
        self.assertEqual(app.new_wizard["workflows"], ["dual-review"])


class NewWizardMemberStepTests(unittest.TestCase):
    """The wizard asks shape first, then the backends that fill its slots."""

    class _Client:
        def __init__(self):
            self.started = []

        def describe_options(self, payload):
            eligible = ["claude_cli", "codex_cli", "xai_cli"]
            return {
                "workflows": [
                    {
                        "id": "cross-review",
                        "sequence": ["claude_cli", "codex_cli", "claude_cli"],
                        "member_selection": {
                            "start_field": "members",
                            "distinct_members": False,
                            "slots": [
                                {
                                    "slot": "claude_cli",
                                    "default": "claude_cli",
                                    "eligible_members": eligible,
                                },
                                {
                                    "slot": "codex_cli",
                                    "default": "codex_cli",
                                    "eligible_members": eligible,
                                },
                            ],
                        },
                    },
                    {
                        "id": "dual-review",
                        "parallel": ["claude_cli", "codex_cli"],
                        "sequence": ["claude_cli", "codex_cli"],
                        "member_selection": {
                            "start_field": "members",
                            "distinct_members": True,
                            "slots": [
                                {
                                    "slot": "claude_cli",
                                    "default": "claude_cli",
                                    "eligible_members": eligible,
                                },
                                {
                                    "slot": "codex_cli",
                                    "default": "codex_cli",
                                    "eligible_members": eligible,
                                },
                            ],
                        },
                    },
                ]
            }

        def start_session(self, payload):
            self.started.append(payload)
            from types import SimpleNamespace

            return SimpleNamespace(session_id=f"daemon-{len(self.started)}")

    def _app(self):
        client = self._Client()
        app = TuiApp(_DummyScreen(), client, initial_session_id=None)
        app.activated = []
        app.activate_session = app.activated.append
        return app, client

    def _select_dual_review(self, app):
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._move_wizard_choice(1)  # ▸ dual-review
        app._advance_new_wizard("")  # toggle ✓
        app._move_wizard_choice(99)  # ▸ continue
        app._advance_new_wizard("")  # → members step

    def test_enter_through_defaults_sends_no_members_field(self):
        app, client = self._app()
        self._select_dual_review(app)
        self.assertEqual(app.new_wizard["step"], "members")
        # The highlight starts on the configured member of each slot.
        self.assertIn("▸ claude_cli ✓ · configured", app.overlay_lines)
        app._advance_new_wizard("")  # slot claude_cli keeps its default
        app._advance_new_wizard("")  # slot codex_cli keeps its default
        self.assertEqual(app.new_wizard["step"], "workdir")
        app._advance_new_wizard("")  # default workdir starts the session
        self.assertEqual(len(client.started), 1)
        self.assertNotIn("members", client.started[0])

    def test_substituted_slot_is_sent_and_shown(self):
        app, client = self._app()
        self._select_dual_review(app)
        app._advance_new_wizard("")  # slot claude_cli keeps its default
        app._advance_new_wizard("xai_cli")  # slot codex_cli → xai_cli
        self.assertEqual(app.new_wizard["step"], "workdir")
        # The workdir question shows the effective members.
        self.assertIn("workflow: dual-review · claude_cli + xai_cli", app.overlay_lines)
        app._advance_new_wizard("")
        self.assertEqual(client.started[0]["members"], {"codex_cli": "xai_cli"})

    def test_arrow_selection_assigns_highlighted_member(self):
        app, client = self._app()
        self._select_dual_review(app)
        app._move_wizard_choice(2)  # ▸ xai_cli for slot claude_cli
        app._advance_new_wizard("")
        app._advance_new_wizard("")  # slot codex_cli keeps its default
        app._advance_new_wizard("")  # workdir
        self.assertEqual(client.started[0]["members"], {"claude_cli": "xai_cli"})

    def test_unknown_member_is_rejected_and_slot_stays(self):
        app, client = self._app()
        self._select_dual_review(app)
        app._advance_new_wizard("nope_cli")
        self.assertIn("unknown agent", app.message)
        self.assertEqual(app.new_wizard["step"], "members")
        self.assertEqual(app.new_wizard["slot_index"], 0)

    def test_each_selected_workflow_gets_its_own_members_question(self):
        app, client = self._app()
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._advance_new_wizard("")  # ▸ cross-review toggled
        app._move_wizard_choice(1)  # ▸ dual-review
        app._advance_new_wizard("")
        app._move_wizard_choice(99)  # ▸ continue
        app._advance_new_wizard("")
        # cross-review first: substitute the lead, keep the reviewer.
        self.assertIn("workflow: cross-review · agent for slot claude_cli (1/2)", app.overlay_lines)
        app._advance_new_wizard("xai_cli")
        app._advance_new_wizard("")
        # then dual-review: keep both defaults.
        self.assertIn("workflow: dual-review · agent for slot claude_cli (1/2)", app.overlay_lines)
        app._advance_new_wizard("")
        app._advance_new_wizard("")
        app._advance_new_wizard("")  # workdir starts both
        self.assertEqual(client.started[0]["members"], {"claude_cli": "xai_cli"})
        self.assertNotIn("members", client.started[1])

    def test_wizard_without_slot_data_skips_members_step(self):
        app, client = self._app()
        client.describe_options = lambda payload: {
            "workflows": [{"id": "dual-review", "parallel": ["claude_cli", "codex_cli"]}]
        }
        app._start_new_wizard()
        app._advance_new_wizard("review the diff")
        app._advance_new_wizard("")  # toggle dual-review
        app._move_wizard_choice(99)
        app._advance_new_wizard("")  # continue → straight to workdir
        self.assertEqual(app.new_wizard["step"], "workdir")


class GutterLabelTests(unittest.TestCase):
    def test_long_source_is_ellipsized_into_the_fixed_gutter(self):
        self.assertEqual(gutter_label("antigravity-tool"), "antigravity-tool")  # 16 fits exactly
        self.assertEqual(len(gutter_label("antigravity_sdk-tool")), GUTTER_WIDTH)
        self.assertEqual(gutter_label("antigravity_sdk-tool"), "antigravity_sdk…")
        self.assertEqual(gutter_label("referee"), "referee")
        self.assertEqual(gutter_label("claude"), "claude")

    def test_long_source_rows_stay_column_aligned(self):
        event = Event.create("antigravity", "message", "first line\nsecond line")
        lines = format_transcript_event(event)
        self.assertTrue(lines[0].text.startswith("antigravity "))
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
        app, screen = _app_with_transcript(24, 20)
        app._render()
        info_row = screen.text().splitlines()[1]
        self.assertIn("…", info_row)


class QKeyBehaviourTests(unittest.TestCase):
    """q never quits — same behaviour in every state (quit is /quit or Ctrl-C)."""

    def _app(self):
        return TuiApp(_DummyScreen(), _DummyClient(), initial_session_id=None)

    def test_q_types_in_a_live_interactive_session(self):
        app = self._app()
        app.session = _session()
        app.session_id = "daemon-1"
        app._handle_key(ord("q"))
        self.assertFalse(app.done)
        self.assertEqual(app.input_text, "q")

    def test_q_types_with_no_session(self):
        app = self._app()
        app._handle_key(ord("q"))
        self.assertFalse(app.done)
        self.assertEqual(app.input_text, "q")

    def test_q_types_in_a_read_only_terminal_session(self):
        app = self._app()
        session = _session().to_dict()
        session["status"] = "done"
        app.session = SessionStateModel.from_dict(session)
        app.session_id = "daemon-1"
        app._handle_key(ord("q"))
        self.assertFalse(app.done)
        self.assertEqual(app.input_text, "q")

    def test_q_appends_mid_word(self):
        app = self._app()
        app.input_text = "status "
        app._handle_key(ord("q"))
        self.assertFalse(app.done)
        self.assertEqual(app.input_text, "status q")


if __name__ == "__main__":
    unittest.main()
