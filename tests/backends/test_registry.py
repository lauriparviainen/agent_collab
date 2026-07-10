"""Backend registry and resolution tests."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_collab import backends
from agent_collab.backends.base import BackendCapabilities
from agent_collab.config import AgentConfig, builtin_config
from agent_collab.options import StartOptionsError, validate_start_backends, validate_start_options
from agent_collab.referee import Referee, RefereeConfig
from agent_collab.runners import AgentRunner, SubprocessRunner


class RegistryResolutionTests(unittest.TestCase):
    def test_canonical_names_are_unique(self):
        self.assertEqual(
            len(backends.registered_backend_names()),
            len(set(backends.registered_backend_names())),
        )

    def test_builtin_cli_pairs_are_registered(self):
        self.assertTrue(backends.is_registered("claude", "cli"))
        self.assertTrue(backends.is_registered("codex", "cli"))

    def test_all_eight_real_provider_backend_pairs_are_registered(self):
        # Stage 5.1 makes `sdk` first-class for every real provider.
        for agent_type in ("claude", "codex", "antigravity", "xai"):
            self.assertTrue(backends.is_registered(agent_type, "cli"), agent_type)
            self.assertTrue(backends.is_registered(agent_type, "sdk"), agent_type)

    def test_registered_backends_lists_ids_for_type(self):
        self.assertEqual(backends.registered_backends("claude"), ["cli", "sdk"])
        self.assertEqual(backends.registered_backends("codex"), ["cli", "sdk"])
        self.assertEqual(backends.registered_backends("antigravity"), ["cli", "sdk"])
        self.assertEqual(backends.registered_backends("xai"), ["cli", "sdk"])
        self.assertEqual(backends.registered_backends("nonesuch"), [])

    def test_resolution_precedence_request_over_config_over_default(self):
        agent = SimpleNamespace(id="a", type="claude", backend="config-backend")
        # request beats agent config beats default
        self.assertEqual(backends.resolve_backend_id(agent, "request-backend"), "request-backend")
        # agent config beats default
        self.assertEqual(backends.resolve_backend_id(agent, None), "config-backend")
        # default when neither present
        plain = AgentConfig(id="b", type="claude")
        self.assertEqual(backends.resolve_backend_id(plain, None), "cli")

    def test_get_backend_unknown_lists_registered_ids(self):
        with self.assertRaises(KeyError) as ctx:
            backends.get_backend("claude", "nonesuch")
        message = str(ctx.exception)
        self.assertIn("nonesuch", message)
        self.assertIn("cli", message)  # registered ids for the type are listed
        self.assertIn("sdk", message)

    def test_get_backend_returns_the_registered_backend(self):
        backend = backends.get_backend("claude", "cli")
        self.assertEqual(backend.agent_type, "claude")
        self.assertEqual(backend.id, "cli")


class CapabilityReducerTests(unittest.TestCase):
    def test_all_false_backends_yield_non_resumable_session(self):
        per_agent = {
            "claude": BackendCapabilities(),
            "codex": BackendCapabilities(),
        }
        summary = backends.summarize_session_capabilities(per_agent)
        self.assertEqual(summary, {"resumable": False, "interruptible": False})

    def test_reducer_ands_inputs_and_requires_captured_id_for_resumable(self):
        # A future stage flips inputs true; the reducer must compute, not hardcode.
        per_agent = {
            "a": BackendCapabilities(resume=True, interrupt=True),
            "b": BackendCapabilities(resume=True, interrupt=True),
        }
        # resume=true everywhere but no captured ids -> not resumable
        self.assertEqual(
            backends.summarize_session_capabilities(per_agent),
            {"resumable": False, "interruptible": True},
        )
        # captured ids for every agent -> resumable
        self.assertEqual(
            backends.summarize_session_capabilities(per_agent, frozenset({"a", "b"})),
            {"resumable": True, "interruptible": True},
        )
        # one agent lacks interrupt -> not interruptible
        mixed = {"a": BackendCapabilities(interrupt=True), "b": BackendCapabilities()}
        self.assertEqual(
            backends.summarize_session_capabilities(mixed),
            {"resumable": False, "interruptible": False},
        )

    def test_empty_agent_set_is_not_resumable(self):
        self.assertEqual(
            backends.summarize_session_capabilities({}),
            {"resumable": False, "interruptible": False},
        )

    def test_builtin_backend_capabilities_are_all_false(self):
        for agent_type in ("claude", "codex", "antigravity", "xai"):
            caps = backends.capabilities_for(agent_type, "cli")
            self.assertEqual(caps.to_dict(), {"resume": False, "interrupt": False, "tool_gate": False})


class _SentinelRunner(AgentRunner):
    name = "sentinel"


class _FakeBackend:
    def __init__(self, agent_type, backend_id):
        self.agent_type = agent_type
        self.id = backend_id
        self.capabilities = BackendCapabilities()
        self.brand_color = "#123456"
        self.event_fidelity = "typed"
        self.provider_session_id_kind = None
        self.checks_credentials = False
        self.block_on_unavailable = False

    def probe(self):  # pragma: no cover - not exercised here
        from agent_collab.backends.base import BackendHealth

        return BackendHealth()

    def option_schema(self, agent):
        return {}

    def normalize_options(self, agent, requested):
        return dict(requested)

    def settings_summary(self, agent, options):
        return {"backend": self.id, "options": dict(options)}

    def command_preview(self, agent, options, workdir=None):
        return None

    def create_runner(self, agent, verbose, options):
        return _SentinelRunner()


class StartBackendValidationTests(unittest.TestCase):
    def test_request_backend_unavailable_for_type_is_rejected(self):
        config = builtin_config()
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(config, "solo-claude", request_backend="nonesuch")
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "backend")
        self.assertIn("claude", detail["message"])
        self.assertIn("cli", detail["message"])  # available backends listed
        self.assertIn("sdk", detail["message"])

    def test_valid_request_backend_resolves_map_for_workflow_agents(self):
        config = builtin_config()
        backends.register(_FakeBackend("claude", "special"))
        try:
            selection = validate_start_backends(config, "solo-claude", request_backend="special")
        finally:
            backends.unregister("claude", "special")
        self.assertEqual(selection.agent_backends, {"claude": "special"})

    def test_default_resolution_uses_cli(self):
        config = builtin_config()
        selection = validate_start_backends(config, "cross-review")
        self.assertEqual(selection.agent_backends, {"claude": "cli", "codex": "cli"})

    def test_unselected_backend_options_are_rejected(self):
        config = builtin_config()
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_options(
                config, "cross-review", {"antigravity_cli": {"model": "gemini"}}
            )
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "backend_options.antigravity_cli")
        self.assertIn("does not apply", detail["message"])


class OverrideReachesExecutionTests(unittest.TestCase):
    def test_resolved_backend_map_selects_the_runner_not_agent_config(self):
        # The resolved map (from a start override) must drive execution, not a
        # re-resolution of agents.<id>.backend. Agent config says "cli"; the map
        # says "special"; the runner must come from "special".
        config = builtin_config()
        self.assertEqual(config.agents["claude"].backend, None)
        backends.register(_FakeBackend("claude", "special"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(Path(tmp) / "home")}):
                    referee = Referee(
                        RefereeConfig(
                            workflow="cross-review",
                            workdir=Path(tmp),
                            collab_config=config,
                            agent_backends={"claude": "special"},
                            color=False,
                        ),
                        printer=lambda event: None,
                    )
                    runners = referee._runners()
        finally:
            backends.unregister("claude", "special")
        self.assertIsInstance(runners["claude"], _SentinelRunner)
        # codex was not in the map -> falls back to its cli subprocess runner.
        self.assertIsInstance(runners["codex"], SubprocessRunner)


if __name__ == "__main__":
    unittest.main()
