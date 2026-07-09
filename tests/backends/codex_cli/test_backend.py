import unittest

from agent_collab.backends.codex_cli import CodexCliBackend
from agent_collab.config import AgentConfig


class CodexCliBackendTests(unittest.TestCase):
    def test_manifest_and_command_are_backend_owned(self):
        backend = CodexCliBackend()
        agent = AgentConfig(id="codex", type="codex", command="codex", args=["exec", "--json"])
        self.assertIn("approval_policy", backend.option_schema(agent))
        options = backend.normalize_options(agent, {"thinking_level": "xhigh"})
        self.assertIn('model_reasoning_effort="xhigh"', backend.build_command(agent, options))
