import unittest

from agent_collab.backends.codex_cli import CodexCliBackend
from agent_collab.backends.codex_cli.parser import parse_codex_line
from agent_collab.config import AgentConfig


class CodexCliBackendTests(unittest.TestCase):
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
