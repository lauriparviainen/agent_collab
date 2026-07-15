"""Generic self-describing backend contract and extension-point tests."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_collab import backends
from agent_collab.backends.base import (
    HEALTH_OK,
    BackendOptionError,
    BackendCapabilities,
    BackendHealth,
    OptionSpec,
    normalize_declared_options,
)
from agent_collab.config import (
    AgentConfig,
    CollaborationConfig,
    WorkflowConfig,
    builtin_config,
)
from agent_collab.backend_contract import load_option_schema
from agent_collab.config import ConfigError
from agent_collab.events import Event
from agent_collab.options import (
    StartOptionsError,
    build_session_settings,
    describe_options,
    normalize_start_options,
    validate_start_backends,
)
from agent_collab.runners import AgentRunner, configured_runner
from agent_collab.outcomes import TurnOutcome


class _ContractRunner(AgentRunner):
    def __init__(self, name, options):
        self.name = name
        self.options = dict(options)

    async def run_turn(self, prompt, workdir, emit):
        await emit(
            Event.create(
                "claude",
                "message",
                f"contract backend: {self.options['contract_mode']}",
                {"options": dict(self.options)},
            )
        )
        return TurnOutcome("completed")


class _ContractBackend:
    id = "contract-test"
    agent_type = "claude"
    capabilities = BackendCapabilities()
    brand_color = "#123456"
    event_fidelity = "typed"
    provider_session_id_kind = None
    checks_credentials = False
    block_on_unavailable = False

    def probe(self):
        return BackendHealth(status=HEALTH_OK)

    def option_schema(self, agent):
        return {
            "contract_mode": OptionSpec(
                "string",
                allowed=("fast", "careful"),
                default="fast",
            )
        }

    def normalize_options(self, agent, requested):
        return normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=agent.options_for(self.id),
        )

    def settings_summary(self, agent, options):
        return {"backend": self.id, "options": dict(options), "contract": True}

    def command_preview(self, agent, options, workdir=None):
        return None

    def create_runner(self, agent, verbose, options):
        return _ContractRunner(agent.id, options)


def _contract_config(options=None):
    agent = AgentConfig(
        id="claude",
        type="claude",
        backend="contract-test",
        options=options or {},
    )
    return CollaborationConfig(
        agents={"claude": agent},
        workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
    )


def _registration_candidate(backend_id):
    source = _ContractBackend()
    return SimpleNamespace(
        id=backend_id,
        agent_type=source.agent_type,
        capabilities=source.capabilities,
        brand_color=source.brand_color,
        event_fidelity=source.event_fidelity,
        provider_session_id_kind=source.provider_session_id_kind,
        checks_credentials=source.checks_credentials,
        block_on_unavailable=source.block_on_unavailable,
        probe=source.probe,
        option_schema=source.option_schema,
        normalize_options=source.normalize_options,
        settings_summary=source.settings_summary,
        command_preview=source.command_preview,
        create_runner=source.create_runner,
    )


class BackendContractExtensionTests(unittest.TestCase):
    def setUp(self):
        backends.register(_ContractBackend())

    def tearDown(self):
        backends.unregister("claude", "contract-test")

    def test_unique_option_is_discovered_normalized_summarized_and_executed(self):
        config = _contract_config()
        selection = validate_start_backends(
            config,
            "solo",
            backend_options={"claude_contract-test": {"contract_mode": "careful"}},
        )
        normalized = normalize_start_options(
            config,
            "solo",
            backend_options={"claude_contract-test": {"contract_mode": "careful"}},
            agent_backends=selection.agent_backends,
        )
        self.assertEqual(selection.agent_backends, {"claude": "contract-test"})
        self.assertEqual(normalized.agent_options, {"claude": {"contract_mode": "careful"}})

        described = describe_options(
            config,
            health=lambda backend: BackendHealth(status=HEALTH_OK),
        )
        entry = described["backends"]["claude_contract-test"]
        self.assertEqual(
            entry["static"]["option_schema"]["properties"]["contract_mode"]["allowed"],
            ["fast", "careful"],
        )

        settings = build_session_settings(
            config,
            "solo",
            normalized.backend_options,
            agent_backends=selection.agent_backends,
            agent_options=normalized.agent_options,
        )
        agent_settings = settings["agents"]["claude"]
        self.assertEqual(agent_settings["contract_mode"], "careful")
        self.assertEqual(agent_settings["backend_summary"]["options"], {"contract_mode": "careful"})

        runner = configured_runner(
            config.agents["claude"],
            options=normalized.agent_options["claude"],
            backend_id="contract-test",
        )

        async def collect():
            events = []

            async def emit(event):
                events.append(event)

            await runner.run_turn("test", Path("."), emit)
            return events

        events = asyncio.run(collect())
        self.assertEqual(events[0].raw["options"], {"contract_mode": "careful"})

    def test_unknown_and_invalid_unique_options_keep_provider_field_paths(self):
        config = _contract_config()
        for requested, expected in (
            ({"unknown": True}, "backend_options.claude_contract-test.unknown"),
            ({"contract_mode": "reckless"}, "backend_options.claude_contract-test.contract_mode"),
        ):
            with self.subTest(requested=requested), self.assertRaises(StartOptionsError) as ctx:
                validate_start_backends(
                    config, "solo", backend_options={"claude_contract-test": requested}
                )
            self.assertEqual(ctx.exception.to_dict()["details"][0]["path"], expected)

    def test_config_supplies_session_default_but_request_can_override(self):
        configured = _contract_config({"contract_mode": "careful"})
        normalized = normalize_start_options(configured, "solo")
        self.assertEqual(normalized.agent_options["claude"], {"contract_mode": "careful"})
        overridden = normalize_start_options(
            configured,
            "solo",
            backend_options={"claude_contract-test": {"contract_mode": "fast"}},
        )
        self.assertEqual(overridden.agent_options["claude"], {"contract_mode": "fast"})

    def test_backend_qualified_request_applies_only_to_matching_backend(self):
        config = CollaborationConfig(
            agents={
                "custom": AgentConfig(id="custom", type="claude", backend="contract-test"),
                "cli": AgentConfig(id="cli", type="claude", command="claude", backend="cli"),
            },
            workflows={
                "mixed": WorkflowConfig(id="mixed", sequence=["custom", "cli"]),
            },
        )
        normalized = normalize_start_options(
            config,
            "mixed",
            backend_options={"claude_contract-test": {"contract_mode": "careful"}},
            agent_backends={"custom": "contract-test", "cli": "cli"},
        )
        self.assertEqual(normalized.agent_options["custom"]["contract_mode"], "careful")
        self.assertNotIn("contract_mode", normalized.agent_options["cli"])


class BuiltinBackendContractTests(unittest.TestCase):
    def test_every_builtin_backend_has_a_colocated_option_manifest(self):
        root = Path(__file__).parents[2] / "agent_collab" / "backends"
        for name in backends.registered_backend_names():
            with self.subTest(name=name):
                path = root / name / "options.toml"
                self.assertTrue(path.is_file())
                self.assertTrue(load_option_schema(path))

    def test_invalid_option_manifest_fails_with_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "options.toml"
            path.write_text(
                'schema_version = 1\n[options.model]\ntype = "mystery"\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ConfigError, "options.toml"):
                load_option_schema(path)

    def test_required_manifest_field_is_declarative_and_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "options.toml"
            path.write_text(
                'schema_version = 1\n[options.model]\ntype = "string"\nrequired = true\n',
                encoding="utf-8",
            )
            schema = load_option_schema(path)
            self.assertTrue(schema["model"].required)
            with self.assertRaises(BackendOptionError) as ctx:
                normalize_declared_options({}, schema)
            self.assertEqual(ctx.exception.field, "model")

    def test_every_builtin_backend_has_well_formed_declarative_schema(self):
        config = builtin_config()
        for agent_type in ("claude", "codex", "antigravity", "xai"):
            for backend_id in ("cli", "sdk"):
                # Derived agents only exist for enabled backends; build the
                # equivalent agent from the canonical backend section so every
                # builtin backend's schema is exercised regardless of policy.
                section = config.backends.get(f"{agent_type}_{backend_id}")
                agent = AgentConfig(
                    id=f"{agent_type}_{backend_id}",
                    type=agent_type,
                    backend=backend_id,
                    command=None if section is None else section.command,
                    args=[] if section is None else list(section.args),
                    options={} if section is None else dict(section.options),
                )
                with self.subTest(agent_type=agent_type, backend=backend_id):
                    backend = backends.get_backend(agent_type, backend_id)
                    schema = backend.option_schema(agent)
                    self.assertTrue(schema)
                    self.assertTrue(all(isinstance(value, OptionSpec) for value in schema.values()))
                    requested = {
                        name: (spec.allowed[0] if spec.allowed else "fixture")
                        for name, spec in schema.items()
                        if spec.required
                    }
                    normalized = backend.normalize_options(agent, requested)
                    self.assertFalse(set(normalized) - set(schema))
                    self.assertIsInstance(backend.settings_summary(agent, normalized), dict)

    def test_incomplete_backend_is_rejected_at_registration(self):
        class Incomplete:
            id = "incomplete"
            agent_type = "claude"
            capabilities = BackendCapabilities()
            block_on_unavailable = False
            checks_credentials = False
            event_fidelity = "typed"
            provider_session_id_kind = None

            def probe(self):
                return BackendHealth()

        with self.assertRaisesRegex(TypeError, "option_schema"):
            backends.register(Incomplete())

    def test_missing_policy_and_fidelity_attributes_are_rejected_at_registration(self):
        for attribute in (
            "block_on_unavailable",
            "checks_credentials",
            "event_fidelity",
            "provider_session_id_kind",
        ):
            candidate = _registration_candidate(f"missing-{attribute}")
            delattr(candidate, attribute)
            try:
                with (
                    self.subTest(attribute=attribute),
                    self.assertRaisesRegex(TypeError, attribute),
                ):
                    backends.register(candidate)
            finally:
                backends.unregister("claude", candidate.id)

    def test_invalid_policy_and_fidelity_attribute_types_are_rejected(self):
        cases = (
            ("block_on_unavailable", 0),
            ("checks_credentials", "yes"),
            ("event_fidelity", ""),
            ("provider_session_id_kind", ""),
            ("provider_session_id_kind", 1),
        )
        for attribute, value in cases:
            candidate = _registration_candidate(f"invalid-{attribute}-{value!r}")
            setattr(candidate, attribute, value)
            try:
                with (
                    self.subTest(attribute=attribute, value=value),
                    self.assertRaisesRegex(TypeError, attribute),
                ):
                    backends.register(candidate)
            finally:
                backends.unregister("claude", candidate.id)

    def test_mixed_cli_and_sdk_agents_get_distinct_normalized_defaults(self):
        config = CollaborationConfig(
            agents={
                "claude-cli": AgentConfig(
                    id="claude-cli",
                    type="claude",
                    command="claude",
                    args=["--effort", "high"],
                    backend="cli",
                ),
                "claude-sdk": AgentConfig(
                    id="claude-sdk",
                    type="claude",
                    command="claude",
                    args=["--effort", "high"],
                    default_options=dict(builtin_config().backends["claude_sdk"].default_options),
                    backend="sdk",
                ),
            },
            workflows={
                "mixed": WorkflowConfig(
                    id="mixed",
                    sequence=["claude-cli", "claude-sdk"],
                )
            },
        )
        normalized = normalize_start_options(
            config,
            "mixed",
            agent_backends={"claude-cli": "cli", "claude-sdk": "sdk"},
        )
        self.assertEqual(normalized.agent_options["claude-cli"]["thinking_level"], "high")
        self.assertEqual(normalized.agent_options["claude-sdk"]["thinking_level"], "high")
        self.assertEqual(normalized.backend_options["claude_sdk"]["thinking_level"], "high")


if __name__ == "__main__":
    unittest.main()
