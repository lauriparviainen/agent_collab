from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_collab import backends
from agent_collab.backends.xai_cli.backend import XaiCliBackend
from agent_collab.backends.xai_cli.sandbox import XaiCliSandboxAdapter
from agent_collab.config import AgentConfig, builtin_config, merge_config_data
from agent_collab.options import build_session_settings, describe_options, normalize_start_options
from agent_collab.runners import DryRunRunner
from agent_collab.sandbox.paths import resolve_state_root
from agent_collab.sandbox.plan import (
    ResolvedSandboxPlan,
    ResolvedSandboxSessionPlan,
    SandboxOperatorConfig,
    resolve_session_plan,
)
from agent_collab.sandbox.policy import resolve_sandbox_policy
from agent_collab.sandbox.specs import (
    ResolvedSandboxPolicy,
    SandboxContext,
    SandboxEnforcement,
    SandboxFailure,
    SandboxPolicy,
    SandboxPolicySource,
    SandboxSupport,
)


class XaiCliSandboxAdapterTests(unittest.TestCase):
    def _plan(
        self,
        policy: SandboxPolicy,
        state: Path,
        *,
        home: Path | None = None,
        command: tuple[str, ...] = ("/usr/bin/grok", "-p"),
        environment: dict[str, str] | None = None,
        workspace: Path = Path("/workspace"),
    ) -> ResolvedSandboxPlan:
        adapter = XaiCliSandboxAdapter()
        inherited = {
            "HOME": str(home or state.parent),
            "GROK_HOME": str(state),
            **(environment or {}),
        }
        context = SandboxContext(workspace, workspace, inherited, command)
        spec = adapter.describe(context)
        return ResolvedSandboxPlan(
            policy=ResolvedSandboxPolicy(policy, policy, SandboxPolicySource.REQUEST),
            support=SandboxSupport.DIRECT_PROCESS,
            enforcement=(
                SandboxEnforcement.OS_ENFORCED
                if policy is SandboxPolicy.READ_ONLY
                else SandboxEnforcement.DISABLED
            ),
            context=context,
            spec=spec,
            adapter=adapter,
        )

    def _run_compatibility(self, plan: ResolvedSandboxPlan) -> None:
        for check in plan.spec.compatibility:
            check.check()

    def test_registry_exposes_stage_3_without_xai_branch_in_common_sandbox(self):
        adapter = backends.get_backend("xai", "cli").sandbox_adapter
        self.assertIsInstance(adapter, XaiCliSandboxAdapter)
        supported = {
            ("antigravity", "cli"),
            ("antigravity", "sdk"),
            ("claude", "cli"),
            ("claude", "sdk"),
            ("codex", "cli"),
            ("codex", "sdk"),
            ("xai", "cli"),
        }
        for agent_type in backends.registered_agent_types():
            for backend_id in backends.registered_backends(agent_type):
                spec = backends.get_backend(agent_type, backend_id).sandbox_adapter.describe(
                    SandboxContext(Path("/w"), Path("/w"), {})
                )
                with self.subTest(agent_type=agent_type, backend_id=backend_id):
                    self.assertEqual(
                        SandboxPolicy.READ_ONLY in spec.policies,
                        (agent_type, backend_id) in supported or agent_type == "mock",
                    )
        for path in Path("agent_collab/sandbox").glob("*.py"):
            self.assertNotIn("xai_cli", path.read_text(encoding="utf-8"))

    def test_explicit_grok_home_is_exact_persistent_writable_projection(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "complete-state"
            plan = self._plan(SandboxPolicy.READ_ONLY, state)
            root = plan.spec.state_roots[0]

        self.assertEqual(root.destination, state)
        self.assertEqual(root.persistence.value, "host_persistent")
        self.assertEqual(root.access.value, "writable")
        self.assertEqual(plan.spec.environment.set_values["GROK_HOME"], str(state))
        self.assertNotEqual(root.destination, state.parent)
        self.assertIn("XAI_API_KEY", plan.spec.environment.secret_names)

    def test_default_state_is_dot_grok_and_is_exposed_through_grok_home(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            adapter = XaiCliSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(Path("/workspace"), Path("/workspace"), {"HOME": str(home)})
            )
        self.assertEqual(spec.state_roots[0].destination, home / ".grok")
        self.assertEqual(spec.environment.set_values["GROK_HOME"], str(home / ".grok"))

    def test_relative_grok_home_and_relative_default_home_fail_closed(self):
        adapter = XaiCliSandboxAdapter()
        for environment in (
            {"HOME": "/home/operator", "GROK_HOME": "relative"},
            {"HOME": "relative"},
        ):
            spec = adapter.describe(
                SandboxContext(Path("/workspace"), Path("/workspace"), environment)
            )
            with self.subTest(environment=environment), self.assertRaises(SandboxFailure) as raised:
                for check in spec.compatibility:
                    check.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_common_path_contract_rejects_missing_symlink_permissions_and_ownership(self):
        adapter = XaiCliSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            state = home / ".grok"
            missing = adapter.describe(
                SandboxContext(
                    Path("/w"),
                    Path("/w"),
                    {"HOME": str(home), "GROK_HOME": str(state)},
                )
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(missing)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_missing")

            target = home / "target"
            target.mkdir(mode=0o700)
            state.symlink_to(target, target_is_directory=True)
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(missing)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_symlink")

            state.unlink()
            state.mkdir(mode=0o770)
            state.chmod(0o770)
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(missing)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_permissions")

            state.chmod(0o700)
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(missing, daemon_uid=os.getuid() + 1)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_ownership")

    def test_home_breadth_and_workspace_overlap_are_rejected(self):
        adapter = XaiCliSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            for state in (Path.home().resolve(), workspace / ".grok"):
                if state != Path.home().resolve():
                    state.mkdir(mode=0o700)
                with (
                    self.subTest(state=state),
                    mock.patch(
                        "agent_collab.sandbox.plan.resolve_scratch_anchor",
                        return_value=Path("/safe/scratch"),
                    ),
                    self.assertRaises(SandboxFailure) as raised,
                ):
                    resolve_session_plan(
                        policy=resolve_sandbox_policy("read-only", "none"),
                        workspace_path=workspace,
                        agents={
                            "xai": (
                                None,
                                {"HOME": str(root), "GROK_HOME": str(state)},
                                adapter,
                            )
                        },
                        command_previews={"xai": ("grok", "-p")},
                        operator=SandboxOperatorConfig(),
                        audit=False,
                    )
                self.assertIn(
                    raised.exception.code,
                    {
                        "outer_sandbox_writable_too_broad",
                        "outer_sandbox_writable_workspace_overlap",
                    },
                )

    def test_effective_environment_home_cannot_be_the_writable_state(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                home=state,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_external_git_state_overlap_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            git_dir = root / "metadata"
            subprocess.run(
                ["git", "init", "-q", f"--separate-git-dir={git_dir}", str(workspace)],
                check=True,
            )
            state = git_dir / "grok-state"
            state.mkdir(mode=0o700)
            with (
                mock.patch(
                    "agent_collab.sandbox.plan.resolve_scratch_anchor",
                    return_value=Path("/safe/scratch"),
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                resolve_session_plan(
                    policy=resolve_sandbox_policy("read-only", "none"),
                    workspace_path=workspace,
                    agents={
                        "xai": (
                            None,
                            {"HOME": str(root), "GROK_HOME": str(state)},
                            XaiCliSandboxAdapter(),
                        )
                    },
                    command_previews={"xai": ("grok", "-p")},
                    operator=SandboxOperatorConfig(),
                    audit=False,
                )
        self.assertEqual(raised.exception.code, "outer_sandbox_writable_git_overlap")

    def test_cached_auth_xai_key_and_configured_env_key_are_never_projected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / ".grok"
            state.mkdir(mode=0o700)
            (state / "auth.json").write_text("fixture-cached-secret", encoding="utf-8")
            (state / "config.toml").write_text(
                """
[model.fixture]
env_key = ["MODEL_FIXTURE_KEY", "MODEL_FALLBACK_KEY"]
api_key = "fixture-inline-secret"
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                environment={
                    "XAI_API_KEY": "fixture-xai-secret",
                    "MODEL_FIXTURE_KEY": "fixture-model-secret",
                },
            )
            self._run_compatibility(plan)
            rendered = repr(plan.spec.native_profile.summary) + repr(plan.spec.external_services)
        for secret in (
            "fixture-cached-secret",
            "fixture-inline-secret",
            "fixture-xai-secret",
            "fixture-model-secret",
        ):
            self.assertNotIn(secret, rendered)

    def test_malformed_configured_env_key_is_rejected_without_value_disclosure(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / ".grok"
            state.mkdir(mode=0o700)
            (state / "config.toml").write_text(
                '[model.fixture]\nenv_key = "NOT-A-VALID-NAME"\n',
                encoding="utf-8",
            )
            plan = self._plan(SandboxPolicy.READ_ONLY, state)
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        self.assertNotIn("NOT-A-VALID-NAME", str(raised.exception))

    def test_external_auth_provider_config_and_environment_are_rejected(self):
        shapes = (
            '[auth]\nauth_provider_command = "/usr/local/bin/provider"\n',
            "",
        )
        for index, contents in enumerate(shapes):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw:
                state = Path(raw) / ".grok"
                state.mkdir(mode=0o700)
                if contents:
                    (state / "config.toml").write_text(contents, encoding="utf-8")
                environment = (
                    {"GROK_AUTH_PROVIDER_COMMAND": "/usr/local/bin/provider"}
                    if not contents
                    else {}
                )
                plan = self._plan(
                    SandboxPolicy.READ_ONLY,
                    state,
                    environment=environment,
                )
                with self.assertRaises(SandboxFailure) as raised:
                    self._run_compatibility(plan)
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_backend_incompatible",
                )
                self.assertNotIn("/usr/local/bin/provider", str(raised.exception))

    def test_managed_and_requirements_configuration_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / ".grok"
            state.mkdir(mode=0o700)
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            (managed / "requirements.toml").write_text(
                '[sandbox]\nprofile = "strict"\n',
                encoding="utf-8",
            )
            plan = self._plan(SandboxPolicy.READ_ONLY, state)
            with (
                mock.patch(
                    "agent_collab.backends.xai_cli.sandbox.SYSTEM_MANAGED_CONFIG_ROOT",
                    managed,
                ),
                mock.patch(
                    "agent_collab.backends.xai_cli.sandbox.LEGACY_MANAGED_SETTINGS",
                    root / "absent",
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                self._run_compatibility(plan)
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_contained_extensions_are_accepted_and_external_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            state = root / ".grok"
            state.mkdir(mode=0o700)
            project_extension = workspace / ".grok" / "extensions"
            project_extension.mkdir(parents=True)
            (state / "config.toml").write_text(
                f"""
[skills]
paths = [{str(project_extension)!r}]

[mcp_servers.fixture]
command = "fixture-mcp"
args = ["--root", {str(workspace)!r}]
enabled = true
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
            )
            self._run_compatibility(plan)

            (state / "config.toml").write_text(
                '[plugins]\npaths = ["/outside/provider-boundary"]\n',
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        self.assertNotIn("/outside/provider-boundary", str(raised.exception))

    def test_mcp_cwd_and_interior_traversal_paths_fail_closed(self):
        configurations = (
            """
[mcp_servers.fixture]
command = "fixture-mcp"
cwd = "/outside/provider-boundary"
enabled = true
""",
            """
[mcp_servers.fixture]
command = "fixture-mcp"
args = ["foo/../../../etc/shadow"]
enabled = true
""",
            """
[ui.notifications]
hooks = [{ command = "notify foo/../../../tmp/out" }]
""",
            """
[mcp_servers.fixture]
command = "fixture-mcp"
args = [".."]
enabled = true
""",
            """
[mcp_servers.fixture]
command = "fixture-mcp"
args = ["--root=/etc"]
enabled = true
""",
            """
[mcp_servers.fixture]
command = "fixture-mcp"
args = ["-I/etc"]
enabled = true
""",
            """
[mcp_servers.fixture]
command = "fixture-mcp"
args = ["-I.."]
enabled = true
""",
            """
[ui.notifications]
hooks = [{ command = "notify --output=../../outside" }]
""",
        )
        for contents in configurations:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                workspace = root / "workspace"
                workspace.mkdir()
                state = root / ".grok"
                state.mkdir()
                (state / "config.toml").write_text(contents, encoding="utf-8")
                plan = self._plan(
                    SandboxPolicy.READ_ONLY,
                    state,
                    workspace=workspace,
                )
                with self.assertRaises(SandboxFailure) as raised:
                    self._run_compatibility(plan)
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_backend_incompatible",
                )
                self.assertNotIn("outside", str(raised.exception))
                self.assertNotIn("shadow", str(raised.exception))

    def test_project_mcp_json_is_validated_with_the_same_containment_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / ".grok"
            state.mkdir()
            project_mcp = workspace / ".mcp.json"
            project_mcp.write_text(
                """
{
  "mcpServers": {
    "fixture": {
      "command": "fixture-mcp",
      "args": ["inside/server.py", "--root=inside"],
      "cwd": "inside"
    }
  }
}
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
            )
            self._run_compatibility(plan)

            project_mcp.write_text(
                """
{
  "mcpServers": {
    "fixture": {
      "command": "fixture-mcp",
      "args": ["/etc/passwd"],
      "cwd": "/var/log"
    }
  }
}
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        self.assertNotIn("/etc/passwd", str(raised.exception))

    def test_effective_command_cwd_is_traced_and_its_project_mcp_is_validated(self):
        for relative in (False, True):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                workspace = root / "workspace"
                workspace.mkdir()
                state = root / ".grok"
                state.mkdir()
                effective_cwd = workspace / "nested" if relative else root / "external-project"
                effective_cwd.mkdir()
                (effective_cwd / ".mcp.json").write_text(
                    """
{
  "mcpServers": {
    "fixture": {
      "command": "fixture-mcp",
      "args": ["/etc/passwd"],
      "cwd": "/var/log"
    }
  }
}
""",
                    encoding="utf-8",
                )
                raw_cwd = "nested" if relative else str(effective_cwd)
                cwd_arguments = (f"--cwd={raw_cwd}",) if relative else ("--cwd", raw_cwd)
                plan = self._plan(
                    SandboxPolicy.READ_ONLY,
                    state,
                    workspace=workspace,
                    command=("grok", *cwd_arguments, "-p"),
                )
                self.assertIn(
                    effective_cwd,
                    [item.destination for item in plan.spec.provider_visible_paths],
                )
                with self.assertRaises(SandboxFailure) as raised:
                    self._run_compatibility(plan)
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_backend_incompatible",
                )
                self.assertNotIn("/etc/passwd", str(raised.exception))

    def test_effective_cwd_intermediate_extensions_and_ambient_trees_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            effective_cwd = workspace / "pkg" / "svc"
            effective_cwd.mkdir(parents=True)
            state = root / ".grok"
            state.mkdir()
            intermediate = workspace / "pkg"
            external = root / "external-skills"
            external.mkdir()
            project_grok = intermediate / ".grok"
            project_grok.mkdir()
            (project_grok / "skills").symlink_to(external, target_is_directory=True)
            command = ("grok", "--cwd=pkg/svc", "-p")

            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
                command=command,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

            (project_grok / "skills").unlink()
            (intermediate / ".claude" / "skills").mkdir(parents=True)
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
                command=command,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

            (state / "config.toml").write_text(
                """
[compat.claude]
skills = false
rules = false
agents = false
mcps = false
hooks = false
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
                command=command,
            )
            self._run_compatibility(plan)

    def test_relative_project_dependencies_use_effective_command_cwd(self):
        configurations = (
            (
                ".mcp.json",
                """
{
  "mcpServers": {
    "fixture": {
      "command": "bin/mcp-server",
      "args": ["payload/tool.py"],
      "cwd": "payload"
    }
  }
}
""",
            ),
            (
                ".grok/config.toml",
                """
[skills]
paths = ["skills-here"]

[ui.notifications]
hooks = [{ command = "hooks/notify.sh payload/input.json" }]
""",
            ),
        )
        for relative_path, contents in configurations:
            with self.subTest(relative_path=relative_path), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                workspace = root / "workspace"
                workspace.mkdir()
                state = root / ".grok"
                state.mkdir()
                effective_cwd = root / "external-project"
                effective_cwd.mkdir()
                config_path = effective_cwd / relative_path
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(contents, encoding="utf-8")
                plan = self._plan(
                    SandboxPolicy.READ_ONLY,
                    state,
                    workspace=workspace,
                    command=("grok", "--cwd", str(effective_cwd), "-p"),
                )
                with self.assertRaises(SandboxFailure) as raised:
                    self._run_compatibility(plan)
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_backend_incompatible",
                )
                self.assertNotIn("external-project", str(raised.exception))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            effective_cwd = workspace / "pkg"
            effective_cwd.mkdir(parents=True)
            state = root / ".grok"
            state.mkdir()
            (effective_cwd / ".mcp.json").write_text(
                """
{
  "mcpServers": {
    "fixture": {
      "command": "bin/mcp-server",
      "args": ["payload/tool.py"],
      "cwd": "payload"
    }
  }
}
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
                command=("grok", "--cwd=pkg", "-p"),
            )
            self._run_compatibility(plan)

    def test_debug_and_log_write_paths_must_remain_below_grok_home(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / ".grok"
            state.mkdir()
            contained = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
                command=("grok", "--debug-file", str(state / "debug.log"), "-p"),
                environment={"GROK_LOG_FILE": str(state / "grok.log")},
            )
            self._run_compatibility(contained)

            escaping = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
                command=("grok", "--debug-file", str(workspace / "debug.log"), "-p"),
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(escaping)
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_project_extension_symlink_and_ambient_compatibility_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            state = root / ".grok"
            state.mkdir(mode=0o700)
            project = workspace / ".grok"
            project.mkdir()
            target = root / "external-hooks"
            target.mkdir()
            (project / "hooks").symlink_to(target, target_is_directory=True)
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                workspace=workspace,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

            (project / "hooks").unlink()
            home = root / "home"
            (home / ".claude").mkdir(parents=True)
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                home=home,
                workspace=workspace,
            )
            with self.assertRaises(SandboxFailure) as raised:
                self._run_compatibility(plan)
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

            (state / "config.toml").write_text(
                """
[compat.claude]
skills = false
rules = false
agents = false
mcps = false
hooks = false
""",
                encoding="utf-8",
            )
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                home=home,
                workspace=workspace,
            )
            self._run_compatibility(plan)

    def test_cwd_and_agent_file_are_declared_read_only_with_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / ".grok"
            state.mkdir()
            agent = root / "profiles" / "reviewer.toml"
            agent.parent.mkdir()
            agent.write_text("name = 'reviewer'\n", encoding="utf-8")
            command = (
                "grok",
                "--cwd",
                str(workspace),
                "--agent",
                str(agent),
                "-p",
            )
            spec = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                command=command,
                workspace=workspace,
            ).spec
        self.assertEqual(
            [item.destination for item in spec.provider_visible_paths],
            [workspace, agent.parent],
        )
        self.assertTrue(
            all(item.access.value == "read_only" for item in spec.provider_visible_paths)
        )

    def test_native_argv_is_exact_and_conflicting_flags_are_removed(self):
        command = (
            "/usr/bin/grok",
            "--permission-mode=plan",
            "--sandbox",
            "strict",
            "--always-approve",
            "--allow",
            "Bash(git:*)",
            "--deny=write",
            "--model",
            "grok-4.5",
            "-p",
        )
        with tempfile.TemporaryDirectory() as raw:
            prepared = self._plan(
                SandboxPolicy.READ_ONLY,
                Path(raw),
                command=command,
            ).prepare_inner(command)
        self.assertEqual(
            prepared,
            (
                "/usr/bin/grok",
                "--model",
                "grok-4.5",
                "--permission-mode",
                "bypassPermissions",
                "--sandbox",
                "off",
                "-p",
            ),
        )

    def test_malformed_and_escaping_execution_shapes_fail_with_sanitized_code(self):
        commands = (
            ("/usr/bin/grok", "--permission-mode", "-p"),
            ("/usr/bin/grok", "--sandbox=", "-p"),
            ("/usr/bin/grok", "--leader-socket", "/private/socket", "-p"),
            ("/usr/bin/grok", "--prompt-file=/private/prompt", "-p"),
            ("/usr/bin/grok", "--worktree", "-p"),
            ("/usr/bin/grok", "--resume=session", "-p"),
            ("/usr/bin/grok", "--restore-code=true", "-p"),
            ("/usr/bin/grok", "--continue=session", "-p"),
            ("/usr/bin/grok", "-c=session", "-p"),
            ("/usr/bin/grok", "--fork-session=session", "-p"),
            ("/usr/bin/grok", "-rsid", "-p"),
            ("/usr/bin/grok", "-r/tmp/session", "-p"),
            ("/usr/bin/grok", "-csession", "-p"),
            ("/usr/bin/grok", "-cr", "-p"),
            ("/usr/bin/grok", "-wname", "-p"),
            ("/usr/bin/grok", "-w/tmp/worktree", "-p"),
            ("/usr/bin/grok", "--", "-p"),
        )
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as raw:
                plan = self._plan(SandboxPolicy.READ_ONLY, Path(raw), command=command)
                with self.assertRaises(SandboxFailure) as raised:
                    plan.prepare_inner(command)
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_backend_incompatible",
                )
                self.assertNotIn("/private", str(raised.exception))

    def test_none_preserves_exact_command_and_skips_incompatible_configuration(self):
        command = (
            "/usr/bin/grok",
            "--permission-mode",
            "plan",
            "--sandbox",
            "strict",
            "--leader-socket",
            "/private/socket",
            "-p",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / ".grok"
            state.mkdir()
            (state / "requirements.toml").write_text("[sandbox]\n", encoding="utf-8")
            plan = self._plan(SandboxPolicy.NONE, state, command=command)
            self.assertEqual(plan.prepare_inner(command), command)
            session = resolve_session_plan(
                policy=resolve_sandbox_policy("none", "none"),
                workspace_path=root,
                agents={
                    "xai": (
                        None,
                        {"HOME": "relative", "GROK_HOME": "relative"},
                        XaiCliSandboxAdapter(),
                    )
                },
                command_previews={"xai": command},
                operator=SandboxOperatorConfig(),
            )
        self.assertEqual(session.agents["xai"].prepare_inner(command), command)

    def test_settings_report_profile_auth_extensions_tmp_and_external_boundary(self):
        facts = describe_options(builtin_config())["backends"]["xai_cli"]["static"]["outer_sandbox"]
        self.assertEqual(facts["support"], "direct_process")
        self.assertIn("read-only", facts["policies"])
        profile = facts["provider_native_profile"]
        self.assertEqual(profile["permissions"], "bypassed_after_outer_ack")
        self.assertEqual(profile["native_sandbox"], "disabled_after_outer_ack")
        self.assertIn("warning_only", profile["legacy_shared_tmp"])
        self.assertIn("external services", facts["external_services"][0])

    def test_settings_preview_uses_exact_effective_inner_command(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                state,
                command=("grok", "--permission-mode", "plan", "--sandbox", "strict", "-p"),
            )
            session_plan = ResolvedSandboxSessionPlan(
                policy=plan.policy,
                engine="bubblewrap",
                establishment="required",
                agents={"xai_cli": plan},
            )
            config = builtin_config()
            merge_config_data(
                config,
                {"workflows": {"solo-xai": {"sequence": ["xai_cli"]}}},
            )
            normalized = normalize_start_options(config, "solo-xai")
            settings = build_session_settings(
                config,
                "solo-xai",
                normalized.backend_options,
                agent_backends={"xai_cli": "cli"},
                agent_options=normalized.agent_options,
                workdir=Path("/workspace"),
                sandbox_plan=session_plan,
            )
        preview = settings["agents"]["xai_cli"]["command_preview"]
        sandbox_index = preview.index("--sandbox")
        self.assertEqual(preview[sandbox_index + 1], "off")
        permission_index = preview.index("--permission-mode")
        self.assertEqual(preview[permission_index + 1], "bypassPermissions")
        self.assertNotIn("USER PROMPT", preview)


class XaiCliSandboxRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def _run_records(self, records: tuple[str, ...]):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        executable = root / "fake-grok"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            + "\n".join(f"print({record!r})" for record in records)
            + "\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        runner = XaiCliBackend().create_runner(
            AgentConfig(
                id="xai",
                type="xai",
                command=str(executable),
                args=["-p"],
            ),
            False,
            {},
        )
        events = []

        async def emit(event):
            events.append(event)

        outcome = await runner.run_turn("prompt", root, emit)
        return events, outcome

    async def test_end_turn_is_the_only_successful_process_terminal(self):
        events, outcome = await self._run_records(
            (
                '{"type":"text","data":"ready"}',
                '{"type":"end","stopReason":"EndTurn","sessionId":"session-ok"}',
            )
        )
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(outcome.provider_stop_reason, "EndTurn")
        self.assertTrue(any(event.text == "ready" for event in events))

    async def test_cancelled_unsuccessful_and_error_terminals_fail_conservatively(self):
        cases = (
            (
                ('{"type":"end","stopReason":"Cancelled","sessionId":"cancelled"}',),
                ("cancelled", "provider_turn_cancelled"),
            ),
            (
                ('{"type":"end","stopReason":"SafetyStop","sessionId":"failed"}',),
                ("failed", "provider_terminal_failure"),
            ),
            (
                ('{"type":"error","error":"private provider detail"}',),
                ("failed", "provider_terminal_failure"),
            ),
        )
        for records, expected in cases:
            with self.subTest(records=records):
                _events, outcome = await self._run_records(records)
                self.assertEqual((outcome.outcome, outcome.code), expected)

    async def test_duplicate_conflicting_and_missing_terminal_evidence(self):
        cases = (
            (
                (
                    '{"type":"end","stopReason":"EndTurn"}',
                    '{"type":"end","stopReason":"EndTurn"}',
                ),
                ("completed", None),
            ),
            (
                (
                    '{"type":"end","stopReason":"EndTurn"}',
                    '{"type":"end","stopReason":"Cancelled"}',
                ),
                ("failed", "provider_protocol_conflict"),
            ),
            (
                ('{"type":"text","data":"partial"}',),
                ("failed", "provider_output_incomplete"),
            ),
        )
        for records, expected in cases:
            with self.subTest(records=records):
                _events, outcome = await self._run_records(records)
                self.assertEqual((outcome.outcome, outcome.code), expected)

    async def test_dry_run_preview_matches_settings_profile_and_excludes_prompt(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            adapter = XaiCliSandboxAdapter()
            context = SandboxContext(
                Path("/workspace"),
                Path("/workspace"),
                {"HOME": str(state.parent), "GROK_HOME": str(state)},
                ("grok", "--permission-mode", "plan", "--sandbox", "strict", "-p"),
            )
            plan = ResolvedSandboxPlan(
                policy=ResolvedSandboxPolicy(
                    SandboxPolicy.READ_ONLY,
                    SandboxPolicy.READ_ONLY,
                    SandboxPolicySource.REQUEST,
                ),
                support=SandboxSupport.DIRECT_PROCESS,
                enforcement=SandboxEnforcement.OS_ENFORCED,
                context=context,
                spec=adapter.describe(context),
                adapter=adapter,
            )
            events = []
            runner = DryRunRunner(
                "xai",
                ["grok", "--permission-mode", "plan", "--sandbox", "strict", "-p"],
                sandbox_plan=plan,
            )

            async def emit(event):
                events.append(event)

            outcome = await runner.run_turn("PRIVATE PROMPT", Path("/workspace"), emit)
        self.assertEqual(outcome.outcome, "completed")
        preview = events[0].raw["command_preview"]
        self.assertIn("bypassPermissions", preview)
        self.assertIn("off", preview)
        self.assertNotIn("PRIVATE PROMPT", preview)


if __name__ == "__main__":
    unittest.main()
