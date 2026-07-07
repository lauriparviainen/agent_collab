import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest import mock

from agent_collab.referee import Referee, RefereeConfig


class RefereeTests(unittest.TestCase):
    def test_mock_loop_writes_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                result = asyncio.run(Referee(
                    RefereeConfig(mock=True, workdir=root, max_turns=2, timeout=5, color=False),
                    printer=lambda event: None,
                ).run(
                    "test task",
                ))
            self.assertTrue(Path(result["jsonl_path"]).exists())
            self.assertTrue(Path(result["markdown_path"]).exists())
            text = Path(result["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("test task", text)

    def test_mock_loop_uses_configured_workflow_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[workflows.codex-only]
sequence = ["codex"]
""",
                encoding="utf-8",
            )
            events = []

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(Referee(
                    RefereeConfig(workflow="codex-only", mock=True, workdir=root, max_turns=1, timeout=5, color=False),
                    printer=events.append,
                ).run("test task"))

            self.assertIn("turn 1: codex", [event.text for event in events])

    def test_dry_run_uses_configured_agent_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[agents.claude]
command = "configured-claude"
""",
                encoding="utf-8",
            )
            events = []

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(Referee(
                    RefereeConfig(dry_run=True, workdir=root, max_turns=1, timeout=5, color=False),
                    printer=events.append,
                ).run("test task"))

            command_events = [event for event in events if event.type == "command"]
            self.assertEqual(command_events[0].raw["argv"][0], "configured-claude")


if __name__ == "__main__":
    unittest.main()
