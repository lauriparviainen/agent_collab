import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab import backends
from agent_collab.backend_contract import BackendOptionError
from agent_collab.backends.common.health import xai_cli_credentials
from agent_collab.backends.xai_cli import XaiCliBackend, parse_xai_line
from agent_collab.config import AgentConfig, SUBPROCESS_AGENT_TYPES, builtin_config, load_config
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

    def test_builtin_is_disabled_and_repository_project_opt_in_is_valid(self):
        self.assertFalse(builtin_config().agents["xai"].enabled)
        repo = Path(__file__).parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(repo, env={"AGENT_COLLAB_HOME": tmp})
        self.assertTrue(config.agents["xai"].enabled)
        self.assertEqual(config.workflows["solo-xai"].sequence, ["xai"])

    def test_manifest_is_backend_owned_and_open_ended_where_required(self):
        schema = self.backend.option_schema(self.agent())
        self.assertEqual(
            set(schema),
            {"model", "permission_mode", "sandbox", "thinking_level", "reasoning_effort"},
        )
        self.assertTrue(schema["model"].inferred)
        self.assertIsNone(schema["model"].allowed)
        self.assertIsNone(schema["sandbox"].allowed)

    def test_reasoning_alias_is_canonical_and_conflicts_on_native_field(self):
        options = self.backend.normalize_options(self.agent(), {"reasoning_effort": "high"})
        self.assertEqual(options, {"thinking_level": "high"})
        self.assertEqual(
            self.backend.normalize_options(
                self.agent(), {"thinking_level": "low", "reasoning_effort": "low"}
            ),
            {"thinking_level": "low"},
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
                self.assertNotIn("--effort", command)
                self.assertNotIn("--cwd", command)

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
        config.agents["xai"].enabled = True
        from agent_collab.config import WorkflowConfig

        config.workflows["solo-xai"] = WorkflowConfig("solo-xai", ["xai"])
        options = self.backend.normalize_options(
            agent, {"model": "grok-build", "thinking_level": "low"}
        )
        settings = build_session_settings(
            config,
            "solo-xai",
            {"xai_cli": dict(options)},
            agent_backends={"xai": "cli"},
            agent_options={"xai": dict(options)},
        )
        self.assertEqual(settings["agents"]["xai"]["backend"], "cli")
        self.assertIn("--reasoning-effort", settings["agents"]["xai"]["command_preview"])

        referee = Referee(
            RefereeConfig(
                workflow="solo-xai",
                dry_run=True,
                collab_config=config,
                agent_backends={"xai": "cli"},
                agent_options={"xai": dict(options)},
                color=False,
            ),
            printer=lambda event: None,
        )
        self.assertIsInstance(referee._runners()["xai"], DryRunRunner)

        described = describe_options(config, health=lambda backend: BackendHealth(status="ok"))
        policy = described["canonical_backends"]["xai_cli"]["policy"]
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
