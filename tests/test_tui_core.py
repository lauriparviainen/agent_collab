import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab import cli
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.events import Event
from agent_collab.tui_core import (
    AgentRef,
    CursorState,
    ScrollState,
    accept_slash_completion,
    advance_cursor_state,
    agents_from_options,
    agents_from_session,
    build_new_session_payload,
    clamp_scroll,
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
    parse_input,
    render_transcript_lines,
    reset_cursor_state,
    resolve_agent_selector,
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
        self.assertEqual(lines, ("claude  hello", "        world"))

        long_event = Event.create("tool", "command", "abcdef ghijkl mnop")
        wrapped = wrap_transcript_lines(format_transcript_event(long_event), 13)
        rendered = render_transcript_lines(wrapped)

        self.assertEqual(rendered[0], "tool    abcde")
        self.assertTrue(rendered[1].startswith("        "))

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
                "workflow": {"name": "solo-codex", "sequence": ["codex"]},
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

        self.assertIn("workflow: solo-codex", text)
        self.assertIn("sequence: codex", text)
        self.assertIn("workdir: /repo", text)
        self.assertIn("mock: false", text)
        self.assertIn("dry_run: true", text)
        self.assertIn("jsonl_path: /logs/s1.jsonl", text)
        self.assertIn("agent codex: type=codex model=gpt-5 thinking_level=high sandbox=read-only approval_policy=never", text)
        self.assertIn("command_preview: codex --model gpt-5", text)
        self.assertNotIn("ended_at:", text)

    def test_parse_input_covers_slash_plain_and_directed_forms(self):
        self.assertEqual(parse_input("/help").command, "help")
        session = parse_input("/session daemon-1")
        self.assertEqual(session.kind, "slash")
        self.assertEqual(session.args, ("daemon-1",))
        self.assertEqual(parse_input("plain note").kind, "text")

        directed = parse_input("#reviewer take a look")
        self.assertEqual(directed.kind, "directed")
        self.assertEqual(directed.agent, "reviewer")
        self.assertEqual(directed.message, "take a look")

        asked = parse_input("/ask claude compare the options")
        self.assertEqual(asked.kind, "directed")
        self.assertEqual(asked.command, "ask")
        self.assertEqual(asked.agent, "claude")
        self.assertEqual(asked.message, "compare the options")

        self.assertEqual(parse_input("/unknown").kind, "invalid")
        self.assertEqual(parse_input("#reviewer").kind, "invalid")
        self.assertEqual(parse_input("/ask claude").kind, "invalid")

    def test_slash_command_completion_filters_deterministically(self):
        all_matches = filter_slash_commands("/")
        s_matches = filter_slash_commands("/s")

        self.assertEqual(all_matches[0].name, "/help")
        self.assertIn("/ask", [match.name for match in all_matches])
        self.assertEqual([match.name for match in s_matches], ["/sessions", "/session", "/stop"])
        self.assertEqual(filter_slash_commands("/ask claude"), ())

    def test_slash_completion_state_moves_and_accepts_selected_command(self):
        state = make_slash_completion("/s")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual([match.name for match in state.matches], ["/sessions", "/session", "/stop"])
        self.assertEqual(selected_slash_command(state), "/sessions")

        state = move_slash_completion(state, 1)

        self.assertEqual(selected_slash_command(state), "/session")
        self.assertFalse(slash_completion_matches_input("/se", state))
        self.assertTrue(slash_completion_matches_input("/session", state))
        self.assertTrue(slash_completion_matches_input("/SESSION", state))
        self.assertEqual(accept_slash_completion("/se", state), "/session ")
        # Headerless menu with ▸ selection marker (Stage 1b amendment).
        self.assertIn("▸ /session", "\n".join(format_slash_completion_lines(state, max_items=2)))
        self.assertEqual(move_slash_completion(state, 99).index, len(state.matches) - 1)
        self.assertEqual(move_slash_completion(state, -99).index, 0)

    def test_slash_completion_hides_for_arguments_and_keeps_no_match_state(self):
        self.assertIsNone(make_slash_completion("plain text"))
        self.assertIsNone(make_slash_completion("/ask claude"))
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
        self.assertEqual(format_activity_indicator({"status": "running"}, tick=0, utf8=False), ". running")
        self.assertEqual(format_activity_indicator({"status": "running"}, tick=2, utf8=False), "... running")
        self.assertEqual(format_activity_indicator({"status": "awaiting_input"}, tick=2), "awaiting input")
        self.assertEqual(format_activity_indicator({"status": "done"}, tick=3), "read-only done")

    def test_agent_resolution_uses_active_session_settings(self):
        session = {
            "settings": {
                "agents": {
                    "claude-a": {"type": "claude"},
                    "claude-b": {"type": "claude"},
                    "worker": {"type": "codex"},
                }
            }
        }
        agents = agents_from_session(session)

        self.assertEqual(resolve_agent_selector("worker", agents).agent_id, "worker")
        self.assertEqual(resolve_agent_selector("codex", agents).agent_id, "worker")

        ambiguous = resolve_agent_selector("claude", agents)
        self.assertFalse(ambiguous.ok)
        self.assertIn("claude-a", ambiguous.error)
        self.assertIn("claude-b", ambiguous.error)

        missing = resolve_agent_selector("reviewer", agents)
        self.assertFalse(missing.ok)
        self.assertIn("valid agent ids", missing.error)

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

    def test_cursor_state_resets_and_drops_stale_batches(self):
        state = reset_cursor_state(CursorState(), "s1")
        self.assertEqual(state.cursor, 0)
        self.assertEqual(state.epoch, 1)

        advanced, accepted = advance_cursor_state(state, session_id="s1", cursor=4, epoch=1)
        self.assertTrue(accepted)
        self.assertEqual(advanced.cursor, 4)

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
            {"session_id": "old", "status": "done", "workflow": "solo-codex", "updated_at": "2026-07-08T00:00:00+00:00", "workdir": "/old"},
            {"session_id": "new", "status": "running", "workflow": "cross-review", "updated_at": "2026-07-08T01:00:00+00:00", "workdir": "/new"},
        ]

        self.assertEqual([session["session_id"] for session in sort_sessions_latest_first(sessions)], ["new", "old"])

        picker = make_session_picker(sessions, current_session_id="old")
        self.assertEqual(selected_picker_session_id(picker), "old")
        picker = move_session_picker(picker, -1)
        self.assertEqual(selected_picker_session_id(picker), "new")

        rendered = "\n".join(format_session_picker_lines(picker))
        # Target delta: lowercase columns and ▸ selection marker.
        self.assertIn("session", rendered)
        self.assertIn("▸   new", rendered)

    def test_options_helpers_extract_enabled_agents_and_workflows(self):
        options = {
            "agents": [
                {"id": "claude", "type": "claude", "enabled": True},
                {"id": "codex", "type": "codex", "enabled": False},
            ],
            "workflows": [
                {"id": "solo-claude", "sequence": ["claude"]},
                {"id": "compare", "sequence": ["claude", "codex"]},
            ],
        }

        self.assertEqual(agents_from_options(options), (AgentRef(id="claude", type="claude", enabled=True),))
        self.assertEqual(workflow_ids_from_options(options), ("solo-claude", "compare"))

    def test_new_session_payload_matches_daemon_start_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_new_session_payload(task=" task ", workflow="solo-codex", workdir=tmp)

        self.assertEqual(payload["task"], "task")
        self.assertEqual(payload["workflow"], "solo-codex")
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
            workflow="solo-codex",
            workdir=tmp,
            interactive=True,
            interactive_idle_timeout=30,
        )

        self.assertEqual(interactive_payload["interactive"], True)
        self.assertEqual(interactive_payload["interactive_idle_timeout"], 30.0)

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
                StartSessionRequest(task="tui mock task", mock=True, max_turns=1, timeout=5, workdir=root)
            )
            final = await self._wait_for_terminal(manager, state.session_id)

            batch = manager.read_events(state.session_id, 0)

        lines = render_transcript_lines(format_transcript_events(batch.events))
        self.assertGreater(batch.cursor, 0)
        self.assertTrue(any(line.startswith("human") for line in lines))
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
