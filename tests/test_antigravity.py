import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.config import builtin_config, load_config, merge_config_data
from agent_collab.backends.antigravity_cli import AntigravityCliBackend, parse_antigravity_line
from agent_collab.options import (
    StartOptionsError,
    build_session_settings,
    validate_start_backends,
    validate_start_options,
)
from agent_collab.referee import Referee, RefereeConfig
from agent_collab.runners import MockRunner, _mock_source

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


def _write_user_config(home: Path, text: str) -> None:
    path = home / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _env(home: Path):
    return {"AGENT_COLLAB_HOME": str(home)}


async def _collect(runner, prompt="do a thing"):
    events = []

    async def emit(event):
        events.append(event)

    await runner.run_turn(prompt, Path("."), emit)
    return events


async def _first_event(runner, prompt="do a thing", workdir=Path(".")):
    events = []

    async def emit(event):
        events.append(event)

    await runner.run_turn(prompt, workdir, emit)
    return events[0] if events else None


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
        section = config.backends["antigravity_cli"]
        self.assertFalse(section.enabled)
        self.assertEqual(section.command, "agy")
        self.assertIn("--mode", section.args)  # non-blocking print posture
        # Disabled backends derive no agents.
        self.assertNotIn("antigravity_cli", config.agents)

    def test_enabling_antigravity_and_referencing_workflow_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
schema_version = 8

[backends.antigravity_cli]
enabled = true

[workflows.solo-antigravity]
sequence = ["antigravity_cli"]
""",
            )
            config = load_config(root, env=_env(home))
            self.assertTrue(config.agents["antigravity_cli"].enabled)
            self.assertEqual(config.workflows["solo-antigravity"].sequence, ["antigravity_cli"])


class AntigravityOptionsTests(unittest.TestCase):
    def _config(self, root, home):
        _write_user_config(
            home,
            """
schema_version = 8

[backends.antigravity_cli]
enabled = true

[workflows.solo-antigravity]
sequence = ["antigravity_cli"]
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
                config,
                "solo-antigravity",
                backend_options={
                    "antigravity_cli": {"model": "Gemini 3.5 Flash (Low)", "mode": "plan"}
                },
            )
            self.assertEqual(validated["antigravity_cli"]["model"], "Gemini 3.5 Flash (Low)")
            self.assertEqual(validated["antigravity_cli"]["mode"], "plan")

            agent = config.agents["antigravity_cli"]
            command = AntigravityCliBackend().build_command(agent, validated["antigravity_cli"])
            self.assertIn("--model", command)
            self.assertIn("Gemini 3.5 Flash (Low)", command)
            self.assertIn("--mode", command)
            self.assertIn("plan", command)
            self.assertEqual(command.count("--mode"), 1)  # replaced, not duplicated
            print_index = command.index("-p")
            self.assertLess(command.index("--model"), print_index)
            self.assertLess(command.index("--mode"), print_index)

    def test_invalid_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            config = self._config(root, home)
            with self.assertRaises(StartOptionsError) as ctx:
                validate_start_options(
                    config, "solo-antigravity", {"antigravity_cli": {"mode": "turbo"}}
                )
            messages = {d["path"]: d["message"] for d in ctx.exception.to_dict()["details"]}
            self.assertIn("backend_options.antigravity_cli.mode", messages)

    def test_settings_records_antigravity_backend_and_command_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            config = self._config(root, home)
            normalized = validate_start_options(config, "solo-antigravity")
            selection = validate_start_backends(config, "solo-antigravity")
            settings = build_session_settings(
                config,
                "solo-antigravity",
                normalized,
                agent_backends=selection.agent_backends,
                workdir=root,
            )
            entry = settings["agents"]["antigravity_cli"]
            self.assertEqual(entry["backend"], "cli")
            self.assertEqual(
                entry["capabilities"], {"resume": False, "interrupt": False, "tool_gate": False}
            )
            self.assertEqual(entry["command_preview"][0], "agy")
            self.assertIn("--add-dir", entry["command_preview"])
            self.assertIn(str(root.resolve()), entry["command_preview"])
            # mode is a displayed settings field, defaulted from the agent args.
            self.assertEqual(entry["mode"], "accept-edits")

    def test_antigravity_command_preview_resolves_agent_cwd_for_add_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            config = self._config(root, home)
            config.agents["antigravity_cli"].cwd = "agent-root"
            normalized = validate_start_options(config, "solo-antigravity")
            selection = validate_start_backends(config, "solo-antigravity")

            settings = build_session_settings(
                config,
                "solo-antigravity",
                normalized,
                agent_backends=selection.agent_backends,
                workdir=root,
            )

            argv = settings["agents"]["antigravity_cli"]["command_preview"]
            add_dir_index = argv.index("--add-dir")
            self.assertEqual(argv[add_dir_index + 1], str((root / "agent-root").resolve()))
            self.assertLess(add_dir_index, argv.index("-p"))

    def test_subprocess_runner_injects_antigravity_add_dir_from_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            config = builtin_config()
            merge_config_data(config, {"backends": {"antigravity_cli": {"enabled": True}}})
            agent = config.agents["antigravity_cli"]
            agent.cwd = "agent-root"
            backend = AntigravityCliBackend()
            runner = backend.create_runner(agent, False, backend.normalize_options(agent, {}))

            event = asyncio.run(_first_event(runner, workdir=root))

            self.assertIsNotNone(event)
            argv = event.raw["argv"]
            add_dir_index = argv.index("--add-dir")
            self.assertEqual(argv[add_dir_index + 1], str((root / "agent-root").resolve()))
            self.assertLess(add_dir_index, argv.index("-p"))


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
            _write_user_config(
                root / "home",
                """
schema_version = 8

[backends.antigravity_cli]
enabled = true

[workflows.solo-antigravity]
sequence = ["antigravity_cli"]
""",
            )
            events = []
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(
                    Referee(
                        RefereeConfig(
                            workflow="solo-antigravity",
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
            _write_user_config(
                root / "home",
                """
schema_version = 8

[backends.antigravity_cli]
enabled = true

[workflows.solo-antigravity]
sequence = ["antigravity_cli"]
""",
            )
            events = []
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(
                    Referee(
                        RefereeConfig(
                            workflow="solo-antigravity",
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
