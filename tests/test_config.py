import tempfile
import unittest
from pathlib import Path

from agent_collab.config import ConfigError, _parse_toml_subset, load_config


def _write_config(root: Path, text: str) -> None:
    path = root / ".agent-collab" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ConfigTests(unittest.TestCase):
    def test_builtin_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()

            config = load_config(root, home=home)

            self.assertEqual(config.agents["claude"].type, "claude")
            self.assertEqual(config.agents["claude"].command, "claude")
            self.assertEqual(config.agents["claude"].args, ["-p", "--output-format", "stream-json", "--verbose"])
            self.assertEqual(config.agents["codex"].command, "codex")
            self.assertEqual(config.agents["codex"].args, ["exec", "--json"])
            self.assertEqual(config.modes["claude-leads"].sequence, ["claude", "codex", "claude"])
            self.assertEqual(config.modes["codex-leads"].sequence, ["codex", "claude", "codex"])
            self.assertEqual(config.modes["debate"].sequence, ["claude", "codex", "claude", "codex"])
            self.assertEqual(config.loaded_paths, [])

    def test_project_config_overrides_user_config_and_adds_custom_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
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

[modes.readonly-review]
sequence = ["codex_readonly", "claude", "codex_readonly"]
""",
            )

            config = load_config(root, home=home)

            self.assertEqual(config.agents["codex"].command, "project-codex")
            self.assertEqual(config.agents["codex_readonly"].args, ["exec", "--json", "--profile", "readonly"])
            self.assertEqual(config.modes["readonly-review"].sequence, ["codex_readonly", "claude", "codex_readonly"])

    def test_toml_subset_parser_supports_config_shape(self):
        data = _parse_toml_subset(
            """
[agents.codex_readonly]
type = "codex"
command = "codex"
args = ["exec", "--json", "--profile", "readonly"]
enabled = true
env = { CODEX_HOME = "/tmp/codex" }

[modes.readonly-review]
sequence = ["codex_readonly", "claude", "codex_readonly"]
"""
        )

        self.assertEqual(data["agents"]["codex_readonly"]["env"], {"CODEX_HOME": "/tmp/codex"})
        self.assertEqual(data["modes"]["readonly-review"]["sequence"], ["codex_readonly", "claude", "codex_readonly"])

    def test_mode_sequence_rejects_unknown_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[modes.bad]
sequence = ["missing"]
""",
            )

            with self.assertRaisesRegex(ConfigError, "unknown agent"):
                load_config(root, home=home)

    def test_mode_sequence_rejects_disabled_agent(self):
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

[modes.bad]
sequence = ["disabled_codex"]
""",
            )

            with self.assertRaisesRegex(ConfigError, "disabled agent"):
                load_config(root, home=home)


if __name__ == "__main__":
    unittest.main()
