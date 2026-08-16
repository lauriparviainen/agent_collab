import unittest

from agent_collab.backends.antigravity_cli.backend import AntigravityCliBackend
from agent_collab.backends.claude_cli.backend import ClaudeCliBackend
from agent_collab.backends.codex_cli.backend import CodexCliBackend
from types import SimpleNamespace

from agent_collab.backends.antigravity_cli.invocation import (
    CLI_OWNERSHIP_FLAGS,
    finalize_antigravity_cli_invocation,
)
from agent_collab.backends.common.cli import (
    config_value,
    flag_value,
    insert_before_print_prompt,
    prepare_cli_invocation,
)
from agent_collab.sandbox.specs import SandboxFailure
from agent_collab.backends.xai_cli.backend import XaiCliBackend
from agent_collab.config import AgentConfig


class RunnerSourceAttributionTests(unittest.TestCase):
    def test_renamed_agent_runner_source_is_the_provider_type(self):
        """Stderr attribution must follow the provider, not the display id (M5)."""
        for backend in (
            ClaudeCliBackend(),
            CodexCliBackend(),
            AntigravityCliBackend(),
            XaiCliBackend(),
        ):
            with self.subTest(backend=backend.agent_type):
                agent = AgentConfig(id="reviewer", type=backend.agent_type, command="provider-cli")
                runner = backend.create_runner(agent, False, {})
                self.assertEqual(runner.source, backend.agent_type)
                self.assertEqual(runner.name, "reviewer")


class PrepareCliInvocationTests(unittest.TestCase):
    def test_identity_finalizer_and_prepare_inner_run_once(self):
        plan = SimpleNamespace(prepare_inner=lambda command: tuple(command) + ("--inner",))
        prepared = prepare_cli_invocation(
            ["agy", "-p"],
            plan,
            None,
            finalizer=finalize_antigravity_cli_invocation,
            ownership_flags=CLI_OWNERSHIP_FLAGS,
        )
        self.assertEqual(prepared, ("agy", "-p", "--inner"))

    def test_validated_descriptor_inserts_conversation_after_prepare_inner(self):
        plan = SimpleNamespace(
            prepare_inner=lambda command: ("agy", "--sandbox=false", "-p"),
        )
        prepared = prepare_cli_invocation(
            ["agy", "-p"],
            plan,
            {"provider_session_id": "conv-1"},
            finalizer=finalize_antigravity_cli_invocation,
            ownership_flags=CLI_OWNERSHIP_FLAGS,
        )
        self.assertEqual(prepared, ("agy", "--sandbox=false", "--conversation", "conv-1", "-p"))

    def test_user_conversation_is_rejected_before_prepare_inner(self):
        plan = SimpleNamespace(prepare_inner=lambda command: tuple(command))
        with self.assertRaises(SandboxFailure) as raised:
            prepare_cli_invocation(
                ["agy", "--continue", "-p"],
                plan,
                None,
                finalizer=finalize_antigravity_cli_invocation,
                ownership_flags=CLI_OWNERSHIP_FLAGS,
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")


class PrintPromptInsertionTests(unittest.TestCase):
    def test_inserts_before_grok_short_single_turn_flag(self):
        self.assertEqual(
            insert_before_print_prompt(["grok", "-p"], ["--model", "grok-build"]),
            ["grok", "--model", "grok-build", "-p"],
        )

    def test_inserts_before_grok_long_single_turn_flag(self):
        self.assertEqual(
            insert_before_print_prompt(["grok", "--single"], ["--model", "grok-build"]),
            ["grok", "--model", "grok-build", "--single"],
        )


class CliValueInferenceTests(unittest.TestCase):
    def test_flag_value_uses_last_separate_or_equals_occurrence(self):
        self.assertEqual(
            flag_value(["--model", "first", "--model=second"], "--model"),
            "second",
        )
        self.assertEqual(
            flag_value(["--model=first", "--model", "second"], "--model"),
            "second",
        )

    def test_config_value_uses_last_short_long_or_equals_occurrence(self):
        args = [
            "-c",
            'model_reasoning_effort="low"',
            "--config=model_reasoning_effort='medium'",
            "--config",
            'model_reasoning_effort="high"',
        ]
        self.assertEqual(config_value(args, "model_reasoning_effort"), "high")


if __name__ == "__main__":
    unittest.main()
