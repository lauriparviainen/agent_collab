import unittest

from agent_collab.backend_contract import BackendOptionError
from agent_collab.backends.codex_cli import CodexCliBackend
from agent_collab.backends.codex_cli.parser import parse_codex_line
from agent_collab.config import AgentConfig, ConfigError
from agent_collab.runners import SubprocessRunner


class CodexCliBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = CodexCliBackend()

    def agent(self, args=None, **kwargs):
        return AgentConfig(
            id=kwargs.pop("id", "codex"),
            type="codex",
            command=kwargs.pop("command", "codex"),
            args=list(args or ["exec", "--json"]),
            **kwargs,
        )

    def test_thread_started_maps_to_uniform_provider_event(self):
        event = parse_codex_line(
            '{"type":"thread.started","thread_id":"thread-123"}',
            agent_id="implementer",
        )
        self.assertEqual(event.source, "codex")
        self.assertEqual(event.type, "status")
        self.assertEqual(event.raw["provider_session_id"], "thread-123")
        self.assertEqual(event.raw["provider_session_kind"], "thread")
        self.assertEqual(event.raw["agent_id"], "implementer")
        self.assertEqual(event.raw["type"], "thread.started")
        self.assertEqual(
            event.provider_session,
            {
                "provider_session_id": "thread-123",
                "provider_session_kind": "thread",
                "agent_id": "implementer",
            },
        )

    def test_unproven_thread_fields_do_not_create_identity(self):
        event = parse_codex_line(
            '{"type":"item.completed","thread_id":"not-a-start-record"}',
            verbose=True,
        )
        self.assertNotIn("provider_session_id", event.raw)
        self.assertIsNone(event.provider_session)

    def test_manifest_and_command_are_backend_owned(self):
        backend = CodexCliBackend()
        agent = AgentConfig(id="codex", type="codex", command="codex", args=["exec", "--json"])
        self.assertIn("approval_policy", backend.option_schema(agent))
        options = backend.normalize_options(agent, {"thinking_level": "xhigh"})
        self.assertIn('model_reasoning_effort="xhigh"', backend.build_command(agent, options))

    def test_runner_parser_attributes_identity_to_configured_agent_id(self):
        backend = CodexCliBackend()
        agent = AgentConfig(id="implementer", type="codex", command="codex")
        runner = backend.create_runner(agent, False, {})
        event = runner.parser(
            '{"type":"thread.started","thread_id":"thread-renamed"}',
            False,
        )
        self.assertEqual(event.raw["agent_id"], "implementer")

    def test_cli_inference_overrides_defaults_and_uses_last_occurrence(self):
        agent = self.agent(
            [
                "exec",
                "--json",
                "--model=old",
                "--model",
                "configured",
                "-c",
                'model_reasoning_effort="low"',
                "--config=model_reasoning_effort='xhigh'",
                "--sandbox",
                "read-only",
                "--search",
            ]
        )

        options = self.backend.normalize_options(agent, {})

        self.assertEqual(options["model"], "configured")
        self.assertEqual(options["thinking_level"], "xhigh")
        self.assertEqual(options["reasoning_effort"], "xhigh")
        self.assertEqual(options["sandbox"], "read-only")
        self.assertTrue(options["search"])

    def test_requested_alias_and_false_search_replace_inferred_flags(self):
        agent = self.agent(["exec", "--json", "--reasoning-effort", "low", "--search"])
        options = self.backend.normalize_options(agent, {"thinking_level": "high", "search": False})
        command = self.backend.build_command(agent, options)

        self.assertEqual(options["thinking_level"], "high")
        self.assertEqual(options["reasoning_effort"], "high")
        self.assertNotIn("--reasoning-effort", command)
        self.assertNotIn("--search", command)
        self.assertEqual(command.count("-c"), 1)
        self.assertIn('model_reasoning_effort="high"', command)

    def test_build_command_replaces_only_owned_config_and_flags(self):
        agent = self.agent(
            [
                "exec",
                "--json",
                "-c",
                'model_reasoning_effort="low"',
                "-c",
                "sandbox_workspace_write.network_access=true",
                "--profile=old",
            ]
        )
        command = self.backend.build_command(
            agent,
            {"reasoning_effort": "high", "profile": "new", "approval_policy": "never"},
        )

        self.assertNotIn('model_reasoning_effort="low"', command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("sandbox_workspace_write.network_access=true", command)
        self.assertEqual(command.count("--profile"), 1)
        self.assertEqual(command[command.index("--profile") + 1], "new")
        self.assertEqual(command[command.index("--approval-policy") + 1], "never")

    def test_invalid_inferred_value_and_missing_command_are_rejected(self):
        with self.assertRaises(BackendOptionError):
            self.backend.normalize_options(self.agent(["--sandbox", "unsafe"]), {})

        with self.assertRaisesRegex(ConfigError, "agents.implementer.command is required"):
            self.backend.create_runner(self.agent(id="implementer", command=None), False, {})

    def test_runner_preserves_transport_settings(self):
        agent = self.agent(id="implementer", cwd="nested", env={"SAFE": "1"})
        runner = self.backend.create_runner(agent, True, {"model": "gpt-test"})
        self.assertIsInstance(runner, SubprocessRunner)
        self.assertEqual(runner.name, "implementer")
        self.assertTrue(runner.verbose)
        self.assertEqual(runner.cwd, "nested")
        self.assertEqual(runner.env, {"SAFE": "1"})
        self.assertEqual(runner.command_prefix[-2:], ["--model", "gpt-test"])
