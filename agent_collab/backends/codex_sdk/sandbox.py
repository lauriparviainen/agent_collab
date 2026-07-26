"""Codex SDK ownership, state, and worker-native profile for outer read-only."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

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
class CodexSdkSandboxAdapter:
    """Describe the complete Codex SDK worker tree and writable state."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        home = context.inherited_environment.get("HOME")
        default_home = Path(home).expanduser() if home else Path.home()
        codex_home = Path(
            context.inherited_environment.get("CODEX_HOME", str(default_home / ".codex"))
        ).expanduser()
        if not codex_home.is_absolute():
            codex_home = default_home / codex_home
        codex_home = codex_home.absolute()
        state_roots: List[StateRootSpec] = [
            StateRootSpec(
                label="Codex state",
                destination=codex_home,
                access=PathAccess.WRITABLE,
                persistence=Persistence.HOST,
                creation=CreationPolicy.MUST_EXIST,
            ),
        ]
        for index, sqlite_root in enumerate(_external_sqlite_roots(context, codex_home)):
            state_roots.append(
                StateRootSpec(
                    label=f"Codex SQLite state{'' if index == 0 else f' {index + 1}'}",
                    destination=sqlite_root,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.MUST_EXIST,
                )
            )
        return BackendSandboxSpec(
            support=SandboxSupport.SDK_WORKER,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=tuple(state_roots),
            environment=EnvironmentSpec(set_values={"CODEX_HOME": str(codex_home)}),
            native_profile=NativeSandboxProfile(
                summary={
                    "shape": "sdk_worker",
                    "approval_and_native_sandbox": "danger_full_access_after_outer_ack",
                    "runtime": "complete_sdk_and_app_server_in_worker",
                    "sqlite_roots": "declared_when_configured_outside_codex_home",
                },
                sdk_options={"sandbox": "danger-full-access"},
            ),
            compatibility=(
                CompatibilityCheck(
                    "sqlite_home_paths",
                    lambda: _validate_sqlite_roots(context, codex_home),
                ),
            ),
        )

    def prepare_inner(
        self,
        plan: ResolvedSandboxPlan,
        command: Sequence[str],
    ) -> Tuple[str, ...]:
        # SDK workers do not use a provider argv rewrite path.
        del plan
        return tuple(command)

    def worker_open_payload(
        self,
        *,
        options: Mapping[str, Any],
        workspace: Path,
        cwd: Path,
        agent_env: Mapping[str, str],
        codex_bin: Optional[str],
        verbose: bool,
    ) -> dict[str, Any]:
        mapped = dict(options)
        # Inside the proven outer boundary, force the permissive SDK sandbox.
        mapped["sandbox"] = "danger-full-access"
        payload: dict[str, Any] = {
            "backend": "codex_sdk",
            "workspace": str(workspace),
            # Effective agent cwd may differ from the session workspace root.
            "cwd": str(cwd),
            "options": mapped,
            "agent_env": dict(agent_env),
            "verbose": bool(verbose),
            "native": {"sandbox": "danger-full-access"},
        }
        if codex_bin:
            payload["codex_bin"] = codex_bin
        return payload

    def worker_open_payload_for_agent(
        self,
        *,
        agent_id: str,
        options: Mapping[str, Any],
        workspace: Path,
        cwd: Path,
        agent_env: Mapping[str, str],
        codex_bin: Optional[str],
        verbose: bool,
    ) -> dict[str, Any]:
        payload = self.worker_open_payload(
            options=options,
            workspace=workspace,
            cwd=cwd,
            agent_env=agent_env,
            codex_bin=codex_bin,
            verbose=verbose,
        )
        payload["agent_id"] = agent_id
        return payload


_SQLITE_ENV_KEYS = (
    "CODEX_SQLITE_HOME",
    "CODEX_SQLITE_PATH",
    "SQLITE_HOME",
)
_SQLITE_CONFIG_KEYS = re.compile(
    r"(?im)^\s*(?:sqlite_home|sqlite_path|sqlite_dir)\s*=\s*[\"']?([^\"'\n#]+)"
)


def _external_sqlite_roots(context: SandboxContext, codex_home: Path) -> Tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()
    # describe() must not raise: outer none and option discovery call it without
    # a read-only policy. Symlink rejection is only for READ_ONLY validation.
    for candidate in _configured_sqlite_paths(context, codex_home, reject_symlinked_config=False):
        try:
            absolute = candidate.expanduser()
            if not absolute.is_absolute():
                absolute = codex_home / absolute
            absolute = absolute.absolute()
        except (OSError, RuntimeError, ValueError):
            continue
        if absolute == codex_home or _is_within(codex_home, absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        roots.append(absolute)
    return tuple(roots)


def _configured_sqlite_paths(
    context: SandboxContext,
    codex_home: Path,
    *,
    reject_symlinked_config: bool,
) -> list[Path]:
    paths: list[Path] = []
    env = context.inherited_environment
    for key in _SQLITE_ENV_KEYS:
        raw = env.get(key) or os.environ.get(key)
        if isinstance(raw, str) and raw.strip():
            paths.append(Path(raw.strip()))
    config = codex_home / "config.toml"
    try:
        # is_symlink() does not follow the final component, so a broken symlink
        # is still detected (exists() would return False and skip the check).
        if config.is_symlink():
            if reject_symlinked_config:
                raise SandboxFailure(
                    "outer_sandbox_backend_incompatible",
                    "CODEX_HOME/config.toml must not be a symlink under outer read-only",
                    phase="validation",
                    remediation=(
                        "Replace the config.toml symlink with a regular file under CODEX_HOME.",
                    ),
                )
            return paths
        if config.is_file():
            text = config.read_text(encoding="utf-8", errors="replace")
            for match in _SQLITE_CONFIG_KEYS.finditer(text):
                value = match.group(1).strip()
                if value:
                    paths.append(Path(value))
    except SandboxFailure:
        raise
    except OSError:
        pass
    return paths


def _validate_sqlite_roots(context: SandboxContext, codex_home: Path) -> None:
    # READ_ONLY only: fail closed on symlinked config. Outer none skips this
    # CompatibilityCheck and keeps the historical in-process rollback.
    _configured_sqlite_paths(context, codex_home, reject_symlinked_config=True)
    for root in _external_sqlite_roots(context, codex_home):
        if not root.exists():
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                "Codex is configured with an external SQLite state root that does not exist",
                phase="validation",
                remediation=(
                    "Create the configured sqlite_home directory or keep SQLite under CODEX_HOME.",
                ),
            )
        if not root.is_dir():
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                "Codex external SQLite state root must be a directory",
                phase="validation",
            )


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
