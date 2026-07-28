from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab import backends
from agent_collab.backends.antigravity_cli.backend import AntigravityCliBackend
from agent_collab.backends.antigravity_cli.sandbox import AntigravityCliSandboxAdapter
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


class AntigravityCliSandboxAdapterTests(unittest.TestCase):
    def _plan(
        self,
        policy: SandboxPolicy,
        home: Path,
        command: tuple[str, ...] = ("/usr/bin/agy", "-p"),
    ) -> ResolvedSandboxPlan:
        adapter = AntigravityCliSandboxAdapter()
        context = SandboxContext(
            Path("/workspace"),
            Path("/workspace"),
            {"HOME": str(home)},
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

    def test_registry_exposes_only_completed_cli_slices_without_common_backend_branch(self):
        adapter = backends.get_backend("antigravity", "cli").sandbox_adapter
        self.assertIsInstance(adapter, AntigravityCliSandboxAdapter)
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
                    self.assertEqual(
                        SandboxPolicy.READ_ONLY in spec.policies,
                        (agent_type, backend_id) in supported or agent_type == "mock",
                    )
        for path in Path("agent_collab/sandbox").glob("*.py"):
            self.assertNotIn("antigravity_cli", path.read_text(encoding="utf-8"))

    def test_complete_dot_gemini_state_maps_home_and_reports_keyring_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            spec = self._plan(SandboxPolicy.READ_ONLY, home).spec

        root = spec.state_roots[0]
        self.assertEqual(root.destination, home / ".gemini")
        self.assertEqual(root.persistence.value, "host_persistent")
        self.assertEqual(root.access.value, "writable")
        self.assertEqual(spec.environment.set_values["HOME"], str(home))
        self.assertEqual(
            spec.external_services,
            ("OS keyring (external service outside filesystem boundary)",),
        )
        self.assertNotIn("secret", repr(spec.external_services).lower())

    def test_relative_home_is_rejected_without_inventing_state_relocation(self):
        adapter = AntigravityCliSandboxAdapter()
        spec = adapter.describe(
            SandboxContext(Path("/workspace"), Path("/workspace"), {"HOME": "relative"})
        )
        with self.assertRaises(SandboxFailure) as raised:
            spec.compatibility[0].check()
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_common_path_contract_rejects_missing_symlink_permissions_and_ownership(self):
        adapter = AntigravityCliSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            missing = adapter.describe(
                SandboxContext(Path("/w"), Path("/w"), {"HOME": str(home)})
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(missing)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_missing")

            target = home / "target"
            target.mkdir(mode=0o700)
            state = home / ".gemini"
            state.symlink_to(target, target_is_directory=True)
            linked = adapter.describe(
                SandboxContext(Path("/w"), Path("/w"), {"HOME": str(home)})
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(linked)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_symlink")

            state.unlink()
            state.mkdir(mode=0o770)
            state.chmod(0o770)
            unsafe = adapter.describe(
                SandboxContext(Path("/w"), Path("/w"), {"HOME": str(home)})
            ).state_roots[0]
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(unsafe)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_permissions")

            state.chmod(0o700)
            with self.assertRaises(SandboxFailure) as raised:
                resolve_state_root(unsafe, daemon_uid=os.getuid() + 1)
            self.assertEqual(raised.exception.code, "outer_sandbox_path_ownership")

    def test_workspace_equal_state_is_rejected_as_overly_broad(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve()
            state = home / ".gemini"
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
                    workspace_path=state,
                    agents={
                        "antigravity": (None, {"HOME": str(home)}, AntigravityCliSandboxAdapter())
                    },
                    command_previews={"antigravity": ("agy", "-p")},
                    operator=SandboxOperatorConfig(),
                    audit=False,
                )
        self.assertEqual(raised.exception.code, "outer_sandbox_writable_workspace_overlap")

    def test_agentapi_materialization_accepts_missing_tree_and_safe_existing_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            state = home / ".gemini"
            state.mkdir(mode=0o700)
            spec = self._plan(SandboxPolicy.READ_ONLY, home).spec
            for check in spec.compatibility:
                check.check()

            helper = state / "antigravity-cli" / "bin" / "agentapi"
            helper.parent.mkdir(parents=True, mode=0o700)
            helper.write_text("fixture", encoding="utf-8")
            helper.chmod(0o700)
            for check in spec.compatibility:
                check.check()

    def test_agentapi_materialization_rejects_symlink_and_nonwritable_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            state = home / ".gemini"
            helper_dir = state / "antigravity-cli" / "bin"
            helper_dir.mkdir(parents=True, mode=0o700)
            target = state / "helper-target"
            target.write_text("fixture", encoding="utf-8")
            (helper_dir / "agentapi").symlink_to(target)
            spec = self._plan(SandboxPolicy.READ_ONLY, home).spec
            with self.assertRaises(SandboxFailure) as raised:
                spec.compatibility[1].check()
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

            (helper_dir / "agentapi").unlink()
            helper_dir.chmod(0o500)
            with self.assertRaises(SandboxFailure) as raised:
                spec.compatibility[1].check()
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_add_dir_separated_equals_relative_identity_and_duplicates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            cwd = root / "workspace"
            cwd.mkdir()
            absolute = root / "absolute"
            adapter = AntigravityCliSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    cwd,
                    cwd,
                    {"HOME": str(root)},
                    (
                        "agy",
                        "--add-dir",
                        "relative",
                        f"--add-dir={absolute}",
                        "--add-dir=relative",
                        "-p",
                    ),
                )
            )
        self.assertEqual(
            [item.destination for item in spec.provider_visible_paths],
            [cwd / "relative", absolute],
        )
        self.assertTrue(
            all(item.access.value == "read_only" for item in spec.provider_visible_paths)
        )

    def test_malformed_add_dir_values_fail_closed(self):
        adapter = AntigravityCliSandboxAdapter()
        for command in (("agy", "--add-dir", "-p"), ("agy", "--add-dir=", "-p")):
            spec = adapter.describe(
                SandboxContext(
                    Path("/workspace"),
                    Path("/workspace"),
                    {"HOME": "/home/operator"},
                    command,
                )
            )
            with self.subTest(command=command), self.assertRaises(SandboxFailure) as raised:
                spec.compatibility[-1].check()
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_native_argv_is_exact_and_conflicting_flags_are_removed(self):
        command = (
            "/usr/bin/agy",
            "--mode=plan",
            "--dangerously-skip-permissions",
            "--sandbox",
            "--model",
            "gemini",
            "--add-dir=/visible",
            "-p",
        )
        with tempfile.TemporaryDirectory() as raw:
            prepared = self._plan(SandboxPolicy.READ_ONLY, Path(raw), command).prepare_inner(
                command
            )
        self.assertEqual(
            prepared,
            (
                "/usr/bin/agy",
                "--model",
                "gemini",
                "--add-dir=/visible",
                "--dangerously-skip-permissions",
                "--mode",
                "accept-edits",
                "--sandbox=false",
                "-p",
            ),
        )

    def test_malformed_owned_flags_fail_with_sanitized_code(self):
        for command in (
            ("/usr/bin/agy", "--mode", "-p"),
            ("/usr/bin/agy", "--mode=", "-p"),
            ("/usr/bin/agy", "--sandbox=", "-p"),
            ("/usr/bin/agy", "--", "-p"),
        ):
            with tempfile.TemporaryDirectory() as raw:
                plan = self._plan(SandboxPolicy.READ_ONLY, Path(raw), command)
                with self.assertRaises(SandboxFailure) as raised:
                    plan.prepare_inner(command)
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
            self.assertNotIn(str(Path.home()), str(raised.exception))

    def test_none_preserves_exact_provider_command_and_skips_compatibility(self):
        command = (
            "/usr/bin/agy",
            "--mode",
            "plan",
            "--sandbox",
            "--add-dir",
            "/private/path",
            "-p",
        )
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(SandboxPolicy.NONE, Path(raw), command)
            self.assertEqual(plan.prepare_inner(command), command)
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            session = resolve_session_plan(
                policy=resolve_sandbox_policy("none", "none"),
                workspace_path=workspace,
                agents={
                    "antigravity": (
                        None,
                        {"HOME": "relative"},
                        AntigravityCliSandboxAdapter(),
                    )
                },
                command_previews={"antigravity": ("agy", "--add-dir=", "-p")},
                operator=SandboxOperatorConfig(),
            )
            self.assertEqual(
                session.agents["antigravity"].prepare_inner(("agy", "--add-dir=", "-p")),
                ("agy", "--add-dir=", "-p"),
            )

    def test_health_settings_project_native_profile_and_keyring_boundary(self):
        facts = describe_options(builtin_config())["backends"]["antigravity_cli"]["static"][
            "outer_sandbox"
        ]
        self.assertEqual(facts["support"], "direct_process")
        self.assertIn("read-only", facts["policies"])
        self.assertEqual(
            facts["provider_native_profile"]["agentapi_helper"],
            "materialization_checked",
        )
        self.assertEqual(
            facts["external_services"],
            ["OS keyring (external service outside filesystem boundary)"],
        )

    def test_settings_preview_uses_exact_effective_inner_command(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            plan = self._plan(
                SandboxPolicy.READ_ONLY,
                home,
                ("agy", "--mode", "plan", "--sandbox", "-p"),
            )
            session_plan = ResolvedSandboxSessionPlan(
                policy=plan.policy,
                engine="bubblewrap",
                establishment="required",
                agents={"antigravity_cli": plan},
            )
            config = builtin_config()
            merge_config_data(
                config,
                {"workflows": {"solo-antigravity": {"sequence": ["antigravity_cli"]}}},
            )
            normalized = normalize_start_options(config, "solo-antigravity")
            settings = build_session_settings(
                config,
                "solo-antigravity",
                normalized.backend_options,
                agent_backends={"antigravity_cli": "cli"},
                agent_options=normalized.agent_options,
                workdir=Path("/workspace"),
                sandbox_plan=session_plan,
            )
        preview = settings["agents"]["antigravity_cli"]["command_preview"]
        self.assertIn("--dangerously-skip-permissions", preview)
        self.assertIn("--sandbox=false", preview)
        self.assertNotIn("--sandbox", preview)
        self.assertNotIn("USER PROMPT", preview)


class AntigravityCliSandboxRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_exit_structural_fragment_is_not_a_completed_turn(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "fake-agy"
            executable.write_text(
                "#!/usr/bin/env python3\nprint('}')\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            backend = AntigravityCliBackend()
            runner = backend.create_runner(
                AgentConfig(
                    id="antigravity",
                    type="antigravity",
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

        self.assertEqual(
            (outcome.outcome, outcome.code, outcome.process_exit_code),
            ("failed", "provider_empty_response", 0),
        )
        self.assertFalse(
            any(event.source == "antigravity" and event.type == "message" for event in events)
        )

    async def test_zero_exit_reported_tool_failure_remains_failed_turn(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "fake-agy"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "print('useful partial response')\n"
                "print('TOOL_ERROR: agentapi materialization failed')\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            backend = AntigravityCliBackend()
            runner = backend.create_runner(
                AgentConfig(
                    id="antigravity",
                    type="antigravity",
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

        self.assertEqual((outcome.outcome, outcome.code), ("failed", "provider_terminal_failure"))
        self.assertEqual(outcome.process_exit_code, 0)
        failure = [event for event in events if event.raw.get("fatal")]
        self.assertEqual(len(failure), 1)
        self.assertNotIn("agentapi materialization failed", failure[0].text)

    async def test_dry_run_preview_matches_settings_profile_and_excludes_prompt(self):
        adapter = AntigravityCliSandboxAdapter()
        context = SandboxContext(
            Path("/workspace"),
            Path("/workspace"),
            {"HOME": "/home/operator"},
            ("agy", "--mode", "plan", "--sandbox", "-p"),
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
            "antigravity",
            ["agy", "--mode", "plan", "--sandbox", "-p"],
            sandbox_plan=plan,
        )

        async def emit(event):
            events.append(event)

        outcome = await runner.run_turn("PRIVATE PROMPT", Path("/workspace"), emit)
        self.assertEqual(outcome.outcome, "completed")
        preview = events[0].raw["command_preview"]
        self.assertIn("--dangerously-skip-permissions", preview)
        self.assertIn("--sandbox=false", preview)
        self.assertNotIn("PRIVATE PROMPT", preview)
