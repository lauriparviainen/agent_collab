import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_collab import cli
from agent_collab import backends
from agent_collab.config import AgentConfig, ConfigError, _parse_toml_subset, load_config, validate_agent


def _write_config(root: Path, text: str) -> None:
    path = root / ".agent-collab" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_user_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def _env(home: Path):
    return {"AGENT_COLLAB_HOME": str(home)}


class ConfigTests(unittest.TestCase):
    def test_builtin_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()

            config = load_config(root, env=_env(home))

            self.assertEqual(config.agents["claude"].type, "claude")
            self.assertEqual(config.agents["claude"].command, "claude")
            self.assertEqual(config.agents["claude"].args, ["-p", "--output-format", "stream-json", "--verbose"])
            self.assertEqual(config.agents["codex"].command, "codex")
            self.assertEqual(config.agents["codex"].args, ["exec", "--json"])
            self.assertEqual(config.workflows["single-claude"].sequence, ["claude"])
            self.assertEqual(config.workflows["single-codex"].sequence, ["codex"])
            self.assertEqual(config.workflows["cross-review"].sequence, ["claude", "codex", "claude"])
            self.assertEqual(config.workflows["compare"].sequence, ["claude", "codex"])
            self.assertEqual(config.loaded_paths, [])

    def test_project_config_overrides_user_config_and_adds_custom_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
[agents.codex]
command = "user-codex"
""",
            )
            _write_config(
                root,
                """
[agents.codex]
command = "project-codex"

[agents.codex_readonly]
type = "codex"
command = "codex"
args = ["exec", "--json", "--profile", "readonly"]
enabled = true

[workflows.readonly-review]
sequence = ["codex_readonly", "claude", "codex_readonly"]
""",
            )

            config = load_config(root, env=_env(home))

            self.assertEqual(config.agents["codex"].command, "project-codex")
            self.assertEqual(config.agents["codex_readonly"].args, ["exec", "--json", "--profile", "readonly"])
            self.assertEqual(config.workflows["readonly-review"].sequence, ["codex_readonly", "claude", "codex_readonly"])

    def test_user_config_overrides_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(
                home,
                """
[agents.claude]
command = "user-claude"
""",
            )

            config = load_config(root, env=_env(home))

            self.assertEqual(config.agents["claude"].command, "user-claude")
            self.assertEqual(config.loaded_paths, [home.resolve() / "config.toml"])

    def test_shell_cwd_does_not_affect_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_a = Path(tmp) / "project-a"
            project_b = Path(tmp) / "project-b"
            home = Path(tmp) / "home"
            project_a.mkdir()
            project_b.mkdir()
            _write_config(
                project_a,
                """
[agents.codex]
command = "project-a-codex"
""",
            )
            _write_config(
                project_b,
                """
[agents.codex]
command = "project-b-codex"
""",
            )

            cwd = os.getcwd()
            os.chdir(project_a)
            try:
                config = load_config(project_b, env=_env(home))
            finally:
                os.chdir(cwd)

            self.assertEqual(config.agents["codex"].command, "project-b-codex")
            self.assertEqual(
                config.loaded_paths,
                [project_b.resolve() / ".agent-collab" / "config.toml"],
            )

    def test_cli_config_show_prints_effective_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_config(
                root,
                """
[workflows.custom]
sequence = ["claude"]
""",
            )

            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "show", "--workdir", str(root)])

            text = output.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("workflow custom: claude", text)
            self.assertIn("workflow cross-review: claude -> codex -> claude", text)
            self.assertIn(str(root.resolve() / ".agent-collab" / "config.toml"), text)

    def test_legacy_modes_section_is_rejected_with_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_config(
                root,
                """
[modes.claude-leads]
sequence = ["claude", "codex", "claude"]
""",
            )

            with self.assertRaisesRegex(ConfigError, "workflows"):
                load_config(root, env=_env(home))

    def test_toml_subset_parser_supports_config_shape(self):
        data = _parse_toml_subset(
            """
[agents.codex_readonly]
type = "codex"
command = "codex"
args = ["exec", "--json", "--profile", "readonly"]
enabled = true
env = { CODEX_HOME = "/tmp/codex" }

[workflows.readonly-review]
sequence = ["codex_readonly", "claude", "codex_readonly"]
"""
        )

        self.assertEqual(data["agents"]["codex_readonly"]["env"], {"CODEX_HOME": "/tmp/codex"})
        self.assertEqual(data["workflows"]["readonly-review"]["sequence"], ["codex_readonly", "claude", "codex_readonly"])

    def test_toml_subset_parser_supports_dotted_option_keys(self):
        data = _parse_toml_subset(
            """
[agents.codex.options]
model.allowed = ["gpt-5-codex", "gpt-5"]
search.allowed = [true, false]
"""
        )

        self.assertEqual(data["agents"]["codex"]["options"]["model"]["allowed"], ["gpt-5-codex", "gpt-5"])
        self.assertEqual(data["agents"]["codex"]["options"]["search"]["allowed"], [True, False])

    def test_agent_options_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[agents.claude.options]
model.default = "opus"
model.allowed = ["sonnet", "opus"]
thinking_level.default = "high"
thinking_level.allowed = ["low", "medium", "high", "xhigh", "max"]
""",
            )

            config = load_config(root, env=_env(home))

            self.assertEqual(config.agents["claude"].options["model"]["allowed"], ["sonnet", "opus"])
            self.assertEqual(config.agents["claude"].options["model"]["default"], "opus")
            self.assertEqual(config.agents["claude"].options["thinking_level"]["default"], "high")
            self.assertEqual(config.agents["claude"].options["thinking_level"]["allowed"], ["low", "medium", "high", "xhigh", "max"])

    def test_workflow_sequence_rejects_unknown_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[workflows.bad]
sequence = ["missing"]
""",
            )

            with self.assertRaisesRegex(ConfigError, "unknown agent"):
                load_config(root, env=_env(home))

    def test_workflow_sequence_rejects_disabled_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[agents.disabled_codex]
type = "codex"
command = "codex"
args = ["exec", "--json"]
enabled = false

[workflows.bad]
sequence = ["disabled_codex"]
""",
            )

            with self.assertRaisesRegex(ConfigError, "disabled agent"):
                load_config(root, env=_env(home))


class AgentBackendConfigTests(unittest.TestCase):
    def test_unregistered_backend_for_type_is_rejected_with_registered_ids(self):
        agent = AgentConfig(id="claude", type="claude", command="claude", backend="sdk")
        with self.assertRaises(ConfigError) as ctx:
            validate_agent(agent)
        message = str(ctx.exception)
        self.assertIn("sdk", message)
        self.assertIn("cli", message)  # registered ids for claude are listed

    def test_mock_agent_rejects_backend_field(self):
        agent = AgentConfig(id="m", type="mock", backend="cli")
        with self.assertRaisesRegex(ConfigError, "backend is not supported for type 'mock'"):
            validate_agent(agent)

    def test_command_required_for_cli_backend(self):
        agent = AgentConfig(id="claude", type="claude", backend="cli")
        with self.assertRaisesRegex(ConfigError, "command is required for backend 'cli'"):
            validate_agent(agent)

    def test_command_optional_for_non_cli_backend(self):
        # Registering a non-cli backend for claude relaxes the command requirement:
        # only the cli backend runs a subprocess and needs a command.
        fake = SimpleNamespace(agent_type="claude", id="fake")
        backends.register(fake)
        try:
            agent = AgentConfig(id="claude", type="claude", backend="fake")
            validate_agent(agent)  # must not raise despite no command
        finally:
            backends.unregister("claude", "fake")

    def test_backend_default_cli_still_requires_command(self):
        # No explicit backend -> effective cli -> command required (unchanged).
        agent = AgentConfig(id="codex", type="codex")
        with self.assertRaisesRegex(ConfigError, "command is required for backend 'cli'"):
            validate_agent(agent)

    def test_backend_field_parses_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[agents.codex]
backend = "cli"
""",
            )

            config = load_config(root, env=_env(home))

            self.assertEqual(config.agents["codex"].backend, "cli")


if __name__ == "__main__":
    unittest.main()
