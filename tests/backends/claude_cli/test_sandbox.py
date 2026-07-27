from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab import backends
from agent_collab.backends.claude_cli import sandbox as claude_sandbox
from agent_collab.backends.claude_cli.sandbox import (
    EMPTY_MCP_CONFIG,
    TRANSIENT_NATIVE_SETTINGS,
    ClaudeCliSandboxAdapter,
)
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
from agent_collab.config import builtin_config
from agent_collab.options import build_session_settings, describe_options, normalize_start_options
from agent_collab.runners import DryRunRunner


class ClaudeCliSandboxAdapterTests(unittest.TestCase):
    def _plan(
        self,
        policy: SandboxPolicy,
        state: Path,
        command: tuple[str, ...] = ("/usr/bin/claude", "-p"),
    ) -> ResolvedSandboxPlan:
        adapter = ClaudeCliSandboxAdapter()
        context = SandboxContext(
            Path("/workspace"),
            Path("/workspace"),
            {
                "CLAUDE_CONFIG_DIR": str(state),
                "HOME": str(state.parent),
            },
            command,
        )
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

    def test_registry_selects_typed_claude_adapter_without_common_backend_branch(self):
        adapter = backends.get_backend("claude", "cli").sandbox_adapter
        self.assertIsInstance(adapter, ClaudeCliSandboxAdapter)
        self.assertNotIn("claude_cli", Path("agent_collab/sandbox/plan.py").read_text())

    def test_every_backend_outside_completed_cli_stages_remains_fail_closed(self):
        supported = {
            ("antigravity", "cli"),
            ("antigravity", "sdk"),
            ("claude", "cli"),
            ("claude", "sdk"),
            ("codex", "cli"),
            ("codex", "sdk"),
            ("xai", "cli"),
            ("xai", "sdk"),
        }
        for agent_type in backends.registered_agent_types():
            for backend_id in backends.registered_backends(agent_type):
                spec = backends.get_backend(agent_type, backend_id).sandbox_adapter.describe(
                    SandboxContext(Path("/w"), Path("/w"), {})
                )
                with self.subTest(agent_type=agent_type, backend_id=backend_id):
                    if (agent_type, backend_id) in supported:
                        self.assertIn(SandboxPolicy.READ_ONLY, spec.policies)
                    else:
                        self.assertNotIn(SandboxPolicy.READ_ONLY, spec.policies)

    def test_explicit_complete_state_is_persistent_writable_and_environment_owned(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            spec = self._plan(SandboxPolicy.READ_ONLY, state).spec

        root = spec.state_roots[0]
        self.assertEqual(root.destination, state)
        self.assertEqual(root.persistence.value, "host_persistent")
        self.assertEqual(root.access.value, "writable")
        self.assertEqual(spec.environment.set_values["CLAUDE_CONFIG_DIR"], str(state))
        self.assertEqual(spec.environment.set_values["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(spec.environment.private_tmp_names, ("CLAUDE_CODE_TMPDIR",))

    def test_options_health_facts_advertise_claude_direct_process_profile(self):
        facts = describe_options(builtin_config())["backends"]["claude_cli"]["static"][
            "outer_sandbox"
        ]
        self.assertEqual(facts["support"], "direct_process")
        self.assertIn("read-only", facts["policies"])
        self.assertEqual(
            facts["provider_native_profile"]["native_sandbox"],
            "disabled_by_transient_settings_after_outer_ack",
        )

    def test_default_state_uses_home_dot_claude_without_relocating_legacy_file(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            adapter = ClaudeCliSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    Path("/workspace"),
                    Path("/workspace"),
                    {"HOME": str(home)},
                    ("/usr/bin/claude", "-p"),
                )
            )

        self.assertEqual(spec.state_roots[0].destination, home / ".claude")
        self.assertNotIn("CLAUDE_CONFIG_DIR", spec.environment.set_values)
        self.assertIn("CLAUDE_CONFIG_DIR", spec.environment.unset_names)
        self.assertNotIn(home / ".claude.json", [item.destination for item in spec.state_roots])

    def test_state_mount_covers_session_env_but_not_legacy_claude_json(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            state = root / "home" / ".claude"
            workspace.mkdir(mode=0o700)
            state.mkdir(parents=True, mode=0o700)
            legacy = root / "home" / ".claude.json"
            legacy.write_text("{}\n", encoding="utf-8")
            adapter = ClaudeCliSandboxAdapter()
            with mock.patch(
                "agent_collab.sandbox.plan.resolve_scratch_anchor",
                return_value=Path("/safe/scratch"),
            ):
                plan = resolve_session_plan(
                    policy=resolve_sandbox_policy("read-only", "none"),
                    workspace_path=workspace,
                    agents={
                        "claude": (
                            None,
                            {"HOME": str(root / "home")},
                            adapter,
                        )
                    },
                    command_previews={"claude": ("claude", "-p")},
                    operator=SandboxOperatorConfig(),
                    audit=False,
                ).agents["claude"]

        writable = [item for item in plan.operations if item.access.value == "writable"]
        self.assertEqual([item.destination for item in writable], [state])
        self.assertTrue((state / "session-env").is_relative_to(writable[0].destination))
        self.assertFalse(legacy.is_relative_to(writable[0].destination))

    def test_common_path_contract_rejects_missing_symlink_permissions_and_ownership(self):
        adapter = ClaudeCliSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            missing = root / "missing"
            missing_spec = adapter.describe(
                SandboxContext(Path("/w"), Path("/w"), {"CLAUDE_CONFIG_DIR": str(missing)})
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(missing_spec)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_missing")

            target = root / "target"
            target.mkdir(mode=0o700)
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            link_spec = adapter.describe(
                SandboxContext(Path("/w"), Path("/w"), {"CLAUDE_CONFIG_DIR": str(link)})
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(link_spec)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_symlink")

            target.chmod(0o770)
            permission_spec = adapter.describe(
                SandboxContext(Path("/w"), Path("/w"), {"CLAUDE_CONFIG_DIR": str(target)})
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(permission_spec)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_permissions")

            target.chmod(0o700)
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(permission_spec, daemon_uid=os.getuid() + 1)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_ownership")

    def test_common_breadth_contract_rejects_daemon_home(self):
        adapter = ClaudeCliSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir(mode=0o700)
            with self.assertRaises(SandboxFailure) as raised:
                resolve_session_plan(
                    policy=resolve_sandbox_policy("read-only", "none"),
                    workspace_path=workspace,
                    agents={
                        "claude": (
                            None,
                            {"CLAUDE_CONFIG_DIR": str(Path.home())},
                            adapter,
                        )
                    },
                    command_previews={"claude": ("claude", "-p")},
                    operator=SandboxOperatorConfig(
                        scratch_root=Path(raw) / "runtime" / "sandbox",
                    ),
                    audit=False,
                )
        self.assertEqual(raised.exception.code, "outer_sandbox_writable_too_broad")

    def test_read_only_command_is_exact_and_conflicts_are_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(SandboxPolicy.READ_ONLY, Path(raw))
            command = (
                "/usr/bin/claude",
                "-p",
                "--permission-mode",
                "plan",
                "--dangerously-skip-permissions",
                "--allow-dangerously-skip-permissions",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{"ambient":{"command":"local"}}}',
                "--model",
                "sonnet",
            )
            prepared = plan.prepare_inner(command)

        self.assertEqual(
            prepared,
            (
                "/usr/bin/claude",
                "-p",
                "--model",
                "sonnet",
                "--dangerously-skip-permissions",
                "--strict-mcp-config",
                "--mcp-config",
                EMPTY_MCP_CONFIG,
                "--settings",
                TRANSIENT_NATIVE_SETTINGS,
                "--",
            ),
        )

    def test_settings_managed_settings_and_malformed_mcp_fail_with_stable_sanitized_code(self):
        commands = (
            ("/usr/bin/claude", "-p", "--settings", "/private/config.json"),
            ("/usr/bin/claude", "-p", "--managed-settings", '{"sandbox":{}}'),
            ("/usr/bin/claude", "-p", "--mcp-config"),
        )
        with tempfile.TemporaryDirectory() as raw:
            for command in commands:
                with self.subTest(command=command[2]):
                    plan = self._plan(SandboxPolicy.READ_ONLY, Path(raw), command)
                    with self.assertRaises(SandboxFailure) as raised:
                        plan.prepare_inner(command)
                    self.assertEqual(
                        raised.exception.code,
                        "outer_sandbox_backend_incompatible",
                    )
                    self.assertNotIn("/private", str(raised.exception))

    def test_additional_directories_are_declared_read_only_and_resolved_from_cwd(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            cwd = root / "workspace"
            cwd.mkdir()
            adapter = ClaudeCliSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    cwd,
                    cwd,
                    {"HOME": str(root)},
                    (
                        "claude",
                        "-p",
                        "--add-dir",
                        "relative",
                        str(root / "absolute"),
                        "--model",
                        "sonnet",
                    ),
                )
            )

        self.assertEqual(
            [item.destination for item in spec.provider_visible_paths],
            [cwd / "relative", root / "absolute"],
        )
        self.assertTrue(
            all(item.access.value == "read_only" for item in spec.provider_visible_paths)
        )

    def test_file_and_dropin_managed_configuration_fail_before_provider(self):
        adapter = ClaudeCliSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw)
            state = managed / "state"
            workspace = managed / "workspace"
            state.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            for relative in (
                Path("managed-settings.json"),
                Path("managed-mcp.json"),
                Path("managed-settings.d") / "10-policy.json",
            ):
                with self.subTest(relative=str(relative)):
                    for item in managed.glob("managed-*"):
                        if item.is_dir():
                            for child in item.iterdir():
                                child.unlink()
                            item.rmdir()
                        else:
                            item.unlink()
                    target = managed / relative
                    target.parent.mkdir(exist_ok=True)
                    target.write_text("{}\n", encoding="utf-8")
                    with (
                        mock.patch.object(
                            claude_sandbox,
                            "LINUX_MANAGED_CONFIG_ROOT",
                            managed,
                        ),
                        self.assertRaises(SandboxFailure) as raised,
                    ):
                        resolve_session_plan(
                            policy=resolve_sandbox_policy("read-only", "none"),
                            workspace_path=workspace,
                            agents={
                                "claude": (
                                    None,
                                    {"CLAUDE_CONFIG_DIR": str(state)},
                                    adapter,
                                )
                            },
                            command_previews={"claude": ("claude", "-p")},
                            operator=SandboxOperatorConfig(
                                scratch_root=managed / "runtime" / "sandbox",
                            ),
                            audit=False,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "outer_sandbox_backend_incompatible",
                    )

    def test_none_preserves_provider_command_byte_for_byte_and_skips_compatibility(self):
        command = (
            "/usr/bin/claude",
            "-p",
            "--permission-mode",
            "plan",
            "--settings",
            "/private/settings.json",
            "--mcp-config",
            "/private/mcp.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(SandboxPolicy.NONE, Path(raw), command)
            self.assertEqual(plan.prepare_inner(command), command)

    def test_full_session_settings_project_effective_inner_profile_separately(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(SandboxPolicy.READ_ONLY, Path(raw))
            session_plan = ResolvedSandboxSessionPlan(
                policy=plan.policy,
                engine="bubblewrap",
                establishment="required",
                agents={"claude_cli": plan},
            )
            config = builtin_config()
            normalized = normalize_start_options(config, "solo")
            settings = build_session_settings(
                config,
                "solo",
                normalized.backend_options,
                agent_backends={"claude_cli": "cli"},
                agent_options=normalized.agent_options,
                workdir=Path("/workspace"),
                sandbox_plan=session_plan,
            )

        agent = settings["agents"]["claude_cli"]
        preview = agent["command_preview"]
        self.assertIn("--dangerously-skip-permissions", preview)
        self.assertNotIn("--permission-mode", preview)
        self.assertNotIn("USER PROMPT", preview)
        self.assertEqual(agent["outer_sandbox"]["support"], "direct_process")
        self.assertEqual(agent["outer_sandbox"]["enforcement"], "os_enforced")
        self.assertEqual(
            agent["outer_sandbox"]["provider_native_profile"]["mcp"],
            "strict_empty_configuration",
        )


class ClaudeCliSandboxProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_reports_prompt_free_effective_inner_command(self):
        adapter = ClaudeCliSandboxAdapter()
        context = SandboxContext(
            Path("/workspace"),
            Path("/workspace"),
            {"CLAUDE_CONFIG_DIR": "/state"},
            ("claude", "-p", "--permission-mode", "default"),
        )
        spec = adapter.describe(context)
        plan = ResolvedSandboxPlan(
            policy=ResolvedSandboxPolicy(
                SandboxPolicy.READ_ONLY,
                SandboxPolicy.READ_ONLY,
                SandboxPolicySource.REQUEST,
            ),
            support=SandboxSupport.DIRECT_PROCESS,
            enforcement=SandboxEnforcement.OS_ENFORCED,
            context=context,
            spec=spec,
            adapter=adapter,
        )
        events = []
        runner = DryRunRunner(
            "claude",
            ["claude", "-p", "--permission-mode", "default"],
            sandbox_plan=plan,
        )

        async def emit(event):
            events.append(event)

        outcome = await runner.run_turn(
            "PRIVATE PROMPT",
            Path("/workspace"),
            emit,
        )

        self.assertEqual(outcome.outcome, "completed")
        preview = events[0].raw["command_preview"]
        self.assertIn("--dangerously-skip-permissions", preview)
        self.assertNotIn("--permission-mode", preview)
        self.assertNotIn("PRIVATE PROMPT", preview)
