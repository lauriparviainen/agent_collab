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
    advance_cursor_state,
    agents_from_options,
    agents_from_session,
    build_new_session_payload,
    clamp_scroll,
    follow_scroll,
    format_session_picker_lines,
    format_session_details,
    format_transcript_event,
    format_transcript_events,
    make_session_picker,
    move_session_picker,
    parse_input,
    render_transcript_lines,
    reset_cursor_state,
    resolve_agent_selector,
    scroll_by,
    select_latest_session_id,
    selected_picker_session_id,
    session_is_terminal,
    should_start_poller,
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

        self.assertEqual(lines, ("CLAUDE  hello", "        world"))

        long_event = Event.create("tool", "command", "abcdef ghijkl mnop")
        wrapped = wrap_transcript_lines(format_transcript_event(long_event), 13)
        rendered = render_transcript_lines(wrapped)

        self.assertEqual(rendered[0], "TOOL    abcde")
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
                "workflow": {"name": "single-codex", "sequence": ["codex"]},
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

        self.assertIn("workflow: single-codex", text)
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

        self.assertEqual(parse_input("/unknown").kind, "invalid")
        self.assertEqual(parse_input("#reviewer").kind, "invalid")

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
            {"session_id": "old", "status": "done", "workflow": "single-codex", "updated_at": "2026-07-08T00:00:00+00:00", "workdir": "/old"},
            {"session_id": "new", "status": "running", "workflow": "cross-review", "updated_at": "2026-07-08T01:00:00+00:00", "workdir": "/new"},
        ]

        self.assertEqual([session["session_id"] for session in sort_sessions_latest_first(sessions)], ["new", "old"])

        picker = make_session_picker(sessions, current_session_id="old")
        self.assertEqual(selected_picker_session_id(picker), "old")
        picker = move_session_picker(picker, -1)
        self.assertEqual(selected_picker_session_id(picker), "new")

        rendered = "\n".join(format_session_picker_lines(picker))
        self.assertIn("SESSION_ID", rendered)
        self.assertIn("> new", rendered)

    def test_options_helpers_extract_enabled_agents_and_workflows(self):
        options = {
            "agents": [
                {"id": "claude", "type": "claude", "enabled": True},
                {"id": "codex", "type": "codex", "enabled": False},
            ],
            "workflows": [
                {"id": "single-claude", "sequence": ["claude"]},
                {"id": "compare", "sequence": ["claude", "codex"]},
            ],
        }

        self.assertEqual(agents_from_options(options), (AgentRef(id="claude", type="claude", enabled=True),))
        self.assertEqual(workflow_ids_from_options(options), ("single-claude", "compare"))

    def test_new_session_payload_matches_daemon_start_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_new_session_payload(task=" task ", workflow="single-codex", workdir=tmp)

        self.assertEqual(payload["task"], "task")
        self.assertEqual(payload["workflow"], "single-codex")
        self.assertEqual(payload["workdir"], str(Path(tmp).resolve()))
        self.assertEqual(payload["max_turns"], 3)
        self.assertEqual(payload["timeout"], 900)
        self.assertEqual(payload["mock"], False)
        self.assertEqual(payload["dry_run"], False)
        self.assertEqual(payload["codex_options"], {})
        self.assertEqual(payload["claude_options"], {})

    def test_terminal_status_controls_poller_and_read_only_helpers(self):
        self.assertTrue(session_is_terminal({"status": "interrupted"}))
        self.assertFalse(should_start_poller({"status": "done"}))
        self.assertTrue(should_start_poller({"status": "running"}))

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
        self.assertTrue(any(line.startswith("HUMAN") for line in lines))
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
