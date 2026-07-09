import unittest

from agent_collab.backends.claude_cli import ClaudeCliBackend
from agent_collab.config import AgentConfig


class ClaudeCliBackendTests(unittest.TestCase):
    def test_manifest_and_command_are_backend_owned(self):
        backend = ClaudeCliBackend()
        agent = AgentConfig(id="claude", type="claude", command="claude", args=["-p"])
        schema = backend.option_schema(agent)
        self.assertTrue(schema["model"].inferred)
        options = backend.normalize_options(agent, {"model": "sonnet"})
        self.assertIn("sonnet", backend.build_command(agent, options))
