import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.config import (
    ConfigError,
    load_config,
    merge_config_data,
    CollaborationConfig,
    validate_config,
)
from agent_collab.config_migrations import (
    CURRENT_CONFIG_SCHEMA,
    ConfigMigrationError,
    migrate_config_data,
    migrate_user_config_file,
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
            validate_config(config)

    def test_unknown_top_level_section_still_fails_after_migration(self):
        config = CollaborationConfig()
        with self.assertRaisesRegex(ConfigError, "unknown config section"):
            merge_config_data(config, migrate_config_data({"surprises": {}}))

    def test_project_backend_policy_is_stripped_with_warning(self):
        with self.assertLogs("agent_collab.config", level="WARNING") as logs:
            migrated = migrate_config_data(
                {"backends": {"claude_cli": {"enabled": False}}},
                source="project.toml",
                scope="project",
            )
        self.assertNotIn("backends", migrated)
        self.assertIn("user config", "\n".join(logs.output))

    def test_v4_config_is_stamped_to_current_schema(self):
        migrated = migrate_config_data({"schema_version": 4, "agents": {}})

        self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA)

    def test_project_sessions_section_is_stripped_with_warning(self):
        with self.assertLogs("agent_collab.config", level="WARNING") as logs:
            migrated = migrate_config_data(
                {"sessions": {"retention_days": 1}},
                source="project.toml",
                scope="project",
            )
        self.assertNotIn("sessions", migrated)
        self.assertIn("user config", "\n".join(logs.output))

    def test_user_sessions_section_is_preserved(self):
        migrated = migrate_config_data(
            {"sessions": {"retention_days": 7}}, source="user.toml", scope="user"
        )

        self.assertEqual(migrated["sessions"], {"retention_days": 7})

    def test_malformed_project_workflows_are_dropped_with_warnings(self):
        cases = (
            {"sequence": "claude"},
            {"sequence": []},
            {"sequence": ["claude"], "unexpected": True},
            "not-a-table",
        )
        for values in cases:
            warnings = []
            with self.subTest(values=values):
                migrated = migrate_config_data(
                    {"workflows": {"review": values}},
                    scope="project",
                    enabled_global_agent_ids={"claude"},
                    warnings=warnings,
                )

                self.assertNotIn("workflows", migrated)
                self.assertEqual(warnings[0]["path"], "workflows.review")
                self.assertIn("malformed", warnings[0]["message"])


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

            self.assertEqual(config.agents["claude"].command, "claude")
            self.assertTrue(config.warnings)

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

            self.assertEqual(config.agents["claude"].command, "claude")
            self.assertTrue(config.warnings)

    def test_load_config_rejects_future_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("schema_version = 99\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigMigrationError, "config.toml"):
                load_config(root, env={"AGENT_COLLAB_HOME": str(home)})


def _tomlkit_available() -> bool:
    try:
        import tomlkit  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _tomllib_available() -> bool:
    try:
        import tomllib  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


class MigrateUserConfigFileTests(unittest.TestCase):
    def _write(self, directory: Path, text: str) -> Path:
        path = directory / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_absent_file_reports_absent_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "absent")
            self.assertFalse(path.exists())

    def test_current_file_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = f"schema_version = {CURRENT_CONFIG_SCHEMA}\n\n[agents.claude]\nname = 'C'\n"
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "current")
            self.assertIsNone(result.backup_path)
            self.assertEqual(path.read_text(encoding="utf-8"), text)
            self.assertFalse(path.with_name("config.toml.bak").exists())

    def test_missing_schema_version_is_stamped_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "# my config\n[agents.claude]\nname = 'C'\n"
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.previous_version, 1)
            self.assertEqual(result.backup_path.read_text(encoding="utf-8"), text)
            migrated = path.read_text(encoding="utf-8")
            self.assertTrue(migrated.startswith(f"schema_version = {CURRENT_CONFIG_SCHEMA}\n"))
            self.assertIn("# my config", migrated)
            self.assertIn("[agents.claude]", migrated)

    @unittest.skipUnless(_tomlkit_available(), "tomlkit is not installed")
    def test_old_schema_version_is_updated_preserving_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "# keep this comment\n"
                "schema_version = 4  # trailing note\n"
                "\n"
                "[agents.claude]\n"
                'name = "Claude"\n'
            )
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.previous_version, 4)
            self.assertEqual(result.backup_path.read_text(encoding="utf-8"), text)
            migrated = path.read_text(encoding="utf-8")
            self.assertIn(f"schema_version = {CURRENT_CONFIG_SCHEMA}", migrated)
            self.assertIn("# keep this comment", migrated)
            self.assertIn("# trailing note", migrated)
            self.assertIn('name = "Claude"', migrated)
            self.assertNotIn("schema_version = 4", migrated)

    def test_permissive_current_config_is_tightened_to_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = f"schema_version = {CURRENT_CONFIG_SCHEMA}\n"
            path = self._write(Path(tmp), text)
            path.chmod(0o644)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "current")
            self.assertTrue(result.permissions_fixed)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_symlinked_config_is_migrated_through_to_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "dotfiles" / "config.toml"
            target.parent.mkdir()
            target.write_text("# linked\n[agents.claude]\nname = 'C'\n", encoding="utf-8")
            link = root / "config.toml"
            link.symlink_to(target)

            result = migrate_user_config_file(link)

            self.assertEqual(result.status, "migrated")
            self.assertTrue(link.is_symlink())
            migrated = target.read_text(encoding="utf-8")
            self.assertTrue(migrated.startswith(f"schema_version = {CURRENT_CONFIG_SCHEMA}\n"))
            self.assertEqual(result.backup_path.parent, target.parent)

    def test_version_stamp_works_without_tomlkit(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "schema_version = 4  # note\n\n[agents.claude]\nname = 'C'\n"
            path = self._write(Path(tmp), text)

            with mock.patch.dict(sys.modules, {"tomlkit": None}):
                result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            migrated = path.read_text(encoding="utf-8")
            self.assertIn(f"schema_version = {CURRENT_CONFIG_SCHEMA}  # note", migrated)
            self.assertIn("[agents.claude]", migrated)

    @unittest.skipUnless(_tomllib_available(), "multi-line TOML needs stdlib tomllib")
    def test_fallback_stamp_refuses_lookalike_inside_multiline_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = 'banner = """\nschema_version = 4\n"""\nschema_version = 4\n'
            path = self._write(Path(tmp), text)

            with mock.patch.dict(sys.modules, {"tomlkit": None}):
                with self.assertRaisesRegex(ConfigMigrationError, "tomlkit"):
                    migrate_user_config_file(path)

            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_newer_schema_version_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = f"schema_version = {CURRENT_CONFIG_SCHEMA + 1}\n"
            path = self._write(Path(tmp), text)

            with self.assertRaisesRegex(ConfigMigrationError, "newer than supported"):
                migrate_user_config_file(path)

            self.assertEqual(path.read_text(encoding="utf-8"), text)
            self.assertFalse(path.with_name("config.toml.bak").exists())


if __name__ == "__main__":
    unittest.main()
