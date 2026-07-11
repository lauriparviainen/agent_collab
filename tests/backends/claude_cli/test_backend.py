import unittest

from agent_collab.backends.claude_cli import ClaudeCliBackend
from agent_collab.backends.claude_cli.parser import ClaudeStreamingParser, parse_claude_line
from agent_collab.config import AgentConfig


class ClaudeCliBackendTests(unittest.TestCase):
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
