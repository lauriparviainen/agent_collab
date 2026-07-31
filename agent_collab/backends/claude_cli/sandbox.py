"""Claude CLI ownership, state, and native-profile facts for the outer sandbox."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Sequence, Tuple

from ..claude_common import claude_accounting_peer_root
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


EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
TRANSIENT_NATIVE_SETTINGS = '{"sandbox":{"enabled":false}}'
LINUX_MANAGED_CONFIG_ROOT = Path("/etc/claude-code")


@dataclass(frozen=True)
class ClaudeCliSandboxAdapter:
    """Describe the complete Claude CLI process tree and writable state."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        inherited = context.inherited_environment
        raw_home = inherited.get("HOME")
        default_home = Path(raw_home).expanduser() if raw_home else Path.home()
        configured = inherited.get("CLAUDE_CONFIG_DIR")
        state = (
            Path(configured).expanduser() if configured is not None else default_home / ".claude"
        )
        environment = {
            "DISABLE_AUTOUPDATER": "1",
        }
        unset_names: tuple[str, ...] = ()
        if configured is None:
            # Preserve Claude's default lookup. In particular, do not relocate
            # the legacy ~/.claude.json file into the writable state mount.
            unset_names = ("CLAUDE_CONFIG_DIR",)
        else:
            environment["CLAUDE_CONFIG_DIR"] = str(state)

        compatibility = [
            CompatibilityCheck("managed_configuration", _reject_managed_configuration),
        ]
        if context.command_preview:
            compatibility.append(
                CompatibilityCheck(
                    "provider_command",
                    lambda: _prepare_read_only_command(context.command_preview),
                )
            )

        return BackendSandboxSpec(
            support=SandboxSupport.DIRECT_PROCESS,
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
            accounting_peer_roots=(claude_accounting_peer_root(context),),
            provider_visible_paths=tuple(_additional_directories(context)),
            environment=EnvironmentSpec(
                set_values=environment,
                unset_names=unset_names,
                private_tmp_names=("CLAUDE_CODE_TMPDIR",),
            ),
            native_profile=NativeSandboxProfile(
                summary={
                    "permissions": "dangerously_skipped_after_outer_ack",
                    "native_sandbox": "disabled_by_transient_settings_after_outer_ack",
                    "mcp": "strict_empty_configuration",
                    "managed_configuration": "incompatible",
                    "legacy_state": "read_only",
                },
                command=(
                    "--dangerously-skip-permissions",
                    "--strict-mcp-config",
                    "--mcp-config",
                    EMPTY_MCP_CONFIG,
                    "--settings",
                    TRANSIENT_NATIVE_SETTINGS,
                ),
            ),
            compatibility=tuple(compatibility),
        )

    def prepare_inner(
        self,
        plan: ResolvedSandboxPlan,
        command: Sequence[str],
    ) -> Tuple[str, ...]:
        if plan.policy.effective is SandboxPolicy.NONE:
            return tuple(command)
        return _prepare_read_only_command(command)


def _additional_directories(context: SandboxContext) -> Iterable[StateRootSpec]:
    command = context.command_preview
    seen: set[Path] = set()
    for value in _option_values(command, "--add-dir", variadic=True):
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = context.cwd / candidate
        candidate = candidate.absolute()
        if candidate in seen:
            continue
        seen.add(candidate)
        yield StateRootSpec(
            label="Claude additional directory",
            destination=candidate,
            access=PathAccess.READ_ONLY,
            persistence=Persistence.HOST,
            creation=CreationPolicy.MUST_EXIST,
        )


def _option_values(
    command: Sequence[str],
    flag: str,
    *,
    variadic: bool,
) -> Tuple[str, ...]:
    values: list[str] = []
    prefix = f"{flag}="
    index = 1
    while index < len(command):
        item = command[index]
        if item == "--":
            break
        if item.startswith(prefix):
            value = item[len(prefix) :]
            if not value:
                _command_incompatible()
            values.append(value)
            index += 1
            continue
        if item != flag:
            index += 1
            continue
        index += 1
        start = len(values)
        while index < len(command):
            value = command[index]
            if value == "--" or value.startswith("-"):
                break
            values.append(value)
            index += 1
            if not variadic:
                break
        if len(values) == start:
            _command_incompatible()
    return tuple(values)


def _prepare_read_only_command(command: Sequence[str]) -> Tuple[str, ...]:
    if not command:
        _command_incompatible()
    result: list[str] = [command[0]]
    index = 1
    while index < len(command):
        item = command[index]
        if item == "--":
            if index + 1 != len(command):
                _command_incompatible()
            index += 1
            continue
        if item in {"--settings", "--managed-settings"} or item.startswith(
            ("--settings=", "--managed-settings=")
        ):
            # Arbitrary settings cannot be merged safely with the transient
            # sandbox override, and managed settings outrank that override.
            _command_incompatible()
        if item in {
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
            "--strict-mcp-config",
        } or item.startswith(
            (
                "--dangerously-skip-permissions=",
                "--allow-dangerously-skip-permissions=",
                "--strict-mcp-config=",
            )
        ):
            index += 1
            continue
        if item == "--permission-mode":
            if index + 1 >= len(command) or command[index + 1].startswith("-"):
                _command_incompatible()
            index += 2
            continue
        if item.startswith("--permission-mode="):
            index += 1
            continue
        if item == "--mcp-config":
            index += 1
            start = index
            while index < len(command):
                value = command[index]
                if value == "--" or value.startswith("-"):
                    break
                index += 1
            if index == start:
                _command_incompatible()
            continue
        if item.startswith("--mcp-config="):
            if item == "--mcp-config=":
                _command_incompatible()
            index += 1
            continue
        result.append(item)
        index += 1

    result.extend(
        (
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--mcp-config",
            EMPTY_MCP_CONFIG,
            "--settings",
            TRANSIENT_NATIVE_SETTINGS,
            "--",
        )
    )
    return tuple(result)


def _reject_managed_configuration() -> None:
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


def _command_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "Claude CLI arguments conflict with the required outer-sandbox profile",
        remediation=(
            "Remove explicit Claude settings, managed settings, and malformed "
            "permission or MCP arguments.",
        ),
    )


def _managed_configuration_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "Claude admin-managed configuration is incompatible with outer read-only",
        remediation=(
            "Use a Claude installation without managed settings or managed MCP for "
            "this outer-sandbox session.",
        ),
    )
