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
from agent_collab.install_readiness import (
    collect_install_readiness,
    default_model_discovery,
)
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


class InstallModelDiscoveryWiringTests(unittest.TestCase):
    """The install-time model-discovery hook: invoked with per-backend health
    versions, folded into the payload, hermetically skipped by default, and
    non-fatal on failure."""

    def test_hook_receives_config_and_health_versions_and_lands_in_payload(self):
        received = {}

        def fake_discovery(config, versions):
            received["config"] = config
            received["versions"] = dict(versions)
            return {"attempted": ["antigravity_cli"], "backends": {}, "warnings": []}

        payload = collect_install_readiness(
            builtin_config(),
            health=lambda backend: _cli_health(backend.agent_type, version="9.9"),
            model_discovery=fake_discovery,
        )

        self.assertEqual(payload["snapshot_version"], 4)
        self.assertEqual(
            payload["model_discovery"],
            {"attempted": ["antigravity_cli"], "backends": {}, "warnings": []},
        )
        self.assertIs(received["config"].__class__, builtin_config().__class__)
        # Every probed backend contributes its health version to the map the
        # discovery fingerprints with.
        self.assertEqual(received["versions"].get("antigravity_cli"), "9.9")
        self.assertEqual(received["versions"].get("xai_cli"), "9.9")

    def test_default_none_skips_discovery_hermetically(self):
        payload = collect_install_readiness(
            builtin_config(), health=lambda backend: _cli_health(backend.agent_type)
        )
        self.assertTrue(payload["model_discovery"]["skipped"])
        self.assertEqual(payload["model_discovery"]["warnings"], [])

    def test_main_wires_the_default_discovery_hook(self):
        from agent_collab import install_readiness

        with mock.patch.object(
            install_readiness,
            "collect_install_readiness",
            return_value={"rows": []},
        ) as collect:
            with mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(install_readiness.main([]), 0)
        self.assertIs(collect.call_args.kwargs.get("model_discovery"), default_model_discovery)

    def test_default_model_discovery_degrades_to_non_fatal_warning(self):
        with mock.patch(
            "agent_collab.model_catalog.run_install_discovery",
            side_effect=RuntimeError("boom"),
        ):
            summary = default_model_discovery(builtin_config(), {})
        self.assertEqual(summary["backends"], {})
        self.assertEqual(summary["warnings"][0]["code"], "model_discovery_failed")


class InstallReadinessCollectionTests(unittest.TestCase):
    def test_collects_enabled_usage_window_config_and_participants(self):
        config = builtin_config()
        merge_config_data(
            config,
            {
                "usage_windows": {
                    "targets": {
                        "claude_cli_sonnet": {"enabled": True},
                        "codex_cli_luna": {
                            "enabled": True,
                            "interval": "3h",
                        },
                    }
                }
            },
        )

        payload = collect_install_readiness(
            config, health=lambda backend: _cli_health(backend.agent_type)
        )

        usage = payload["usage_windows"]
        self.assertEqual(usage["timezone"], "local")
        self.assertEqual(usage["days"], ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual(usage["work_time"], "09:00-17:00")
        self.assertEqual(usage["interval"], "5h")
        self.assertEqual(usage["jitter"], "±5m")
        self.assertEqual(
            usage["targets"],
            [
                {"backend": "claude_cli", "model": "sonnet", "overrides": []},
                {
                    "backend": "codex_cli",
                    "model": "gpt-5.6-luna",
                    "overrides": ["interval=3h"],
                },
            ],
        )

    def test_cli_probe_uses_the_agent_configured_command(self):
        from agent_collab import backends as backend_registry

        config = builtin_config()
        merge_config_data(
            config,
            {
                "backends": {
                    "claude_cli": {"command": "custom-claude"},
                    "codex_cli": {"enabled": False},
                    # Disable the default-on cli backends this test does not
                    # inject health for, so it never shells out to real agy/grok.
                    "antigravity_cli": {"enabled": False},
                    "xai_cli": {"enabled": False},
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
        # Keep this collection test focused on the claude/codex pair.
        merge_config_data(
            config,
            {"backends": {"antigravity_cli": {"enabled": False}, "xai_cli": {"enabled": False}}},
        )
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
                "antigravity_sdk",
                "claude_sdk",
                "codex_sdk",
                "xai_cli",
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
        merge_config_data(
            config,
            {"backends": {"antigravity_cli": {"enabled": False}, "xai_cli": {"enabled": False}}},
        )
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
        merge_config_data(
            config,
            {
                "backends": {
                    "codex_cli": {"enabled": False},
                    # No injected health for agy/grok here: disable them so the
                    # test never shells out to real provider CLIs.
                    "antigravity_cli": {"enabled": False},
                    "xai_cli": {"enabled": False},
                }
            },
        )
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
                    "antigravity_cli": {"enabled": False},
                    "xai_cli": {"enabled": False},
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
        # A not-ready backend is framed as "a workflow that uses it will not
        # succeed" (accurate whether it is start-gated or fails on first turn),
        # not an install failure.
        self.assertIn(
            "ⓘ Info: 1 of 2 enabled backends is not ready yet; "
            "a workflow that uses one will not succeed",
            output,
        )
        self.assertIn("disabled backends  antigravity_cli, xai_cli", output)
        # Blank lines separate the summary block, each table, and what follows.
        self.assertIn("antigravity_cli, xai_cli\n\n", output)
        self.assertIn("—\n\n  backend    remediation", output)
        # The disable-a-backend hint closes the section.
        self.assertIn("enabled = false under its [backends.<name>] section", output)
        self.assertTrue(output.rstrip().endswith("in the user config."))
        # Only non-default agents appear in the agents cell.
        self.assertIn("backend     agents", output)
        self.assertIn("claude_cli  claude-copy", output)
        self.assertIn("backend    remediation", output)

    def test_agents_column_is_omitted_when_only_default_agents_exist(self):
        payload = {
            "scope": "global user config",
            "config_source": "built-in defaults + user config",
            "probe_source": "installed environment",
            "enabled_count": 1,
            "selected_count": 1,
            "attention_count": 0,
            "disabled_backends": [],
            "rows": [
                {
                    "backend": "claude_cli",
                    "agents": ["claude_cli"],
                    "dependency": "claude found",
                    "credentials": "not checked",
                    "version": "1.2.3",
                    "remediation": [],
                }
            ],
        }

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            warned = _print_backend_readiness(payload)

        output = stdout.getvalue()
        self.assertFalse(warned)
        self.assertNotIn("agents", output)
        self.assertIn("backend     dependency", output)
        self.assertIn("claude_cli  claude found", output)
        self.assertTrue(output.endswith("1.2.3\n\n"))

    def test_installer_renders_usage_window_config_before_participants(self):
        payload = {
            "selected_count": 1,
            "attention_count": 0,
            "disabled_backends": [],
            "rows": [
                {
                    "backend": "claude_cli",
                    "agents": ["claude_cli"],
                    "dependency": "claude found",
                    "credentials": "not checked",
                    "version": "1.2.3",
                }
            ],
            "usage_windows": {
                "timezone": "local",
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "work_time": "09:00-17:00",
                "interval": "5h",
                "jitter": "±5m",
                "targets": [
                    {"backend": "claude_cli", "model": "sonnet", "overrides": []},
                    {
                        "backend": "codex_cli",
                        "model": "gpt-5.6-luna",
                        "overrides": [],
                    },
                ],
            },
        }

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            warned = _print_backend_readiness(payload)

        output = stdout.getvalue()
        self.assertFalse(warned)
        self.assertIn("✓ 2 usage-window targets enabled", output)
        self.assertIn("timezone   local", output)
        self.assertIn("days       mon,tue,wed,thu,fri", output)
        self.assertIn("work time  09:00-17:00", output)
        self.assertIn("interval   5h", output)
        self.assertIn("jitter     ±5m", output)
        self.assertLess(output.index("timezone"), output.index("participating backends"))
        self.assertIn("    backend     model", output)
        self.assertIn("    claude_cli  sonnet", output)
        self.assertIn("    codex_cli   gpt-5.6-luna", output)


if __name__ == "__main__":
    unittest.main()
