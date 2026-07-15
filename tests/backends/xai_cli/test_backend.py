import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab import backends
from agent_collab.backend_contract import OPTION_UNSET, BackendOptionError
from agent_collab.backends.common.health import xai_cli_credentials
from agent_collab.backends.xai_cli import XaiCliBackend, parse_xai_line
from agent_collab.config import (
    AgentConfig,
    SUBPROCESS_AGENT_TYPES,
    builtin_config,
    load_config,
    merge_config_data,
)
from agent_collab.events import VALID_SOURCES
from agent_collab.options import build_session_settings, describe_options
from agent_collab.referee import Referee, RefereeConfig
from agent_collab.runners import PROVIDER_SOURCES, DryRunRunner, SubprocessRunner
from agent_collab.backends.base import CREDENTIALS_OK, CREDENTIALS_UNKNOWN, BackendHealth


FIXTURES = Path(__file__).parents[2] / "fixtures" / "xai"


class XaiCliBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = XaiCliBackend()

    def agent(self, args=None, **kwargs):
        # Carry the shipped option defaults so the tests exercise the same
        # posture (permission-bypassed inside a read-only sandbox) as the
        # built-in config.
        kwargs.setdefault(
            "default_options", dict(builtin_config().backends["xai_cli"].default_options)
        )
        return AgentConfig(
            id=kwargs.pop("id", "xai"),
            type="xai",
            command="grok",
            args=list(args or ["--output-format", "streaming-json", "-p"]),
            **kwargs,
        )

    def test_registration_and_all_provider_allowlists(self):
        self.assertIs(backends.get_backend("xai", "cli").__class__, XaiCliBackend)
        self.assertIn("xai", VALID_SOURCES)
        self.assertIn("xai", PROVIDER_SOURCES)
        self.assertIn("xai", SUBPROCESS_AGENT_TYPES)
        self.assertEqual(backends.backend_name("xai", "cli"), "xai_cli")

    def test_builtin_is_enabled_and_a_workflow_can_reference_it(self):
        builtin = builtin_config()
        self.assertTrue(builtin.backends["xai_cli"].enabled)
        # Enabled backends derive their default agent.
        self.assertIn("xai_cli", builtin.agents)
        repo = Path(__file__).parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # No enable line needed: xai_cli is on by default, so a workflow can
            # reference it directly.
            (home / "config.toml").write_text(
                """
schema_version = 8

[workflows.solo-xai]
sequence = ["xai_cli"]
""",
                encoding="utf-8",
            )
            config = load_config(repo, env={"AGENT_COLLAB_HOME": str(home)})
        self.assertTrue(config.agents["xai_cli"].enabled)
        self.assertEqual(config.workflows["solo-xai"].sequence, ["xai_cli"])

    def test_manifest_is_backend_owned_and_open_ended_where_required(self):
        schema = self.backend.option_schema(self.agent())
        self.assertEqual(
            set(schema),
            {
                "model",
                "permission_mode",
                "sandbox",
                "thinking_level",
                "reasoning_effort",
                "provider_max_turns",
            },
        )
        self.assertTrue(schema["model"].inferred)
        self.assertIsNone(schema["model"].allowed)
        self.assertIsNone(schema["sandbox"].allowed)
        # Defaults ship in the built-in config, not the backend manifest.
        self.assertIs(schema["permission_mode"].default, OPTION_UNSET)
        self.assertIs(schema["sandbox"].default, OPTION_UNSET)
        defaults = builtin_config().backends["xai_cli"].default_options
        self.assertEqual(defaults["permission_mode"], "bypassPermissions")
        self.assertEqual(defaults["sandbox"], "read-only")
        self.assertEqual(schema["provider_max_turns"].minimum, 1)

    def test_reasoning_alias_is_canonical_and_conflicts_on_native_field(self):
        options = self.backend.normalize_options(self.agent(), {"reasoning_effort": "high"})
        self.assertEqual(
            options,
            {
                "permission_mode": "bypassPermissions",
                "sandbox": "read-only",
                "thinking_level": "high",
            },
        )
        self.assertEqual(
            self.backend.normalize_options(
                self.agent(), {"thinking_level": "low", "reasoning_effort": "low"}
            ),
            {
                "permission_mode": "bypassPermissions",
                "sandbox": "read-only",
                "thinking_level": "low",
            },
        )
        with self.assertRaises(BackendOptionError) as ctx:
            self.backend.normalize_options(
                self.agent(), {"thinking_level": "low", "reasoning_effort": "high"}
            )
        self.assertEqual(ctx.exception.field, "reasoning_effort")

    def test_cli_inference_and_flags_render_before_both_prompt_spellings(self):
        for sentinel in ("-p", "--single"):
            with self.subTest(sentinel=sentinel):
                agent = self.agent(
                    [
                        "--output-format",
                        "streaming-json",
                        "--model",
                        "configured-model",
                        "--effort",
                        "low",
                        sentinel,
                    ]
                )
                options = self.backend.normalize_options(agent, {"thinking_level": "high"})
                self.assertEqual(options["model"], "configured-model")
                command = self.backend.build_command(agent, options)
                prompt_index = command.index(sentinel)
                self.assertLess(command.index("--model"), prompt_index)
                self.assertLess(command.index("--reasoning-effort"), prompt_index)
                self.assertLess(command.index("--permission-mode"), prompt_index)
                self.assertLess(command.index("--sandbox"), prompt_index)
                self.assertLess(command.index("--rules"), prompt_index)
                self.assertEqual(
                    command[command.index("--permission-mode") + 1], "bypassPermissions"
                )
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertIn(
                    "do not prepend cd or chain commands",
                    command[command.index("--rules") + 1],
                )
                self.assertNotIn("--effort", command)
                self.assertNotIn("--cwd", command)

    def test_configured_rules_and_explicit_permissions_are_preserved(self):
        agent = self.agent(["--rules", "Keep the answer concise.", "-p"])
        options = self.backend.normalize_options(
            agent,
            {
                "permission_mode": "dontAsk",
                "sandbox": "workspace",
                "provider_max_turns": 75,
            },
        )

        command = self.backend.build_command(agent, options)

        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace")
        self.assertEqual(command[command.index("--max-turns") + 1], "75")
        rules = command[command.index("--rules") + 1]
        self.assertIn("Keep the answer concise.", rules)
        self.assertIn("do not prepend cd or chain commands", rules)

    def test_provider_max_turns_is_inferred_and_rejects_invalid_configured_value(self):
        options = self.backend.normalize_options(self.agent(["--max-turns", "80", "-p"]), {})
        self.assertEqual(options["provider_max_turns"], 80)

        with self.assertRaises(BackendOptionError) as ctx:
            self.backend.normalize_options(self.agent(["--max-turns", "many", "-p"]), {})
        self.assertEqual(ctx.exception.field, "provider_max_turns")

    def test_runner_binds_agent_identity_and_preserves_configured_cwd(self):
        agent = self.agent(id="reviewer", cwd="nested")
        runner = self.backend.create_runner(agent, False, {})
        self.assertIsInstance(runner, SubprocessRunner)
        self.assertEqual(runner.cwd, "nested")
        parsed = runner.parser(
            '{"type":"end","stopReason":"EndTurn","sessionId":"sess","requestId":"req"}',
            False,
        )
        self.assertIsInstance(parsed, list)
        event = parsed[0]
        self.assertEqual(event.raw["agent_id"], "reviewer")
        self.assertEqual(event.raw["provider_session_id"], "sess")
        self.assertEqual(event.raw["provider_session_kind"], "session")
        self.assertEqual(event.raw["sessionId"], "sess")
        self.assertEqual(event.raw["requestId"], "req")
        self.assertEqual(
            event.provider_session,
            {
                "provider_session_id": "sess",
                "provider_session_kind": "session",
                "agent_id": "reviewer",
            },
        )

    def test_runner_coalesces_text_deltas_and_keeps_session_event(self):
        runner = self.backend.create_runner(self.agent(), False, {})
        self.assertIsNone(runner.parser('{"type":"text","data":"hello"}', False))
        self.assertIsNone(runner.parser('{"type":"text","data":" world"}', False))
        events = runner.parser(
            '{"type":"end","stopReason":"EndTurn","sessionId":"sess","requestId":"req"}',
            False,
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(
            (events[0].source, events[0].type, events[0].text), ("xai", "message", "hello world")
        )
        self.assertEqual(events[0].raw["delta_count"], 2)
        self.assertEqual(events[1].raw["provider_session_id"], "sess")

    def test_runner_flushes_partial_text_when_stdout_ends_without_end_record(self):
        runner = self.backend.create_runner(self.agent(), False, {})
        runner.parser('{"type":"text","data":"partial"}', False)
        events = runner.parser.finish()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, "partial")

    def test_runner_reports_cancelled_end_as_fatal_and_keeps_session_identity(self):
        runner = self.backend.create_runner(self.agent(id="reviewer"), False, {})

        events = runner.parser(
            '{"type":"end","stopReason":"Cancelled","sessionId":"sess","requestId":"req"}',
            False,
        )

        self.assertEqual(len(events), 2)
        failure, identity = events
        self.assertEqual((failure.source, failure.type), ("error", "error"))
        self.assertIn("before producing a response", failure.text)
        self.assertEqual(failure.raw["code"], "provider_turn_cancelled")
        self.assertTrue(failure.raw["fatal"])
        self.assertEqual(failure.raw["provider_stop_reason"], "Cancelled")
        self.assertIsNone(failure.provider_session)
        self.assertEqual(identity.provider_session["provider_session_id"], "sess")

    def test_unknown_end_reason_is_not_treated_as_success(self):
        event = parse_xai_line('{"type":"end","stopReason":"SafetyStop","sessionId":"sess"}')

        self.assertEqual((event.source, event.type), ("error", "error"))
        self.assertEqual(event.raw["code"], "provider_terminal_failure")
        self.assertIn("SafetyStop", event.text)

    def test_probe_reports_missing_dependency_and_observed_version(self):
        with mock.patch("agent_collab.backends.xai_cli.backend.probe_cli_backend") as probe:
            probe.return_value.status = "unavailable"
            self.assertEqual(self.backend.probe().status, "unavailable")
            probe.return_value.status = "ok"
            probe.return_value.version = "grok 0.2.93"
            self.assertEqual(self.backend.probe().version, "grok 0.2.93")

    def test_generic_settings_dry_run_and_policy_use_canonical_backend(self):
        agent = self.agent()
        config = builtin_config()
        merge_config_data(config, {"backends": {"xai_cli": {"enabled": True}}})
        from agent_collab.config import WorkflowConfig

        config.workflows["solo-xai"] = WorkflowConfig("solo-xai", ["xai_cli"])
        options = self.backend.normalize_options(
            agent, {"model": "grok-build", "thinking_level": "low"}
        )
        settings = build_session_settings(
            config,
            "solo-xai",
            {"xai_cli": dict(options)},
            agent_backends={"xai_cli": "cli"},
            agent_options={"xai_cli": dict(options)},
        )
        self.assertEqual(settings["agents"]["xai_cli"]["backend"], "cli")
        self.assertIn("--reasoning-effort", settings["agents"]["xai_cli"]["command_preview"])

        referee = Referee(
            RefereeConfig(
                workflow="solo-xai",
                dry_run=True,
                collab_config=config,
                agent_backends={"xai_cli": "cli"},
                agent_options={"xai_cli": dict(options)},
                color=False,
            ),
            printer=lambda event: None,
        )
        self.assertIsInstance(referee._runners()["xai_cli"], DryRunRunner)

        described = describe_options(config, health=lambda backend: BackendHealth(status="ok"))
        policy = described["backends"]["xai_cli"]["policy"]
        self.assertTrue(policy["enabled"])
        self.assertTrue(policy["selection_eligible"])


class XaiParserFixtureTests(unittest.TestCase):
    def fixture_events(self, name, verbose=False):
        lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        return [event for line in lines if (event := parse_xai_line(line, verbose))]

    def test_real_reasoning_fixture_maps_thought_text_and_session(self):
        hidden = self.fixture_events("streaming-json-reasoning.ndjson")
        self.assertEqual([event.type for event in hidden], ["message", "message", "status"])
        self.assertEqual("".join(event.text for event in hidden[:2]), "fixture-ok")
        self.assertTrue(all(event.source == "xai" for event in hidden))
        verbose = self.fixture_events("streaming-json-reasoning.ndjson", verbose=True)
        self.assertEqual(verbose[0].type, "status")

    def test_real_tooluse_fixture_does_not_guess_typed_action_events(self):
        events = self.fixture_events("streaming-json-tooluse.ndjson", verbose=True)
        self.assertFalse(any(event.source == "tool" for event in events))
        self.assertFalse(
            any(event.type in {"tool_call", "command", "file_change"} for event in events)
        )

    def test_real_error_fixture_maps_explicit_error(self):
        events = self.fixture_events("streaming-json-error.ndjson")
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].source, events[0].type), ("error", "error"))
        self.assertIn("unknown model id", events[0].text)

    def test_malformed_scalar_unknown_and_partial_final_are_tolerated(self):
        for line in ("", "not-json", "42", "[]", '{"type":"end"}', '{"other":true}'):
            with self.subTest(line=line):
                parse_xai_line(line)
                parse_xai_line(line, verbose=True)
        self.assertIsNone(parse_xai_line("not-json"))
        self.assertIsNotNone(parse_xai_line("not-json", verbose=True))


class XaiCredentialTests(unittest.TestCase):
    def test_environment_key_is_ok_without_reading_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                xai_cli_credentials(Path(tmp), {"XAI_API_KEY": "fixture-secret"}),
                CREDENTIALS_OK,
            )

    def test_nonempty_cached_auth_entry_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "auth.json").write_text(
                json.dumps({"https://auth.x.ai::fixture": {"cached": True}}),
                encoding="utf-8",
            )
            self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_OK)

    def test_missing_or_malformed_auth_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_UNKNOWN)
            (base / "auth.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_UNKNOWN)

    def test_valid_but_empty_cached_auth_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for payload in ({}, {"https://auth.x.ai::fixture": {}}):
                with self.subTest(payload=payload):
                    (base / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
