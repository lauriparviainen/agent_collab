import unittest
from pathlib import Path

from agent_collab.backend_contract import BackendOptionError
from agent_collab.backends.antigravity_cli import AntigravityCliBackend
from agent_collab.backends.antigravity_cli.parser import parse_antigravity_line
from agent_collab.config import AgentConfig, ConfigError
from agent_collab.runners import SubprocessRunner


class AntigravityCliBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = AntigravityCliBackend()

    def agent(self, args=None, **kwargs):
        return AgentConfig(
            id=kwargs.pop("id", "ag"),
            type="antigravity",
            command=kwargs.pop("command", "agy"),
            args=list(args or ["-p"]),
            **kwargs,
        )

    def test_plain_text_does_not_invent_provider_identity(self):
        event = parse_antigravity_line("conversation_id=looks-real-but-is-prose")
        self.assertEqual(event.type, "message")
        self.assertNotIn("provider_session_id", event.raw)
        self.assertIsNone(event.provider_session)
        self.assertIsNone(AntigravityCliBackend.provider_session_id_kind)

    def test_manifest_and_workdir_mapping_are_backend_owned(self):
        backend = AntigravityCliBackend()
        agent = AgentConfig(id="ag", type="antigravity", command="agy", args=["-p"])
        options = backend.normalize_options(agent, {"mode": "plan"})
        command = backend.build_command(agent, options, Path("/tmp/work"))
        self.assertIn("plan", command)
        self.assertIn("--add-dir", command)

    def test_cli_inference_overrides_defaults_and_last_flag_wins(self):
        agent = self.agent(
            [
                "--model",
                "Gemini 3.5 Flash (High)",
                "--model=Gemini 3.1 Pro (Low)",
                "--mode",
                "plan",
                "-p",
            ]
        )

        options = self.backend.normalize_options(agent, {})
        command = self.backend.build_command(agent, options, Path("/tmp/work"))

        self.assertEqual(options["model"], "Gemini 3.1 Pro (Low)")
        self.assertEqual(options["mode"], "plan")
        self.assertEqual(command.count("--model"), 1)
        self.assertLess(command.index("--model"), command.index("-p"))
        self.assertLess(command.index("--mode"), command.index("-p"))
        self.assertLess(command.index("--add-dir"), command.index("-p"))

    def test_request_replaces_inferred_values_and_existing_add_dir_is_preserved(self):
        agent = self.agent(["--mode=plan", "--add-dir", "/configured", "-p"])
        options = self.backend.normalize_options(agent, {"mode": "default"})
        command = self.backend.build_command(agent, options, Path("/ignored"))

        self.assertEqual(options["mode"], "default")
        self.assertEqual(command.count("--mode"), 1)
        self.assertEqual(command[command.index("--mode") + 1], "default")
        self.assertEqual(command.count("--add-dir"), 1)
        self.assertEqual(command[command.index("--add-dir") + 1], "/configured")

    def test_turn_timeout_maps_to_print_timeout_before_print_mode(self):
        command = self.backend.build_command(self.agent(timeout=900), {}, Path("/tmp/work"))

        self.assertEqual(command.count("--print-timeout"), 1)
        timeout_index = command.index("--print-timeout")
        self.assertEqual(command[timeout_index + 1], "900s")
        self.assertLess(timeout_index, command.index("-p"))

    def test_explicit_print_timeout_arg_is_preserved(self):
        command = self.backend.build_command(
            self.agent(["--print-timeout=20m", "-p"], timeout=900),
            {},
            Path("/tmp/work"),
        )

        self.assertIn("--print-timeout=20m", command)
        self.assertNotIn("900s", command)

    def test_invalid_inferred_mode_and_missing_command_are_rejected(self):
        with self.assertRaises(BackendOptionError):
            self.backend.normalize_options(self.agent(["--mode", "turbo", "-p"]), {})

        with self.assertRaisesRegex(ConfigError, "agents.reviewer.command is required"):
            self.backend.create_runner(self.agent(id="reviewer", command=None), False, {})

    def test_runner_and_preview_resolve_configured_cwd(self):
        agent = self.agent(id="reviewer", cwd="nested", env={"SAFE": "1"})
        options = self.backend.normalize_options(agent, {"mode": "plan"})
        preview = self.backend.command_preview(agent, options, Path("/workspace"))
        runner = self.backend.create_runner(agent, True, options)

        self.assertEqual(
            preview[preview.index("--add-dir") + 1],
            str(Path("/workspace/nested").resolve()),
        )
        self.assertIsInstance(runner, SubprocessRunner)
        self.assertEqual(runner.name, "reviewer")
        self.assertTrue(runner.verbose)
        self.assertEqual(runner.cwd, "nested")
        self.assertEqual(runner.env, {"SAFE": "1"})
