"""Claude SDK ownership, state, and worker-native profile for outer read-only."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple

from ...sandbox.plan import ResolvedSandboxPlan
from ...sandbox.specs import (
    BackendSandboxSpec,
    CompatibilityCheck,
    CreationPolicy,
    EnvironmentSpec,
    NativeSandboxProfile,
    PathAccess,
    Persistence,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
    StateRootSpec,
)

LINUX_MANAGED_CONFIG_ROOT = Path("/etc/claude-code")


@dataclass(frozen=True)
class ClaudeSdkSandboxAdapter:
    """Describe the complete Claude SDK worker tree and writable state."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        inherited = context.inherited_environment
        raw_home = inherited.get("HOME")
        default_home = Path(raw_home).expanduser() if raw_home else Path.home()
        configured = inherited.get("CLAUDE_CONFIG_DIR")
        # describe() must not raise: discovery and outer none call it. Pathological
        # CLAUDE_CONFIG_DIR values are rejected only by the READ_ONLY check below.
        state = _resolve_claude_state(configured, default_home)

        environment: dict[str, str] = {
            "DISABLE_AUTOUPDATER": "1",
        }
        unset_names: tuple[str, ...] = ()
        if configured is None:
            # Preserve Claude's default lookup so legacy ~/.claude.json is not
            # relocated into the writable state mount.
            unset_names = ("CLAUDE_CONFIG_DIR",)
        elif _usable_config_dir(configured):
            environment["CLAUDE_CONFIG_DIR"] = str(state)

        return BackendSandboxSpec(
            support=SandboxSupport.SDK_WORKER,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=(
                StateRootSpec(
                    label="Claude state",
                    destination=state,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.MUST_EXIST,
                ),
            ),
            environment=EnvironmentSpec(
                set_values=environment,
                unset_names=unset_names,
                private_tmp_names=("CLAUDE_CODE_TMPDIR",),
            ),
            native_profile=NativeSandboxProfile(
                summary={
                    "shape": "sdk_worker",
                    "permissions": "bypassPermissions_after_outer_ack",
                    "setting_sources": "none",
                    "mcp": "strict_empty_configuration",
                    "managed_configuration": "incompatible",
                    "legacy_state": "read_only_home_claude_json",
                    "runtime": "complete_sdk_and_claude_code_in_worker",
                },
                sdk_options={
                    "permission_mode": "bypassPermissions",
                    "strict_mcp_config": True,
                    "mcp_servers": {},
                },
            ),
            compatibility=(
                CompatibilityCheck("managed_configuration", _reject_managed_configuration),
                CompatibilityCheck(
                    "claude_config_dir",
                    lambda: _validate_claude_state(configured, state, default_home),
                ),
            ),
        )

    def prepare_inner(
        self,
        plan: ResolvedSandboxPlan,
        command: Sequence[str],
    ) -> Tuple[str, ...]:
        del plan
        return tuple(command)

    def worker_open_payload_for_agent(
        self,
        *,
        agent_id: str,
        options: Mapping[str, Any],
        workspace: Path,
        cwd: Path,
        agent_env: Mapping[str, str],
        verbose: bool,
    ) -> dict[str, Any]:
        mapped = dict(options)
        # Inside the proven outer boundary, force non-interactive approval bypass.
        mapped["permission_mode"] = "bypassPermissions"
        return {
            "backend": "claude_sdk",
            "agent_id": agent_id,
            "workspace": str(workspace),
            "cwd": str(cwd),
            "options": mapped,
            "agent_env": dict(agent_env),
            "verbose": bool(verbose),
            "native": {"permission_mode": "bypassPermissions"},
        }


def _usable_config_dir(configured: object) -> bool:
    return (
        isinstance(configured, str)
        and bool(configured.strip())
        and configured.strip()
        not in {
            ".",
            "..",
        }
    )


def _resolve_claude_state(configured: object, default_home: Path) -> Path:
    if configured is None or not _usable_config_dir(configured):
        # Safe default for discovery/describe; invalid values fail closed later.
        return (default_home / ".claude").absolute()
    state = Path(str(configured).strip()).expanduser()
    if not state.is_absolute():
        state = default_home / state
    return state.absolute()


def _validate_claude_state(
    configured: object,
    state: Path,
    default_home: Path,
) -> None:
    if configured is not None and not _usable_config_dir(configured):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "CLAUDE_CONFIG_DIR must be a non-empty config directory path",
            phase="validation",
            remediation=(
                "Set CLAUDE_CONFIG_DIR to an absolute or home-relative Claude config "
                "directory, or unset it to use ~/.claude.",
            ),
        )
    home = default_home.absolute()
    if state == Path("/") or state == home:
        raise SandboxFailure(
            "outer_sandbox_writable_too_broad",
            "Claude state root may not be the filesystem root or home directory",
            phase="validation",
            remediation=(
                "Point CLAUDE_CONFIG_DIR at a dedicated directory such as ~/.claude, "
                "not HOME itself.",
            ),
        )


def _reject_managed_configuration() -> None:
    # Same fail-closed surface as claude_cli: only actual managed settings and
    # drop-ins are incompatible, not an empty /etc/claude-code directory.
    root = LINUX_MANAGED_CONFIG_ROOT
    for name in ("managed-settings.json", "managed-mcp.json"):
        if os.path.lexists(root / name):
            _managed_configuration_incompatible()
    dropins = root / "managed-settings.d"
    if not os.path.lexists(dropins):
        return
    if dropins.is_symlink() or not dropins.is_dir():
        _managed_configuration_incompatible()
    try:
        with os.scandir(dropins) as entries:
            if any(
                not entry.name.startswith(".") and entry.name.endswith(".json") for entry in entries
            ):
                _managed_configuration_incompatible()
    except OSError:
        _managed_configuration_incompatible()


def _managed_configuration_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "admin-managed Claude configuration is incompatible with outer read-only",
        phase="validation",
        remediation=(
            "Remove or relocate managed Claude settings under /etc/claude-code, "
            "or start with sandbox='none'.",
        ),
    )
