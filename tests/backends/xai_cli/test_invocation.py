"""Hermetic coverage for Grok CLI resume-by-id finalizer."""

from __future__ import annotations

import unittest

from agent_collab.backends.common.cli import prepare_cli_invocation
from agent_collab.backends.xai_cli.invocation import (
    CLI_OWNERSHIP_FLAGS,
    finalize_xai_cli_invocation,
)
from agent_collab.sandbox.specs import SandboxFailure
from types import SimpleNamespace


class XaiCliInvocationTests(unittest.TestCase):
    def test_finalizer_is_identity_without_descriptor(self):
        ordinary = ("grok", "--output-format", "stream-json", "-p")
        self.assertEqual(finalize_xai_cli_invocation(ordinary, None), ordinary)

    def test_finalizer_inserts_resume_before_print_marker(self):
        prepared = ("grok", "--output-format", "stream-json", "-p")
        result = finalize_xai_cli_invocation(prepared, {"provider_session_id": "sess-g"})
        self.assertEqual(
            result,
            ("grok", "--output-format", "stream-json", "--resume", "sess-g", "-p"),
        )
        self.assertLess(result.index("--resume"), result.index("-p"))
        self.assertNotIn("--continue", result)
        self.assertNotIn("--session-id", result)

    def test_user_session_id_is_rejected_before_finalizer(self):
        plan = SimpleNamespace(prepare_inner=lambda command: tuple(command))
        with self.assertRaises(SandboxFailure) as raised:
            prepare_cli_invocation(
                ("grok", "--session-id", "owned", "-p"),
                plan,
                None,
                finalizer=finalize_xai_cli_invocation,
                ownership_flags=CLI_OWNERSHIP_FLAGS,
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_short_continue_and_resume_flags_are_rejected_on_direct_path(self):
        plan = SimpleNamespace(prepare_inner=lambda command: tuple(command))
        for command in (
            ("grok", "-c", "-p"),
            ("grok", "-r", "-p"),
            ("grok", "-csession", "-p"),
        ):
            with self.subTest(command=command):
                with self.assertRaises(SandboxFailure) as raised:
                    prepare_cli_invocation(
                        command,
                        plan,
                        None,
                        finalizer=finalize_xai_cli_invocation,
                        ownership_flags=CLI_OWNERSHIP_FLAGS,
                    )
                self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
