import unittest
from pathlib import Path

from agent_collab.backend_contract import BackendOptionError
from agent_collab.backends.antigravity_cli import AntigravityCliBackend
from agent_collab.backends.antigravity_cli.parser import (
    AntigravityStreamingParser,
    parse_antigravity_line,
)
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

    def test_structured_records_do_not_capture_provider_identity(self):
        line = (
            '{"event":"result","result":{"conversation_id":'
            '"00000000-0000-4000-8000-000000000001","status":"SUCCESS",'
            '"response":"ready\\n"}}'
        )
        event = parse_antigravity_line(line)
        parser = AntigravityStreamingParser()
        parsed = parser(line)
        events = parsed if isinstance(parsed, list) else [parsed]
        self.assertEqual(event.type, "message")
        self.assertIsNone(event.provider_session)
        self.assertNotIn("provider_session_id", event.raw)
        self.assertTrue(all(item.provider_session is None for item in events if item is not None))
        self.assertIsNone(AntigravityCliBackend.provider_session_id_kind)
        self.assertFalse(self.backend.capabilities.continuity)
        self.assertFalse(self.backend.capabilities.resume)

    def test_manifest_and_workdir_mapping_are_backend_owned(self):
        backend = AntigravityCliBackend()
        agent = AgentConfig(id="ag", type="antigravity", command="agy", args=["-p"])
        options = backend.normalize_options(agent, {"mode": "plan"})
        command = backend.build_command(agent, options, Path("/tmp/work"))
        self.assertIn("plan", command)
        self.assertIn("--add-dir", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertLess(command.index("--output-format"), command.index("-p"))

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
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertLess(command.index("--output-format"), command.index("-p"))

    def test_request_replaces_inferred_values_and_existing_add_dir_is_preserved(self):
        agent = self.agent(["--mode=plan", "--add-dir", "/configured", "-p"])
        options = self.backend.normalize_options(agent, {"mode": "default"})
        command = self.backend.build_command(agent, options, Path("/ignored"))

        self.assertEqual(options["mode"], "default")
        self.assertEqual(command.count("--mode"), 1)
        self.assertEqual(command[command.index("--mode") + 1], "default")
        self.assertEqual(command.count("--add-dir"), 1)
        self.assertEqual(command[command.index("--add-dir") + 1], "/configured")

    def test_build_command_injects_and_overrides_stream_json_before_print(self):
        injected = self.backend.build_command(self.agent(["-p"]), {})
        self.assertEqual(injected[injected.index("--output-format") + 1], "stream-json")
        self.assertLess(injected.index("--output-format"), injected.index("-p"))

        overridden = self.backend.build_command(self.agent(["--output-format", "json", "-p"]), {})
        self.assertEqual(overridden.count("--output-format"), 1)
        self.assertEqual(overridden[overridden.index("--output-format") + 1], "stream-json")
        self.assertLess(overridden.index("--output-format"), overridden.index("-p"))
        self.assertNotIn("json", overridden)

        equals_form = self.backend.build_command(self.agent(["--output-format=text", "-p"]), {})
        self.assertEqual(equals_form[equals_form.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--output-format=text", equals_form)

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
