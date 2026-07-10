import unittest
from pathlib import Path

from agent_collab.backends.antigravity_cli import AntigravityCliBackend
from agent_collab.backends.antigravity_cli.parser import parse_antigravity_line
from agent_collab.config import AgentConfig


class AntigravityCliBackendTests(unittest.TestCase):
    def test_plain_text_does_not_invent_provider_identity(self):
        event = parse_antigravity_line("conversation_id=looks-real-but-is-prose")
        self.assertEqual(event.type, "message")
        self.assertNotIn("provider_session_id", event.raw)
        self.assertIsNone(event.provider_session)
        self.assertIsNone(AntigravityCliBackend.provider_session_id_kind)

    def test_manifest_and_workdir_mapping_are_backend_owned(self):
        backend = AntigravityCliBackend()
        agent = AgentConfig(id="ag", type="antigravity", command="agy", args=["-p"])
        options = backend.normalize_options(agent, {"mode": "plan"})
        command = backend.build_command(agent, options, Path("/tmp/work"))
        self.assertIn("plan", command)
        self.assertIn("--add-dir", command)
