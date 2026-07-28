import asyncio
import os
import threading
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab import cli
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.events import Event
from agent_collab.tui_core import (
    GUTTER_WIDTH,
    MENU_ROW_SOURCE,
    MENU_SELECTED_SOURCE,
    MENU_TITLE_SOURCE,
    PICKER_HEADER_LINES,
    CursorState,
    ScrollState,
    accept_slash_completion,
    advance_cursor_state,
    build_new_session_payload,
    cell_width,
    clamp_scroll,
    ensure_scroll_visible,
    fit_display_text,
    gutter_label,
    sanitize_display_text,
    split_display_cells,
    VARIATION_SELECTOR_16,
    wrap_plain_lines,
    filter_slash_commands,
    format_activity_indicator,
    follow_scroll,
    format_session_picker_lines,
    format_session_details,
    format_slash_completion_lines,
    format_transcript_event,
    format_transcript_events,
    make_slash_completion,
    make_session_picker,
    move_session_picker,
    move_slash_completion,
    parallel_workflow_ids_from_options,
    parse_input,
    picker_menu_lines,
    picker_scroll,
    render_transcript_lines,
    reset_cursor_state,
    scroll_by,
    select_latest_session_id,
    selected_picker_session_id,
    selected_slash_command,
    session_is_terminal,
    should_start_poller,
    slash_completion_matches_input,
    sort_sessions_latest_first,
    visible_scroll_top,
    wrap_transcript_lines,
    workflow_ids_from_options,
)


LONG_TEST_WORKDIR = "/workspace/projects/example-agent-collab"


class TuiCoreTests(unittest.TestCase):
    def setUp(self):
        self._home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._home_tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": self._home_tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_transcript_formatting_matches_watch_labels_and_wraps(self):
        event = Event.create("claude", "message", "hello\nworld")

        lines = render_transcript_lines(format_transcript_event(event))

        # Calm direction: lowercase gutter labels (was uppercase CLAUDE).
        self.assertEqual(lines, ("claude           hello", "                 world"))

        long_event = Event.create("tool", "command", "abcdef ghijkl mnop")
        wrapped = wrap_transcript_lines(format_transcript_event(long_event), 22)
        rendered = render_transcript_lines(wrapped)

        self.assertEqual(rendered[0], "tool             abcde")
        self.assertTrue(rendered[1].startswith("             "))

    def test_transcript_attributes_parallel_member_rows(self):
        tool_event = Event.create("tool", "tool_call", "Read file\ndetail", agent_id="claude")
        (line,) = format_transcript_event(tool_event)
        self.assertEqual(line.text, "claude-tool      Read file · +1 lines")
        self.assertEqual(line.source, "tool")

        member_event = Event.create("claude", "message", "review text", agent_id="claude-b")
        lines = format_transcript_event(member_event)
        self.assertEqual(lines[0].text, "claude-b         review text")
        self.assertEqual(lines[0].source, "claude")

        matching = Event.create("claude", "message", "review text", agent_id="claude")
        lines = render_transcript_lines(format_transcript_event(matching))
        self.assertEqual(lines, ("claude           review text",))

        boundary = Event.create("referee", "status", "turn-1 claude completed", agent_id="claude")
        lines = render_transcript_lines(format_transcript_event(boundary))
        self.assertEqual(lines, ("referee          turn-1 claude completed",))

    def test_human_source_renders_as_prompt_alias(self):
        event = Event.create("human", "message", "Review the diff")
        lines = format_transcript_event(event)
        self.assertEqual(lines[0].text, "prompt           Review the diff")
        # Wire source is untouched: band styling and colors key on it.
        self.assertEqual(lines[0].source, "human")

    def test_terminal_provider_evidence_is_hidden_for_canonical_boundary(self):
        event = Event.create(
            "error",
            "error",
            "hostile provider detail",
            {"fatal": True, "error": "hostile provider detail"},
        )
        self.assertEqual(format_transcript_event(event), ())

    def test_details_render_structured_failure_once_with_outcomes(self):
        session = {
            "session_id": "s1",
            "status": "failed",
            "error": "The provider cancelled the turn",
            "failure": {
                "code": "provider_turn_cancelled",
                "message": "The provider cancelled the turn",
                "turn_id": "turn-2",
            },
            "turn_outcomes": [
                {
                    "turn_id": "turn-1",
                    "agent_id": "claude",
                    "outcome": "completed",
                },
                {
                    "turn_id": "turn-2",
                    "agent_id": "xai",
                    "outcome": "cancelled",
                },
            ],
        }
        lines = format_session_details(session)
        self.assertEqual(sum("The provider cancelled the turn" in line for line in lines), 1)
        self.assertIn(
            "failure turn-2: provider_turn_cancelled — The provider cancelled the turn",
            lines,
        )
        self.assertIn("outcome turn-1: claude completed", lines)

    def test_details_format_uses_top_level_state_and_settings_agents(self):
        session = {
            "session_id": "s1",
            "status": "running",
            "workflow": "cross-review",
            "workdir": "/repo",
            "created_at": "2026-07-08T00:00:00+00:00",
            "updated_at": "2026-07-08T00:01:00+00:00",
            "max_turns": 2,
            "timeout": 30,
            "mock": False,
            "dry_run": True,
            "jsonl_path": "/logs/s1.jsonl",
            "markdown_path": "/logs/s1.md",
            "settings": {
                "workflow": {"name": "solo-codex-cli", "sequence": ["codex"]},
                "agents": {
                    "codex": {
                        "type": "codex",
                        "model": "gpt-5",
                        "thinking_level": "high",
                        "sandbox": "read-only",
                        "approval_policy": "never",
                        "command_preview": ["codex", "--model", "gpt-5"],
                    }
                },
            },
        }

        lines = format_session_details(session)
        text = "\n".join(lines)

        self.assertIn("workflow: solo-codex-cli", text)
        self.assertIn("sequence: codex", text)
        self.assertIn("workdir: /repo", text)
        self.assertIn("mock: false", text)
        self.assertIn("dry_run: true", text)
        self.assertIn("jsonl_path: /logs/s1.jsonl", text)
        self.assertIn(
            "agent codex: type=codex model=gpt-5 thinking_level=high sandbox=read-only approval_policy=never",
            text,
        )
        self.assertIn("command_preview: codex --model gpt-5", text)
        self.assertNotIn("ended_at:", text)

    def test_parse_input_covers_slash_and_plain_forms(self):
        self.assertEqual(parse_input("/help").command, "help")
        session = parse_input("/session daemon-1")
        self.assertEqual(session.kind, "slash")
        self.assertEqual(session.args, ("daemon-1",))
        self.assertEqual(parse_input("plain note").kind, "text")
        # Directed input (/ask, #AGENT) was removed: CLI backends run each
        # turn as a fresh one-shot, so a "#agent" note is just a plain note.
        self.assertEqual(parse_input("#reviewer take a look").kind, "text")
        self.assertEqual(parse_input("/ask claude anything").kind, "invalid")
        self.assertEqual(parse_input("/unknown").kind, "invalid")

    def test_slash_command_completion_filters_deterministically(self):
        all_matches = filter_slash_commands("/")
        s_matches = filter_slash_commands("/s")

        self.assertEqual(all_matches[0].name, "/help")
        self.assertNotIn("/ask", [match.name for match in all_matches])
        self.assertEqual([match.name for match in s_matches], ["/sessions", "/session", "/stop"])
        self.assertEqual(filter_slash_commands("/session x"), ())

    def test_slash_completion_state_moves_and_accepts_selected_command(self):
        state = make_slash_completion("/s")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(
            [match.name for match in state.matches], ["/sessions", "/session", "/stop"]
        )
        self.assertEqual(selected_slash_command(state), "/sessions")

        state = move_slash_completion(state, 1)

        self.assertEqual(selected_slash_command(state), "/session")
        self.assertFalse(slash_completion_matches_input("/se", state))
        self.assertTrue(slash_completion_matches_input("/session", state))
        self.assertTrue(slash_completion_matches_input("/SESSION", state))
        self.assertEqual(accept_slash_completion("/se", state), "/session ")
        # Band-headed menu with ▸ selection marker.
        self.assertIn("▸ /session", "\n".join(format_slash_completion_lines(state, max_items=2)))
        self.assertEqual(move_slash_completion(state, 99).index, len(state.matches) - 1)
        self.assertEqual(move_slash_completion(state, -99).index, 0)

    def test_slash_completion_hides_for_arguments_and_keeps_no_match_state(self):
        self.assertIsNone(make_slash_completion("plain text"))
        self.assertIsNone(make_slash_completion("/session "))

        state = make_slash_completion("/zz")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.matches, ())
        self.assertEqual(selected_slash_command(state), None)
        self.assertEqual(accept_slash_completion("/zz", state), "/zz")
        self.assertIn("no matches", "\n".join(format_slash_completion_lines(state)))

    def test_activity_indicator_changes_for_running_waiting_and_terminal_sessions(self):
        self.assertEqual(format_activity_indicator(None), "no session")
        # Approved change: braille-orbit spinner (was ASCII - \ | /).
        self.assertEqual(format_activity_indicator({"status": "running"}, tick=0), "⠋ running")
        self.assertEqual(format_activity_indicator({"status": "running"}, tick=1), "⠙ running")
        # ASCII dot-pulse fallback on non-UTF-8 terminals.
        self.assertEqual(
            format_activity_indicator({"status": "running"}, tick=0, utf8=False), ". running"
        )
        self.assertEqual(
            format_activity_indicator({"status": "running"}, tick=2, utf8=False), "... running"
        )
        self.assertEqual(
            format_activity_indicator({"status": "awaiting_input"}, tick=2), "awaiting input"
        )
        # Terminal sessions show just the status — the input chip carries "read-only".
        self.assertEqual(format_activity_indicator({"status": "done"}, tick=3), "done")

    def test_scroll_follow_rules(self):
        state = follow_scroll(100, 10)
        self.assertEqual(state, ScrollState(top=90, follow=True))
        self.assertEqual(visible_scroll_top(state, 100, 10), 90)

        state = scroll_by(state, 100, 10, -5)
        self.assertEqual(state, ScrollState(top=85, follow=False))
        self.assertEqual(clamp_scroll(state, 120, 10), ScrollState(top=85, follow=False))

        state = scroll_by(state, 100, 10, 999)
        self.assertEqual(state, ScrollState(top=90, follow=True))
        self.assertEqual(clamp_scroll(state, 120, 10), ScrollState(top=110, follow=True))

    def test_ensure_scroll_visible_adjusts_minimally_and_never_follows(self):
        state = ScrollState(top=10, follow=False)
        # Row already on screen: unchanged.
        self.assertEqual(
            ensure_scroll_visible(state, 12, 13, 100, 10), ScrollState(top=10, follow=False)
        )
        # Row above the viewport: scroll up to it.
        self.assertEqual(
            ensure_scroll_visible(state, 4, 5, 100, 10), ScrollState(top=4, follow=False)
        )
        # Row below the viewport: scroll down just enough.
        self.assertEqual(
            ensure_scroll_visible(state, 25, 26, 100, 10), ScrollState(top=16, follow=False)
        )
        # A following (tail-pinned) state is re-anchored to the row.
        self.assertEqual(
            ensure_scroll_visible(follow_scroll(100, 10), 0, 1, 100, 10),
            ScrollState(top=0, follow=False),
        )

    def test_picker_scroll_opens_at_top_and_tracks_selection(self):
        sessions = [
            {
                "session_id": f"s{index:02d}",
                "status": "done",
                "workflow": "solo-codex-cli",
                "updated_at": f"2026-07-08T00:00:{index:02d}+00:00",
                "workdir": "/w",
            }
            for index in range(20)
        ]

        # Opening pins the top: title, column header, and the latest-first rows
        # (including the pre-selected newest session) are all visible.
        picker = make_session_picker(sessions)
        state = picker_scroll(picker, ScrollState(top=0, follow=False), 200, 10)
        self.assertEqual(state, ScrollState(top=0, follow=False))

        # Moving the selection below the fold scrolls it into view (width 200:
        # no wrapping, so rows map 1:1 to display lines).
        picker = move_session_picker(picker, 15)
        state = picker_scroll(picker, state, 200, 10)
        self.assertEqual(state, ScrollState(top=PICKER_HEADER_LINES + 15 + 1 - 10, follow=False))

        # Moving back up to the first row re-pins the top so the header
        # (scrolled off while below the fold) comes back into view.
        picker = move_session_picker(picker, -15)
        state = picker_scroll(picker, state, 200, 10)
        self.assertEqual(state, ScrollState(top=0, follow=False))

        # An empty picker pins to the top even from a following state.
        self.assertEqual(
            picker_scroll(make_session_picker([]), follow_scroll(30, 10), 200, 10),
            ScrollState(top=0, follow=False),
        )

    def test_picker_menu_lines_tag_roles_and_wrapped_continuations(self):
        sessions = [
            {
                "session_id": "one",
                "status": "done",
                "workflow": "solo-codex-cli",
                "updated_at": "2026-07-08T00:00:01+00:00",
                "workdir": "/short",
            },
            {
                "session_id": "two",
                "status": "running",
                "workflow": "solo-xai",
                "updated_at": "2026-07-08T00:00:02+00:00",
                "workdir": LONG_TEST_WORKDIR,
            },
        ]
        picker = make_session_picker(sessions)  # newest ("two") preselected
        lines = format_session_picker_lines(picker)

        tagged = picker_menu_lines(lines, 200)
        self.assertEqual(tagged[0].source, MENU_TITLE_SOURCE)  # combined header
        self.assertEqual(tagged[1].source, MENU_SELECTED_SOURCE)
        self.assertTrue(tagged[1].text.startswith("▸"))
        self.assertEqual(tagged[2].source, MENU_ROW_SOURCE)

        # Narrow width: the selected row wraps and its continuations keep the
        # selected-bar role so the highlight spans the whole logical row.
        narrow = picker_menu_lines(lines, 40)
        selected = [line for line in narrow if line.source == MENU_SELECTED_SOURCE]
        self.assertGreater(len(selected), 1)
        self.assertFalse(selected[0].continuation)
        self.assertTrue(all(line.continuation for line in selected[1:]))

    def test_cursor_state_resets_and_drops_stale_batches(self):
        state = reset_cursor_state(CursorState(), "s1")
        self.assertEqual(state.cursor, 0)
        self.assertEqual(state.epoch, 1)

        advanced, accepted = advance_cursor_state(state, session_id="s1", cursor=4, epoch=1)
        self.assertTrue(accepted)
        self.assertEqual(advanced.cursor, 4)

        replayed, accepted = advance_cursor_state(advanced, session_id="s1", cursor=4, epoch=1)
        self.assertFalse(accepted)
        self.assertEqual(replayed, advanced)

        backward, accepted = advance_cursor_state(advanced, session_id="s1", cursor=3, epoch=1)
        self.assertFalse(accepted)
        self.assertEqual(backward, advanced)

        stale, accepted = advance_cursor_state(advanced, session_id="s1", cursor=9, epoch=0)
        self.assertFalse(accepted)
        self.assertEqual(stale, advanced)

        wrong_session, accepted = advance_cursor_state(advanced, session_id="s2", cursor=9, epoch=1)
        self.assertFalse(accepted)
        self.assertEqual(wrong_session, advanced)

    def test_latest_session_selection_matches_watch_ordering(self):
        sessions = [
            {"session_id": "old", "updated_at": "2026-07-08T00:00:00+00:00"},
            {"session_id": "tie-a", "updated_at": "2026-07-08T01:00:00+00:00"},
            {"session_id": "tie-b", "updated_at": "2026-07-08T01:00:00+00:00"},
        ]

        self.assertEqual(select_latest_session_id(sessions), "tie-b")
        with self.assertRaises(ValueError):
            select_latest_session_id([])

    def test_session_picker_helpers_sort_move_and_render(self):
        sessions = [
            {
                "session_id": "old",
                "status": "done",
                "workflow": "solo-codex-cli",
                "updated_at": "2026-07-08T00:00:00+00:00",
                "workdir": "/old",
            },
            {
                "session_id": "new",
                "status": "running",
                "workflow": "cross-review",
                "updated_at": "2026-07-08T01:00:00+00:00",
                "workdir": "/new",
            },
        ]

        self.assertEqual(
            [session["session_id"] for session in sort_sessions_latest_first(sessions)],
            ["new", "old"],
        )

        picker = make_session_picker(sessions, current_session_id="old")
        self.assertEqual(selected_picker_session_id(picker), "old")
        picker = move_session_picker(picker, -1)
        self.assertEqual(selected_picker_session_id(picker), "new")

        rendered = "\n".join(format_session_picker_lines(picker))
        # Target delta: lowercase columns and ▸ selection marker.
        self.assertIn("session", rendered)
        self.assertIn("▸   new", rendered)

    def test_picker_columns_align_and_trim_timestamps(self):
        sessions = [
            {
                "session_id": "daemon-129d8368dc9047d6",
                "status": "done",
                "workflow": "solo-antigravity-cli",
                "updated_at": "2026-07-13T19:21:27.187654+00:00",
                "workdir": "/home/devel/projects/agent_collab",
            },
            {
                "session_id": "daemon-7387a888e6034416",
                "status": "failed",
                "workflow": "dual-review",
                "updated_at": "2026-07-13T20:38:18.614285+00:00",
                "workdir": "/home/devel/projects/agent_collab",
            },
        ]
        picker = make_session_picker(sessions)
        lines = format_session_picker_lines(picker)
        header, rows = lines[0], lines[1:]

        self.assertIn("2026-07-13 20:38", rows[0])
        self.assertNotIn("+00:00", "\n".join(rows))
        # Every column starts where its header says, even when a value is
        # wider than the header (long workflow names, wide session ids).
        self.assertEqual(header.index("updated"), rows[1].index("2026-07-13 19:21"))
        self.assertEqual(header.index("workdir"), rows[0].index("/home/devel"))
        self.assertEqual(rows[0].index("/home/devel"), rows[1].index("/home/devel"))

        # With a width, the key hints right-align into the header row; without
        # (or when too narrow) they are dropped — the status line carries them.
        wide = format_session_picker_lines(picker, 160)[0]
        self.assertTrue(wide.endswith("Esc close"))
        self.assertEqual(len(wide), 160)
        self.assertNotIn("Esc close", format_session_picker_lines(picker, 40)[0])
        self.assertNotIn("Esc close", header)

    def test_picker_backends_column_shows_effective_members(self):
        # With one built-in `solo` shape, the workflow id no longer says which
        # agents ran; the picker reads them from the settings echo (which
        # reflects start-time member selection) and dedupes reprised slots.
        sessions = [
            {
                "session_id": "substituted",
                "status": "done",
                "workflow": "solo",
                "updated_at": "2026-07-15T01:00:00+00:00",
                "workdir": "/repo",
                "settings": {"workflow": {"name": "solo", "sequence": ["xai_cli"]}},
            },
            {
                "session_id": "reprised",
                "status": "done",
                "workflow": "cross-review",
                "updated_at": "2026-07-15T00:00:00+00:00",
                "workdir": "/repo",
                "settings": {
                    "workflow": {
                        "name": "cross-review",
                        "sequence": ["claude_cli", "codex_cli", "claude_cli"],
                    }
                },
            },
            # A record carrying only the parallel shape (no sequence mirror)
            # still shows its members.
            {
                "session_id": "parallel-only",
                "status": "done",
                "workflow": "dual-review",
                "updated_at": "2026-07-14T12:00:00+00:00",
                "workdir": "/repo",
                "settings": {
                    "workflow": {
                        "name": "dual-review",
                        "parallel": ["codex_cli", "xai_cli"],
                    }
                },
            },
            # Legacy record without settings: the column stays blank.
            {
                "session_id": "legacy",
                "status": "interrupted",
                "workflow": "solo",
                "updated_at": "2026-07-14T00:00:00+00:00",
                "workdir": "/repo",
            },
        ]
        picker = make_session_picker(sessions)
        lines = format_session_picker_lines(picker)
        header, rows = lines[0], lines[1:]

        self.assertIn("backends", header)
        self.assertIn("xai_cli", rows[0])
        self.assertIn("claude_cli+codex_cli", rows[1])
        self.assertNotIn("claude_cli+codex_cli+claude_cli", rows[1])
        self.assertIn("codex_cli+xai_cli", rows[2])
        # The agents column starts where its header says on every row.
        self.assertEqual(header.index("backends"), rows[0].index("xai_cli"))
        self.assertEqual(header.index("backends"), rows[1].index("claude_cli+codex_cli"))

    def test_options_helpers_extract_workflows(self):
        options = {
            "workflows": [
                {"id": "solo-claude-cli", "sequence": ["claude"], "parallel": None},
                {
                    "id": "dual-review",
                    "sequence": ["claude", "codex"],
                    "parallel": ["claude", "codex"],
                },
            ],
        }

        self.assertEqual(workflow_ids_from_options(options), ("solo-claude-cli", "dual-review"))
        self.assertEqual(parallel_workflow_ids_from_options(options), ("dual-review",))

    def test_new_session_payload_matches_daemon_start_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_new_session_payload(
                task=" task ", workflow="solo-codex-cli", workdir=tmp
            )

        self.assertEqual(payload["task"], "task")
        self.assertEqual(payload["workflow"], "solo-codex-cli")
        self.assertEqual(payload["workdir"], str(Path(tmp).resolve()))
        self.assertEqual(payload["max_turns"], 3)
        self.assertEqual(payload["timeout"], 900)
        self.assertEqual(payload["mock"], False)
        self.assertEqual(payload["dry_run"], False)
        self.assertEqual(payload["interactive"], False)
        self.assertEqual(payload["interactive_idle_timeout"], 600.0)
        self.assertEqual(payload["backend_options"], {})

        interactive_payload = build_new_session_payload(
            task="task",
            workflow="solo-codex-cli",
            workdir=tmp,
            interactive=True,
            interactive_idle_timeout=30,
        )

        self.assertEqual(interactive_payload["interactive"], True)
        self.assertEqual(interactive_payload["interactive_idle_timeout"], 30.0)

    def test_new_session_payload_carries_members_only_when_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = build_new_session_payload(task="task", workflow="dual-review", workdir=tmp)
            substituted = build_new_session_payload(
                task="task",
                workflow="dual-review",
                workdir=tmp,
                members={"codex_cli": "xai_cli"},
            )

        self.assertNotIn("members", plain)
        self.assertEqual(substituted["members"], {"codex_cli": "xai_cli"})

    def test_member_slots_from_options_requires_complete_slot_data(self):
        from agent_collab.tui_core import member_slots_from_options

        slots = {
            "start_field": "members",
            "distinct_members": True,
            "slots": [
                {
                    "slot": "claude_cli",
                    "default": "claude_cli",
                    "eligible_members": ["claude_cli", "codex_cli"],
                }
            ],
        }
        options = {
            "workflows": [
                {"id": "with-slots", "parallel": ["claude_cli"], "member_selection": slots},
                # Pre-member-selection daemon payload: no member_selection key.
                {"id": "legacy", "sequence": ["claude_cli"]},
                # A slot with no eligible members disables the whole step.
                {
                    "id": "empty-eligible",
                    "sequence": ["claude_cli"],
                    "member_selection": {"slots": [{"slot": "claude_cli", "eligible_members": []}]},
                },
            ]
        }

        parsed = member_slots_from_options(options)
        self.assertEqual(set(parsed), {"with-slots"})
        self.assertEqual(
            parsed["with-slots"],
            [
                {
                    "slot": "claude_cli",
                    "default": "claude_cli",
                    "eligible_members": ["claude_cli", "codex_cli"],
                }
            ],
        )

    def test_terminal_status_controls_poller_and_read_only_helpers(self):
        self.assertTrue(session_is_terminal({"status": "interrupted"}))
        self.assertFalse(session_is_terminal({"status": "awaiting_input"}))
        self.assertFalse(should_start_poller({"status": "done"}))
        self.assertTrue(should_start_poller({"status": "running"}))
        self.assertTrue(should_start_poller({"status": "awaiting_input"}))

    def test_cli_tui_dispatch_is_additive(self):
        with mock.patch("agent_collab.tui.run_tui", return_value=0) as run_tui:
            code = cli.main(["tui", "--server-url", "http://127.0.0.1:9999", "s1"])

        self.assertEqual(code, 0)
        run_tui.assert_called_once_with(session_id="s1", server_url="http://127.0.0.1:9999")


class DisplayCellWidthTests(unittest.TestCase):
    """Text is measured in terminal cells, not Python characters (issue #54).

    ncurses wraps whatever overflows a row onto column 0 of the next one, where
    body rows (text at x=1) leave it visible as a stray character in the left
    margin. Tabs, wide characters, and control characters are the ways a line
    can pass a ``len()`` budget and still paint too many cells.
    """

    OVERFLOWING = (
        "12\tif value:",  # file-reading tool digest: one tab, up to four cells
        "done ✅ shipped",  # emoji, two cells
        "日本語のテキストです",  # fullwidth CJK, two cells each
        "carriage\rreturn\x07bell",  # control characters, painted as ^X
        "mixed\t日本 ✅\x0btail",  # all three at once
        "⚠️ warn ❤️ heart",  # base + U+FE0F: two code points, two cells
        "1️⃣ then 2⃣ keycaps",  # ASCII base that still paints two cells
    )

    def test_cell_width_counts_wide_characters_as_two(self):
        self.assertEqual(cell_width("abc"), 3)
        self.assertEqual(cell_width("日本語"), 6)
        self.assertEqual(cell_width("✅"), 2)
        # Zero-width joiners and combining marks paint nothing.
        self.assertEqual(cell_width("é"), 1)
        self.assertEqual(cell_width("a‍b"), 2)
        # U+FE0F asks for emoji presentation: a narrow base then paints two.
        self.assertEqual(cell_width("⚠"), 1)
        self.assertEqual(cell_width("⚠️"), 2)
        self.assertEqual(split_display_cells("⚠️x", 1), ("", "⚠️x"))
        # Only a symbol takes emoji presentation; a padding space never does.
        self.assertEqual(cell_width(" " + VARIATION_SELECTOR_16), 1)
        # Keycaps are the one ASCII base that does — their enclosing mark says
        # so. Counted by hand rather than by the helper under test: base, the
        # selector, and the keycap paint two cells together.
        self.assertEqual(cell_width("1️⃣"), 2)
        self.assertEqual(cell_width("#️⃣"), 2)
        self.assertEqual(cell_width("1" + VARIATION_SELECTOR_16), 1)  # no keycap mark
        # The selector is optional: the enclosing mark alone makes a keycap.
        self.assertEqual(cell_width("1⃣"), 2)
        self.assertEqual(cell_width("#⃣"), 2)
        self.assertEqual(fit_display_text("1️⃣x", 2), "1️⃣")
        self.assertEqual(fit_display_text("y" * 39 + "1⃣", 40), "y" * 39)

    def test_sanitize_expands_tabs_and_neutralizes_control_characters(self):
        self.assertEqual(sanitize_display_text("12\tif x:"), "12  if x:")
        self.assertEqual(sanitize_display_text("12\tif x:", collapse_tabs=True), "12 if x:")
        self.assertEqual(sanitize_display_text("a\rb\x07c"), "a b c")
        # Zero-width formatting characters survive: dropping a joiner would
        # split an emoji sequence into wider components.
        self.assertEqual(sanitize_display_text("a‍b"), "a‍b")

    def test_split_and_fit_never_exceed_the_cell_budget(self):
        head, rest = split_display_cells("日本語", 3)
        self.assertEqual((head, rest), ("日", "本語"))  # the straddling glyph moves down
        self.assertEqual(split_display_cells("日", 1), ("", "日"))
        self.assertEqual(fit_display_text("12\tif x:", 4), "12  ")
        self.assertEqual(fit_display_text("日本語", 5), "日本")
        self.assertEqual(fit_display_text("anything", 0), "")

    def test_transcript_wrapping_keeps_every_chunk_inside_its_row(self):
        for text in self.OVERFLOWING:
            for width in (12, 17, 18, 19, 22, 39, 99):
                event = Event.create("claude", "message", text)
                wrapped = wrap_transcript_lines(format_transcript_event(event), width)
                self.assertTrue(wrapped)
                for line in wrapped:
                    self.assertLessEqual(
                        cell_width(line.text), width, f"{text!r} at width {width}: {line.text!r}"
                    )

    def test_details_wrapping_keeps_every_line_inside_the_panel(self):
        for text in self.OVERFLOWING:
            for width in (3, 4, 10, 30, 46):
                for line in wrap_plain_lines((f"workdir: {text}",), width):
                    self.assertLessEqual(cell_width(line), width, f"{text!r} at width {width}")

    def test_tool_digest_collapses_tabs_and_drops_control_characters(self):
        event = Event.create("tool", "tool_call", "  12\tif value:\x07\nmore\nlines")
        (line,) = format_transcript_event(event)

        self.assertEqual(line.text, "tool             12 if value: · +2 lines")
        self.assertNotIn("\t", line.text)

    def test_body_rows_expand_tabs_so_the_gutter_stays_aligned(self):
        event = Event.create("claude", "message", "12\tif value:\nplain")
        lines = format_transcript_event(event)

        self.assertEqual(lines[0].text, f"{'claude':<{GUTTER_WIDTH}} 12  if value:")
        self.assertEqual(cell_width(lines[1].text.split("plain")[0]), GUTTER_WIDTH + 1)

    def test_gutter_label_ellipsizes_by_cells(self):
        label = gutter_label("日本語エージェント識別子")

        self.assertLessEqual(cell_width(label), GUTTER_WIDTH)
        self.assertTrue(label.endswith("…"))

    def test_narrow_width_terminates_even_when_a_glyph_cannot_fit(self):
        wrapped = self._within_a_second(
            wrap_transcript_lines,
            format_transcript_event(Event.create("claude", "message", "日本語")),
            1,
        )

        self.assertTrue(wrapped)  # forward progress rather than an infinite loop

    def test_wide_glyph_after_the_gutter_indent_still_makes_progress(self):
        """Dual review, round 1: both reviewers caught this hang.

        At width 18 the 17-cell continuation indent leaves one cell, which no
        two-cell glyph fits. Re-taking cells from ``indent + remainder`` then
        peeled off only the indent and rebuilt the same line forever, freezing
        the interface on a 19-column terminal. ``wrap_plain_lines`` has the
        same shape at width 3 against its two-space indent.
        """
        wrapped = self._within_a_second(
            wrap_transcript_lines,
            format_transcript_event(Event.create("claude", "message", "hello world and 日本語")),
            18,
        )
        self.assertTrue(wrapped)
        self.assertIn("日", "".join(line.text for line in wrapped))  # content kept
        for line in wrapped:
            self.assertLessEqual(cell_width(line.text), 18, line.text)

        plain = self._within_a_second(wrap_plain_lines, ("workdir: 日本語です",), 3)
        self.assertTrue(plain)
        for line in plain:
            self.assertLessEqual(cell_width(line), 3, line)

    def test_presentation_selector_travels_with_its_base(self):
        """Round 5: an orphaned U+FE0F widened the space in front of it.

        Forced through a one-cell body budget, the base was emitted alone and
        the selector opened the next chunk, where the continuation indent's
        last space then measured two cells and pushed the row over budget.
        """
        wrapped = self._within_a_second(
            wrap_transcript_lines,
            format_transcript_event(Event.create("claude", "message", "⚠️warn")),
            18,
        )
        for line in wrapped:
            self.assertLessEqual(cell_width(line.text), 18, line.text)
            self.assertFalse(line.text.lstrip().startswith(VARIATION_SELECTOR_16), line.text)

        plain = self._within_a_second(wrap_plain_lines, ("note: ⚠️ see log",), 3)
        for line in plain:
            self.assertLessEqual(cell_width(line), 3, line)

    def _within_a_second(self, func, *args):
        """Run ``func`` on a worker so a wrapping hang fails instead of freezing."""
        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.setdefault("value", func(*args)), daemon=True
        )
        worker.start()
        worker.join(5.0)
        self.assertFalse(worker.is_alive(), f"{func.__name__} did not terminate")
        return outcome["value"]


class TuiCoreMockDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._home_tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": self._home_tmp.name})
        patcher.start()
        self.addAsyncCleanup(self._cleanup, patcher, self._home_tmp)

    async def _cleanup(self, patcher, home_tmp):
        patcher.stop()
        home_tmp.cleanup()

    async def test_mock_daemon_events_feed_transcript_helpers_to_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            state = await manager.start_session(
                StartSessionRequest(
                    task="tui mock task", mock=True, max_turns=1, timeout=5, workdir=root
                )
            )
            final = await self._wait_for_terminal(manager, state.session_id)

            batch = manager.read_events(state.session_id, 0)

        lines = render_transcript_lines(format_transcript_events(batch.events))
        self.assertGreater(batch.cursor, 0)
        self.assertTrue(any(line.startswith("prompt") for line in lines))
        self.assertIn("tui mock task", "\n".join(lines))
        self.assertTrue(session_is_terminal(final.to_dict()))
        self.assertFalse(should_start_poller(final.to_dict()))

    async def _wait_for_terminal(self, manager, session_id):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while loop.time() < deadline:
            state = manager.get_session(session_id)
            if state.status != "running":
                return state
            await asyncio.sleep(0.02)
        self.fail(f"session {session_id} did not finish")


if __name__ == "__main__":
    unittest.main()
