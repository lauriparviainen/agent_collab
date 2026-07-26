"""Antigravity CLI ownership, state, and native-profile facts."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterable, Sequence, Tuple

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


@dataclass(frozen=True)
class AntigravityCliSandboxAdapter:
    """Describe the complete Antigravity CLI process tree and writable state."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        raw_home = context.inherited_environment.get("HOME")
        home = Path(raw_home).expanduser() if raw_home else Path.home()
        state = home / ".gemini"

        compatibility = [
            CompatibilityCheck(
                "state_lookup",
                lambda: _validate_state_contract(home, state),
            ),
            CompatibilityCheck(
                "agentapi_materialization",
                lambda: _validate_agentapi_materialization(state),
            ),
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
                    label="Antigravity state",
                    destination=state,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.MUST_EXIST,
                ),
            ),
            provider_visible_paths=tuple(_additional_directories(context)),
            environment=EnvironmentSpec(set_values={"HOME": str(state.parent)}),
            native_profile=NativeSandboxProfile(
                summary={
                    "permissions": "dangerously_skipped_after_outer_ack",
                    "mode": "accept_edits_after_outer_ack",
                    "native_sandbox": "disabled_after_outer_ack",
                    "agentapi_helper": "materialization_checked",
                    "keyring": "external_service_outside_filesystem_boundary",
                },
                command=(
                    "--dangerously-skip-permissions",
                    "--mode",
                    "accept-edits",
                    "--sandbox=false",
                ),
            ),
            compatibility=tuple(compatibility),
            external_services=("OS keyring (external service outside filesystem boundary)",),
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
    seen: set[Path] = set()
    for value in _option_values(context.command_preview, "--add-dir", strict=False):
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = context.cwd / candidate
        candidate = candidate.absolute()
        if candidate in seen:
            continue
        seen.add(candidate)
        yield StateRootSpec(
            label="Antigravity additional directory",
            destination=candidate,
            access=PathAccess.READ_ONLY,
            persistence=Persistence.HOST,
            creation=CreationPolicy.MUST_EXIST,
        )


def _option_values(command: Sequence[str], flag: str, *, strict: bool) -> Tuple[str, ...]:
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
                if strict:
                    _command_incompatible()
                index += 1
                continue
            values.append(value)
            index += 1
            continue
        if item != flag:
            index += 1
            continue
        if index + 1 >= len(command) or command[index + 1].startswith("-"):
            if strict:
                _command_incompatible()
            index += 1
            continue
        values.append(command[index + 1])
        index += 2
    return tuple(values)


def _prepare_read_only_command(command: Sequence[str]) -> Tuple[str, ...]:
    if not command:
        _command_incompatible()
    _option_values(command, "--add-dir", strict=True)
    result: list[str] = [command[0]]
    index = 1
    while index < len(command):
        item = command[index]
        if item == "--":
            _command_incompatible()
        if item == "--dangerously-skip-permissions":
            index += 1
            continue
        if item.startswith("--dangerously-skip-permissions="):
            index += 1
            continue
        if item in {"--mode"}:
            if index + 1 >= len(command) or command[index + 1].startswith("-"):
                _command_incompatible()
            index += 2
            continue
        if item.startswith("--mode="):
            if item == "--mode=":
                _command_incompatible()
            index += 1
            continue
        if item == "--sandbox":
            index += 1
            continue
        if item.startswith("--sandbox="):
            if item == "--sandbox=":
                _command_incompatible()
            index += 1
            continue
        result.append(item)
        index += 1

    insertion = len(result)
    for position, item in enumerate(result):
        if item in {"-p", "--print", "--prompt"}:
            insertion = position
            break
    result[insertion:insertion] = [
        "--dangerously-skip-permissions",
        "--mode",
        "accept-edits",
        "--sandbox=false",
    ]
    return tuple(result)


def _validate_agentapi_materialization(state: Path) -> None:
    """Fail before launch when the CLI cannot safely materialize ``agentapi``."""

    try:
        root_value = os.lstat(state)
    except FileNotFoundError:
        # The common MUST_EXIST resolver owns the stable missing-path result.
        return
    except OSError:
        _helper_incompatible()
    if stat.S_ISLNK(root_value.st_mode):
        # Preserve the common resolver's more specific symlink result.
        return
    if not stat.S_ISDIR(root_value.st_mode):
        return

    current = state
    for name in ("antigravity-cli", "bin"):
        _require_materialization_directory(current)
        candidate = current / name
        try:
            value = os.lstat(candidate)
        except FileNotFoundError:
            return
        except OSError:
            _helper_incompatible()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            _helper_incompatible()
        current = candidate

    _require_materialization_directory(current)
    helper = current / "agentapi"
    try:
        value = os.lstat(helper)
    except FileNotFoundError:
        return
    except OSError:
        _helper_incompatible()
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        _helper_incompatible()
    if value.st_uid != os.getuid() or value.st_mode & 0o022:
        _helper_incompatible()


def _validate_state_contract(home: Path, state: Path) -> None:
    if not home.is_absolute() or state.name != ".gemini" or state.parent != home:
        _state_incompatible()


def _require_materialization_directory(path: Path) -> None:
    try:
        value = os.lstat(path)
    except OSError:
        _helper_incompatible()
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or value.st_mode & 0o022
        or value.st_mode & 0o300 != 0o300
    ):
        _helper_incompatible()


def _command_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "Antigravity CLI arguments conflict with the required outer-sandbox profile",
        remediation=(
            "Remove malformed Antigravity mode, sandbox, permission, or add-dir arguments.",
        ),
    )


def _state_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "Antigravity state must retain the ~/.gemini lookup contract",
        remediation=("Set HOME to the absolute parent of the complete .gemini state directory.",),
    )


def _helper_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "Antigravity agentapi helper cannot be safely materialized in provider state",
        remediation=(
            "Repair the owner-only .gemini/antigravity-cli/bin path or select sandbox='none'.",
        ),
    )
