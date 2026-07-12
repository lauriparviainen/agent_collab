import subprocess
import sys
import unittest
from pathlib import Path

from agent_collab.cli import PUBLIC_COMMANDS, _command_handlers, build_parser


ROOT = Path(__file__).resolve().parents[1]


class CliHelpTests(unittest.TestCase):
    def test_root_help_is_provider_neutral_and_lists_every_public_command(self):
        text = build_parser().format_help()

        self.assertIn("configured AI agents", text)
        self.assertNotIn("Claude Code and Codex", text)
        self.assertNotIn("simulated Claude/Codex", text)
        for command, description in PUBLIC_COMMANDS:
            self.assertIn(command, text)
            self.assertIn(description, text)
        self.assertIn("agent-collab COMMAND --help", text)

    def test_advertised_commands_match_dispatcher(self):
        self.assertEqual(
            {name for name, _description in PUBLIC_COMMANDS},
            set(_command_handlers()),
        )

    def test_sessions_prune_help_documents_safety_defaults(self):
        result = subprocess.run(
            [sys.executable, "-m", "agent_collab.cli", "sessions", "prune", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: agent-collab sessions prune", result.stdout)
        self.assertIn("30 days by default", result.stdout)
        self.assertIn("--apply", result.stdout)
        self.assertIn("--older-than", result.stdout)
        self.assertIn("--keep", result.stdout)

    def test_tui_command_specific_help_is_reachable(self):
        result = subprocess.run(
            [sys.executable, "-m", "agent_collab.cli", "tui", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: agent-collab tui", result.stdout)
        self.assertIn("interactive daemon session TUI", result.stdout)


if __name__ == "__main__":
    unittest.main()
