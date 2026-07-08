import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.config import ConfigError, builtin_config, load_config
from agent_collab.events import parse_antigravity_line
from agent_collab.options import (
    StartOptionsError,
    apply_agent_options,
    build_session_settings,
    validate_start_backends,
    validate_start_options,
)
from agent_collab.referee import Referee, RefereeConfig
from agent_collab.runners import DryRunRunner, MockRunner, _mock_source

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


def _write_config(root: Path, text: str) -> None:
    path = root / ".agent-collab" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _env(home: Path):
    return {"AGENT_COLLAB_HOME": str(home)}


async def _collect(runner, prompt="do a thing"):
    return [event async for event in runner.run(prompt, Path("."))]


class AntigravityParserTests(unittest.TestCase):
    def test_plain_text_line_becomes_antigravity_message(self):
        event = parse_antigravity_line("### Supported Modes")
        self.assertEqual(event.source, "antigravity")
        self.assertEqual(event.type, "message")
        self.assertEqual(event.text, "### Supported Modes")

    def test_blank_and_whitespace_lines_return_none(self):
        self.assertIsNone(parse_antigravity_line(""))
        self.assertIsNone(parse_antigravity_line("   \n"))
        self.assertIsNone(parse_antigravity_line("\t\n"))

    def test_captured_fixture_yields_only_antigravity_messages(self):
        lines = (FIXTURES / "agy-print-sample.stdout.txt").read_text(encoding="utf-8").splitlines()
        events = [parse_antigravity_line(line) for line in lines]
        non_none = [event for event in events if event is not None]
        self.assertTrue(non_none, "captured fixture should yield events")
        self.assertTrue(all(e.source == "antigravity" and e.type == "message" for e in non_none))
        # blank lines in the prose are skipped, so fewer events than input lines.
        self.assertLess(len(non_none), len(lines))


class AntigravityConfigTests(unittest.TestCase):
    def test_antigravity_is_disabled_by_default(self):
        config = builtin_config()
        agent = config.agents["antigravity"]
        self.assertEqual(agent.type, "antigravity")
        self.assertFalse(agent.enabled)
        self.assertEqual(agent.command, "agy")
        self.assertIn("--mode", agent.args)  # non-blocking print posture

    def test_enabling_antigravity_and_referencing_workflow_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[agents.antigravity]
enabled = true

[workflows.antigravity-solo]
sequence = ["antigravity"]
""",
            )
            config = load_config(root, env=_env(home))
            self.assertTrue(config.agents["antigravity"].enabled)
            self.assertEqual(config.workflows["antigravity-solo"].sequence, ["antigravity"])


class AntigravityOptionsTests(unittest.TestCase):
    def _config(self, root, home):
        _write_config(
            root,
            """
[agents.antigravity]
enabled = true

[workflows.antigravity-solo]
sequence = ["antigravity"]
""",
        )
        return load_config(root, env=_env(home))

    def test_valid_options_validate_and_map_to_agy_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            config = self._config(root, home)
            validated = validate_start_options(
                config, "antigravity-solo", antigravity_options={"model": "gemini-x", "mode": "plan"}
            )
            self.assertEqual(validated["antigravity_options"]["model"], "gemini-x")
            self.assertEqual(validated["antigravity_options"]["mode"], "plan")

            agent = config.agents["antigravity"]
            command = apply_agent_options(
                [agent.command] + list(agent.args), agent, validated["antigravity_options"]
            )
            self.assertIn("--model", command)
            self.assertIn("gemini-x", command)
            self.assertIn("--mode", command)
            self.assertIn("plan", command)
            self.assertEqual(command.count("--mode"), 1)  # replaced, not duplicated

    def test_invalid_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            config = self._config(root, home)
            with self.assertRaises(StartOptionsError) as ctx:
                validate_start_options(config, "antigravity-solo", antigravity_options={"mode": "turbo"})
            messages = {d["path"]: d["message"] for d in ctx.exception.to_dict()["details"]}
            self.assertIn("antigravity_options.mode", messages)

    def test_settings_records_antigravity_backend_and_command_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            config = self._config(root, home)
            normalized = validate_start_options(config, "antigravity-solo")
            selection = validate_start_backends(config, "antigravity-solo")
            settings = build_session_settings(
                config, "antigravity-solo", normalized, agent_backends=selection.agent_backends
            )
            entry = settings["agents"]["antigravity"]
            self.assertEqual(entry["backend"], "cli")
            self.assertEqual(
                entry["capabilities"], {"resume": False, "interrupt": False, "tool_gate": False}
            )
            self.assertEqual(entry["command_preview"][0], "agy")
            # mode is a displayed settings field, defaulted from the agent args.
            self.assertEqual(entry["mode"], "accept-edits")


class AntigravityMockSourceTests(unittest.TestCase):
    def test_mock_runner_attributes_events_to_antigravity_not_codex(self):
        runner = MockRunner("antigravity", source=_mock_source("antigravity", "antigravity"))
        events = asyncio.run(_collect(runner))
        sources = {event.source for event in events}
        self.assertIn("antigravity", sources)
        self.assertNotIn("codex", sources)  # guards against the old codex fallback

    def test_referee_mock_run_emits_antigravity_sourced_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(
                root,
                """
[agents.antigravity]
enabled = true

[workflows.antigravity-solo]
sequence = ["antigravity"]
""",
            )
            events = []
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(
                    Referee(
                        RefereeConfig(
                            workflow="antigravity-solo",
                            mock=True,
                            workdir=root,
                            max_turns=1,
                            timeout=5,
                            color=False,
                        ),
                        printer=events.append,
                    ).run("mock antigravity task")
                )
            message_sources = {e.source for e in events if e.type == "message"}
            self.assertIn("antigravity", message_sources)
            self.assertNotIn("codex", message_sources)

    def test_dry_run_uses_agy_command_for_antigravity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(
                root,
                """
[agents.antigravity]
enabled = true

[workflows.antigravity-solo]
sequence = ["antigravity"]
""",
            )
            events = []
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(
                    Referee(
                        RefereeConfig(
                            workflow="antigravity-solo",
                            dry_run=True,
                            workdir=root,
                            max_turns=1,
                            timeout=5,
                            color=False,
                        ),
                        printer=events.append,
                    ).run("dry antigravity task")
                )
            command_events = [e for e in events if e.type == "command"]
            self.assertEqual(command_events[0].raw["argv"][0], "agy")


if __name__ == "__main__":
    unittest.main()
