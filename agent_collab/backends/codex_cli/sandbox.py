"""Codex CLI ownership/state facts for the common outer sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Tuple

from ...sandbox.plan import ResolvedSandboxPlan
from ...sandbox.specs import (
    BackendSandboxSpec,
    CreationPolicy,
    EnvironmentSpec,
    NativeSandboxProfile,
    PathAccess,
    Persistence,
    SandboxContext,
    SandboxPolicy,
    SandboxSupport,
    StateRootSpec,
)
from ..common.cli import remove_config_value, remove_flag


@dataclass(frozen=True)
class CodexCliSandboxAdapter:
    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        home = context.inherited_environment.get("HOME")
        default_home = Path(home).expanduser() if home else Path.home()
        codex_home = Path(
            context.inherited_environment.get("CODEX_HOME", str(default_home / ".codex"))
        ).expanduser()
        if not codex_home.is_absolute():
            codex_home = default_home / codex_home
        codex_home = codex_home.absolute()
        return BackendSandboxSpec(
            support=SandboxSupport.DIRECT_PROCESS,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=(
                StateRootSpec(
                    label="Codex state",
                    destination=codex_home,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.MUST_EXIST,
                ),
            ),
            environment=EnvironmentSpec(set_values={"CODEX_HOME": str(codex_home)}),
            native_profile=NativeSandboxProfile(
                summary={
                    "approval_and_native_sandbox": "dangerously_bypassed_after_outer_ack",
                },
                command=("--dangerously-bypass-approvals-and-sandbox",),
            ),
        )

    def prepare_inner(
        self,
        plan: ResolvedSandboxPlan,
        command: Sequence[str],
    ) -> Tuple[str, ...]:
        if plan.policy.effective is SandboxPolicy.NONE:
            return tuple(command)
        result = list(command)
        for flag, has_value in (
            ("--dangerously-bypass-approvals-and-sandbox", False),
            ("--full-auto", False),
            ("--sandbox", True),
            ("-s", True),
            ("--approval-policy", True),
            ("--ask-for-approval", True),
            ("-a", True),
        ):
            result = remove_flag(result, flag, has_value=has_value)
        for key in ("sandbox_mode", "approval_policy"):
            result = remove_config_value(result, key)
        result.append("--dangerously-bypass-approvals-and-sandbox")
        return tuple(result)
