"""Hermetic coverage for Claude CLI resume-by-id finalizer."""

from __future__ import annotations

import unittest

from agent_collab.backends.claude_cli.invocation import finalize_claude_cli_invocation
from agent_collab.sandbox.specs import SandboxFailure


class ClaudeCliInvocationTests(unittest.TestCase):
    def test_finalizer_is_identity_without_descriptor(self):
        ordinary = ("claude", "--output-format", "stream-json", "-p")
        self.assertEqual(finalize_claude_cli_invocation(ordinary, None), ordinary)

    def test_finalizer_inserts_resume_before_print_marker(self):
        prepared = (
            "claude",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "default",
            "-p",
        )
        result = finalize_claude_cli_invocation(prepared, {"provider_session_id": "sess-1"})
        self.assertEqual(
            result,
            (
                "claude",
                "--output-format",
                "stream-json",
                "--permission-mode",
                "default",
                "--resume",
                "sess-1",
                "-p",
            ),
        )
        self.assertLess(result.index("--resume"), result.index("-p"))
        self.assertNotIn("--continue", result)

    def test_finalizer_rejects_missing_id_or_print_marker(self):
        with self.assertRaises(SandboxFailure):
            finalize_claude_cli_invocation(("claude", "-p"), {})
        with self.assertRaises(SandboxFailure) as raised:
            finalize_claude_cli_invocation(
                ("claude", "--output-format", "stream-json"),
                {"provider_session_id": "sess-1"},
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_inner_command_invalid")
