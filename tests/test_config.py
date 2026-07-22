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
from agent_collab.backends.base import BackendCapabilities, BackendHealth
from agent_collab.config import (
    DEFAULT_CONFIG_PATH,
    MAX_PARALLEL_WORKFLOW_WIDTH,
    AgentConfig,
    CollaborationConfig,
    ConfigError,
    WorkflowConfig,
    _parse_toml_subset,
    ensure_daemon_token,
    load_config,
    load_daemon_token,
    merge_config_data,
    validate_agent,
    validate_workflow,
)
from agent_collab.paths import AgentCollabHome


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
    def test_default_config_file_parses_with_fallback_toml_parser(self):
        data = _parse_toml_subset(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 10)
        self.assertNotIn("agents", data)
        self.assertNotIn("backends", data)
        self.assertNotIn("targets", data["usage_windows"])
        self.assertEqual(data["workflows"]["solo"]["sequence"], ["claude_cli"])
        self.assertEqual(data["workflows"]["dual-review"]["parallel"], ["claude_cli", "codex_cli"])

    def test_fallback_toml_parser_accepts_flat_parallel_array(self):
        data = _parse_toml_subset('[workflows.review]\nparallel = ["a", "b"]\n')

        self.assertEqual(data["workflows"]["review"]["parallel"], ["a", "b"])

    def test_fallback_toml_parser_keeps_quoted_section_parts_intact(self):
        # Persona display-name overrides quote the derived id, which contains
        # a literal dot; Python 3.10 uses this parser instead of tomllib.
        data = _parse_toml_subset('[agents."codex_cli.readonly"]\nname = "X"\n')

        self.assertEqual(data["agents"]["codex_cli.readonly"]["name"], "X")

    def test_parallel_workflow_validation(self):
        agents = {name: AgentConfig(id=name, type="mock") for name in ("a", "b", "c", "d", "e")}
        invalid = {
            "empty": ([], "must not be empty"),
            "single": (["a"], "use sequence for a single agent"),
            "duplicate": (["a", "a"], "must not contain duplicate agents"),
            "wide": (
                ["a", "b", "c", "d", "e"],
                f"maximum width of {MAX_PARALLEL_WORKFLOW_WIDTH}",
            ),
            "missing": (["a", "missing"], "references unknown agent 'missing'"),
        }
        for workflow_id, (members, message) in invalid.items():
            with self.subTest(workflow=workflow_id):
                config = CollaborationConfig(
                    agents=agents,
                    workflows={workflow_id: WorkflowConfig(id=workflow_id, parallel=members)},
                )
                with self.assertRaisesRegex(ConfigError, message):
                    validate_workflow(config, workflow_id)

        valid = CollaborationConfig(
            agents=agents,
            workflows={"valid": WorkflowConfig(id="valid", parallel=["a", "b"])},
        )
        validate_workflow(valid, "valid")

        agents["b"].enabled = False
        config = CollaborationConfig(
            agents=agents,
            workflows={"disabled": WorkflowConfig(id="disabled", parallel=["a", "b"])},
        )
        with self.assertRaisesRegex(ConfigError, "references agent 'b' of a disabled backend"):
            validate_workflow(config, "disabled")
        # Load-time validation keeps such a workflow; it is only start-ineligible.
        validate_workflow(config, "disabled", allow_disabled=True)

    def test_workflow_table_rejects_both_shapes(self):
        config = CollaborationConfig()

        with self.assertRaisesRegex(ConfigError, "exactly one of sequence or parallel"):
            merge_config_data(
                config,
                {"workflows": {"review": {"sequence": ["a"], "parallel": ["a", "b"]}}},
            )

    def test_workflow_validator_rejects_both_shapes(self):
        config = CollaborationConfig(
            agents={name: AgentConfig(id=name, type="mock") for name in ("a", "b")},
            workflows={
                "review": WorkflowConfig(
                    id="review",
                    sequence=["a"],
                    parallel=["a", "b"],
                )
            },
        )

        with self.assertRaisesRegex(ConfigError, "exactly one of sequence or parallel"):
            validate_workflow(config, "review")

    def test_workflow_shape_override_clears_inherited_shape(self):
        config = CollaborationConfig()
        merge_config_data(
            config,
            {"workflows": {"review": {"parallel": ["a", "b"]}}},
        )
        merge_config_data(config, {"workflows": {"review": {"sequence": ["a"]}}})

        self.assertEqual(config.workflows["review"].sequence, ["a"])
        self.assertIsNone(config.workflows["review"].parallel)

        merge_config_data(
            config,
            {"workflows": {"review": {"parallel": ["a", "b"]}}},
        )
        self.assertEqual(config.workflows["review"].sequence, [])
        self.assertEqual(config.workflows["review"].parallel, ["a", "b"])

    def test_builtin_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()

            config = load_config(root, env=_env(home))

            self.assertTrue(DEFAULT_CONFIG_PATH.exists())
            self.assertEqual(config.agents["claude_cli"].type, "claude")
            self.assertEqual(config.agents["claude_cli"].command, "claude")
            self.assertEqual(
                config.agents["claude_cli"].args,
                ["-p", "--output-format", "stream-json", "--verbose"],
            )
            self.assertEqual(config.agents["claude_cli"].options, {})
            self.assertEqual(config.agents["codex_cli"].command, "codex")
            self.assertEqual(config.agents["codex_cli"].args, ["exec", "--json"])
            self.assertEqual(config.agents["codex_cli"].options, {})
            # The cli backends ship enabled and derive their default agents;
            # they are only reported unavailable when the provider CLI is absent.
            self.assertIn("antigravity_cli", config.agents)
            self.assertIn("xai_cli", config.agents)
            self.assertTrue(config.backends["antigravity_cli"].enabled)
            self.assertEqual(config.backends["antigravity_cli"].command, "agy")
            self.assertTrue(config.backends["xai_cli"].enabled)
            self.assertEqual(config.backends["xai_cli"].command, "grok")
            # SDK backends stay opt-in (disabled) and derive no agents.
            self.assertNotIn("claude_sdk", config.agents)
            self.assertFalse(config.backends["claude_sdk"].enabled)
            self.assertFalse(config.backends["xai_sdk"].enabled)
            self.assertEqual(
                config.backends["xai_cli"].args,
                ["--no-auto-update", "--output-format", "streaming-json", "-p"],
            )
            self.assertEqual(config.workflows["solo"].sequence, ["claude_cli"])
            self.assertEqual(
                config.workflows["cross-review"].sequence,
                ["claude_cli", "codex_cli", "claude_cli"],
            )
            self.assertEqual(config.workflows["dual-review"].parallel, ["claude_cli", "codex_cli"])
            self.assertNotIn("compare", config.workflows)
            self.assertEqual(config.workdir.restrict_workdir_roots, [])
            self.assertEqual(config.loaded_paths, [])

    def test_project_config_cannot_override_agents_or_add_project_only_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
[backends.codex_cli]
command = "user-codex"

[backends.codex_cli.agents.readonly]
name = "Read-only Codex"
""",
            )
            _write_config(
                root,
                """
[backends.codex_cli]
command = "project-codex"

[agents."codex_cli.readonly"]
name = "Project Read-only"
command = "untrusted"

[workflows.readonly-review]
sequence = ["codex_cli.readonly", "claude_cli", "codex_cli.readonly"]
""",
            )

            config = load_config(root, env=_env(home))

            # Project [backends.*] is stripped; the user command stands.
            self.assertEqual(config.agents["codex_cli"].command, "user-codex")
            readonly = config.agents["codex_cli.readonly"]
            self.assertEqual(readonly.command, "user-codex")
            # The project may rename the persona but not change what runs.
            self.assertEqual(readonly.name, "Project Read-only")
            self.assertEqual(
                config.workflows["readonly-review"].sequence,
                ["codex_cli.readonly", "claude_cli", "codex_cli.readonly"],
            )
            rendered = "\n".join(warning["message"] for warning in config.warnings)
            self.assertIn("[backends.*] policy", rendered)
            self.assertIn("command", rendered)
            self.assertNotIn("untrusted", rendered)

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

            self.assertEqual(config.agents["claude_cli"].command, "user-claude")
            self.assertEqual(config.loaded_paths, [home.resolve() / "config.toml"])

    def test_user_backend_policy_is_loaded_and_project_cannot_override_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(home, "[backends.claude_cli]\nenabled = false\n")
            _write_config(root, "[backends.claude_cli]\nenabled = true\n")

            config = load_config(root, env=_env(home))

            self.assertFalse(config.backends["claude_cli"].enabled)
            self.assertEqual(config.backends["claude_cli"].source, "user_config")

    def test_project_agent_execution_fields_and_project_only_agents_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(
                root,
                """
[agents.claude]
type = "codex"
command = "untrusted-command"
args = ["--untrusted"]
enabled = false
name = "project-reviewer"
env = { TOKEN = "untrusted-secret-value" }
cwd = "/"
timeout = 1
backend = "sdk"
dangerous_backend_setting = true

[agents.claude.options]
permission_mode = "bypassPermissions"

[agents.project_only]
type = "codex"
command = "untrusted-project-agent"
enabled = true

[workflows.safe-project-review]
sequence = ["claude", "codex"]

[workflows.unsafe-project-review]
sequence = ["project_only"]
""",
            )

            config = load_config(root, env=_env(home))

            claude = config.agents["claude_cli"]
            self.assertEqual(claude.type, "claude")
            self.assertEqual(claude.command, "claude")
            self.assertTrue(claude.enabled)
            self.assertEqual(claude.backend, "cli")
            self.assertEqual(claude.name, "project-reviewer")
            self.assertEqual(claude.env, {})
            self.assertEqual(claude.options, {})
            self.assertNotIn("project_only", config.agents)
            self.assertIn("safe-project-review", config.workflows)
            self.assertNotIn("unsafe-project-review", config.workflows)
            rendered_warnings = "\n".join(warning["message"] for warning in config.warnings)
            self.assertIn("command", rendered_warnings)
            self.assertIn("options", rendered_warnings)
            self.assertIn("project-only agent", rendered_warnings)
            self.assertNotIn("untrusted-secret-value", rendered_warnings)
            self.assertNotIn("bypassPermissions", rendered_warnings)
            self.assertNotIn("untrusted-command", rendered_warnings)

    def test_restrict_workdir_roots_accepts_root_and_specific_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            normal_root = base / "normal"
            project = normal_root / "project"
            exception = base / "exception"
            home = base / "home"
            project.mkdir(parents=True)
            exception.mkdir()
            _write_user_config(
                home,
                (f'[workdir]\nrestrict_workdir_roots = ["{normal_root}", "{exception}"]\n'),
            )

            normal_config = load_config(project, env=_env(home))
            exception_config = load_config(exception, env=_env(home))

            expected = [normal_root.resolve(), exception.resolve()]
            self.assertEqual(normal_config.workdir.restrict_workdir_roots, expected)
            self.assertEqual(exception_config.workdir.restrict_workdir_roots, expected)

    def test_restrict_workdir_roots_rejects_outside_path_with_user_override_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            allowed = base / "allowed"
            outside = base / "outside"
            home = base / "home"
            allowed.mkdir()
            outside.mkdir()
            _write_user_config(home, f'[workdir]\nrestrict_workdir_roots = ["{allowed}"]\n')

            with self.assertRaisesRegex(ConfigError, "add that directory to the user config"):
                load_config(outside, env=_env(home))

    def test_workdir_at_agent_collab_home_parent_does_not_reload_user_config_as_project(self):
        """A home-directory workdir must not strip the user config as project config."""

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "userhome"
            home = workdir / ".agent-collab"
            _write_user_config(
                home,
                'schema_version = 6\n\n[daemon]\ntoken = "secret-token-value"\n',
            )

            config = load_config(workdir, env=_env(home))

            self.assertEqual(config.daemon_token, "secret-token-value")
            self.assertEqual(config.warnings, [])
            self.assertEqual(len(config.loaded_paths), 1)

    def test_project_workdir_policy_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "allowed" / "project"
            home = base / "home"
            root.mkdir(parents=True)
            _write_user_config(
                home,
                f'[workdir]\nrestrict_workdir_roots = ["{base / "allowed"}"]\n',
            )
            _write_config(root, '[workdir]\nrestrict_workdir_roots = ["/"]\n')

            config = load_config(root, env=_env(home))

            self.assertEqual(config.workdir.restrict_workdir_roots, [(base / "allowed").resolve()])
            self.assertTrue(any(warning["path"] == "workdir" for warning in config.warnings))

    def test_restrict_workdir_roots_rejects_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(home, '[workdir]\nrestrict_workdir_roots = ["relative"]\n')

            with self.assertRaisesRegex(ConfigError, "must be absolute"):
                load_config(root, env=_env(home))

    def test_sessions_defaults_without_any_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()

            config = load_config(root, env=_env(home))

            self.assertEqual(config.sessions.retention_days, 30)
            self.assertEqual(config.sessions.cleanup_interval_hours, 24)

    def test_user_sessions_settings_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(home, "[sessions]\nretention_days = 7\n")

            config = load_config(root, env=_env(home))

            self.assertEqual(config.sessions.retention_days, 7)
            # An unset key keeps its built-in default.
            self.assertEqual(config.sessions.cleanup_interval_hours, 24)

    def test_sessions_retention_zero_disables_pruning_and_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(home, "[sessions]\nretention_days = 0\n")

            config = load_config(root, env=_env(home))

            self.assertEqual(config.sessions.retention_days, 0)

    def test_sessions_rejects_invalid_values(self):
        from agent_collab.config import CollaborationConfig, merge_config_data

        cases = [
            ({"sessions": {"retention_days": -1}}, "retention_days"),
            ({"sessions": {"retention_days": True}}, "retention_days"),
            ({"sessions": {"retention_days": 1.5}}, "retention_days"),
            ({"sessions": {"retention_days": "30"}}, "retention_days"),
            ({"sessions": {"cleanup_interval_hours": 0}}, "cleanup_interval_hours"),
            ({"sessions": {"cleanup_interval_hours": -6}}, "cleanup_interval_hours"),
            ({"sessions": {"cleanup_interval_hours": False}}, "cleanup_interval_hours"),
            ({"sessions": {"bogus": 1}}, "sessions.bogus"),
            ({"sessions": "not a table"}, r"\[sessions\] must be a table"),
        ]
        for data, message in cases:
            with self.assertRaisesRegex(ConfigError, message, msg=repr(data)):
                merge_config_data(CollaborationConfig(), data)

    def test_project_sessions_section_is_stripped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_config(root, "[sessions]\nretention_days = 1\n")

            with self.assertLogs("agent_collab.config", level="WARNING") as logs:
                config = load_config(root, env=_env(home))

            self.assertEqual(config.sessions.retention_days, 30)
            self.assertTrue(any("[sessions]" in line for line in logs.output))

    def test_cli_config_show_prints_sessions_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(home, "[sessions]\nretention_days = 90\n")
            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "show", "--workdir", str(root)])

            self.assertEqual(code, 0)
            self.assertIn(
                "sessions: retention_days=90 cleanup_interval_hours=24", output.getvalue()
            )

    def test_cli_config_show_labels_parallel_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(
                home,
                'schema_version = 7\n[workflows.review]\nparallel = ["claude", "codex"]\n',
            )
            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "show", "--workdir", str(root)])

            self.assertEqual(code, 0)
            self.assertIn("workflow review (parallel): claude_cli + codex_cli", output.getvalue())

    def test_config_init_materializes_every_registered_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(Path(tmp) / "home")):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "init"])
            config_path = Path(tmp) / "home" / "config.toml"
            text = config_path.read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            for name in backends.registered_backend_names():
                self.assertIn(f"[backends.{name}]", text)
            self.assertIn("[workdir]", text)
            self.assertIn("restrict_workdir_roots = []", text)
            self.assertIn("[daemon]", text)
            self.assertIn("token = ", text)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("holds the daemon bearer token", output.getvalue())
            config = load_config(Path(tmp), env=_env(Path(tmp) / "home"))
            self.assertEqual(config.workdir.restrict_workdir_roots, [])

    def test_shell_cwd_does_not_affect_safe_project_config(self):
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
name = "project-a-codex"
""",
            )
            _write_config(
                project_b,
                """
[agents.codex]
name = "project-b-codex"
""",
            )

            cwd = os.getcwd()
            os.chdir(project_a)
            try:
                config = load_config(project_b, env=_env(home))
            finally:
                os.chdir(cwd)

            self.assertEqual(config.agents["codex_cli"].name, "project-b-codex")
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
            self.assertIn("workflow custom: claude_cli", text)
            self.assertIn("workflow cross-review: claude_cli -> codex_cli -> claude_cli", text)
            self.assertIn(str(root.resolve() / ".agent-collab" / "config.toml"), text)

    def test_cli_config_show_prints_configured_agent_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(
                home,
                """
[agents.claude.options]
model = "opus"
thinking_level = "high"
""",
            )

            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "show", "--workdir", str(root)])

            text = output.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("backend cli option model = 'opus'", text)
            self.assertIn("backend cli option thinking_level = 'high'", text)

    def test_cli_config_show_prints_all_agent_fields_and_redacts_env_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(
                home,
                """
[agents.claude]
name = "primary-reviewer"
cwd = "/tmp/claude-cwd"
timeout = 600
env = { ZZZ_LAST = "other-value", ANTHROPIC_API_KEY = "sk-secret-value" }

[agents.bare_sdk]
type = "claude"
backend = "sdk"
enabled = true

[agents.gemini]
type = "antigravity"
backend = "sdk"
enabled = true
vertex = true
project = "example-project"
location = "us-central1"
""",
            )

            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "show", "--workdir", str(root)])

            text = output.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("agent claude_cli: type=claude backend=cli", text)
            self.assertIn("agent claude_sdk: type=claude backend=sdk", text)
            self.assertIn("name='primary-reviewer'", text)
            self.assertIn("cwd='/tmp/claude-cwd'", text)
            self.assertIn("timeout=600", text)
            self.assertIn("env_keys=ANTHROPIC_API_KEY,ZZZ_LAST", text)
            self.assertNotIn("sk-secret-value", text)
            self.assertNotIn("other-value", text)
            self.assertIn("backend sdk config vertex = True", text)
            self.assertIn("backend sdk config project = 'example-project'", text)
            self.assertIn("backend sdk config location = 'us-central1'", text)

    def test_cli_config_show_handles_migrated_schema_3_user_config_with_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            (home / "config.toml").write_text(
                """
schema_version = 3

[agents.claude_cli]
type = "claude"
backend = "cli"
command = "claude"
args = ["-p", "--output-format", "stream-json", "--verbose"]
enabled = true

[agents.claude_cli.options]
model = "opus"
thinking_level = "high"

[workflows.solo-claude-cli]
sequence = ["claude_cli"]
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            errors = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = cli.main(["config", "show", "--workdir", str(root)])

            text = output.getvalue()
            self.assertEqual(code, 0, errors.getvalue())
            self.assertIn("agent claude_cli:", text)
            self.assertIn("backend cli option model = 'opus'", text)
            self.assertIn("workflow solo-claude-cli: claude_cli", text)
            self.assertEqual(errors.getvalue(), "")

    def test_cli_options_is_a_projection_of_daemon_discovery(self):
        client = mock.Mock()
        client.describe_options.return_value = {
            "discovery": {"workdir": "/repo", "health_request": "fresh"},
            "backends": {
                "claude_cli": {
                    "probe": {"health": {"status": "ok"}},
                    "policy": {"enabled": True, "start_probe_policy": "not_probed"},
                    "assessment": {"state": "usable"},
                }
            },
            "workflows": [
                {
                    "id": "solo-claude-cli",
                    "selected_backends": ["claude_cli"],
                    "start_eligible": True,
                }
            ],
        }
        output = io.StringIO()
        with mock.patch("agent_collab.cli._client", return_value=client):
            with contextlib.redirect_stdout(output):
                code = cli.main(["options", "--workdir", "/repo", "--fresh"])
        self.assertEqual(code, 0)
        client.describe_options.assert_called_once_with(
            {"workdir": str(Path("/repo").resolve()), "health_refresh": "fresh"}
        )
        self.assertIn("backend claude_cli", output.getvalue())

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
        self.assertEqual(
            data["workflows"]["readonly-review"]["sequence"],
            ["codex_readonly", "claude", "codex_readonly"],
        )

    def test_toml_subset_parser_supports_backend_agent_options(self):
        data = _parse_toml_subset(
            """
[agents.antigravity_sdk]
type = "antigravity"
backend = "sdk"
vertex = true
project = "test-project"
"""
        )

        agent = data["agents"]["antigravity_sdk"]
        self.assertTrue(agent["vertex"])
        self.assertEqual(agent["project"], "test-project")

    def test_toml_subset_parser_supports_workdir_policy(self):
        data = _parse_toml_subset(
            """
[workdir]
restrict_workdir_roots = ["/projects", "/one/exception"]
"""
        )

        self.assertEqual(
            data["workdir"]["restrict_workdir_roots"],
            ["/projects", "/one/exception"],
        )

    def test_agent_options_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
[agents.claude_sdk]
type = "claude"
backend = "sdk"

[agents.claude_sdk.options]
model = "opus"
thinking_level = "high"
""",
            )

            config = load_config(root, env=_env(home))

            options = config.agents["claude_sdk"].options_for("sdk")
            self.assertEqual(options["model"], "opus")
            self.assertEqual(options["thinking_level"], "high")

    def test_unknown_static_backend_config_is_rejected_by_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(home, "[backends.claude_cli]\nbogus = true\n")

            with self.assertRaisesRegex(ConfigError, "backends.claude_cli.bogus"):
                load_config(root, env=_env(home))

    def test_antigravity_static_config_is_validated_by_sdk_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
schema_version = 8

[backends.antigravity_sdk]
enabled = true
vertex = true
location = "us-central1"
""",
            )

            with self.assertRaisesRegex(ConfigError, "backends.antigravity_sdk.project"):
                load_config(root, env=_env(home))

    def test_project_workflow_with_unknown_agent_is_dropped(self):
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

            config = load_config(root, env=_env(home))

            self.assertNotIn("bad", config.workflows)
            self.assertTrue(any(warning["path"] == "workflows.bad" for warning in config.warnings))

    def test_project_workflow_with_disabled_agent_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
[agents.disabled_codex]
type = "codex"
command = "codex"
args = ["exec", "--json"]
enabled = false
""",
            )
            _write_config(
                root,
                """

[workflows.bad]
sequence = ["disabled_codex"]
""",
            )

            config = load_config(root, env=_env(home))

            self.assertNotIn("bad", config.workflows)
            self.assertTrue(any(warning["path"] == "workflows.bad" for warning in config.warnings))


class AgentBackendConfigTests(unittest.TestCase):
    def test_unregistered_backend_for_type_is_rejected_with_registered_ids(self):
        agent = AgentConfig(id="claude", type="claude", command="claude", backend="nonesuch")
        with self.assertRaises(ConfigError) as ctx:
            validate_agent(agent)
        message = str(ctx.exception)
        self.assertIn("nonesuch", message)
        self.assertIn("cli", message)  # registered ids for claude are listed
        self.assertIn("sdk", message)

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
        fake = SimpleNamespace(
            agent_type="claude",
            id="fake",
            capabilities=BackendCapabilities(),
            brand_color="#123456",
            event_fidelity="typed",
            provider_session_id_kind=None,
            checks_credentials=False,
            block_on_unavailable=False,
            probe=lambda: BackendHealth(),
            option_schema=lambda agent: {},
            normalize_options=lambda agent, requested: dict(requested),
            settings_summary=lambda agent, options: {"backend": "fake", "options": dict(options)},
            command_preview=lambda agent, options, workdir=None: None,
            create_runner=lambda agent, verbose, options: None,
        )
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

    def test_backend_kind_derives_from_the_section_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
schema_version = 8

[backends.codex_sdk]
enabled = true
""",
            )

            config = load_config(root, env=_env(home))

            self.assertEqual(config.agents["codex_sdk"].backend, "sdk")
            self.assertEqual(config.agents["codex_sdk"].type, "codex")
            self.assertEqual(config.agents["codex_cli"].backend, "cli")

    def test_nested_persona_derives_options_only_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            _write_user_config(
                home,
                """
[backends.claude_cli.agents.deep]
name = "Deep Claude"

[backends.claude_cli.agents.deep.options]
model = "opus"

[workflows.deep-review]
sequence = ["claude_cli.deep"]
""",
            )

            config = load_config(root, env=_env(home))

            persona = config.agents["claude_cli.deep"]
            self.assertEqual(persona.type, "claude")
            self.assertEqual(persona.command, "claude")
            self.assertEqual(persona.name, "Deep Claude")
            self.assertEqual(persona.options["model"], "opus")
            self.assertEqual(config.workflows["deep-review"].sequence, ["claude_cli.deep"])

    def test_persona_rejects_execution_fields(self):
        config = CollaborationConfig()
        with self.assertRaisesRegex(ConfigError, "may set only 'name' and 'options'"):
            merge_config_data(
                config,
                {"backends": {"claude_cli": {"agents": {"deep": {"command": "evil"}}}}},
            )

    def test_stray_top_level_agent_section_is_rejected(self):
        config = CollaborationConfig()
        with self.assertRaisesRegex(ConfigError, "no longer supported"):
            merge_config_data(
                config,
                {"agents": {"claude": {"command": "claude"}}},
            )


class DaemonTokenTests(unittest.TestCase):
    def _home(self, tmp: str) -> AgentCollabHome:
        root = Path(tmp) / "home"
        return AgentCollabHome(root=root, config_path=root / "config.toml")

    def test_ensure_creates_private_config_and_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            first = ensure_daemon_token(home)
            second = ensure_daemon_token(home)

            self.assertEqual(first, second)
            self.assertEqual(home.config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_daemon_token(home), first)
            self.assertIn(f'token = "{first}"', home.config_path.read_text(encoding="utf-8"))

    def test_ensure_appends_to_existing_config_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            existing = "# my notes\nschema_version = 4\n\n[agents.claude]\nname = 'x'\n"
            home.root.mkdir(parents=True)
            home.config_path.write_text(existing, encoding="utf-8")
            home.config_path.chmod(0o600)

            token = ensure_daemon_token(home)

            text = home.config_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(existing))
            self.assertIn("[daemon]", text)
            self.assertEqual(load_daemon_token(home), token)
            self.assertEqual(home.config_path.stat().st_mode & 0o777, 0o600)

    def test_ensure_preserves_a_symlinked_config(self):
        # A dotfile-managed config is a symlink into a repository. Appending the
        # token must write through to the target, not sever the link with an
        # os.replace on the symlink path itself.
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            home.root.mkdir(parents=True)
            target = Path(tmp) / "dotfiles" / "config.toml"
            target.parent.mkdir(parents=True)
            target.write_text("schema_version = 8\n", encoding="utf-8")
            target.chmod(0o600)
            home.config_path.symlink_to(target)

            token = ensure_daemon_token(home)

            self.assertTrue(home.config_path.is_symlink())
            self.assertEqual(home.config_path.resolve(), target.resolve())
            self.assertIn("[daemon]", target.read_text(encoding="utf-8"))
            self.assertEqual(load_daemon_token(home), token)

    def test_ensure_preserves_a_broken_symlinked_config(self):
        # A dangling dotfile link (repository cloned, config target not written
        # yet) must not be os.replaced with a regular file: the token config is
        # created at the link target and the symlink survives.
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            home.root.mkdir(parents=True)
            target = Path(tmp) / "dotfiles" / "config.toml"
            home.config_path.symlink_to(target)

            token = ensure_daemon_token(home)

            self.assertTrue(home.config_path.is_symlink())
            self.assertEqual(home.config_path.resolve(), target.resolve())
            self.assertIn("[daemon]", target.read_text(encoding="utf-8"))
            self.assertEqual(load_daemon_token(home), token)

    def test_ensure_returns_concurrent_winner_without_overwriting(self):
        # Regression for #37: if another creator wins the race while we wait for
        # the lock, the re-check under the lock must return that persisted token
        # rather than generating and writing a second one.
        from agent_collab import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            home.root.mkdir(parents=True)
            calls = {"n": 0}

            def fake_load(_home):
                # First call is the pre-lock fast path (still missing); the
                # second is the in-lock re-check, where the winner is present.
                calls["n"] += 1
                return None if calls["n"] == 1 else "winner-token"

            with (
                mock.patch.object(config_module, "load_daemon_token", side_effect=fake_load),
                mock.patch.object(config_module, "atomic_write_private_text") as write,
            ):
                token = ensure_daemon_token(home)

            self.assertEqual(token, "winner-token")
            write.assert_not_called()
            self.assertFalse(home.config_path.exists())

    def test_ensure_token_creation_is_serialized_by_lock(self):
        # The blocking lock must actually exclude a second creator: while the
        # lock is held, a concurrent ensure_daemon_token blocks, and once it is
        # released the waiter creates and returns the single persisted token.
        import threading

        from agent_collab.config import _daemon_token_lock

        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            home.root.mkdir(parents=True)
            result: dict = {}
            started = threading.Event()

            def worker():
                started.set()
                result["token"] = ensure_daemon_token(home)

            with _daemon_token_lock(home):
                thread = threading.Thread(target=worker)
                thread.start()
                started.wait()
                thread.join(timeout=0.3)
                # Still blocked on the lock we hold: no token produced yet.
                self.assertTrue(thread.is_alive())
                self.assertNotIn("token", result)

            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result["token"])
            self.assertEqual(load_daemon_token(home), result["token"])

    def test_ensure_refuses_group_or_world_readable_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            home.root.mkdir(parents=True)
            home.config_path.write_text("schema_version = 4\n", encoding="utf-8")
            home.config_path.chmod(0o644)

            with self.assertRaisesRegex(ConfigError, "chmod 600"):
                ensure_daemon_token(home)
            self.assertNotIn("[daemon]", home.config_path.read_text(encoding="utf-8"))

    def test_ensure_rejects_daemon_section_without_usable_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            home.root.mkdir(parents=True)
            home.config_path.write_text('[daemon]\ntoken = ""\n', encoding="utf-8")
            home.config_path.chmod(0o600)

            with self.assertRaisesRegex(ConfigError, "without a usable token"):
                ensure_daemon_token(home)

    def test_load_returns_none_when_missing_and_warns_when_permissive(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            self.assertIsNone(load_daemon_token(home))

            home.root.mkdir(parents=True)
            home.config_path.write_text('[daemon]\ntoken = "abc"\n', encoding="utf-8")
            home.config_path.chmod(0o644)
            with self.assertLogs("agent_collab.config", level="WARNING") as logs:
                self.assertEqual(load_daemon_token(home), "abc")
            self.assertIn("chmod 600", logs.output[0])

    def test_user_daemon_token_loads_and_never_prints_in_config_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(
                home,
                """
schema_version = 4

[daemon]
token = "user-config-secret"
""",
            )

            config = load_config(root, env=_env(home))
            self.assertEqual(config.daemon_token, "user-config-secret")

            output = io.StringIO()
            with mock.patch.dict(os.environ, _env(home)):
                with contextlib.redirect_stdout(output):
                    code = cli.main(["config", "show", "--workdir", str(root)])
            self.assertEqual(code, 0)
            self.assertNotIn("user-config-secret", output.getvalue())

    def test_project_daemon_section_is_stripped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_config(
                root,
                """
schema_version = 4

[daemon]
token = "project-injected"
""",
            )

            with self.assertLogs("agent_collab.config", level="WARNING") as logs:
                config = load_config(root, env=_env(home))

            self.assertIsNone(config.daemon_token)
            self.assertIn("[daemon]", logs.output[0])

    def test_unknown_daemon_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            home = Path(tmp) / "home"
            root.mkdir()
            _write_user_config(
                home,
                """
schema_version = 4

[daemon]
token = "abc"
port = 9999
""",
            )

            with self.assertRaisesRegex(ConfigError, "unknown field daemon.port"):
                load_config(root, env=_env(home))


if __name__ == "__main__":
    unittest.main()
