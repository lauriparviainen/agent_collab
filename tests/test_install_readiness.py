import io
from pathlib import Path
import unittest
from unittest import mock

from agent_collab.backends.base import (
    CREDENTIALS_UNKNOWN,
    HEALTH_OK,
    HEALTH_UNAVAILABLE,
    BackendHealth,
)
from agent_collab.cli_output import format_table
from agent_collab.config import AgentConfig, builtin_config, merge_config_data
from agent_collab.install_readiness import collect_install_readiness
from agent_collab.user_install import _print_backend_readiness


def _cli_health(command, *, present=True, version="1.2.3"):
    status = "present" if present else "missing"
    return BackendHealth(
        status=HEALTH_OK if present else HEALTH_UNAVAILABLE,
        reason=None if present else f"{command}: command not found on PATH",
        credentials=CREDENTIALS_UNKNOWN,
        version=version if present else None,
        checks={
            "dependency": {"status": status, "kind": "path", "command": command},
            "credentials": {"status": "not_checked"},
        },
        remediation=(
            ()
            if present
            else (
                {
                    "code": "install_cli",
                    "message": f"Install {command} and ensure it is available on PATH.",
                },
            )
        ),
    )


def _sdk_health(module, version="4.5.6"):
    return BackendHealth(
        status=HEALTH_OK,
        credentials=CREDENTIALS_UNKNOWN,
        version=version,
        checks={
            "dependency": {"status": "present", "kind": "python_module", "module": module},
            "credentials": {"status": "unknown"},
        },
    )


class InstallReadinessCollectionTests(unittest.TestCase):
    def test_cli_probe_uses_the_agent_configured_command(self):
        from agent_collab import backends as backend_registry

        config = builtin_config()
        merge_config_data(
            config,
            {
                "backends": {
                    "claude_cli": {"command": "custom-claude"},
                    "codex_cli": {"enabled": False},
                }
            },
        )
        backend = backend_registry.get_backend("claude", "cli")

        with mock.patch.object(
            backend, "probe_for_agent", return_value=_cli_health("custom-claude")
        ) as probe:
            payload = collect_install_readiness(config)

        probe.assert_called_once_with(config.agents["claude_cli"])
        row = next(item for item in payload["rows"] if item["backend"] == "claude_cli")
        self.assertEqual(row["agents"], ["claude_cli"])
        self.assertEqual(row["dependency"], "custom-claude found")

    def test_collects_only_effective_backends_for_enabled_agents(self):
        config = builtin_config()
        calls = []

        def health(backend):
            calls.append(f"{backend.agent_type}_{backend.id}")
            if backend.agent_type == "claude":
                return _cli_health("claude")
            return _cli_health("codex", present=False)

        payload = collect_install_readiness(config, health=health)

        self.assertEqual(calls, ["claude_cli", "codex_cli"])
        self.assertEqual(payload["enabled_count"], 2)
        self.assertEqual(payload["selected_count"], 2)
        self.assertEqual(payload["attention_count"], 1)
        self.assertEqual(
            payload["disabled_backends"],
            [
                "antigravity_cli",
                "xai_cli",
                "claude_sdk",
                "codex_sdk",
                "antigravity_sdk",
                "xai_sdk",
            ],
        )
        rows = {row["backend"]: row for row in payload["rows"]}
        self.assertEqual(sorted(rows), ["claude_cli", "codex_cli"])
        self.assertEqual(rows["claude_cli"]["agents"], ["claude_cli"])
        self.assertEqual(rows["claude_cli"]["dependency"], "claude found")
        self.assertEqual(rows["claude_cli"]["credentials"], "not checked")
        self.assertEqual(rows["codex_cli"]["dependency"], "codex missing")

    def test_deduplicates_shared_effective_backend_probe(self):
        config = builtin_config()
        config.agents["claude-copy"] = AgentConfig(
            id="claude-copy", type="claude", command="claude", enabled=True
        )
        calls = []

        def health(backend):
            calls.append(f"{backend.agent_type}_{backend.id}")
            return _cli_health(backend.agent_type)

        payload = collect_install_readiness(config, health=health)

        self.assertEqual(calls.count("claude_cli"), 1)
        self.assertEqual(payload["enabled_count"], 3)
        self.assertEqual(payload["attention_count"], 0)
        row = next(item for item in payload["rows"] if item["backend"] == "claude_cli")
        self.assertEqual(row["agents"], ["claude_cli", "claude-copy"])

    def test_distinct_configured_commands_keep_separate_rows(self):
        config = builtin_config()
        merge_config_data(config, {"backends": {"codex_cli": {"enabled": False}}})
        config.agents["claude-custom"] = AgentConfig(
            id="claude-custom", type="claude", command="custom-claude", enabled=True
        )
        from agent_collab import backends as backend_registry

        backend = backend_registry.get_backend("claude", "cli")

        def probe(agent):
            return _cli_health(agent.command)

        with mock.patch.object(backend, "probe_for_agent", side_effect=probe):
            payload = collect_install_readiness(config)

        claude_rows = [row for row in payload["rows"] if row["backend"] == "claude_cli"]
        self.assertEqual(len(claude_rows), 2)
        self.assertEqual(
            {row["dependency"] for row in claude_rows},
            {"claude found", "custom-claude found"},
        )

    def test_reports_selected_sdk_and_disabled_backends_without_extra_probes(self):
        # Backend enablement is the policy: disabled backends derive no agents,
        # are summarized in disabled_backends, and are never probed.
        config = builtin_config()
        merge_config_data(
            config,
            {
                "backends": {
                    "claude_cli": {"enabled": False},
                    "codex_cli": {"enabled": False},
                    "claude_sdk": {"enabled": True},
                }
            },
        )
        calls = []

        def health(backend):
            calls.append(f"{backend.agent_type}_{backend.id}")
            return _sdk_health("claude_agent_sdk")

        payload = collect_install_readiness(config, health=health)

        self.assertEqual(calls, ["claude_sdk"])
        rows = {row["backend"]: row for row in payload["rows"]}
        self.assertEqual(sorted(rows), ["claude_sdk"])
        self.assertEqual(rows["claude_sdk"]["agents"], ["claude_sdk"])
        self.assertEqual(rows["claude_sdk"]["dependency"], "claude_agent_sdk found")
        self.assertIn("claude_cli", payload["disabled_backends"])
        self.assertIn("codex_cli", payload["disabled_backends"])

    def test_defaults_to_loading_user_config_without_creating_one(self):
        config = builtin_config()

        def health(backend):
            return _cli_health(backend.agent_type)

        with mock.patch(
            "agent_collab.install_readiness.load_user_config", return_value=config
        ) as loader:
            payload = collect_install_readiness(health=health)

        loader.assert_called_once_with()
        self.assertEqual(payload["config_source"], "built-in defaults (no user config)")

    def test_reports_user_config_source_when_config_files_loaded(self):
        config = builtin_config()
        config.loaded_paths.append(Path("/tmp/does-not-matter/config.toml"))

        payload = collect_install_readiness(config, health=lambda b: _cli_health(b.agent_type))

        self.assertEqual(payload["config_source"], "built-in defaults + user config")

    def test_probe_exception_becomes_unknown_nonfatal_fact(self):
        config = builtin_config()

        def health(backend):
            if backend.agent_type == "claude":
                raise RuntimeError("private provider failure")
            return _cli_health("codex")

        payload = collect_install_readiness(config, health=health)

        row = next(item for item in payload["rows"] if item["backend"] == "claude_cli")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "backend health probe failed")
        self.assertNotIn("private provider failure", str(payload))


class InstallReadinessTableTests(unittest.TestCase):
    def test_dependency_free_table_aligns_and_truncates(self):
        lines = format_table(
            ("agent", "backend"),
            (("claude", "claude_cli"), ("very-long-agent-name", "codex_cli")),
            max_widths=(10, 12),
        )

        self.assertEqual(lines[0], "  agent       backend")
        self.assertEqual(lines[1], "  ----------  ----------")
        self.assertIn("very-long…", lines[3])

    def test_installer_renders_readiness_and_remediation_tables(self):
        payload = {
            "scope": "global user config",
            "config_source": "built-in defaults + user config",
            "probe_source": "installed environment",
            "enabled_count": 3,
            "selected_count": 2,
            "attention_count": 1,
            "disabled_backends": ["antigravity_cli", "xai_cli"],
            "rows": [
                {
                    "backend": "claude_cli",
                    "agents": ["claude_cli", "claude-copy"],
                    "dependency": "claude found",
                    "credentials": "not checked",
                    "version": "1.2.3",
                    "remediation": [],
                },
                {
                    "backend": "codex_cli",
                    "agents": ["codex_cli"],
                    "dependency": "codex missing",
                    "credentials": "not checked",
                    "version": None,
                    "remediation": [
                        {
                            "code": "install_cli",
                            "message": "Install codex and ensure it is available on PATH.",
                        }
                    ],
                },
            ],
        }

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            warned = _print_backend_readiness(payload)

        output = stdout.getvalue()
        self.assertTrue(warned)
        self.assertIn("! Warning: 1 of 2 selected backends needs attention", output)
        self.assertIn("disabled backends  antigravity_cli, xai_cli", output)
        self.assertIn("backend     agents", output)
        self.assertIn("claude_cli  claude_cli, claude-copy", output)
        self.assertIn("backend    remediation", output)
        self.assertIn("Install codex", output)


if __name__ == "__main__":
    unittest.main()
