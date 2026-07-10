import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab import cli
from agent_collab.api_schema import EventBatchModel, SessionListModel, SessionStateModel
from agent_collab.events import Event
from agent_collab.watch import resolve_jsonl_path, watch_jsonl


def _session_dict(session_id, **overrides):
    """Minimal wire-shaped session dict for SessionStateModel.from_dict."""
    data = {"session_id": session_id, "status": "running"}
    data.update(overrides)
    return data


def _write_events(path, events):
    path.write_text("\n".join(event.to_json() for event in events) + "\n", encoding="utf-8")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self._home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._home_tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": self._home_tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_watch_jsonl_prints_existing_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            _write_events(
                path,
                [
                    Event.create("human", "message", "hello"),
                    Event.create("referee", "status", "ready"),
                ],
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                watch_jsonl(path, follow=False, color=False)

            text = output.getvalue()
            self.assertIn("HUMAN   hello", text)
            self.assertIn("REFEREE ready", text)

    def test_watch_jsonl_respects_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            _write_events(
                path,
                [
                    Event.create("human", "message", "first"),
                    Event.create("codex", "message", "second"),
                    Event.create("claude", "message", "third"),
                ],
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                watch_jsonl(path, follow=False, start_cursor=1, color=False)

            text = output.getvalue()
            self.assertNotIn("first", text)
            self.assertIn("CODEX   second", text)
            self.assertIn("CLAUDE  third", text)

    def test_resolve_jsonl_path_defaults_to_global_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = Path(self._home_tmp.name).resolve() / "data" / "sessions" / "mcp-1.jsonl"

            self.assertEqual(resolve_jsonl_path(workdir=root, session_id="mcp-1"), expected)
            self.assertEqual(resolve_jsonl_path("mcp-1", workdir=root), expected)

    def test_resolve_jsonl_path_finds_global_home_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_log = Path(self._home_tmp.name).resolve() / "data" / "sessions" / "mcp-1.jsonl"
            global_log.parent.mkdir(parents=True)
            global_log.touch()

            self.assertEqual(resolve_jsonl_path(workdir=root, session_id="mcp-1"), global_log)

    def test_resolve_jsonl_path_falls_back_to_project_data_session_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / ".agent-collab" / "data" / "sessions" / "mcp-1.jsonl"
            data.parent.mkdir(parents=True)
            data.touch()

            self.assertEqual(resolve_jsonl_path(workdir=root, session_id="mcp-1"), data)

    def test_resolve_jsonl_path_falls_back_to_legacy_session_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".agent-collab" / "sessions" / "mcp-1.jsonl"
            legacy.parent.mkdir(parents=True)
            legacy.touch()

            self.assertEqual(resolve_jsonl_path(workdir=root, session_id="mcp-1"), legacy)

    def test_resolve_jsonl_path_prefers_data_session_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / ".agent-collab" / "data" / "sessions" / "mcp-1.jsonl"
            legacy = root / ".agent-collab" / "sessions" / "mcp-1.jsonl"
            data.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            data.touch()
            legacy.touch()

            self.assertEqual(resolve_jsonl_path(workdir=root, session_id="mcp-1"), data)

    def test_resolve_jsonl_path_accepts_direct_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.touch()

            self.assertEqual(resolve_jsonl_path(path), path.resolve())

    def test_resolve_jsonl_path_uses_latest_log_when_session_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / ".agent-collab" / "sessions"
            log_dir.mkdir(parents=True)
            old_path = log_dir / "old.jsonl"
            new_path = log_dir / "new.jsonl"
            old_path.write_text("{}\n", encoding="utf-8")
            new_path.write_text("{}\n", encoding="utf-8")
            os.utime(old_path, (1, 1))
            os.utime(new_path, (2, 2))

            self.assertEqual(resolve_jsonl_path(log_dir=log_dir), new_path)

    def test_watch_jsonl_prints_malformed_lines_as_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(
                Event.create("human", "message", "ok").to_json() + "\nnot-json\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                watch_jsonl(path, follow=False, color=False)

            text = output.getvalue()
            self.assertIn("HUMAN   ok", text)
            self.assertIn("ERROR   malformed JSONL line 2: not-json", text)

    def test_cli_watch_no_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / ".agent-collab" / "sessions"
            log_dir.mkdir(parents=True)
            path = log_dir / "mcp-1.jsonl"
            _write_events(path, [Event.create("codex", "message", "from cli")])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["watch", "--workdir", str(root), "--session-id", "mcp-1", "--no-follow"])

            self.assertEqual(code, 0)
            self.assertIn("CODEX   from cli", output.getvalue())

    def test_cli_watch_without_session_uses_latest_daemon_session(self):
        class FakeClient:
            def list_sessions(self):
                return SessionListModel.from_dict(
                    {
                        "sessions": [
                            _session_dict("old", updated_at="2026-01-01T00:00:00+00:00"),
                            _session_dict("new", updated_at="2026-01-02T00:00:00+00:00"),
                        ]
                    }
                )

            def read_events(self, session_id, cursor):
                self.session_id = session_id
                return EventBatchModel.from_dict(
                    {
                        "session_id": session_id,
                        "cursor": 1,
                        "events": [Event.create("human", "message", f"from {session_id}").to_dict()],
                    }
                )

        fake = FakeClient()
        output = io.StringIO()
        with mock.patch("agent_collab.cli._client", return_value=fake):
            with contextlib.redirect_stdout(output):
                code = cli.main(["watch", "--no-follow", "--no-color"])

        self.assertEqual(code, 0)
        self.assertIn("HUMAN   from new", output.getvalue())

    def test_cli_start_watch_starts_then_watches_session(self):
        class FakeClient:
            def __init__(self):
                self.sent = False

            def start_session(self, payload):
                self.payload = payload
                return SessionStateModel.from_dict(
                    _session_dict(
                        "started",
                        status="running",
                        workdir=payload["workdir"],
                        jsonl_path="/tmp/started.jsonl",
                        markdown_path="/tmp/started.md",
                    )
                )

            def wait_events(self, session_id, cursor, timeout_ms):
                if self.sent:
                    return EventBatchModel.from_dict({"session_id": session_id, "cursor": cursor, "events": []})
                self.sent = True
                return EventBatchModel.from_dict(
                    {
                        "session_id": session_id,
                        "cursor": 1,
                        "events": [Event.create("referee", "message", f"watching {session_id}").to_dict()],
                    }
                )

            def get_session(self, session_id):
                return SessionStateModel.from_dict(_session_dict(session_id, status="done"))

        fake = FakeClient()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("agent_collab.cli._client", return_value=fake):
                with contextlib.redirect_stdout(output):
                    code = cli.main([
                        "start",
                        "--mock",
                        "--watch",
                        "--watch-wait-ms",
                        "1",
                        "--no-color",
                        "--workdir",
                        tmp,
                        "task",
                    ])

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("session_id: started", text)
        self.assertIn("REFEREE watching started", text)

    def test_cli_start_passes_typed_option_json(self):
        class FakeClient:
            def start_session(self, payload):
                self.payload = payload
                return SessionStateModel.from_dict(
                    _session_dict(
                        "started",
                        status="running",
                        workdir=payload["workdir"],
                        jsonl_path="/tmp/started.jsonl",
                        markdown_path="/tmp/started.md",
                    )
                )

        fake = FakeClient()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("agent_collab.cli._client", return_value=fake):
                with contextlib.redirect_stdout(output):
                    code = cli.main(
                        [
                            "start",
                            "--mock",
                            "--workdir",
                            tmp,
                            "--backend-options",
                            '{"codex_cli":{"reasoning_effort":"medium"},"claude_cli":{"model":"sonnet"}}',
                            "task",
                        ]
                    )

        self.assertEqual(code, 0)
        self.assertEqual(
            fake.payload["backend_options"],
            {"codex_cli": {"reasoning_effort": "medium"}, "claude_cli": {"model": "sonnet"}},
        )


if __name__ == "__main__":
    unittest.main()
