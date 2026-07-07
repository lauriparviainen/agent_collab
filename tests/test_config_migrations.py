import copy
import tempfile
import unittest
from pathlib import Path

from agent_collab.config import ConfigError, load_config, merge_config_data, CollaborationConfig, validate_config
from agent_collab.config_migrations import (
    CURRENT_CONFIG_SCHEMA,
    ConfigMigrationError,
    migrate_config_data,
)


class MigrateConfigDataTests(unittest.TestCase):
    def test_missing_schema_version_is_treated_as_v1_and_migrated(self):
        data = {"agents": {"claude": {"type": "claude", "command": "claude"}}}

        migrated = migrate_config_data(data)

        self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA)
        self.assertEqual(migrated["agents"], data["agents"])

    def test_input_is_not_mutated(self):
        data = {"agents": {"claude": {"type": "claude", "args": ["-p"]}}}
        snapshot = copy.deepcopy(data)

        migrated = migrate_config_data(data)
        migrated["agents"]["claude"]["args"].append("--changed")

        self.assertEqual(data, snapshot)

    def test_current_schema_passes_through(self):
        data = {"schema_version": CURRENT_CONFIG_SCHEMA, "agents": {}}

        migrated = migrate_config_data(data)

        self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA)

    def test_rejects_bad_versions(self):
        for version in (0, -1, CURRENT_CONFIG_SCHEMA + 1, "2", True):
            with self.assertRaises(ConfigMigrationError, msg=repr(version)):
                migrate_config_data({"schema_version": version}, source="test.toml")

    def test_error_includes_source(self):
        with self.assertRaisesRegex(ConfigMigrationError, "bad.toml"):
            migrate_config_data({"schema_version": 99}, source="bad.toml")

    def test_migrated_config_validates_with_latest_validator(self):
        data = {
            "agents": {"claude": {"type": "claude", "command": "claude"}},
        }

        config = CollaborationConfig()
        merge_config_data(config, migrate_config_data(data))
        validate_config(config)

        self.assertEqual(config.agents["claude"].command, "claude")

    def test_unknown_agent_field_still_fails_after_migration(self):
        data = {"agents": {"claude": {"type": "claude", "bogus_field": 1}}}

        config = CollaborationConfig()
        with self.assertRaisesRegex(ConfigError, "bogus_field"):
            merge_config_data(config, migrate_config_data(data))

    def test_unknown_top_level_section_still_fails_after_migration(self):
        config = CollaborationConfig()
        with self.assertRaisesRegex(ConfigError, "unknown config section"):
            merge_config_data(config, migrate_config_data({"surprises": {}}))


class LoadConfigMigrationTests(unittest.TestCase):
    def test_load_config_migrates_versionless_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[agents.claude]
command = "project-claude"
""",
                encoding="utf-8",
            )

            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})

            self.assertEqual(config.agents["claude"].command, "project-claude")

    def test_load_config_accepts_declared_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
schema_version = 2

[agents.claude]
command = "project-claude"
""",
                encoding="utf-8",
            )

            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})

            self.assertEqual(config.agents["claude"].command, "project-claude")

    def test_load_config_rejects_future_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("schema_version = 99\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigMigrationError, "config.toml"):
                load_config(root, env={"AGENT_COLLAB_HOME": str(home)})


if __name__ == "__main__":
    unittest.main()
