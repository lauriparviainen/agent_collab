import contextlib
import io
import unittest
from unittest import mock

from agent_collab.api_schema import (
    PruneResultModel,
    PruneSessionDetailModel,
    SessionResultModel,
    SessionStateModel,
)
from agent_collab.cli import main
from agent_collab.client import ClientError


def _session_state():
    # Settings carry command_preview, one of the fields the compact view drops;
    # the CLI must request detail="full" to render it.
    return SessionStateModel.from_dict(
        {
            "session_id": "s1",
            "status": "running",
            "task": "t",
            "workflow": "solo",
            "workdir": "/w",
            "jsonl_path": "/l.jsonl",
            "markdown_path": "/l.md",
            "created_at": "t",
            "updated_at": "t",
            "settings": {
                "workflow": {"sequence": ["claude_cli"]},
                "agents": {
                    "claude_cli": {
                        "type": "claude",
                        "model": "m",
                        "command_preview": ["claude", "-p"],
                    }
                },
            },
        }
    )


def _session_result(settled, **overrides):
    data = {
        "session_id": "s1",
        "status": "done" if settled else "running",
        "terminal": settled,
        "settled": settled,
        "cursor": 5 if settled else 1,
        "answers": (
            [{"agent_id": "claude_cli", "text": "the answer", "event_id": 3, "timestamp": "t"}]
            if settled
            else []
        ),
    }
    data.update(overrides)
    return SessionResultModel.from_dict(data)


def _result(**overrides):
    values = {
        "apply": False,
        "cutoff": "2026-06-12T12:00:00+00:00",
        "keep": 0,
        "candidates": 1,
        "pruned": 0,
        "failed": 0,
        "bytes_reclaimed": 42,
        "unparseable_records": 0,
        "sessions": [
            PruneSessionDetailModel(
                session_id="daemon-old",
                status="done",
                disposition="preview",
                effective_at="2026-05-01T00:00:00+00:00",
                removed_files=["/home/x/.agent-collab/data/sessions/daemon-old.jsonl"],
                preserved_files=[],
                bytes_reclaimed=42,
            )
        ],
    }
    values.update(overrides)
    return PruneResultModel(**values)


def _run(argv, client):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch("agent_collab.cli._client", return_value=client):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class StartStatusDetailCliTests(unittest.TestCase):
    """Stage 2: the CLI renders command_preview/backend_summary, so it asks for
    the full settings view that the wire now compacts by default."""

    def test_start_requests_full_detail_and_renders_command_preview(self):
        captured = {}
        client = mock.MagicMock()

        def start_session(payload):
            captured["payload"] = payload
            return _session_state()

        client.start_session.side_effect = start_session
        code, out, _ = _run(["start", "t", "--workdir", "/tmp", "--mock"], client)

        self.assertEqual(code, 0)
        self.assertEqual(captured["payload"].get("detail"), "full")
        self.assertIn("command_preview", out)

    def test_status_requests_full_detail_and_renders_command_preview(self):
        client = mock.MagicMock()
        client.get_session.return_value = _session_state()
        code, out, _ = _run(["status", "s1"], client)

        self.assertEqual(code, 0)
        client.get_session.assert_called_once_with("s1", detail="full")
        self.assertIn("command_preview", out)


class SessionsPruneCliTests(unittest.TestCase):
    def test_default_invocation_previews_and_suggests_apply(self):
        client = mock.Mock()
        client.prune_sessions.return_value = _result()

        code, out, _err = _run(["sessions", "prune"], client)

        self.assertEqual(code, 0)
        client.prune_sessions.assert_called_once_with({"apply": False, "keep": 0})
        self.assertIn("mode: preview", out)
        self.assertIn("daemon-old [done]", out)
        self.assertIn("would remove", out)
        self.assertIn("rerun with --apply", out)

    def test_apply_passes_flags_and_reports_removals(self):
        client = mock.Mock()
        client.prune_sessions.return_value = _result(
            apply=True,
            pruned=1,
            sessions=[
                PruneSessionDetailModel(
                    session_id="daemon-old",
                    status="stopped",
                    disposition="pruned",
                    removed_files=["/tmp/daemon-old.jsonl"],
                    preserved_files=[{"path": "/elsewhere/custom.md", "reason": "symlink"}],
                    bytes_reclaimed=42,
                )
            ],
        )

        code, out, _err = _run(
            ["sessions", "prune", "--apply", "--older-than", "7d", "--keep", "5"], client
        )

        self.assertEqual(code, 0)
        client.prune_sessions.assert_called_once_with(
            {"apply": True, "keep": 5, "older_than": "7d"}
        )
        self.assertIn("mode: apply", out)
        self.assertIn("removed: /tmp/daemon-old.jsonl", out)
        self.assertIn("preserved: /elsewhere/custom.md (symlink)", out)
        self.assertNotIn("rerun with --apply", out)

    def test_empty_preview_says_nothing_to_prune(self):
        client = mock.Mock()
        client.prune_sessions.return_value = _result(candidates=0, bytes_reclaimed=0, sessions=[])

        code, out, _err = _run(["sessions", "prune", "--dry-run"], client)

        self.assertEqual(code, 0)
        self.assertIn("nothing to prune", out)

    def test_json_output_is_machine_readable(self):
        import json as json_module

        client = mock.Mock()
        client.prune_sessions.return_value = _result()

        code, out, _err = _run(["sessions", "prune", "--json"], client)

        self.assertEqual(code, 0)
        payload = json_module.loads(out)
        self.assertEqual(payload["candidates"], 1)
        self.assertEqual(payload["sessions"][0]["session_id"], "daemon-old")

    def test_invalid_duration_fails_locally_without_a_request(self):
        client = mock.Mock()

        code, _out, err = _run(["sessions", "prune", "--older-than", "0d"], client)

        self.assertEqual(code, 1)
        self.assertIn("invalid duration", err)
        client.prune_sessions.assert_not_called()

    def test_negative_keep_fails_locally_without_a_request(self):
        client = mock.Mock()

        code, _out, err = _run(["sessions", "prune", "--keep", "-1"], client)

        self.assertEqual(code, 1)
        self.assertIn("keep must be >= 0", err)
        client.prune_sessions.assert_not_called()

    def test_dry_run_and_apply_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["sessions", "prune", "--dry-run", "--apply"])

        self.assertEqual(ctx.exception.code, 2)

    def test_daemon_errors_surface_as_cli_errors(self):
        client = mock.Mock()
        client.prune_sessions.side_effect = ClientError(
            "automatic retention is disabled (sessions.retention_days = 0); "
            "pass older_than to prune manually"
        )

        code, _out, err = _run(["sessions", "prune"], client)

        self.assertEqual(code, 1)
        self.assertIn("retention is disabled", err)


class ResultCliTests(unittest.TestCase):
    def test_loops_through_heartbeats_until_settled(self):
        client = mock.Mock()
        client.wait_result.side_effect = [_session_result(False), _session_result(True)]

        code, out, _err = _run(["result", "s1"], client)

        self.assertEqual(code, 0)
        # The command absorbs the heartbeat internally and re-polls until settled.
        self.assertEqual(client.wait_result.call_count, 2)
        self.assertIn("status", out)
        self.assertIn("done", out)
        self.assertIn("the answer", out)

    def test_timeout_ms_expiry_exits_with_distinct_code(self):
        client = mock.Mock()
        client.wait_result.return_value = _session_result(False)

        code, out, _err = _run(["result", "s1", "--timeout-ms", "0"], client)

        self.assertEqual(code, 124)
        client.wait_result.assert_called_once_with("s1", 0)
        self.assertIn("did not settle", out)

    def test_json_emits_settled_result(self):
        import json as json_module

        client = mock.Mock()
        client.wait_result.side_effect = [_session_result(True)]

        code, out, _err = _run(["result", "s1", "--json"], client)

        self.assertEqual(code, 0)
        payload = json_module.loads(out)
        self.assertTrue(payload["settled"])
        self.assertEqual(payload["answers"][0]["agent_id"], "claude_cli")

    def test_daemon_errors_surface_as_cli_errors(self):
        client = mock.Mock()
        client.wait_result.side_effect = ClientError("unknown session_id s1")

        code, _out, err = _run(["result", "s1"], client)

        self.assertEqual(code, 1)
        self.assertIn("unknown session_id", err)


if __name__ == "__main__":
    unittest.main()
