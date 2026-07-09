import unittest
from pathlib import Path

from agent_collab.backends.antigravity_cli import AntigravityCliBackend
from agent_collab.config import AgentConfig


class AntigravityCliBackendTests(unittest.TestCase):
    def test_manifest_and_workdir_mapping_are_backend_owned(self):
        backend = AntigravityCliBackend()
        agent = AgentConfig(id="ag", type="antigravity", command="agy", args=["-p"])
        options = backend.normalize_options(agent, {"mode": "plan"})
        command = backend.build_command(agent, options, Path("/tmp/work"))
        self.assertIn("plan", command)
        self.assertIn("--add-dir", command)
