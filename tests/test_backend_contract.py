"""Generic self-describing backend contract and extension-point tests."""

import asyncio
import unittest

from agent_collab import backends
from agent_collab.backends.base import (
    HEALTH_OK,
    BackendCapabilities,
    BackendHealth,
    OptionSpec,
    normalize_declared_options,
)
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig, builtin_config
from agent_collab.events import Event
from agent_collab.options import (
    StartOptionsError,
    build_session_settings,
    describe_options,
    normalize_start_options,
    validate_start_backends,
)
from agent_collab.runners import AgentRunner, configured_runner


class _ContractRunner(AgentRunner):
    def __init__(self, name, options):
        self.name = name
        self.options = dict(options)

    async def run(self, prompt, workdir):
        yield Event.create(
            "claude",
            "message",
            f"contract backend: {self.options['contract_mode']}",
            {"options": dict(self.options)},
        )


class _ContractBackend:
    id = "contract-test"
    agent_type = "claude"
    capabilities = BackendCapabilities()
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
        return normalize_declared_options(agent, requested, self.option_schema(agent))

    def settings_summary(self, agent, options):
        return {"backend": self.id, "options": dict(options), "contract": True}

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
            claude_options={"contract_mode": "careful"},
        )
        normalized = normalize_start_options(
            config,
            "solo",
            claude_options={"contract_mode": "careful"},
            agent_backends=selection.agent_backends,
        )
        self.assertEqual(selection.agent_backends, {"claude": "contract-test"})
        self.assertEqual(normalized.agent_options, {"claude": {"contract_mode": "careful"}})

        described = describe_options(
            config,
            health=lambda backend: BackendHealth(status=HEALTH_OK),
        )
        entry = described["backends"]["claude"]["entries"]["contract-test"]
        self.assertEqual(
            entry["option_schema"]["properties"]["contract_mode"]["allowed"],
            ["fast", "careful"],
        )
        self.assertIn("contract_mode", described["claude_options"]["properties"])

        settings = build_session_settings(
            config,
            "solo",
            normalized.provider_options,
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
            return [event async for event in runner.run("test", ".")]

        events = asyncio.run(collect())
        self.assertEqual(events[0].raw["options"], {"contract_mode": "careful"})

    def test_unknown_and_invalid_unique_options_keep_provider_field_paths(self):
        config = _contract_config()
        for requested, expected in (
            ({"unknown": True}, "claude_options.unknown"),
            ({"contract_mode": "reckless"}, "claude_options.contract_mode"),
        ):
            with self.subTest(requested=requested), self.assertRaises(StartOptionsError) as ctx:
                validate_start_backends(config, "solo", claude_options=requested)
            self.assertEqual(ctx.exception.to_dict()["details"][0]["path"], expected)

    def test_config_may_narrow_but_not_expand_backend_schema(self):
        narrowed = _contract_config(
            {"contract_mode": {"allowed": ["careful"], "default": "careful"}}
        )
        normalized = normalize_start_options(narrowed, "solo")
        self.assertEqual(normalized.agent_options["claude"], {"contract_mode": "careful"})
        with self.assertRaises(StartOptionsError):
            normalize_start_options(
                _contract_config({"contract_mode": {"allowed": ["careful", "reckless"]}}),
                "solo",
            )

    def test_provider_request_must_be_supported_by_every_selected_backend(self):
        config = CollaborationConfig(
            agents={
                "custom": AgentConfig(id="custom", type="claude", backend="contract-test"),
                "cli": AgentConfig(id="cli", type="claude", command="claude", backend="cli"),
            },
            workflows={
                "mixed": WorkflowConfig(id="mixed", sequence=["custom", "cli"]),
            },
        )
        with self.assertRaises(StartOptionsError) as ctx:
            normalize_start_options(
                config,
                "mixed",
                claude_options={"contract_mode": "careful"},
                agent_backends={"custom": "contract-test", "cli": "cli"},
            )
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "claude_options.contract_mode")
        self.assertIn("agent 'cli'", detail["message"])
        self.assertIn("backend 'cli'", detail["message"])


class BuiltinBackendContractTests(unittest.TestCase):
    def test_every_builtin_backend_has_well_formed_declarative_schema(self):
        config = builtin_config()
        for agent_type in ("claude", "codex", "antigravity"):
            agent = config.agents[agent_type]
            for backend_id in ("cli", "sdk"):
                with self.subTest(agent_type=agent_type, backend=backend_id):
                    backend = backends.get_backend(agent_type, backend_id)
                    schema = backend.option_schema(agent)
                    self.assertTrue(schema)
                    self.assertTrue(all(isinstance(value, OptionSpec) for value in schema.values()))
                    normalized = backend.normalize_options(agent, {})
                    self.assertFalse(set(normalized) - set(schema))
                    self.assertIsInstance(backend.settings_summary(agent, normalized), dict)

    def test_incomplete_backend_is_rejected_at_registration(self):
        class Incomplete:
            id = "incomplete"
            agent_type = "claude"
            capabilities = BackendCapabilities()

            def probe(self):
                return BackendHealth()

        with self.assertRaisesRegex(TypeError, "option_schema"):
            backends.register(Incomplete())

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
        self.assertNotIn("thinking_level", normalized.agent_options["claude-sdk"])
        self.assertNotIn("thinking_level", normalized.provider_options["claude_options"])


if __name__ == "__main__":
    unittest.main()
