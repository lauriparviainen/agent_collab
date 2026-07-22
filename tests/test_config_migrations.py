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
        self.assertNotIn("agents", migrated)
        self.assertEqual(migrated["backends"]["claude_cli"]["command"], "claude")
        self.assertTrue(migrated["backends"]["claude_cli"]["enabled"])

    def test_input_is_not_mutated(self):
        data = {"agents": {"claude": {"type": "claude", "args": ["-p"]}}}
        snapshot = copy.deepcopy(data)

        migrated = migrate_config_data(data)
        migrated["backends"]["claude_cli"]["args"].append("--changed")

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

        self.assertEqual(config.agents["claude_cli"].command, "claude")

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

    def test_v6_config_migrates_and_rewrites_builtin_workflow_references(self):
        data = {
            "schema_version": 6,
            "workflows": {"review": {"parallel": ["claude", "codex"]}},
        }

        migrated = migrate_config_data(data, source="user.toml", scope="user")

        self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA)
        self.assertEqual(migrated["workflows"]["review"]["parallel"], ["claude_cli", "codex_cli"])

    def test_v7_agents_fold_into_backend_sections(self):
        data = {
            "schema_version": 7,
            "agents": {
                "claude": {"type": "claude", "enabled": False},
                "claude_sdk": {
                    "type": "claude",
                    "backend": "sdk",
                    "enabled": True,
                    "options": {"model": "opus"},
                },
                "helper": {"type": "mock"},
            },
            "workflows": {
                "solo": {"sequence": ["claude_sdk"]},
                "pair": {"parallel": ["claude", "claude_sdk"]},
            },
        }

        migrated = migrate_config_data(data, source="user.toml", scope="user")

        self.assertNotIn("agents", migrated)
        self.assertFalse(migrated["backends"]["claude_cli"]["enabled"])
        sdk = migrated["backends"]["claude_sdk"]
        self.assertTrue(sdk["enabled"])
        self.assertEqual(sdk["options"], {"model": "opus"})
        self.assertTrue(migrated["backends"]["mock"]["enabled"])
        self.assertEqual(migrated["workflows"]["solo"]["sequence"], ["claude_sdk"])
        self.assertEqual(migrated["workflows"]["pair"]["parallel"], ["claude_cli", "claude_sdk"])

    def test_v7_second_options_only_agent_becomes_nested_persona(self):
        data = {
            "schema_version": 7,
            "agents": {
                "antigravity_cli": {
                    "type": "antigravity",
                    "backend": "cli",
                    "command": "agy",
                    "options": {"model": "flash"},
                },
                "gemini_pro": {
                    "type": "antigravity",
                    "backend": "cli",
                    "options": {"model": "pro"},
                },
            },
            "workflows": {"pro-review": {"sequence": ["gemini_pro"]}},
        }

        migrated = migrate_config_data(data, source="user.toml", scope="user")

        section = migrated["backends"]["antigravity_cli"]
        self.assertEqual(section["command"], "agy")
        self.assertEqual(section["agents"]["gemini_pro"], {"options": {"model": "pro"}})
        self.assertEqual(
            migrated["workflows"]["pro-review"]["sequence"],
            ["antigravity_cli.gemini_pro"],
        )

    def test_v7_conflicting_same_backend_agents_fail_migration(self):
        data = {
            "schema_version": 7,
            "agents": {
                "codex": {"type": "codex", "command": "codex"},
                "other": {"type": "codex", "command": "different-codex"},
            },
        }

        with self.assertRaisesRegex(ConfigMigrationError, "agents.other"):
            migrate_config_data(data, source="user.toml", scope="user")

    def test_v7_builtin_agent_omitting_command_conflicts_with_custom_default(self):
        # Review finding: [agents.claude] omits command (it ran with the
        # built-in "claude"); folding it as a persona of a custom-command
        # default would silently change what it executes. Must fail clearly.
        data = {
            "schema_version": 7,
            "agents": {
                "hardened": {"type": "claude", "command": "/usr/local/bin/secure-claude"},
                "claude": {"options": {"model": "opus"}},
            },
        }

        with self.assertRaisesRegex(ConfigMigrationError, "agents.claude"):
            migrate_config_data(data, source="user.toml", scope="user")

    def test_v7_enabled_agent_wins_the_default_slot_regardless_of_file_order(self):
        # Review finding: a disabled agent listed first must not claim the
        # backend section and silently disable the later enabled agent.
        data = {
            "schema_version": 7,
            "agents": {
                "retired": {"type": "codex", "command": "codex", "enabled": False},
                "active": {"type": "codex", "command": "codex", "enabled": True},
            },
        }

        with self.assertLogs("agent_collab.config", level="WARNING"):
            migrated = migrate_config_data(data, source="user.toml", scope="user")

        section = migrated["backends"]["codex_cli"]
        self.assertTrue(section["enabled"])
        self.assertNotIn("agents", section)

    def test_v7_canonical_id_wins_the_default_slot_over_file_order(self):
        data = {
            "schema_version": 7,
            "agents": {
                "pro": {"type": "antigravity", "backend": "cli", "options": {"model": "pro"}},
                "antigravity_cli": {
                    "type": "antigravity",
                    "backend": "cli",
                    "command": "agy",
                },
            },
        }

        migrated = migrate_config_data(data, source="user.toml", scope="user")

        section = migrated["backends"]["antigravity_cli"]
        self.assertEqual(section["command"], "agy")
        self.assertEqual(section["agents"]["pro"], {"options": {"model": "pro"}})

    def test_v7_disabled_duplicate_agent_is_dropped_with_warning(self):
        data = {
            "schema_version": 7,
            "agents": {
                "codex": {"type": "codex", "command": "codex"},
                "old": {"type": "codex", "command": "different", "enabled": False},
            },
        }

        with self.assertLogs("agent_collab.config", level="WARNING") as logs:
            migrated = migrate_config_data(data, source="user.toml", scope="user")

        self.assertIn("dropping disabled agent", "\n".join(logs.output))
        self.assertNotIn("agents", migrated["backends"]["codex_cli"])

    def test_v7_permissive_policy_only_backend_section_is_dropped(self):
        data = {
            "schema_version": 7,
            "agents": {},
            "backends": {
                "claude_sdk": {"enabled": True},
                "xai_cli": {"enabled": False},
            },
        }

        migrated = migrate_config_data(data, source="user.toml", scope="user")

        self.assertNotIn("claude_sdk", migrated["backends"])
        self.assertEqual(migrated["backends"]["xai_cli"], {"enabled": False})

    def test_v7_name_only_agent_survives_as_display_override(self):
        data = {
            "schema_version": 7,
            "agents": {"claude": {"name": "Primary"}},
        }

        migrated = migrate_config_data(data, source="user.toml", scope="user")

        self.assertEqual(migrated["agents"], {"claude_cli": {"name": "Primary"}})

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

    def test_project_parallel_workflow_is_dropped_with_sanitized_warning(self):
        warnings = []

        migrated = migrate_config_data(
            {"workflows": {"review": {"parallel": ["claude", "codex"]}}},
            source="project.toml",
            scope="project",
            enabled_global_agent_ids={"claude", "codex"},
            warnings=warnings,
        )

        self.assertNotIn("workflows", migrated)
        self.assertEqual(warnings[0]["path"], "workflows.review")
        self.assertEqual(warnings[0]["message"], "ignoring malformed project workflow 'review'")


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

            self.assertEqual(config.agents["claude_cli"].command, "claude")
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

            self.assertEqual(config.agents["claude_cli"].command, "claude")
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

    def test_v6_file_is_rewritten_backend_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "# keep this comment\n"
                "schema_version = 6\n\n"
                "[workflows.review]\n"
                'parallel = ["claude", "codex"]\n'
            )
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.previous_version, 6)
            self.assertEqual(result.backup_path.read_text(encoding="utf-8"), text)
            migrated = path.read_text(encoding="utf-8")
            self.assertIn("# keep this comment", migrated)
            self.assertIn(f"schema_version = {CURRENT_CONFIG_SCHEMA}", migrated)
            self.assertIn('"claude_cli"', migrated)
            self.assertIn('"codex_cli"', migrated)
            self.assertNotIn("schema_version = 6", migrated)

    def test_v7_file_write_back_folds_agents_and_preserves_daemon_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "schema_version = 7\n\n"
                "[agents.claude]\n"
                'command = "user-claude"\n\n'
                "[agents.gemini_pro]\n"
                'type = "antigravity"\n'
                'backend = "cli"\n\n'
                "[agents.gemini_pro.options]\n"
                'model = "pro"\n\n'
                "[workflows.pro]\n"
                'sequence = ["gemini_pro"]\n\n'
                "# the daemon token comment survives\n"
                "[daemon]\n"
                'token = "test-token-value"\n'
            )
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            migrated = path.read_text(encoding="utf-8")
            self.assertNotIn("[agents.claude]", migrated)
            self.assertIn("[backends.claude_cli]", migrated)
            self.assertIn('command = "user-claude"', migrated)
            # The only agent on a backend becomes its default: the section
            # absorbs the options and the workflow references the backend.
            self.assertIn("[backends.antigravity_cli.options]", migrated)
            self.assertIn('model = "pro"', migrated)
            self.assertIn('sequence = ["antigravity_cli"]', migrated)
            self.assertIn('token = "test-token-value"', migrated)

    def test_v7_file_with_unmigratable_agents_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "schema_version = 7\n\n"
                "[agents.codex]\n"
                'command = "codex"\n\n'
                "[agents.other]\n"
                'type = "codex"\n'
                'command = "different-codex"\n'
            )
            path = self._write(Path(tmp), text)

            with self.assertRaisesRegex(ConfigMigrationError, "agents.other"):
                migrate_user_config_file(path)

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
            # The display-name-only agent survives, remapped to the derived id.
            self.assertIn("[agents.claude_cli]", migrated)

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

    def test_backend_first_rewrite_requires_tomlkit(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "schema_version = 4  # note\n\n[agents.claude]\nname = 'C'\n"
            path = self._write(Path(tmp), text)

            with mock.patch.dict(sys.modules, {"tomlkit": None}):
                with self.assertRaisesRegex(ConfigMigrationError, "tomlkit"):
                    migrate_user_config_file(path)

            # Nothing was written; install reports the error and stops.
            self.assertEqual(path.read_text(encoding="utf-8"), text)

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


class V10AntigravityModelRenameTests(unittest.TestCase):
    """v10 retires the Antigravity display-name model namespace: known display
    names are renamed to canonical catalog ids in backend options, personae,
    and antigravity usage-window targets; everything else passes through."""

    def _write(self, directory: Path, text: str) -> Path:
        path = directory / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_v9_display_names_are_renamed_across_all_model_sites(self):
        data = {
            "schema_version": 9,
            "backends": {
                "antigravity_cli": {
                    "options": {"model": "Gemini 3.5 Flash (High)"},
                    "agents": {"pro": {"options": {"model": "Gemini 3.1 Pro (High)"}}},
                },
                "antigravity_sdk": {"options": {"model": "Claude Sonnet 4.6 (Thinking)"}},
                # Other backends are never rewritten, even for a lookalike value.
                "claude_cli": {"options": {"model": "Gemini 3.5 Flash (High)"}},
            },
            "usage_windows": {
                "targets": {
                    "agy_low": {"backend": "antigravity_cli", "model": "Gemini 3.5 Flash (Low)"},
                    "claude": {"backend": "claude_cli", "model": "sonnet"},
                }
            },
        }

        migrated = migrate_config_data(data, "test")

        self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA)
        backends = migrated["backends"]
        self.assertEqual(backends["antigravity_cli"]["options"]["model"], "gemini-3.5-flash-high")
        self.assertEqual(
            backends["antigravity_cli"]["agents"]["pro"]["options"]["model"],
            "gemini-3.1-pro-high",
        )
        self.assertEqual(backends["antigravity_sdk"]["options"]["model"], "claude-sonnet-4-6")
        self.assertEqual(backends["claude_cli"]["options"]["model"], "Gemini 3.5 Flash (High)")
        targets = migrated["usage_windows"]["targets"]
        self.assertEqual(targets["agy_low"]["model"], "gemini-3.5-flash-low")
        self.assertEqual(targets["claude"]["model"], "sonnet")

    def test_unknown_model_values_pass_through_unchanged(self):
        data = {
            "schema_version": 9,
            "backends": {
                "antigravity_cli": {"options": {"model": "gemini-99-experimental"}},
                "antigravity_sdk": {"options": {"model": "Gemini 99 Ultra (Max)"}},
            },
        }

        migrated = migrate_config_data(data, "test")

        # A value outside the known display-name table is the user's own
        # choice; the migration never guesses.
        self.assertEqual(
            migrated["backends"]["antigravity_cli"]["options"]["model"],
            "gemini-99-experimental",
        )
        self.assertEqual(
            migrated["backends"]["antigravity_sdk"]["options"]["model"],
            "Gemini 99 Ultra (Max)",
        )

    @unittest.skipUnless(_tomlkit_available(), "tomlkit is not installed")
    def test_v9_write_back_renames_models_preserving_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "# my agent-collab config\n"
                "schema_version = 9\n\n"
                "[backends.antigravity_cli]\n"
                'command = "agy"\n\n'
                "# the model I picked\n"
                "[backends.antigravity_cli.options]\n"
                'model = "Gemini 3.5 Flash (High)"\n\n'
                "[usage_windows.targets.agy_low]\n"
                'backend = "antigravity_cli"\n'
                'model = "Gemini 3.5 Flash (Low)"\n\n'
                "[daemon]\n"
                'token = "test-token-value"\n'
            )
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.previous_version, 9)
            self.assertEqual(result.backup_path.read_text(encoding="utf-8"), text)
            migrated = path.read_text(encoding="utf-8")
            self.assertIn(f"schema_version = {CURRENT_CONFIG_SCHEMA}", migrated)
            self.assertIn('model = "gemini-3.5-flash-high"', migrated)
            self.assertIn('model = "gemini-3.5-flash-low"', migrated)
            self.assertNotIn("Gemini 3.5 Flash", migrated)
            self.assertIn("# my agent-collab config", migrated)
            self.assertIn("# the model I picked", migrated)
            self.assertIn('token = "test-token-value"', migrated)

    def test_v9_write_back_without_antigravity_models_only_stamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "schema_version = 9\n\n[backends.claude_cli.options]\nmodel = 'opus'\n"
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            migrated = path.read_text(encoding="utf-8")
            self.assertIn(f"schema_version = {CURRENT_CONFIG_SCHEMA}", migrated)
            self.assertIn("model = 'opus'", migrated)

    @unittest.skipUnless(_tomlkit_available(), "tomlkit is not installed")
    def test_pre_v8_write_back_renames_models_in_kept_sections_too(self):
        # Round-5 review finding: the pre-v8 structural rewrite keeps sections
        # like [usage_windows] as original text; the rename pass must still
        # reach them, or a display-name model would be frozen on disk under
        # the freshly stamped current version and never migrated again.
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "schema_version = 7\n\n"
                "[agents.antigravity]\n"
                'type = "antigravity"\n'
                'backend = "cli"\n\n'
                "[agents.antigravity.options]\n"
                'model = "Gemini 3.5 Flash (High)"\n\n'
                "[usage_windows.targets.agy_low]\n"
                'backend = "antigravity_cli"\n'
                'model = "Gemini 3.5 Flash (Low)"\n'
            )
            path = self._write(Path(tmp), text)

            result = migrate_user_config_file(path)

            self.assertEqual(result.status, "migrated")
            migrated = path.read_text(encoding="utf-8")
            self.assertIn(f"schema_version = {CURRENT_CONFIG_SCHEMA}", migrated)
            self.assertIn('model = "gemini-3.5-flash-high"', migrated)
            self.assertIn('model = "gemini-3.5-flash-low"', migrated)
            self.assertNotIn("Gemini 3.5 Flash", migrated)

    def test_v9_write_back_with_renames_requires_tomlkit(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (
                "schema_version = 9\n\n"
                "[backends.antigravity_cli.options]\n"
                'model = "Gemini 3.5 Flash (High)"\n'
            )
            path = self._write(Path(tmp), text)

            with mock.patch.dict(sys.modules, {"tomlkit": None}):
                with self.assertRaisesRegex(ConfigMigrationError, "tomlkit"):
                    migrate_user_config_file(path)


if __name__ == "__main__":
    unittest.main()
