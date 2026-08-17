"""Antigravity SDK ownership, state, and worker-native profile for outer read-only."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
from typing import Any, Mapping, Optional, Sequence, Tuple

from ...retention import is_valid_session_id
from ...sandbox.plan import ResolvedSandboxPlan
from ...sandbox.specs import (
    BackendSandboxSpec,
    CompatibilityCheck,
    CreationPolicy,
    EnvironmentSpec,
    NativeSandboxProfile,
    PathAccess,
    PathOrigin,
    Persistence,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
    StateRootSpec,
)
from ..common.sdk import package_version

# Keep version floors here to avoid a circular import with backend.py (which
# imports this adapter). Values match the backend probe contract.
REQUIRED_GLIBC = "2.26"
REQUIRED_PROTOBUF = "7.35"


@dataclass(frozen=True)
class AntigravitySdkSandboxAdapter:
    """Describe the complete Antigravity SDK worker tree and writable state."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        inherited = context.inherited_environment
        # Session-private app-data/home are created at plan resolve for
        # read-only starts (CREATE_PRIVATE_DIRECTORY). Discovery/none paths
        # never materialise them. The trajectory is host-persistent and
        # session-keyed; without a session_id, describe() reports the base only.
        state_root = _session_state_root(context)
        trajectory = durable_trajectory_dir(context)
        app_data = state_root / "app-data"
        private_home = state_root / "home"

        provider_visible: list[StateRootSpec] = []
        # Prefer explicit ADC. An explicit empty value means "no ADC env" and
        # must not restore daemon ambient credentials. When unset, pin the
        # standard gcloud ADC under the effective inherited HOME so a private
        # session HOME cannot hide default discovery under outer read-only.
        if "GOOGLE_APPLICATION_CREDENTIALS" in inherited:
            raw_adc = inherited.get("GOOGLE_APPLICATION_CREDENTIALS")
            if isinstance(raw_adc, str) and not raw_adc.strip():
                adc = None
            else:
                adc = _adc_path(inherited)
        else:
            adc = _adc_path(inherited) or _default_gcloud_adc_path(inherited)
        if adc is not None:
            # Mount the parent directory read-only: path resolution only accepts
            # directories, while ADC is a credentials *file*.
            provider_visible.append(
                StateRootSpec(
                    label="Application Default Credentials directory",
                    destination=_adc_mount_directory(adc),
                    access=PathAccess.READ_ONLY,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.MUST_EXIST,
                    origin=PathOrigin.PROVIDER_STATE,
                )
            )

        environment: dict[str, str] = {
            "HOME": str(private_home),
            "ANTIGRAVITY_SAVE_DIR": str(trajectory),
            "ANTIGRAVITY_APP_DATA_DIR": str(app_data),
            "ANTIGRAVITY_STATE_ROOT": str(state_root),
        }
        if adc is not None:
            environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc)

        return BackendSandboxSpec(
            support=SandboxSupport.SDK_WORKER,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=(
                StateRootSpec(
                    label="Antigravity SDK trajectory",
                    destination=trajectory,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.CREATE_PRIVATE_DIRECTORY,
                ),
                StateRootSpec(
                    label="Antigravity SDK app data",
                    destination=app_data,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.SESSION,
                    creation=CreationPolicy.CREATE_PRIVATE_DIRECTORY,
                ),
                StateRootSpec(
                    label="Antigravity SDK private home",
                    destination=private_home,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.SESSION,
                    creation=CreationPolicy.CREATE_PRIVATE_DIRECTORY,
                ),
            ),
            provider_visible_paths=tuple(provider_visible),
            environment=EnvironmentSpec(
                set_values=environment,
                private_tmp_names=(
                    "TMPDIR",
                    "XDG_CACHE_HOME",
                    "XDG_CONFIG_HOME",
                    "XDG_DATA_HOME",
                    "XDG_STATE_HOME",
                ),
            ),
            native_profile=NativeSandboxProfile(
                summary={
                    "shape": "sdk_worker",
                    "policy": "ask_user",
                    "trajectory": "host_persistent_session_keyed_save_dir",
                    "app_data": "session_private_app_data_dir",
                    "adc": "read_only_when_configured",
                    "keyring": "external_service_outside_filesystem_boundary",
                    "runtime": "complete_sdk_and_localharness_in_worker",
                    "protobuf": f">= {REQUIRED_PROTOBUF}",
                    "glibc": f">= {REQUIRED_GLIBC}",
                },
                sdk_options={"policy": "ask_user"},
            ),
            compatibility=(
                CompatibilityCheck("protobuf_runtime", _reject_incompatible_protobuf),
                CompatibilityCheck("native_runtime", _reject_incompatible_glibc),
                CompatibilityCheck("adc_path", lambda: _validate_adc(inherited)),
                CompatibilityCheck(
                    "session_state_anchor",
                    lambda: _require_session_state_base(context),
                ),
                CompatibilityCheck(
                    "trajectory_state_anchor",
                    lambda: _require_persistent_trajectory_base(context),
                ),
                CompatibilityCheck(
                    "session_id_path_safety",
                    lambda: _require_safe_session_id(context),
                ),
            ),
            external_services=(
                "OS keyring (external service outside filesystem boundary)",
                "Google Application Default Credentials / Vertex",
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
        backend_config: Mapping[str, Any],
        verbose: bool,
        save_dir: Optional[str] = None,
        app_data_dir: Optional[str] = None,
        conversation_id: Optional[str] = None,
        resume: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        if not isinstance(save_dir, str) or not save_dir.strip():
            raise RuntimeError("antigravity sdk worker open requires a durable save_dir")
        if not isinstance(app_data_dir, str) or not app_data_dir.strip():
            raise RuntimeError("antigravity sdk worker open requires app_data_dir")
        payload = {
            "backend": "antigravity_sdk",
            "agent_id": agent_id,
            "workspace": str(workspace),
            "cwd": str(cwd),
            "options": dict(options),
            "backend_config": dict(backend_config),
            "agent_env": dict(agent_env),
            "verbose": bool(verbose),
            "save_dir": save_dir,
            "app_data_dir": app_data_dir,
            "native": {"policy": "ask_user"},
        }
        from ...resume import attach_resume_block, require_resume_session_id

        if resume is not None:
            attach_resume_block(payload, resume)
            payload["conversation_id"] = require_resume_session_id(payload)
        elif isinstance(conversation_id, str) and conversation_id:
            payload["conversation_id"] = conversation_id
        return payload


_SENTINEL_ROOT = Path("/nonexistent/agent-collab-antigravity-sdk")
_TRAJECTORIES = "trajectories"
_SESSION_PRIVATE_KEY = "session-private"


def _workspace_absolute(context: SandboxContext) -> Path:
    return Path(os.path.normpath(str(context.workspace.absolute())))


def _owned_absolute_base(candidate: Path, workspace: Path) -> Optional[Path]:
    try:
        absolute = candidate.expanduser()
        if not absolute.is_absolute():
            return None
        absolute = Path(os.path.normpath(str(absolute.absolute())))
        if _path_overlaps(absolute, workspace):
            return None
        parent = absolute
        while not parent.exists() and parent.parent != parent:
            parent = parent.parent
        if parent.exists() and parent.is_dir() and not parent.is_symlink():
            if os.stat(parent).st_uid == os.getuid():
                return absolute
    except OSError:
        return None
    return None


def _session_state_root(context: SandboxContext) -> Path:
    # Prefer daemon-owned agent-collab runtime (same ownership model as scratch).
    # describe() must not raise: when no safe base exists, use a sentinel path
    # and fail closed in the session_state_anchor compatibility check.
    base = _select_session_state_base(context)
    key = _session_path_key(context)
    if key is False:
        return _SENTINEL_ROOT / _SESSION_PRIVATE_KEY
    if base is None:
        return _SENTINEL_ROOT / (key or _SESSION_PRIVATE_KEY)
    return (base / (key or _SESSION_PRIVATE_KEY)).absolute()


def durable_trajectory_dir(context: SandboxContext) -> Path:
    """Host-persistent trajectory path, or the trajectories base when session_id is absent."""

    base = _select_persistent_trajectory_base(context)
    key = _session_path_key(context)
    if key is False or base is None:
        return _SENTINEL_ROOT / _TRAJECTORIES
    if key is None:
        return (base / _TRAJECTORIES).absolute()
    return (base / _TRAJECTORIES / key).absolute()


def _session_path_key(context: SandboxContext) -> Optional[str] | bool:
    """Validated session_id, None when describe-only, False when unsafe."""

    session_id = context.session_id
    if session_id is None:
        return None
    if not is_valid_session_id(session_id):
        return False
    return session_id


def _select_session_state_base(context: SandboxContext) -> Optional[Path]:
    candidates: list[Path] = []
    runtime = context.inherited_environment.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime).expanduser() / "agent-collab" / "antigravity-sdk")
    home = context.inherited_environment.get("AGENT_COLLAB_HOME")
    if home:
        # Explicit AGENT_COLLAB_HOME must win or fail closed: never fall back to
        # the real ~/.agent-collab when the configured home is unusable (e.g.
        # overlaps the workspace). That would write session state into an
        # unintended host profile.
        candidates.append(Path(home).expanduser() / "runtime" / "antigravity-sdk")
    else:
        candidates.append(Path.home() / ".agent-collab" / "runtime" / "antigravity-sdk")
    workspace = _workspace_absolute(context)
    for candidate in candidates:
        selected = _owned_absolute_base(candidate, workspace)
        if selected is not None:
            return selected
    return None


def _select_persistent_trajectory_base(context: SandboxContext) -> Optional[Path]:
    """Host-persistent AGENT_COLLAB_HOME (never XDG_RUNTIME_DIR)."""

    home = context.inherited_environment.get("AGENT_COLLAB_HOME")
    if home:
        candidates = [Path(home).expanduser()]
    else:
        candidates = [Path.home() / ".agent-collab"]
    workspace = _workspace_absolute(context)
    for candidate in candidates:
        selected = _owned_absolute_base(candidate, workspace)
        if selected is not None:
            return selected
    return None


def _require_session_state_base(context: SandboxContext) -> None:
    if _select_session_state_base(context) is None:
        raise SandboxFailure(
            "outer_sandbox_scratch_anchor_invalid",
            "no safe daemon-owned session state root for Antigravity SDK",
            phase="validation",
            remediation=(
                "Set AGENT_COLLAB_HOME or XDG_RUNTIME_DIR to a daemon-owned absolute "
                "path outside the workspace (do not point AGENT_COLLAB_HOME inside "
                "the session workspace).",
            ),
        )


def _require_persistent_trajectory_base(context: SandboxContext) -> None:
    if _select_persistent_trajectory_base(context) is None:
        raise SandboxFailure(
            "outer_sandbox_scratch_anchor_invalid",
            "no safe host-persistent trajectory root for Antigravity SDK",
            phase="validation",
            remediation=(
                "Set AGENT_COLLAB_HOME to a daemon-owned absolute path outside the "
                "workspace. Durable Antigravity trajectories are not stored under "
                "XDG_RUNTIME_DIR.",
            ),
        )


def _require_safe_session_id(context: SandboxContext) -> None:
    if context.session_id is None:
        return
    if not is_valid_session_id(context.session_id):
        raise SandboxFailure(
            "outer_sandbox_scratch_anchor_invalid",
            "session_id is not safe for a filesystem path",
            phase="validation",
            remediation=("Use a session id without path separators or '.' / '..'.",),
        )


def _path_overlaps(candidate: Path, workspace: Path) -> bool:
    try:
        candidate.relative_to(workspace)
        return True
    except ValueError:
        pass
    try:
        workspace.relative_to(candidate)
        return True
    except ValueError:
        return False


def _env_lookup(inherited: Mapping[str, str], key: str) -> Optional[str]:
    # Explicit agent/env presence wins, including empty string (do not fall back
    # to the daemon ambient value when the agent deliberately clears the key).
    if key in inherited:
        raw = inherited[key]
        return raw if isinstance(raw, str) else None
    raw = os.environ.get(key)
    return raw if isinstance(raw, str) else None


def _adc_path(inherited: Mapping[str, str]) -> Optional[Path]:
    raw = _env_lookup(inherited, "GOOGLE_APPLICATION_CREDENTIALS")
    if raw is None or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        return None
    return path.absolute()


def _default_gcloud_adc_path(inherited: Mapping[str, str]) -> Optional[Path]:
    raw_home = inherited.get("HOME")
    home = Path(raw_home).expanduser() if raw_home else Path.home()
    if not home.is_absolute():
        home = Path.home()
    candidate = (home / ".config" / "gcloud" / "application_default_credentials.json").absolute()
    try:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    except OSError:
        return None
    return None


def _adc_mount_directory(adc_file: Path) -> Path:
    # resolve_state_root only accepts directories; mount the credentials parent
    # read-only so the ADC file remains reachable.
    return adc_file.parent.absolute()


def _validate_adc(inherited: Mapping[str, str]) -> None:
    raw = _env_lookup(inherited, "GOOGLE_APPLICATION_CREDENTIALS")
    if raw is None:
        return
    if not raw.strip():
        # Explicit empty value: do not restore daemon ambient ADC.
        return
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "GOOGLE_APPLICATION_CREDENTIALS must be an absolute path under outer read-only",
            phase="validation",
            remediation=(
                "Set GOOGLE_APPLICATION_CREDENTIALS to an absolute ADC file path, "
                "or unset it to rely on the default ADC lookup.",
            ),
        )
    if not path.is_file():
        raise SandboxFailure(
            "outer_sandbox_path_missing",
            "Application Default Credentials file does not exist",
            phase="validation",
        )
    parent = path.parent
    if not parent.is_dir():
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            "Application Default Credentials parent directory is invalid",
            phase="validation",
        )


def _version_tuple(value: str) -> Tuple[int, ...]:
    parts: list[int] = []
    for part in (value or "").split("."):
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _reject_incompatible_protobuf() -> None:
    observed = package_version("protobuf")
    if not observed:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "protobuf package version metadata is unavailable for Antigravity SDK",
            phase="validation",
            remediation=(
                "Install the verified Antigravity extra so protobuf metadata is present.",
            ),
        )
    if _version_tuple(observed) < _version_tuple(REQUIRED_PROTOBUF):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"protobuf {observed} is incompatible with Antigravity SDK "
            f"(requires >= {REQUIRED_PROTOBUF})",
            phase="validation",
            remediation=(
                f"Use an isolated Antigravity environment with protobuf >= {REQUIRED_PROTOBUF},<8.",
            ),
        )


def _reject_incompatible_glibc() -> None:
    # Lazy import breaks the sandbox <-> backend cycle at module load time.
    # Outer-sandbox worker launch needs a positively compatible glibc host: the
    # bundled localharness is a glibc-linked Linux binary. musl / unknown libc
    # must fail closed here rather than deferring to the first turn.
    from .backend import assess_native_runtime

    native = assess_native_runtime(platform.libc_ver(), required=REQUIRED_GLIBC)
    if native.get("status") == "compatible":
        return
    observed = native.get("observed") or "unknown"
    status = native.get("status") or "unknown"
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        (
            f"bundled native runtime requires glibc >= {REQUIRED_GLIBC} "
            f"(status={status}; observed {observed})"
        ),
        phase="validation",
        remediation=(
            f"Use a glibc {REQUIRED_GLIBC}+ host/container. "
            "Do not replace the host system glibc manually.",
        ),
    )
