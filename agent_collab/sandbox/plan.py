"""Resolve immutable per-agent sandbox plans before session creation."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .paths import (
    GitProtectionRecord,
    MountOperation,
    ResolvedSandboxPath,
    audit_aliases,
    component_contains,
    create_private_directory,
    discover_session_git,
    normalize_mounts,
    resolve_accounting_peer_roots,
    resolve_effective_cwd,
    resolve_state_root,
    resolve_workspace,
)
from .specs import (
    BackendSandboxSpec,
    CreationPolicy,
    PathAccess,
    PathOrigin,
    Persistence,
    ResolvedSandboxPolicy,
    SandboxAdapter,
    SandboxContext,
    SandboxEnforcement,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
    StateRootSpec,
)


@dataclass(frozen=True)
class SandboxOperatorConfig:
    extra_readable_dirs: Tuple[Path, ...] = ()
    extra_writable_dirs: Tuple[Path, ...] = ()
    alias_audit_max_entries: int = 1_000_000
    alias_audit_timeout_seconds: int = 10
    scratch_root: Optional[Path] = None
    agent_collab_home: Optional[Path] = None


def remove_created_session_private_roots(paths: Sequence[Path]) -> None:
    """Best-effort removal of CREATE_PRIVATE_DIRECTORY trees and empty parents."""

    import shutil
    import stat as stat_mod

    def _onerror(func: Any, target: str, _exc_info: Any) -> None:
        try:
            os.chmod(target, stat_mod.S_IRWXU)
            func(target)
        except Exception:
            pass

    parents: set[Path] = set()
    for path in paths:
        try:
            shutil.rmtree(path, onerror=_onerror)
        except Exception:
            pass
        parents.add(path.parent)
    for parent in parents:
        try:
            if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


@dataclass(frozen=True)
class ResolvedSandboxPlan:
    policy: ResolvedSandboxPolicy
    support: SandboxSupport
    enforcement: SandboxEnforcement
    context: SandboxContext
    spec: BackendSandboxSpec
    adapter: SandboxAdapter
    operations: Tuple[MountOperation, ...] = ()
    git_records: Tuple[GitProtectionRecord, ...] = ()
    accounting_peer_roots: Tuple[Path, ...] = ()
    scratch_anchor: Optional[Path] = None
    git_metadata_scope: str = "session_root"
    alias_audit_max_entries: int = 1_000_000
    alias_audit_timeout_seconds: int = 10
    # Session-private roots created during plan resolve (CREATE_PRIVATE_DIRECTORY).
    created_session_private_roots: Tuple[Path, ...] = ()
    alias_audit_log: Optional[Callable[[str], None]] = field(
        default=None,
        compare=False,
        repr=False,
    )

    def cleanup_created_session_private_roots(self) -> None:
        """Best-effort removal of session-private directories this plan created."""

        remove_created_session_private_roots(self.created_session_private_roots)

    def prepare_inner(self, command: Sequence[str]) -> Tuple[str, ...]:
        return self.adapter.prepare_inner(self, command)

    def render_prompt(self, user_prompt: str, scratch: Optional[Path]) -> str:
        if self.policy.effective is SandboxPolicy.NONE:
            return user_prompt
        if self.enforcement is SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS:
            block = (
                "FILESYSTEM POLICY\n"
                "This backend exposes no local file, tool, callback, scratch, or child-process "
                "surface. No OS sandbox is applicable.\n"
            )
        else:
            if scratch is None:
                raise SandboxFailure(
                    "outer_sandbox_scratch_anchor_invalid",
                    "an OS-enforced prompt requires resolved private scratch",
                    phase="launch",
                )
            temp = scratch / "tmp"
            block = (
                "FILESYSTEM POLICY\n"
                f"Workspace root: {self.context.workspace}\n"
                f"Current directory: {self.context.cwd}\n"
                "Access: OS-enforced read-only.\n"
                f"Use $TMPDIR ({temp}) for temporary files and command output.\n"
                "Do not attempt workspace edits; describe proposed changes in your response.\n"
            )
        if self.spec.backend_prompt_augmentation:
            block += self.spec.backend_prompt_augmentation.rstrip() + "\n"
        return block + "\n" + user_prompt

    def settings(self, *, full: bool) -> dict[str, Any]:
        writable = [
            {
                "label": operation.labels[0],
                "access": "persistent writable"
                if operation.persistence is Persistence.HOST
                else "private writable",
                **({"destination": str(operation.destination)} if full else {}),
                "origin": operation.origins[0].value,
                "persistence": operation.persistence.value,
            }
            for operation in self.operations
            if operation.access is PathAccess.WRITABLE
        ]
        return {
            "support": self.support.value,
            "enforcement": self.enforcement.value,
            "provider_native_profile": dict(self.spec.native_profile.summary),
            "writable_exceptions": writable
            if full
            else [{"label": item["label"], "access": item["access"]} for item in writable],
            "git_metadata_scope": self.git_metadata_scope,
            "external_services": list(self.spec.external_services),
        }


@dataclass(frozen=True)
class ResolvedSandboxSessionPlan:
    policy: ResolvedSandboxPolicy
    engine: str
    establishment: str
    agents: Mapping[str, ResolvedSandboxPlan]

    def __post_init__(self) -> None:
        object.__setattr__(self, "agents", MappingProxyType(dict(self.agents)))

    def settings(self) -> dict[str, Optional[str]]:
        return {
            **self.policy.to_dict(),
            "engine": self.engine,
            "establishment": self.establishment,
        }

    def cleanup_created_session_private_roots(self) -> None:
        for plan in self.agents.values():
            plan.cleanup_created_session_private_roots()


def resolve_session_plan(
    *,
    policy: ResolvedSandboxPolicy,
    workspace_path: Path,
    agents: Mapping[str, tuple[Optional[str], Mapping[str, str], SandboxAdapter]],
    operator: SandboxOperatorConfig,
    command_previews: Optional[Mapping[str, Sequence[str]]] = None,
    audit: bool = True,
    alias_audit_log: Optional[Callable[[str], None]] = None,
) -> ResolvedSandboxSessionPlan:
    workspace = resolve_workspace(workspace_path)
    previews = command_previews or {}
    resolved: dict[str, ResolvedSandboxPlan] = {}
    # Paths created for the agent currently being resolved (not yet owned by a plan).
    in_progress_created: list[Path] = []
    try:
        for agent_id, (configured_cwd, environment, adapter) in agents.items():
            in_progress_created = []
            cwd = resolve_effective_cwd(workspace, configured_cwd)
            inherited = os.environ.copy()
            inherited.update(environment)
            context = SandboxContext(
                workspace.destination,
                cwd,
                inherited,
                tuple(previews.get(agent_id, ())),
            )
            spec = adapter.describe(context)
            if policy.effective not in spec.policies:
                raise SandboxFailure(
                    "outer_sandbox_unsupported",
                    f"agent {agent_id!r} does not support outer sandbox policy "
                    f"{policy.effective.value!r}",
                    remediation=(
                        "Select sandbox='none' only when installation policy permits it.",
                    ),
                )
            if policy.effective is SandboxPolicy.NONE:
                resolved[agent_id] = ResolvedSandboxPlan(
                    policy=policy,
                    support=spec.support,
                    enforcement=SandboxEnforcement.DISABLED,
                    context=context,
                    spec=spec,
                    adapter=adapter,
                )
                continue
            if spec.support is SandboxSupport.UNSUPPORTED:
                raise SandboxFailure(
                    "outer_sandbox_unsupported",
                    f"agent {agent_id!r} has no Stage 1 read-only sandbox adapter",
                )
            if spec.support is SandboxSupport.NO_LOCAL_EFFECTS:
                # Versioned capability audit only — no Bubblewrap mounts.
                for check in spec.compatibility:
                    try:
                        check.check()
                    except SandboxFailure:
                        raise
                    except Exception as exc:
                        raise SandboxFailure(
                            "outer_sandbox_backend_incompatible",
                            f"backend compatibility check {check.name!r} failed",
                        ) from exc
                resolved[agent_id] = ResolvedSandboxPlan(
                    policy=policy,
                    support=spec.support,
                    enforcement=SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS,
                    context=context,
                    spec=spec,
                    adapter=adapter,
                )
                continue

            for check in spec.compatibility:
                try:
                    check.check()
                except SandboxFailure:
                    raise
                except Exception as exc:
                    raise SandboxFailure(
                        "outer_sandbox_backend_incompatible",
                        f"backend compatibility check {check.name!r} failed",
                    ) from exc

            declarations: list[ResolvedSandboxPath] = []
            for state_spec in spec.state_roots:
                resolved_path = resolve_state_root(state_spec)
                declarations.append(resolved_path)
                if resolved_path.created and resolved_path.persistence is Persistence.SESSION:
                    in_progress_created.append(resolved_path.destination)
            for visible_spec in spec.provider_visible_paths:
                declarations.append(resolve_state_root(visible_spec))
            for label, access, paths in (
                ("Operator readable", PathAccess.READ_ONLY, operator.extra_readable_dirs),
                ("Operator writable", PathAccess.WRITABLE, operator.extra_writable_dirs),
            ):
                for path in paths:
                    declarations.append(
                        resolve_state_root(
                            StateRootSpec(
                                label=label,
                                destination=path,
                                access=access,
                                persistence=Persistence.HOST,
                                creation=CreationPolicy.MUST_EXIST,
                                origin=PathOrigin.OPERATOR,
                            )
                        )
                    )
            git = discover_session_git(workspace)
            operations = normalize_mounts(workspace, declarations, git.records)
            accounting_peer_roots = resolve_accounting_peer_roots(
                spec.accounting_peer_roots,
                operations,
            )
            scratch_anchor = resolve_scratch_anchor(
                operator,
                workspace=workspace.destination,
                overmounts=tuple(item.destination for item in operations),
            )
            if audit:
                audit_aliases(
                    operations,
                    git.records,
                    accounting_peer_roots=accounting_peer_roots,
                    max_entries=operator.alias_audit_max_entries,
                    timeout_seconds=operator.alias_audit_timeout_seconds,
                    log=alias_audit_log,
                )
            resolved[agent_id] = ResolvedSandboxPlan(
                policy=policy,
                support=spec.support,
                enforcement=SandboxEnforcement.OS_ENFORCED,
                context=context,
                spec=spec,
                adapter=adapter,
                operations=operations,
                git_records=git.records,
                accounting_peer_roots=accounting_peer_roots,
                scratch_anchor=scratch_anchor,
                alias_audit_max_entries=operator.alias_audit_max_entries,
                alias_audit_timeout_seconds=operator.alias_audit_timeout_seconds,
                created_session_private_roots=tuple(in_progress_created),
                alias_audit_log=alias_audit_log,
            )
            # Ownership transferred to the plan; do not double-clean on later
            # agents' failures via in_progress_created.
            in_progress_created = []

        enforcement = {item.enforcement for item in resolved.values()}
        if policy.effective is SandboxPolicy.NONE:
            engine = "none"
            establishment = "disabled"
        elif enforcement == {SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS}:
            engine = "not_applicable"
            establishment = "not_applicable"
        elif SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS in enforcement:
            engine = "mixed"
            establishment = "required"
        else:
            engine = "bubblewrap"
            establishment = "required"
        return ResolvedSandboxSessionPlan(policy, engine, establishment, resolved)
    except Exception:
        # Roll back roots already owned by successfully resolved earlier agents,
        # plus any in-progress CREATE_PRIVATE dirs for the failing agent
        # (including empty shared random parents).
        for plan in resolved.values():
            plan.cleanup_created_session_private_roots()
        remove_created_session_private_roots(in_progress_created)
        raise


def resolve_scratch_anchor(
    operator: SandboxOperatorConfig,
    *,
    workspace: Path,
    overmounts: Sequence[Path],
) -> Path:
    candidates: list[Path] = []
    if operator.scratch_root is not None:
        candidates.append(operator.scratch_root.expanduser())
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            candidates.append(Path(runtime) / "agent-collab" / "sandbox")
        home = operator.agent_collab_home
        if home is None:
            home = Path(os.environ.get("AGENT_COLLAB_HOME", "~/.agent-collab")).expanduser()
        candidates.append(home / "runtime" / "sandbox")
    for candidate in candidates:
        try:
            absolute = candidate.absolute()
            if (
                component_contains(Path("/tmp"), absolute)
                or component_contains(Path("/var/tmp"), absolute)
                or component_contains(workspace, absolute)
                or any(component_contains(destination, absolute) for destination in overmounts)
            ):
                continue
            parent = absolute
            while not parent.exists() and parent.parent != parent:
                parent = parent.parent
            if parent.is_symlink() or os.stat(parent).st_uid != os.getuid():
                continue
            create_private_directory(absolute)
            if absolute.resolve(strict=True) != absolute:
                continue
            value = os.stat(absolute, follow_symlinks=False)
            if value.st_uid != os.getuid() or value.st_mode & 0o077:
                continue
            return absolute
        except (OSError, SandboxFailure):
            continue
    raise SandboxFailure(
        "outer_sandbox_scratch_anchor_invalid",
        "no safe daemon-owned sandbox scratch anchor is available",
        remediation=("Set system.sandbox_scratch_root to a safe absolute private directory.",),
    )
