"""Hermetic coverage for Codex CLI ``exec resume`` finalizer."""

from __future__ import annotations

import unittest

from agent_collab.backends.codex_cli.invocation import finalize_codex_cli_invocation
from agent_collab.sandbox.specs import SandboxFailure


class CodexCliInvocationTests(unittest.TestCase):
    def test_finalizer_is_identity_without_descriptor(self):
        ordinary = ("codex", "exec", "--json", "--sandbox", "read-only")
        self.assertEqual(finalize_codex_cli_invocation(ordinary, None), ordinary)

    def test_finalizer_rewrites_to_exec_resume(self):
        prepared = (
            "codex",
            "--sandbox",
            "workspace-write",
            "exec",
            "--json",
            "--model",
            "gpt-5.6-luna",
        )
        result = finalize_codex_cli_invocation(prepared, {"provider_session_id": "thread-1"})
        self.assertEqual(
            result,
            (
                "codex",
                "--sandbox",
                "workspace-write",
                "exec",
                "resume",
                "--json",
                "--model",
                "gpt-5.6-luna",
                "thread-1",
            ),
        )
        self.assertEqual(result[result.index("exec") + 1], "resume")
        self.assertNotIn("--continue", result)

    def test_finalizer_rejects_ordinary_resume_token(self):
        with self.assertRaises(SandboxFailure) as raised:
            finalize_codex_cli_invocation(
                ("codex", "exec", "resume", "--json"),
                {"provider_session_id": "thread-1"},
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
