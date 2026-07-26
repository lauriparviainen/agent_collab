from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_collab.backends.codex_cli.sandbox import CodexCliSandboxAdapter
from agent_collab.sandbox.plan import ResolvedSandboxPlan
from agent_collab.sandbox.specs import (
    ResolvedSandboxPolicy,
    SandboxContext,
    SandboxEnforcement,
    SandboxPolicy,
    SandboxPolicySource,
    SandboxSupport,
)


class CodexCliSandboxAdapterTests(unittest.TestCase):
    def _plan(self, policy: SandboxPolicy, state: Path) -> ResolvedSandboxPlan:
        adapter = CodexCliSandboxAdapter()
        context = SandboxContext(
            Path("/workspace"),
            Path("/workspace"),
            {"CODEX_HOME": str(state), "HOME": str(state.parent)},
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

    def test_complete_effective_codex_home_is_persistent_writable_state(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            spec = self._plan(SandboxPolicy.READ_ONLY, state).spec
            self.assertEqual(spec.state_roots[0].destination, state)
            self.assertEqual(spec.state_roots[0].persistence.value, "host_persistent")
            self.assertEqual(spec.environment.set_values["CODEX_HOME"], str(state))

    def test_read_only_removes_conflicts_and_adds_composite_bypass_once(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(SandboxPolicy.READ_ONLY, Path(raw))
            command = (
                "/usr/bin/codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "-a",
                "never",
                "-c",
                'sandbox_mode="read-only"',
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
            )
            prepared = plan.prepare_inner(command)
            self.assertEqual(prepared.count("--dangerously-bypass-approvals-and-sandbox"), 1)
            self.assertNotIn("--sandbox", prepared)
            self.assertNotIn("-a", prepared)
            self.assertNotIn('sandbox_mode="read-only"', prepared)
            self.assertEqual(prepared[-1], "--dangerously-bypass-approvals-and-sandbox")

    def test_none_preserves_provider_native_command_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(SandboxPolicy.NONE, Path(raw))
            command = ("/usr/bin/codex", "exec", "--sandbox", "read-only", "--json")
            self.assertEqual(plan.prepare_inner(command), command)
