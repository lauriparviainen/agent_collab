import unittest
from types import SimpleNamespace

from agent_collab import backends
from agent_collab.backends.base import BackendCapabilities
from agent_collab.config import AgentConfig


class RegistryResolutionTests(unittest.TestCase):
    def test_builtin_cli_pairs_are_registered(self):
        self.assertTrue(backends.is_registered("claude", "cli"))
        self.assertTrue(backends.is_registered("codex", "cli"))

    def test_claude_sdk_is_not_registered_this_stage(self):
        self.assertFalse(backends.is_registered("claude", "sdk"))
        self.assertFalse(backends.is_registered("codex", "sdk"))

    def test_registered_backends_lists_ids_for_type(self):
        self.assertEqual(backends.registered_backends("claude"), ["cli"])
        self.assertEqual(backends.registered_backends("codex"), ["cli"])
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
            backends.get_backend("claude", "sdk")
        message = str(ctx.exception)
        self.assertIn("sdk", message)
        self.assertIn("cli", message)  # registered ids for the type are listed

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
        for agent_type in ("claude", "codex"):
            caps = backends.capabilities_for(agent_type, "cli")
            self.assertEqual(caps.to_dict(), {"resume": False, "interrupt": False, "tool_gate": False})


if __name__ == "__main__":
    unittest.main()
