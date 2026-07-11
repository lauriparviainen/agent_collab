import unittest

from agent_collab.backend_contract import BackendOptionError
from agent_collab.backends.claude_cli import ClaudeCliBackend
from agent_collab.backends.claude_cli.parser import ClaudeStreamingParser, parse_claude_line
from agent_collab.config import AgentConfig, ConfigError
from agent_collab.runners import SubprocessRunner


class ClaudeCliBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = ClaudeCliBackend()

    def agent(self, args=None, **kwargs):
        return AgentConfig(
            id=kwargs.pop("id", "claude"),
            type="claude",
            command=kwargs.pop("command", "claude"),
            args=list(args or []),
            **kwargs,
        )

    def test_system_session_id_maps_to_uniform_provider_event(self):
        event = parse_claude_line(
            '{"type":"system","subtype":"init","session_id":"sess-123"}',
            agent_id="reviewer",
        )
        self.assertEqual(event.source, "claude")
        self.assertEqual(event.type, "status")
        self.assertEqual(event.raw["provider_session_id"], "sess-123")
        self.assertEqual(event.raw["provider_session_kind"], "session")
        self.assertEqual(event.raw["agent_id"], "reviewer")
        self.assertEqual(event.raw["subtype"], "init")
        self.assertEqual(
            event.provider_session,
            {
                "provider_session_id": "sess-123",
                "provider_session_kind": "session",
                "agent_id": "reviewer",
            },
        )

    def test_streaming_parser_emits_repeated_session_id_once(self):
        parser = ClaudeStreamingParser("reviewer")
        first = parser('{"type":"system","subtype":"init","session_id":"sess-123"}')
        repeated = parser('{"type":"result","subtype":"success","session_id":"sess-123"}')
        self.assertEqual(first.raw["provider_session_id"], "sess-123")
        self.assertIsNone(repeated)

    def test_untrusted_raw_identity_cannot_poison_session_deduplication(self):
        parser = ClaudeStreamingParser("reviewer")
        forged = parser(
            '{"type":"assistant","provider_session_id":"sess-123",'
            '"message":{"content":[{"type":"text","text":"keep me"}]}}'
        )
        genuine = parser('{"type":"system","subtype":"init","session_id":"sess-123"}')
        self.assertEqual(forged.text, "keep me")
        self.assertIsNone(forged.provider_session)
        self.assertEqual(genuine.provider_session["provider_session_id"], "sess-123")

    def test_verbose_repeated_session_keeps_non_identity_status(self):
        parser = ClaudeStreamingParser("reviewer")
        parser('{"type":"system","subtype":"init","session_id":"sess-123"}')
        repeated = parser(
            '{"type":"result","subtype":"success","session_id":"sess-123"}',
            verbose=True,
        )
        self.assertEqual(repeated.type, "status")
        self.assertEqual(repeated.text, "success")
        self.assertIsNone(repeated.provider_session)

    def test_manifest_and_command_are_backend_owned(self):
        backend = ClaudeCliBackend()
        agent = AgentConfig(id="claude", type="claude", command="claude", args=["-p"])
        schema = backend.option_schema(agent)
        self.assertTrue(schema["model"].inferred)
        options = backend.normalize_options(agent, {"model": "sonnet"})
        self.assertIn("sonnet", backend.build_command(agent, options))

    def test_runner_parser_attributes_identity_to_configured_agent_id(self):
        backend = ClaudeCliBackend()
        agent = AgentConfig(id="reviewer", type="claude", command="claude")
        runner = backend.create_runner(agent, False, {})
        event = runner.parser(
            '{"type":"system","subtype":"init","session_id":"sess-renamed"}',
            False,
        )
        self.assertEqual(event.raw["agent_id"], "reviewer")

    def test_cli_inference_overrides_manifest_defaults_and_last_flag_wins(self):
        agent = self.agent(["-p", "--model", "opus", "--model=sonnet", "--effort", "low"])

        options = self.backend.normalize_options(agent, {})

        self.assertEqual(options["model"], "sonnet")
        self.assertEqual(options["thinking_level"], "low")
        command = self.backend.build_command(agent, options)
        self.assertEqual(command.count("--model"), 1)
        self.assertNotIn("--model=sonnet", command)
        self.assertEqual(command[command.index("--model") + 1], "sonnet")

    def test_inferred_budget_replaces_default_effort_and_request_wins(self):
        agent = self.agent(["-p", "--thinking-budget-tokens", "2048"])
        inferred = self.backend.normalize_options(agent, {})
        self.assertEqual(inferred["thinking_budget_tokens"], 2048)
        self.assertNotIn("thinking_level", inferred)

        requested = self.backend.normalize_options(agent, {"thinking_level": "max"})
        self.assertEqual(requested["thinking_level"], "max")
        self.assertNotIn("thinking_budget_tokens", requested)

    def test_invalid_inferred_value_and_same_layer_conflict_are_rejected(self):
        with self.assertRaises(BackendOptionError):
            self.backend.normalize_options(self.agent(["--effort", "turbo"]), {})
        with self.assertRaises(BackendOptionError):
            self.backend.normalize_options(
                self.agent(["--effort", "high", "--thinking-budget-tokens", "1024"]),
                {},
            )

    def test_runner_preserves_transport_settings_and_missing_command_fails(self):
        agent = self.agent(id="reviewer", cwd="nested", env={"SAFE": "1"})
        runner = self.backend.create_runner(agent, True, {"model": "sonnet"})
        self.assertIsInstance(runner, SubprocessRunner)
        self.assertEqual(runner.name, "reviewer")
        self.assertTrue(runner.verbose)
        self.assertEqual(runner.cwd, "nested")
        self.assertEqual(runner.env, {"SAFE": "1"})
        self.assertEqual(runner.command_prefix[-2:], ["--model", "sonnet"])

        with self.assertRaisesRegex(ConfigError, "agents.reviewer.command is required"):
            self.backend.create_runner(self.agent(id="reviewer", command=None), False, {})
