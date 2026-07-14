"""Shipped write-posture regression tests.

The built-in config must keep every backend that supports a permission or
sandbox control on a read-only default: agents can call tools and inspect the
repository, but file writes require an explicit opt-in. A change that loosens
a shipped default should fail here, not ship silently.
"""

import tempfile
import unittest
from pathlib import Path

from agent_collab import backends as backend_registry
from agent_collab.backends.antigravity_cli import AntigravityCliBackend
from agent_collab.config import (
    AgentConfig,
    builtin_config,
    load_user_config,
    split_canonical_backend,
)


def _builtin_agent(canonical: str) -> AgentConfig:
    """Derive an agent from the built-in backend section, enabled or not."""

    section = builtin_config().backends[canonical]
    agent_type, backend_id = split_canonical_backend(canonical)
    return AgentConfig(
        id=canonical,
        type=agent_type,
        command=section.command,
        args=list(section.args),
        backend_config=dict(section.backend_config),
        options=dict(section.options),
        default_options=dict(section.default_options),
        backend=backend_id,
    )


def _normalized_defaults(canonical: str):
    agent = _builtin_agent(canonical)
    backend = backend_registry.get_backend(agent.type, agent.backend)
    return backend, agent, backend.normalize_options(agent, {})


class ShippedWritePostureTests(unittest.TestCase):
    def test_claude_cli_defaults_to_permission_mode_default(self):
        backend, agent, options = _normalized_defaults("claude_cli")
        self.assertEqual(options["permission_mode"], "default")
        command = backend.build_command(agent, options)
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")

    def test_claude_cli_accepts_plan_mode_for_strict_read_only(self):
        backend, agent, _ = _normalized_defaults("claude_cli")
        options = backend.normalize_options(agent, {"permission_mode": "plan"})
        command = backend.build_command(agent, options)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")

    def test_codex_cli_defaults_to_read_only_sandbox(self):
        backend, agent, options = _normalized_defaults("codex_cli")
        self.assertEqual(options["sandbox"], "read-only")
        command = backend.build_command(agent, options)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_antigravity_cli_defaults_to_plan_mode(self):
        backend, agent, options = _normalized_defaults("antigravity_cli")
        self.assertEqual(options["mode"], "plan")
        self.assertNotIn("sandbox", options)  # terminal sandbox is opt-in
        command = backend.build_command(agent, options, Path("/tmp/work"))
        self.assertEqual(command[command.index("--mode") + 1], "plan")

    def test_xai_cli_defaults_to_read_only_sandbox(self):
        backend, agent, options = _normalized_defaults("xai_cli")
        self.assertEqual(options["sandbox"], "read-only")
        self.assertEqual(options["permission_mode"], "bypassPermissions")

    def test_sdk_backends_default_to_read_only_posture(self):
        _, _, claude = _normalized_defaults("claude_sdk")
        self.assertEqual(claude["permission_mode"], "default")
        _, _, codex = _normalized_defaults("codex_sdk")
        self.assertEqual(codex["sandbox"], "read-only")

    def test_args_flag_still_overrides_builtin_default(self):
        # Built-in option defaults rank below flags configured in args, so a
        # deliberate args-level opt-in keeps working.
        agent = _builtin_agent("codex_cli")
        agent.args = ["exec", "--json", "--sandbox", "workspace-write"]
        backend = backend_registry.get_backend(agent.type, agent.backend)
        options = backend.normalize_options(agent, {})
        self.assertEqual(options["sandbox"], "workspace-write")

    def test_user_options_table_does_not_drop_builtin_posture(self):
        # A user config that sets an unrelated option must not silently lose
        # the shipped read-only default.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text(
                """
schema_version = 8

[backends.codex_cli.options]
model = "gpt-5.6-sol"
""",
                encoding="utf-8",
            )
            config = load_user_config(env={"AGENT_COLLAB_HOME": str(home)})
        agent = config.agents["codex_cli"]
        backend = backend_registry.get_backend(agent.type, agent.backend)
        options = backend.normalize_options(agent, {})
        self.assertEqual(options["sandbox"], "read-only")
        # ...while an explicit user override still wins.
        agent.options["sandbox"] = "workspace-write"
        self.assertEqual(backend.normalize_options(agent, {})["sandbox"], "workspace-write")


class AntigravitySandboxOptionTests(unittest.TestCase):
    def setUp(self):
        self.backend = AntigravityCliBackend()

    def agent(self, args=None):
        return AgentConfig(id="ag", type="antigravity", command="agy", args=list(args or ["-p"]))

    def test_sandbox_true_inserts_flag_before_prompt(self):
        agent = self.agent()
        options = self.backend.normalize_options(agent, {"sandbox": True})
        command = self.backend.build_command(agent, options, Path("/tmp/work"))
        self.assertLess(command.index("--sandbox"), command.index("-p"))

    def test_sandbox_is_inferred_from_args_and_removable_per_session(self):
        agent = self.agent(["--sandbox", "-p"])
        self.assertTrue(self.backend.normalize_options(agent, {})["sandbox"])
        options = self.backend.normalize_options(agent, {"sandbox": False})
        command = self.backend.build_command(agent, options, Path("/tmp/work"))
        self.assertNotIn("--sandbox", command)


if __name__ == "__main__":
    unittest.main()
