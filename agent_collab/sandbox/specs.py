"""Immutable contracts shared by sandbox policy, engines, and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

JSONScalar = Optional[object]


class SandboxPolicy(str, Enum):
    READ_ONLY = "read-only"
    NONE = "none"


class SandboxPolicySource(str, Enum):
    REQUEST = "request"
    CONFIGURED_DEFAULT = "configured_default"
    INSTALLATION_OVERRIDE = "installation_override"
    LEGACY_SESSION = "legacy_session"


class SandboxSupport(str, Enum):
    DIRECT_PROCESS = "direct_process"
    SDK_WORKER = "sdk_worker"
    NO_LOCAL_EFFECTS = "no_local_effects"
    UNSUPPORTED = "unsupported"


class SandboxEnforcement(str, Enum):
    OS_ENFORCED = "os_enforced"
    NOT_APPLICABLE_NO_LOCAL_EFFECTS = "not_applicable_no_local_effects"
    DISABLED = "disabled"


class PathAccess(str, Enum):
    READ_ONLY = "read_only"
    WRITABLE = "writable"


class PathOrigin(str, Enum):
    WORKSPACE = "workspace"
    GIT_METADATA = "git_metadata"
    PROVIDER_STATE = "provider_state"
    OPERATOR = "operator"
    SCRATCH = "scratch"


class Persistence(str, Enum):
    HOST = "host_persistent"
    SESSION = "session_private"
    TURN = "turn_private"


class CreationPolicy(str, Enum):
    MUST_EXIST = "must_exist"
    CREATE_PRIVATE_DIRECTORY = "create_private_directory"


class GitRole(str, Enum):
    WORKTREE_GIT_DIR = "worktree_git_dir"
    COMMON_GIT_DIR = "common_git_dir"
    PRIMARY_OBJECT_STORE = "primary_object_store"
    ALTERNATE_OBJECT_STORE = "alternate_object_store"


@dataclass(frozen=True)
class ResolvedSandboxPolicy:
    requested: Optional[SandboxPolicy]
    effective: SandboxPolicy
    source: SandboxPolicySource

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "requested": None if self.requested is None else self.requested.value,
            "effective": self.effective.value,
            "source": self.source.value,
        }


@dataclass(frozen=True)
class StateRootSpec:
    label: str
    destination: Path
    access: PathAccess
    persistence: Persistence
    creation: CreationPolicy
    origin: PathOrigin = PathOrigin.PROVIDER_STATE


@dataclass(frozen=True)
class EnvironmentSpec:
    set_values: Mapping[str, str] = field(default_factory=dict)
    unset_names: Tuple[str, ...] = ()
    secret_names: Tuple[str, ...] = ()
    private_tmp_names: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_values", MappingProxyType(dict(self.set_values)))
        object.__setattr__(self, "unset_names", tuple(self.unset_names))
        object.__setattr__(self, "secret_names", tuple(self.secret_names))
        object.__setattr__(self, "private_tmp_names", tuple(self.private_tmp_names))


@dataclass(frozen=True)
class NativeSandboxProfile:
    summary: Mapping[str, JSONScalar] = field(default_factory=dict)
    command: Optional[Tuple[str, ...]] = None
    sdk_options: Mapping[str, JSONScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))
        object.__setattr__(
            self,
            "command",
            None if self.command is None else tuple(self.command),
        )
        object.__setattr__(self, "sdk_options", MappingProxyType(dict(self.sdk_options)))


@dataclass(frozen=True)
class CompatibilityCheck:
    name: str
    check: Callable[[], None]


@dataclass(frozen=True)
class BackendSandboxSpec:
    support: SandboxSupport
    policies: frozenset[SandboxPolicy]
    state_roots: Tuple[StateRootSpec, ...] = ()
    accounting_peer_roots: Tuple[Path, ...] = ()
    provider_visible_paths: Tuple[StateRootSpec, ...] = ()
    environment: EnvironmentSpec = field(default_factory=EnvironmentSpec)
    native_profile: NativeSandboxProfile = field(default_factory=NativeSandboxProfile)
    compatibility: Tuple[CompatibilityCheck, ...] = ()
    backend_prompt_augmentation: Optional[str] = None
    external_services: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "policies", frozenset(self.policies))
        object.__setattr__(self, "state_roots", tuple(self.state_roots))
        object.__setattr__(self, "accounting_peer_roots", tuple(self.accounting_peer_roots))
        object.__setattr__(self, "provider_visible_paths", tuple(self.provider_visible_paths))
        object.__setattr__(self, "compatibility", tuple(self.compatibility))
        object.__setattr__(self, "external_services", tuple(self.external_services))


@dataclass(frozen=True)
class SandboxContext:
    workspace: Path
    cwd: Path
    inherited_environment: Mapping[str, str]
    command_preview: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace)
        object.__setattr__(self, "cwd", self.cwd)
        object.__setattr__(
            self,
            "inherited_environment",
            MappingProxyType(dict(self.inherited_environment)),
        )
        object.__setattr__(self, "command_preview", tuple(self.command_preview))


@runtime_checkable
class SandboxAdapter(Protocol):
    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        """Return immutable provider facts for the resolved session context."""

    def prepare_inner(self, plan: Any, command: Sequence[str]) -> Tuple[str, ...]:
        """Return the provider command selected for the effective outer policy."""


@dataclass(frozen=True)
class UnsupportedSandboxAdapter:
    """Explicit fail-closed declaration used by backends not in this stage."""

    reason: str = "outer read-only sandbox support is not implemented for this backend"

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        del context
        return BackendSandboxSpec(
            support=SandboxSupport.UNSUPPORTED,
            policies=frozenset({SandboxPolicy.NONE}),
        )

    def prepare_inner(self, plan: Any, command: Sequence[str]) -> Tuple[str, ...]:
        del plan
        return tuple(command)


@dataclass(frozen=True)
class NoLocalEffectsSandboxAdapter:
    """Typed adapter for the in-memory mock runner."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        del context
        return BackendSandboxSpec(
            support=SandboxSupport.NO_LOCAL_EFFECTS,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
        )

    def prepare_inner(self, plan: Any, command: Sequence[str]) -> Tuple[str, ...]:
        del plan
        return tuple(command)


class SandboxFailure(RuntimeError):
    """A sanitized, stable outer-sandbox failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "validation",
        remediation: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.phase = phase
        self.remediation = tuple(remediation)
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "phase": self.phase,
            "message": str(self),
            "remediation": list(self.remediation),
        }
