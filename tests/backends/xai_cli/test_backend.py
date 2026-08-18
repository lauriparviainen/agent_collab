import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab import backends
from agent_collab.backend_contract import OPTION_UNSET, BackendOptionError
from agent_collab.backends.common.health import xai_cli_credentials
from agent_collab.backends.xai_cli import XaiCliBackend, XaiStreamingParser, parse_xai_line
from agent_collab.config import (
    AgentConfig,
    SUBPROCESS_AGENT_TYPES,
    builtin_config,
    load_config,
    merge_config_data,
)
from agent_collab.events import VALID_SOURCES, Event, harvest_message_text
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
        self.assertEqual(self.backend.event_fidelity, "message_first")
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
        self.assertEqual(
            schema["model"].suggested,
            ("grok-4.6", "grok-4.5", "grok-composer-2.5-fast"),
        )
        self.assertIsNone(schema["model"].allowed)
        self.assertIsNone(schema["sandbox"].allowed)
        # Defaults ship in the built-in config, not the backend manifest.
        self.assertIs(schema["permission_mode"].default, OPTION_UNSET)
        self.assertIs(schema["sandbox"].default, OPTION_UNSET)
        defaults = builtin_config().backends["xai_cli"].default_options
        self.assertEqual(defaults["model"], "grok-4.6")
        self.assertEqual(defaults["thinking_level"], "high")
        self.assertEqual(defaults["permission_mode"], "bypassPermissions")
        self.assertEqual(defaults["sandbox"], "read-only")
        self.assertEqual(schema["provider_max_turns"].minimum, 1)

    def test_reasoning_alias_is_canonical_and_conflicts_on_native_field(self):
        options = self.backend.normalize_options(self.agent(), {"reasoning_effort": "high"})
        self.assertEqual(
            options,
            {
                "model": "grok-4.6",
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
                "model": "grok-4.6",
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
                        "grok-composer-2.5-fast",
                        "--effort",
                        "low",
                        sentinel,
                    ]
                )
                options = self.backend.normalize_options(agent, {"thinking_level": "high"})
                self.assertEqual(options["model"], "grok-composer-2.5-fast")
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
        self.assertTrue(events[0].raw.get("final"))
        self.assertNotIn("full_text", events[0].raw)
        self.assertEqual(events[1].raw["provider_session_id"], "sess")

    def test_current_grok_end_turn_completes_and_keeps_session_identity(self):
        """Live Grok streaming-json emits snake_case end_turn, not EndTurn."""
        runner = self.backend.create_runner(self.agent(id="reviewer"), False, {})
        self.assertIsNone(runner.parser('{"type":"text","data":"ready"}', False))
        events = runner.parser(
            '{"type":"end","stopReason":"end_turn","sessionId":"sess","requestId":"req",'
            '"num_turns":10}',
            False,
        )
        self.assertEqual(len(events), 2)
        message, identity = events
        self.assertEqual((message.source, message.type, message.text), ("xai", "message", "ready"))
        self.assertEqual(identity.provider_session["provider_session_id"], "sess")
        evidence = runner.parser.take_terminal_evidence()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].outcome, "completed")
        self.assertIsNone(evidence[0].code)
        self.assertEqual(evidence[0].provider_stop_reason, "end_turn")

    def test_runner_flushes_partial_text_when_stdout_ends_without_end_record(self):
        runner = self.backend.create_runner(self.agent(), False, {})
        runner.parser('{"type":"text","data":"partial"}', False)
        events = runner.parser.finish()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, "partial")

    def test_runner_reports_cancelled_end_as_fatal_and_keeps_session_identity(self):
        runner = self.backend.create_runner(self.agent(id="reviewer"), False, {})

        for stop_reason in ("Cancelled", "cancelled"):
            with self.subTest(stop_reason=stop_reason):
                runner = self.backend.create_runner(self.agent(id="reviewer"), False, {})
                events = runner.parser(
                    f'{{"type":"end","stopReason":"{stop_reason}",'
                    f'"sessionId":"sess","requestId":"req"}}',
                    False,
                )

                self.assertEqual(len(events), 2)
                failure, identity = events
                self.assertEqual((failure.source, failure.type), ("error", "error"))
                self.assertIn("before producing a response", failure.text)
                self.assertEqual(failure.raw["code"], "provider_turn_cancelled")
                self.assertTrue(failure.raw["fatal"])
                self.assertEqual(failure.raw["provider_stop_reason"], stop_reason)
                self.assertIsNone(failure.provider_session)
                self.assertEqual(identity.provider_session["provider_session_id"], "sess")
                evidence = runner.parser.take_terminal_evidence()
                self.assertEqual(
                    (evidence[0].outcome, evidence[0].code, evidence[0].provider_stop_reason),
                    ("cancelled", "provider_turn_cancelled", stop_reason),
                )

    def test_unknown_end_reason_is_not_treated_as_success(self):
        event = parse_xai_line('{"type":"end","stopReason":"SafetyStop","sessionId":"sess"}')

        self.assertEqual((event.source, event.type), ("error", "error"))
        self.assertEqual(event.raw["code"], "provider_terminal_failure")
        self.assertIn("SafetyStop", event.text)

    def test_incomplete_and_refusal_end_reasons_are_structured(self):
        cases = (
            ("max_tokens", "failed", "provider_output_incomplete"),
            ("max_turn_requests", "failed", "provider_output_incomplete"),
            ("refusal", "refused", "provider_turn_refused"),
        )
        for stop_reason, outcome, code in cases:
            with self.subTest(stop_reason=stop_reason):
                event = parse_xai_line(
                    f'{{"type":"end","stopReason":"{stop_reason}","sessionId":"sess"}}'
                )
                self.assertEqual((event.source, event.type), ("error", "error"))
                self.assertEqual(event.raw["code"], code)
                self.assertTrue(event.raw["fatal"])
                runner = self.backend.create_runner(self.agent(), False, {})
                runner.parser(
                    f'{{"type":"end","stopReason":"{stop_reason}","sessionId":"sess"}}',
                    False,
                )
                evidence = runner.parser.take_terminal_evidence()
                self.assertEqual(
                    (evidence[0].outcome, evidence[0].code, evidence[0].provider_stop_reason),
                    (outcome, code, stop_reason),
                )

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
            agent, {"model": "grok-4.5", "thinking_level": "low"}
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
                # Avoid host ambient GROK_HOME validation for a dry-run unit check.
                sandbox="none",
                color=False,
            ),
            printer=lambda event: None,
        )
        self.assertIsInstance(referee._runners()["xai_cli"], DryRunRunner)

        described = describe_options(config, health=lambda backend: BackendHealth(status="ok"))
        model_schema = described["backends"]["xai_cli"]["static"]["option_schema"]["properties"][
            "model"
        ]
        self.assertEqual(
            model_schema["suggested"],
            ["grok-4.6", "grok-4.5", "grok-composer-2.5-fast"],
        )
        self.assertNotIn("allowed", model_schema)
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


def _feed_parser(parser, lines, verbose=False):
    events = []
    for line in lines:
        parsed = parser(line, verbose)
        if parsed is None:
            continue
        events.extend(parsed if isinstance(parsed, list) else [parsed])
    return events


def _harvest(events, agent_id="xai"):
    for event in events:
        event.agent_id = agent_id
    referee = Referee(RefereeConfig(mock=True, workdir=Path("."), color=False))
    return referee._find_turn_answer(events, 0, agent_id)


class XaiStreamingParserTests(unittest.TestCase):
    def fixture_lines(self, name):
        return (FIXTURES / name).read_text(encoding="utf-8").splitlines()

    def test_legacy_reasoning_fixture_heartbeats_and_coalesces_without_tools(self):
        parser = XaiStreamingParser()
        events = _feed_parser(parser, self.fixture_lines("streaming-json-reasoning.ndjson"))
        self.assertEqual([event.source for event in events], ["xai", "xai", "xai"])
        self.assertEqual([event.type for event in events], ["status", "message", "status"])
        self.assertEqual(events[0].text, "thinking…")
        self.assertEqual(events[1].text, "fixture-ok")
        self.assertFalse(any(event.source == "tool" for event in events))
        evidence = parser.take_terminal_evidence()
        self.assertEqual(evidence[0].outcome, "completed")
        self.assertEqual(evidence[0].provider_stop_reason, "EndTurn")

    def test_legacy_tooluse_fixture_still_has_no_typed_action_rows(self):
        parser = XaiStreamingParser()
        events = _feed_parser(parser, self.fixture_lines("streaming-json-tooluse.ndjson"))
        self.assertFalse(any(event.source == "tool" for event in events))
        self.assertFalse(
            any(event.type in {"tool_call", "command", "file_change"} for event in events)
        )
        evidence = parser.take_terminal_evidence()
        self.assertEqual(evidence[0].outcome, "completed")
        self.assertEqual(evidence[0].provider_stop_reason, "EndTurn")

    def test_two_hundred_char_flush_keeps_running_full_text_for_harvest(self):
        first = "a" * 200
        parser = XaiStreamingParser()
        first_events = _feed_parser(parser, [json.dumps({"type": "text", "data": first})])
        self.assertEqual(len(first_events), 1)
        self.assertEqual(first_events[0].text, first)
        self.assertEqual(first_events[0].raw["full_text"], first)
        self.assertFalse(first_events[0].raw.get("final"))

        rest = _feed_parser(
            parser,
            [
                json.dumps({"type": "text", "data": " more"}),
                json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": "sess"}),
            ],
        )
        messages = [event for event in rest if event.type == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, " more")
        self.assertEqual(messages[0].raw["full_text"], first + " more")
        self.assertTrue(messages[0].raw["final"])
        answer = _harvest(first_events + rest)
        self.assertEqual(answer["text"], first + " more")

    def test_usage_flushes_text_but_is_not_terminal(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps({"type": "text", "data": "ready"}),
                json.dumps({"type": "usage", "stopReason": "tool_use", "messageId": "resp"}),
                json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": "sess"}),
            ],
        )
        messages = [event for event in events if event.type == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "ready")
        self.assertFalse(messages[0].raw.get("final"))
        self.assertEqual(messages[0].raw["full_text"], "ready")
        evidence = parser.take_terminal_evidence()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].outcome, "completed")
        self.assertEqual(evidence[0].provider_stop_reason, "end_turn")
        answer = _harvest(events)
        self.assertEqual(answer["text"], "ready")

    def test_thought_only_end_turn_completes_without_harvested_message(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps({"type": "thought", "data": "planning"}),
                json.dumps({"type": "thought", "data": "still planning"}),
                json.dumps({"type": "end", "stopReason": "EndTurn", "sessionId": "sess"}),
            ],
        )
        self.assertEqual([event.type for event in events], ["status", "status"])
        self.assertEqual(events[0].text, "thinking…")
        self.assertFalse(any(event.type == "message" for event in events))
        self.assertIsNone(_harvest(events))
        verbose = _feed_parser(
            XaiStreamingParser(),
            [
                json.dumps({"type": "thought", "data": "planning"}),
                json.dumps({"type": "thought", "data": "still planning"}),
            ],
            verbose=True,
        )
        self.assertEqual([event.text for event in verbose], ["planning", "still planning"])

    def test_unknown_types_are_ignored_and_non_terminal(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps({"type": "plan", "entries": []}),
                json.dumps({"type": "available_commands", "tools": []}),
                json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": "sess"}),
            ],
        )
        self.assertFalse(any(event.source == "error" for event in events))
        self.assertEqual(parser.take_terminal_evidence()[0].outcome, "completed")
        loud = _feed_parser(
            XaiStreamingParser(),
            [json.dumps({"type": "plan", "entries": []})],
            verbose=True,
        )
        self.assertEqual(loud[0].type, "status")

    def test_invalid_json_still_fails_closed(self):
        with self.assertRaises(ValueError):
            XaiStreamingParser()("not-json")

    def test_documented_tool_update_merges_start_fields_on_close(self):
        parser = XaiStreamingParser()
        events = _feed_parser(parser, self.fixture_lines("streaming-json-documented-tools.ndjson"))
        tools = [event for event in events if event.source == "tool"]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0].type, "tool_call")
        self.assertEqual(tools[0].text, 'Read {"path": "src/main.rs"}')
        self.assertEqual(tools[1].type, "tool_call")
        self.assertEqual(tools[1].text, 'Read {"path": "src/main.rs"} · completed')
        self.assertNotIn("diff", tools[1].raw)
        self.assertNotIn("patch", tools[1].raw)
        self.assertEqual(tools[1].raw["name"], "read_file")
        self.assertEqual(tools[1].raw["input"], {"path": "src/main.rs"})
        messages = [event for event in events if event.type == "message"]
        self.assertEqual([event.text for event in messages], ["Here's a summary"])
        self.assertEqual(parser.take_terminal_evidence()[0].outcome, "completed")

    def test_live_1_0_5_read_turn_coalesces_pending_and_null_status_update(self):
        parser = XaiStreamingParser()
        events = _feed_parser(parser, self.fixture_lines("streaming-json-live-tools-1.0.5.ndjson"))
        tools = [event for event in events if event.source == "tool"]
        self.assertEqual(
            [(event.type, event.text) for event in tools],
            [
                ("tool_call", 'read_file {"target_file": "note.txt"}'),
                ("tool_call", 'read_file {"target_file": "note.txt"} · completed'),
            ],
        )
        self.assertEqual(tools[0].raw["kind"], "read")
        self.assertNotIn("diff", tools[1].raw)
        self.assertNotIn("patch", tools[1].raw)
        self.assertEqual(events[0].text, "thinking…")
        messages = [event for event in events if event.type == "message"]
        self.assertEqual([event.text for event in messages], ["OK"])
        self.assertEqual(parser.take_terminal_evidence()[0].provider_stop_reason, "end_turn")

    def test_kind_table_maps_execute_edit_and_unknown(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "toolCallId": "exec",
                        "title": "Shell",
                        "kind": "execute",
                        "status": "in_progress",
                        "rawInput": {"command": "ls"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "toolCallId": "edit",
                        "title": "Edit",
                        "kind": "edit",
                        "status": "in_progress",
                        "rawInput": {"path": "a.py"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "toolCallId": "weird",
                        "title": "Other",
                        "kind": "not-a-kind",
                        "status": "in_progress",
                    }
                ),
            ],
        )
        self.assertEqual(
            [(event.type, event.text) for event in events],
            [
                ("command", 'Shell {"command": "ls"}'),
                ("file_change", 'Edit {"path": "a.py"}'),
                ("tool_call", "Other"),
            ],
        )

    def test_failed_close_row_and_identical_status_updates_are_coalesced(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "toolCallId": "call_2",
                        "title": "Read",
                        "kind": "read",
                        "status": "in_progress",
                        "rawInput": {"path": "a"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call_update",
                        "toolCallId": "call_2",
                        "status": "in_progress",
                        "content": [{"type": "spam"}],
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call_update",
                        "toolCallId": "call_2",
                        "status": "failed",
                    }
                ),
            ],
        )
        self.assertEqual(
            [event.text for event in events],
            ['Read {"path": "a"}', 'Read {"path": "a"} · failed'],
        )

    def test_pending_then_in_progress_does_not_double_open_when_first_has_args(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "toolCallId": "call_3",
                        "title": "Read",
                        "status": "pending",
                        "rawInput": {"path": "a"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call_update",
                        "toolCallId": "call_3",
                        "status": "in_progress",
                    }
                ),
            ],
        )
        self.assertEqual([event.text for event in events], ['Read {"path": "a"}'])

    def test_bare_pending_then_in_progress_with_args_emits_second_open_row(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "toolCallId": "call_4",
                        "status": "pending",
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call_update",
                        "toolCallId": "call_4",
                        "status": "in_progress",
                        "title": "Read",
                        "rawInput": {"path": "a"},
                    }
                ),
            ],
        )
        self.assertEqual([event.text for event in events], ["tool", 'Read {"path": "a"}'])

    def test_missing_tool_call_id_emits_once_without_crashing(self):
        parser = XaiStreamingParser()
        events = _feed_parser(
            parser,
            [
                json.dumps(
                    {
                        "type": "tool_call",
                        "title": "Read",
                        "kind": "execute",
                        "status": "in_progress",
                    }
                )
            ],
        )
        self.assertEqual(
            [(events[0].source, events[0].type, events[0].text)], [("tool", "tool_call", "Read")]
        )

    def test_harvest_prefers_full_text_without_requiring_final(self):
        events = [
            Event.create(
                "xai",
                "message",
                "last fragment",
                {"full_text": "the whole answer"},
                agent_id="xai",
            )
        ]
        self.assertEqual(_harvest(events)["text"], "the whole answer")
        self.assertEqual(
            harvest_message_text("last fragment", {"full_text": "the whole answer"}),
            "the whole answer",
        )

    def test_reset_clears_full_text_so_the_next_turn_does_not_concatenate(self):
        parser = XaiStreamingParser()
        first = _feed_parser(
            parser,
            [
                json.dumps({"type": "text", "data": "first-turn answer"}),
                json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": "sess"}),
            ],
        )
        parser.reset()
        second = _feed_parser(
            parser,
            [
                json.dumps({"type": "text", "data": "ready"}),
                json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": "sess"}),
            ],
        )
        messages = [event for event in second if event.type == "message"]
        self.assertEqual([event.text for event in messages], ["ready"])
        self.assertNotIn("full_text", messages[0].raw)
        self.assertNotIn("first-turn", messages[0].text)
        self.assertEqual(_harvest(first)["text"], "first-turn answer")
        self.assertEqual(_harvest(second)["text"], "ready")

    def test_codex_style_final_without_full_text_still_harvests_event_text(self):
        events = [
            Event.create(
                "codex",
                "message",
                "Final answer.",
                {"final": True},
                agent_id="codex",
            ),
            Event.create(
                "codex",
                "message",
                "Trailing commentary.",
                {"phase": "commentary"},
                agent_id="codex",
            ),
        ]
        self.assertEqual(_harvest(events, "codex")["text"], "Final answer.")


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

    def test_missing_or_empty_auth_is_unknown_without_opening_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_UNKNOWN)
            (base / "auth.json").write_text("", encoding="utf-8")
            self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_UNKNOWN)

    def test_cached_auth_uses_only_regular_file_presence_not_secret_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            auth = base / "auth.json"
            for payload in ("{broken", json.dumps({}), json.dumps({"entry": {}})):
                with self.subTest(payload=payload):
                    auth.write_text(payload, encoding="utf-8")
                    with mock.patch.object(Path, "read_text", side_effect=AssertionError):
                        self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_OK)
            auth.unlink()
            auth.symlink_to(base / "missing")
            self.assertEqual(xai_cli_credentials(base, {}), CREDENTIALS_UNKNOWN)

    def test_effective_grok_home_environment_selects_cached_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "custom"
            base.mkdir()
            (base / "auth.json").write_text("cached", encoding="utf-8")
            self.assertEqual(
                xai_cli_credentials(env={"GROK_HOME": str(base)}),
                CREDENTIALS_OK,
            )


if __name__ == "__main__":
    unittest.main()
