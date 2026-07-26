from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_collab.config import CollaborationConfig, ConfigError, merge_config_data
from agent_collab.config_migrations import CURRENT_CONFIG_SCHEMA, migrate_config_data
from agent_collab.sandbox.specs import SandboxPolicy


class SandboxConfigTests(unittest.TestCase):
    def test_v10_migration_does_not_write_a_platform_default_into_user_data(self):
        migrated = migrate_config_data({"schema_version": 10}, scope="user")
        self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA)
        self.assertNotIn("system", migrated)

    def test_system_policy_operator_paths_and_limits_normalize(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            readable = root / "readable"
            writable = root / "writable"
            scratch = root / "scratch"
            readable.mkdir()
            writable.mkdir()
            scratch.mkdir()
            config = CollaborationConfig()
            merge_config_data(
                config,
                {
                    "system": {
                        "sandbox_default": "read-only",
                        "sandbox_override": "read-only",
                        "sandbox_extra_readable_dirs": [str(readable)],
                        "sandbox_extra_writable_dirs": [str(writable)],
                        "sandbox_alias_audit_max_entries": 2_000_000,
                        "sandbox_alias_audit_timeout_seconds": 20,
                        "sandbox_scratch_root": str(scratch),
                    }
                },
            )
            self.assertIs(config.system.sandbox_default, SandboxPolicy.READ_ONLY)
            self.assertIs(config.system.sandbox_override, SandboxPolicy.READ_ONLY)
            self.assertEqual(config.system.sandbox_extra_readable_dirs, [readable])
            self.assertEqual(config.system.sandbox_extra_writable_dirs, [writable])
            self.assertEqual(config.system.sandbox_scratch_root, scratch)

    def test_operator_limits_cannot_weaken_built_in_minimum(self):
        for field, value in (
            ("sandbox_alias_audit_max_entries", 999_999),
            ("sandbox_alias_audit_timeout_seconds", 9),
        ):
            with self.subTest(field=field), self.assertRaises(ConfigError):
                merge_config_data(CollaborationConfig(), {"system": {field: value}})

    def test_operator_path_parsing_preserves_symlink_evidence_for_launch_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target)
            config = CollaborationConfig()

            merge_config_data(
                config,
                {
                    "system": {
                        "sandbox_extra_writable_dirs": [str(link)],
                        "sandbox_scratch_root": str(link),
                    }
                },
            )

            self.assertEqual(config.system.sandbox_extra_writable_dirs, [link])
            self.assertEqual(config.system.sandbox_scratch_root, link)

    def test_project_system_section_is_stripped_with_sanitized_warning(self):
        warnings = []
        migrated = migrate_config_data(
            {
                "schema_version": CURRENT_CONFIG_SCHEMA,
                "system": {
                    "sandbox_default": "none",
                    "sandbox_scratch_root": "/private/operator/path",
                },
            },
            source="/project/.agent-collab/config.toml",
            scope="project",
            warnings=warnings,
        )
        self.assertNotIn("system", migrated)
        self.assertEqual(warnings[0]["code"], "ignored_project_config")
        self.assertEqual(warnings[0]["path"], "system")
        self.assertNotIn("/private/operator/path", warnings[0]["message"])
