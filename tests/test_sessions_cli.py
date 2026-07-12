import contextlib
import io
import unittest
from unittest import mock

from agent_collab.api_schema import PruneResultModel, PruneSessionDetailModel
from agent_collab.cli import main
from agent_collab.client import ClientError


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


if __name__ == "__main__":
    unittest.main()
