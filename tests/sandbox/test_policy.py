from __future__ import annotations

import unittest
from pathlib import Path

from agent_collab.sandbox.plan import ResolvedSandboxPlan
from agent_collab.sandbox.policy import resolve_sandbox_policy
from agent_collab.sandbox.specs import (
    BackendSandboxSpec,
    NoLocalEffectsSandboxAdapter,
    SandboxContext,
    SandboxEnforcement,
    SandboxFailure,
    SandboxPolicy,
    SandboxPolicySource,
    SandboxSupport,
    UnsupportedSandboxAdapter,
)


class SandboxPolicyTests(unittest.TestCase):
    def test_resolution_precedence_and_sources(self):
        cases = (
            (None, "none", None, SandboxPolicy.NONE, SandboxPolicySource.CONFIGURED_DEFAULT),
            (
                "read-only",
                "none",
                None,
                SandboxPolicy.READ_ONLY,
                SandboxPolicySource.REQUEST,
            ),
            (
                None,
                "none",
                "read-only",
                SandboxPolicy.READ_ONLY,
                SandboxPolicySource.INSTALLATION_OVERRIDE,
            ),
            (
                "read-only",
                "none",
                "read-only",
                SandboxPolicy.READ_ONLY,
                SandboxPolicySource.INSTALLATION_OVERRIDE,
            ),
        )
        for requested, default, override, effective, source in cases:
            with self.subTest(requested=requested, override=override):
                resolved = resolve_sandbox_policy(requested, default, override)
                self.assertIs(resolved.effective, effective)
                self.assertIs(resolved.source, source)

    def test_override_conflict_is_stable_fail_closed_error(self):
        with self.assertRaises(SandboxFailure) as raised:
            resolve_sandbox_policy("none", "none", "read-only")
        self.assertEqual(raised.exception.code, "outer_sandbox_override_conflict")

    def test_legacy_session_records_none_and_read_only_override_blocks(self):
        resolved = resolve_sandbox_policy(None, "read-only", legacy_session=True)
        self.assertIs(resolved.effective, SandboxPolicy.NONE)
        self.assertIs(resolved.source, SandboxPolicySource.LEGACY_SESSION)
        with self.assertRaises(SandboxFailure) as raised:
            resolve_sandbox_policy(None, "none", "read-only", legacy_session=True)
        self.assertEqual(raised.exception.code, "outer_sandbox_legacy_session")

    def test_unknown_policy_has_field_validation_code(self):
        with self.assertRaises(SandboxFailure) as raised:
            resolve_sandbox_policy("prompt-only", "none")
        self.assertEqual(raised.exception.code, "outer_sandbox_policy_invalid")

    def test_prompt_augmentation_is_policy_owned_and_none_is_byte_exact(self):
        context = SandboxContext(Path("/work tree"), Path("/work tree/subdir"), {})
        adapter = UnsupportedSandboxAdapter()
        spec = BackendSandboxSpec(
            support=SandboxSupport.DIRECT_PROCESS,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
        )
        read_only = ResolvedSandboxPlan(
            policy=resolve_sandbox_policy("read-only", "none"),
            support=SandboxSupport.DIRECT_PROCESS,
            enforcement=SandboxEnforcement.OS_ENFORCED,
            context=context,
            spec=spec,
            adapter=adapter,
        )
        prompt = read_only.render_prompt("USER", Path("/private/scratch"))
        self.assertIn("Workspace root: /work tree", prompt)
        self.assertIn("Current directory: /work tree/subdir", prompt)
        self.assertIn("$TMPDIR (/private/scratch/tmp)", prompt)
        self.assertTrue(prompt.endswith("\nUSER"))

        none = ResolvedSandboxPlan(
            policy=resolve_sandbox_policy("none", "read-only"),
            support=SandboxSupport.DIRECT_PROCESS,
            enforcement=SandboxEnforcement.DISABLED,
            context=context,
            spec=spec,
            adapter=adapter,
        )
        self.assertEqual(none.render_prompt("USER\n", None), "USER\n")

    def test_no_local_effects_prompt_never_promises_scratch(self):
        adapter = NoLocalEffectsSandboxAdapter()
        context = SandboxContext(Path("/work"), Path("/work"), {})
        spec = adapter.describe(context)
        plan = ResolvedSandboxPlan(
            policy=resolve_sandbox_policy("read-only", "none"),
            support=SandboxSupport.NO_LOCAL_EFFECTS,
            enforcement=SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS,
            context=context,
            spec=spec,
            adapter=adapter,
        )
        prompt = plan.render_prompt("USER", None)
        self.assertIn("no local file", prompt)
        self.assertNotIn("$TMPDIR", prompt)
