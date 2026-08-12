"""macOS per-user LaunchAgent backend for daemon autostart."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
import time
from typing import Mapping, Optional

from .config import load_daemon_token
from .daemon_lifecycle import (
    account_home,
    lifecycle_lock_held,
    registration_lock_held,
)
from .daemon_service import (
    AutostartError,
    AutostartStatus,
    EndpointInUseError,
    ManagedCommand,
    ManagedServiceIdentity,
    SafeManagedRestoreError,
    absolute_path,
    atomic_rename_noreplace,
    close_reservations,
    endpoints_overlap,
    parse_managed_command,
    readiness,
    reserve_server_endpoint,
)
from .daemon_supervisor import DaemonStatus, daemon_status, start_daemon, stop_daemon
from .paths import AgentCollabHome, GlobalDataPaths, atomic_write_private_text


MANAGER = "launchd"
LABEL = "io.github.lauriparviainen.agent-collab"
PLIST_NAME = f"{LABEL}.plist"
PLIST_MARKER = "<!-- Managed by agent-collab. Do not edit. -->"
RECOVERY_SUFFIX = ".agent-collab-recovery"
RECOVERY_FALLBACK_SUFFIX = ".agent-collab-recovery-fallback"


class RecoveryBytesLost(AutostartError):
    """The canonical definition was made non-loadable without retaining its bytes."""


EXIT_TIMEOUT_SECONDS = 10
SHUTDOWN_GRACE_SECONDS = 2
THROTTLE_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class LaunchdSnapshot:
    loaded: bool
    pid: Optional[int]
    command: Optional[ManagedCommand]
    attributable: bool
    disabled: bool
    raw: str = ""
    effective_home: Optional[Path] = None


def launchagent_path(*, home: Optional[Path] = None) -> Path:
    return (home or account_home()) / "Library" / "LaunchAgents" / PLIST_NAME


def recovery_paths(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(path.name + RECOVERY_SUFFIX),
        path.with_name(path.name + RECOVERY_FALLBACK_SUFFIX),
    )


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def launchd_target() -> str:
    return f"{launchd_domain()}/{LABEL}"


def render_launchd_plist(
    *,
    paths: GlobalDataPaths,
    interpreter: Path,
    env: Mapping[str, str],
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
) -> str:
    interpreter = absolute_path(interpreter)
    argv = [
        str(interpreter),
        "-m",
        "agent_collab.cli",
        "daemon",
        "run",
        "--manager",
        MANAGER,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if default_workdir is not None:
        argv.extend(["--workdir", str(default_workdir.expanduser().resolve())])
    document = {
        "Label": LABEL,
        "ProgramArguments": argv,
        "EnvironmentVariables": {
            "PATH": env.get("PATH") or os.defpath,
            "AGENT_COLLAB_HOME": str(paths.home.expanduser().resolve()),
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_INTERVAL_SECONDS,
        "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
    }
    rendered = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")
    marker_at = rendered.index("<plist")
    return rendered[:marker_at] + PLIST_MARKER + "\n" + rendered[marker_at:]


def parse_launchd_plist(
    content: str,
    *,
    current_paths: Optional[GlobalDataPaths] = None,
    expected_interpreter: Optional[Path] = None,
) -> ManagedServiceIdentity:
    if PLIST_MARKER not in content:
        raise AutostartError("LaunchAgent definition is not owned by agent-collab")
    try:
        value = plistlib.loads(content.encode("utf-8"))
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise AutostartError(f"invalid agent-collab LaunchAgent plist: {exc}") from exc
    if not isinstance(value, dict) or value.get("Label") != LABEL:
        raise AutostartError("LaunchAgent definition has the wrong Label")
    argv = value.get("ProgramArguments")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        raise AutostartError("LaunchAgent ProgramArguments must be a string array")
    command = parse_managed_command(argv, platform_manager=MANAGER)
    environment = value.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        environment = {}
    raw_home = environment.get("AGENT_COLLAB_HOME")
    effective_home = (
        Path(str(raw_home)).expanduser().resolve()
        if raw_home
        else (account_home() / ".agent-collab").resolve()
    )
    current = False
    if current_paths is not None and expected_interpreter is not None:
        current = (
            effective_home == current_paths.home.resolve()
            and command.interpreter == absolute_path(expected_interpreter)
        )
    return ManagedServiceIdentity(
        manager=MANAGER,
        source="definition",
        owned=True,
        current_home_owned=current,
        installed=True,
        loaded=False,
        enabled=True,
        command=command,
        effective_home=effective_home,
    )


def definition_identity(
    path: Path,
    *,
    current_paths: Optional[GlobalDataPaths] = None,
    expected_interpreter: Optional[Path] = None,
) -> Optional[ManagedServiceIdentity]:
    snapshot = _definition_snapshot(path)
    if snapshot is None:
        return None
    content, _device, _inode = snapshot
    return parse_launchd_plist(
        content, current_paths=current_paths, expected_interpreter=expected_interpreter
    )


def inspect(
    *,
    paths: GlobalDataPaths,
    definition_path: Path,
    interpreter: Path,
) -> ManagedServiceIdentity:
    recoveries = _owned_recovery_identities(
        definition_path,
        current_paths=paths,
        expected_interpreter=interpreter,
        strict=False,
    )
    recovery_collisions = _recovery_collision_paths(definition_path, recoveries)
    disk = definition_identity(
        definition_path, current_paths=paths, expected_interpreter=interpreter
    )
    native = launchd_snapshot()
    if native.loaded and not native.attributable:
        detail = "the launchd label is loaded by an unattributable program"
        if disk is None:
            return ManagedServiceIdentity(
                MANAGER,
                "loaded",
                False,
                False,
                False,
                True,
                not native.disabled,
                None,
                None,
                native.pid,
                detail,
            )
        return ManagedServiceIdentity(
            MANAGER,
            "definition+collision",
            True,
            disk.current_home_owned,
            True,
            True,
            not native.disabled,
            disk.command,
            disk.effective_home,
            native.pid,
            _with_recovery_detail(detail, recoveries, paths, recovery_collisions),
        )
    if disk is not None:
        command = native.command if native.loaded and native.command else disk.command
        effective_home = disk.effective_home
        current = disk.current_home_owned
        source = "definition"
        detail = "installed but not loaded"
        if native.loaded:
            runtime_status = daemon_status(paths)
            loaded_home = _loaded_effective_home(native, runtime_status)
            loaded_current = bool(
                loaded_home == paths.home.resolve()
                and native.command
                and native.command.interpreter == absolute_path(interpreter)
            )
            current = bool(disk.current_home_owned and loaded_current)
            effective_home = loaded_home
            drift = native.command != disk.command or loaded_home != disk.effective_home
            source = "definition+loaded-drift" if drift else "definition+loaded"
            detail = "loaded job differs from definition" if drift else "loaded"
        return ManagedServiceIdentity(
            MANAGER,
            source,
            True,
            current,
            True,
            native.loaded,
            not native.disabled,
            command,
            effective_home,
            native.pid,
            _with_recovery_detail(detail, recoveries, paths, recovery_collisions),
        )
    if native.loaded and native.attributable:
        runtime_status = daemon_status(paths)
        runtime = _loaded_effective_home(native, runtime_status)
        current = bool(
            runtime == paths.home.resolve()
            and native.command
            and native.command.interpreter == absolute_path(interpreter)
        )
        return ManagedServiceIdentity(
            MANAGER,
            "loaded",
            True,
            current,
            False,
            True,
            not native.disabled,
            native.command,
            runtime,
            native.pid,
            _with_recovery_detail(
                "loaded job exists but the LaunchAgent plist is missing",
                recoveries,
                paths,
                recovery_collisions,
            ),
        )
    runtime_status = daemon_status(paths)
    if runtime_status.running and runtime_status.state.get("manager") == MANAGER:
        runtime_command = _runtime_managed_command(runtime_status)
        runtime_home = _runtime_state_home(runtime_status)
        runtime_pid = runtime_status.state.get("pid")
        current = bool(
            runtime_home == paths.home.resolve()
            and runtime_command is not None
            and runtime_command.interpreter == absolute_path(interpreter)
        )
        return ManagedServiceIdentity(
            MANAGER,
            "runtime",
            runtime_command is not None,
            current,
            False,
            False,
            False,
            runtime_command,
            runtime_home,
            int(runtime_pid) if runtime_pid is not None else None,
            _with_recovery_detail(
                "launchd runtime exists but the LaunchAgent plist and loaded target are missing",
                recoveries,
                paths,
                recovery_collisions,
            ),
        )
    if recoveries:
        recovery_path, recovered = recoveries[0]
        return ManagedServiceIdentity(
            MANAGER,
            "recovery",
            True,
            recovered.current_home_owned,
            False,
            False,
            False,
            recovered.command,
            recovered.effective_home,
            None,
            _with_recovery_detail(
                f"LaunchAgent definition is quarantined at {recovery_path}",
                recoveries,
                paths,
                recovery_collisions,
            ),
        )
    return ManagedServiceIdentity(
        MANAGER,
        "absent",
        False,
        False,
        False,
        False,
        False,
        None,
        None,
        None,
        _with_recovery_detail("not installed", recoveries, paths, recovery_collisions),
    )


def status(*, paths: GlobalDataPaths, definition_path: Path, interpreter: Path) -> AutostartStatus:
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    command = identity.command
    current = bool(
        identity.current_home_owned
        and command
        and command.interpreter.exists()
        and command.interpreter == absolute_path(interpreter)
    )
    healthy = False
    detail = identity.detail
    if (
        identity.loaded
        and identity.pid
        and identity.owned
        and identity.source != "definition+collision"
        and identity.effective_home == paths.home.resolve()
        and command
    ):
        before = launchd_snapshot()
        healthy, health_detail, _payload = readiness(
            host=command.host,
            port=command.port,
            paths=paths,
            expected_manager=MANAGER,
            expected_pid=identity.pid,
        )
        after = launchd_snapshot()
        if before.pid != after.pid or after.pid != identity.pid:
            healthy = False
            health_detail = "launchd process generation changed during readiness probe"
        detail = health_detail
    elif identity.loaded and identity.effective_home != paths.home.resolve():
        if identity.effective_home is None:
            detail = (
                "native registration ownership is indeterminate because its effective home "
                "could not be proven; authenticated health was not probed"
            )
        else:
            detail = (
                "native registration belongs to another agent-collab home; authenticated health "
                "was not probed with this home's token"
            )
    elif identity.loaded and identity.pid is None:
        detail = "LaunchAgent is loaded but dormant"
    if identity.enabled is False and identity.installed:
        detail = f"launchd label has a persisted disabled override; {detail}"
    recovery_snapshot = _owned_recovery_identities(
        definition_path, current_paths=paths, expected_interpreter=interpreter
    )
    for note in _recovery_notes(recovery_snapshot, paths):
        if note not in detail:
            detail = f"{detail}; {note}" if detail else note
    return AutostartStatus(
        installed=identity.installed,
        enabled=identity.installed and identity.enabled,
        active=bool(
            identity.loaded
            and identity.pid
            and identity.owned
            and identity.source != "definition+collision"
        ),
        healthy=bool(identity.loaded and identity.pid and healthy),
        definition_current=current,
        definition_path=definition_path,
        detail=detail,
        manager=MANAGER,
    )


def enable_locked(
    *,
    paths: GlobalDataPaths,
    definition_path: Path,
    interpreter: Path,
    env: Mapping[str, str],
    host: str,
    port: int,
    default_workdir: Optional[Path],
    readiness_timeout: float,
    takeover: bool,
) -> AutostartStatus:
    _require_transaction_locks()
    _ensure_launchd_available()
    _ensure_launchagents_directory(definition_path.parent)
    expected = render_launchd_plist(
        paths=paths,
        interpreter=interpreter,
        env=env,
        host=host,
        port=port,
        default_workdir=default_workdir,
    )
    candidate_command = parse_launchd_plist(expected).command
    _lint_plist(expected)
    definition_snapshot = _definition_snapshot(definition_path)
    existing = definition_snapshot[0] if definition_snapshot is not None else None
    prior_identity = (
        parse_launchd_plist(existing, current_paths=paths, expected_interpreter=interpreter)
        if existing is not None
        else None
    )
    prior_recoveries = _owned_recovery_identities(
        definition_path, current_paths=paths, expected_interpreter=interpreter
    )
    prior_native = launchd_snapshot()
    if prior_native.loaded and not prior_native.attributable:
        raise AutostartError(
            f"refusing to replace loaded launchd label {LABEL}; its program is not agent-collab"
        )
    manual = daemon_status(paths)
    loaded_home = _loaded_effective_home(prior_native, manual)
    loaded_job_is_current = bool(
        prior_native.loaded
        and loaded_home == paths.home.resolve()
        and prior_native.command
        and prior_native.command.interpreter == absolute_path(interpreter)
    )
    loaded_without_definition_is_current = bool(prior_identity is None and loaded_job_is_current)
    if (
        prior_identity is not None
        and prior_native.loaded
        and not (prior_identity.current_home_owned and loaded_job_is_current)
        and not takeover
    ):
        raise AutostartError(
            "the loaded launchd job does not match the current-home plist ownership; "
            "re-run with --takeover to replace it explicitly"
        )
    if (
        prior_identity is None
        and prior_native.loaded
        and not loaded_without_definition_is_current
        and not takeover
    ):
        raise AutostartError(
            "the loaded launchd job has no durable plist and cannot be proven to belong to "
            "this agent-collab home; re-run with --takeover to replace it explicitly"
        )
    if prior_identity is not None and not prior_identity.current_home_owned and not takeover:
        raise _foreign_owner_error(prior_identity)
    for recovery_path, recovered in prior_recoveries:
        if not recovered.current_home_owned and not takeover and prior_identity is None:
            raise AutostartError(
                f"LaunchAgent recovery at {recovery_path} belongs to {recovered.effective_home}; "
                "re-run with --takeover to replace it"
            )
    if manual.running and manual.state.get("manager") not in {None, "detached", MANAGER}:
        raise AutostartError(
            f"live daemon pid {manual.state.get('pid')} is owned by "
            f"{manual.state.get('manager')}; refusing launchd takeover"
        )
    if manual.running and manual.state.get("manager") == MANAGER and not prior_native.loaded:
        raise AutostartError(
            f"launchd-owned daemon pid {manual.state.get('pid')} is live while the launchd "
            "target is unloaded; stop that orphan manually before enabling autostart"
        )
    reservations = []
    try:
        reservations = reserve_server_endpoint(host, port)
    except EndpointInUseError:
        claimed_by_native = bool(
            prior_native.pid
            and prior_native.attributable
            and prior_native.command
            and endpoints_overlap(host, port, prior_native.command.host, prior_native.command.port)
        )
        claimed_by_runtime = bool(
            manual.running
            and manual.state.get("manager") in {None, "detached", MANAGER}
            and endpoints_overlap(
                host,
                port,
                str(manual.state.get("host") or "127.0.0.1"),
                _safe_port(manual.state.get("port")),
            )
        )
        if not claimed_by_native and not claimed_by_runtime:
            raise
    stopped_manual = False
    candidate_started = False
    untouched_prior_fast_path = False
    candidate_pids: set[int] = set()
    prior_booted_out = False
    loaded_definition_mismatch = bool(
        prior_identity is not None
        and prior_native.loaded
        and (
            prior_native.command != prior_identity.command
            or loaded_home != prior_identity.effective_home
        )
    )
    changed = existing != expected or loaded_definition_mismatch
    irreversible_takeover = bool(
        takeover
        and (
            loaded_definition_mismatch
            or (
                prior_identity is None
                and prior_native.loaded
                and not loaded_without_definition_is_current
            )
            or (
                prior_identity is not None
                and not prior_identity.current_home_owned
                and (
                    prior_identity.effective_home is None
                    or not _home_has_token(prior_identity.effective_home)
                )
            )
        )
    )
    displaced_recovery: Optional[Path] = None
    recovery_warnings: list[str] = []
    rollback_enablement_started = False
    authorized_recoveries = _authorized_recovery_snapshot(
        prior_recoveries,
        takeover=takeover,
        target_identity=(
            prior_identity
            or (prior_recoveries[0][1] if not prior_native.loaded and prior_recoveries else None)
        ),
        target_command=prior_native.command,
        target_home=loaded_home,
    )
    restore_existing = None if irreversible_takeover else existing
    takeover_barrier_established = False
    try:
        if irreversible_takeover:
            # A foreign definition that cannot be reconstructed is displaced
            # only behind a proven login-safety barrier. Clearing this override
            # below is the replacement's enable commit.
            _set_disabled(True)
            takeover_barrier_established = True
        if changed and existing is not None:
            try:
                displaced_recovery = _quarantine_definition(
                    definition_path,
                    paths,
                    interpreter,
                    authorized_recoveries=authorized_recoveries,
                    expected_content=existing,
                )
            except RecoveryBytesLost as warning:
                recovery_warnings.append(str(warning))
        if manual.running and manual.state.get("manager") in {None, "detached"}:
            stop_daemon(paths, _lifecycle_locked=True)
            stopped_manual = True
        if prior_native.loaded and (changed or prior_native.pid is not None):
            if not changed and prior_native.pid is not None and prior_identity is not None:
                command = prior_native.command or prior_identity.command
                if command:
                    good, _, _ = readiness(
                        host=command.host,
                        port=command.port,
                        paths=paths,
                        expected_manager=MANAGER,
                        expected_pid=prior_native.pid,
                    )
                    if good:
                        untouched_prior_fast_path = True
                        if prior_native.disabled:
                            _set_disabled(False)
                        _wait_for_ready(
                            paths,
                            command.host,
                            command.port,
                            readiness_timeout,
                            expected_command=command,
                        )
                        result = status(
                            paths=paths,
                            definition_path=definition_path,
                            interpreter=interpreter,
                        )
                        _require_enabled_healthy_status(result)
                        cleanup_notes = _post_commit_recovery_cleanup(
                            definition_path,
                            paths,
                            interpreter,
                            authorized_recoveries=authorized_recoveries,
                        )
                        return _append_status_detail(result, cleanup_notes)
            _bootout_matching(prior_native.command, prior_native.pid)
            prior_booted_out = True
        if changed:
            atomic_write_private_text(definition_path, expected)
        _set_disabled(False)
        close_reservations(reservations)
        current_native = launchd_snapshot()
        candidate_started = True
        if current_native.loaded:
            _launchctl("kickstart", launchd_target())
        else:
            _launchctl("bootstrap", launchd_domain(), str(definition_path))
        _wait_for_ready(
            paths,
            host,
            port,
            readiness_timeout,
            expected_command=candidate_command,
            observed_pids=candidate_pids,
        )
        result = status(
            paths=paths,
            definition_path=definition_path,
            interpreter=interpreter,
        )
        _require_enabled_healthy_status(result)
    except Exception as exc:
        close_reservations(reservations)
        if untouched_prior_fast_path:
            detail = ""
            if prior_native.disabled:
                try:
                    _set_disabled(True)
                except Exception as recovery_exc:
                    errors = [f"critical: could not restore disabled override: {recovery_exc}"]
                    try:
                        _preserve_prior_bytes_nonloadably(
                            definition_path,
                            prior_bytes=existing,
                            current_paths=paths,
                            expected_interpreter=interpreter,
                            authorized_recoveries=authorized_recoveries,
                        )
                    except Exception as safety_exc:
                        errors.append(
                            "critical: could not preserve the prior plist nonloadably: "
                            f"{safety_exc}"
                        )
                    detail = "; " + "; ".join(errors)
            raise AutostartError(
                f"existing launchd daemon changed or became unhealthy during enable: {exc}{detail}"
            ) from exc
        if irreversible_takeover and not takeover_barrier_established:
            raise AutostartError(
                f"could not establish the disabled launchd takeover barrier; "
                f"the prior registration was left untouched: {exc}"
            ) from exc
        recovery_errors: list[str] = list(recovery_warnings)
        terminal_login_safety = False
        if prior_native.disabled or irreversible_takeover:
            try:
                # Restoring disabled intent is the first registration action:
                # no loadable plist bytes may reappear before this is proven.
                _set_disabled(True)
            except Exception as recovery_exc:
                terminal_login_safety = True
                recovery_errors.append(
                    f"critical: could not restore the disabled override: {recovery_exc}"
                )
                try:
                    _preserve_prior_bytes_nonloadably(
                        definition_path,
                        prior_bytes=existing,
                        current_paths=paths,
                        expected_interpreter=interpreter,
                        authorized_recoveries=authorized_recoveries,
                    )
                except Exception as safety_exc:
                    recovery_errors.append(
                        f"critical: could not prove a non-loadable plist residual: {safety_exc}"
                    )
        if candidate_started:
            try:
                _cleanup_candidate(paths, candidate_command, candidate_pids)
            except Exception as recovery_exc:
                terminal_login_safety = True
                recovery_errors.append(
                    f"critical: candidate cleanup could not be proven: {recovery_exc}"
                )
                try:
                    _set_disabled(True)
                except Exception as barrier_exc:
                    recovery_errors.append(
                        f"critical: could not disable the failed candidate: {barrier_exc}"
                    )
                try:
                    _preserve_prior_bytes_nonloadably(
                        definition_path,
                        prior_bytes=restore_existing,
                        current_paths=paths,
                        expected_interpreter=interpreter,
                        authorized_recoveries=authorized_recoveries,
                    )
                except Exception as safety_exc:
                    recovery_errors.append(
                        f"critical: could not preserve registration bytes nonloadably: {safety_exc}"
                    )
        if not terminal_login_safety:
            try:
                if restore_existing is None:
                    _remove_owned_definition(definition_path)
                else:
                    atomic_write_private_text(definition_path, restore_existing)
                    if displaced_recovery is not None:
                        displaced_recovery.unlink(missing_ok=True)
            except Exception as recovery_exc:
                recovery_errors.append(f"plist rollback failed: {recovery_exc}")
            try:
                if not prior_native.disabled and not irreversible_takeover:
                    rollback_enablement_started = True
                    _set_disabled(False)
                if (
                    not irreversible_takeover
                    and prior_native.loaded
                    and prior_native.pid is not None
                    and not prior_native.disabled
                    and restore_existing is not None
                ):
                    prior_command = prior_identity.command
                    prior_home = prior_identity.effective_home
                    if prior_command is None or prior_home is None:
                        raise AutostartError("prior launchd rollback identity is incomplete")
                    current = launchd_snapshot()
                    if current.loaded:
                        if not current.attributable or current.command != prior_command:
                            raise AutostartError(
                                "loaded launchd job changed before prior-state rollback"
                            )
                    else:
                        _launchctl("bootstrap", launchd_domain(), str(definition_path))
                    prior_paths = GlobalDataPaths.resolve(
                        env={"AGENT_COLLAB_HOME": str(prior_home)}
                    )
                    _wait_for_ready(
                        prior_paths,
                        prior_command.host,
                        prior_command.port,
                        readiness_timeout,
                        expected_command=prior_command,
                    )
                elif (
                    not irreversible_takeover
                    and loaded_without_definition_is_current
                    and manual.running
                    and _home_has_token(paths.home)
                ):
                    _restore_manual(paths, manual)
                elif not irreversible_takeover and prior_native.loaded and prior_native.pid is None:
                    recovery_errors.append(
                        "prior loaded-dormant launchd state remains unloaded to preserve "
                        "its stopped intent"
                    )
            except Exception as recovery_exc:
                recovery_errors.append(f"launchd rollback failed: {recovery_exc}")
                if rollback_enablement_started:
                    rollback_barrier = False
                    try:
                        _set_disabled(True)
                        rollback_barrier = True
                    except Exception as barrier_exc:
                        recovery_errors.append(
                            "critical: could not disable the failed prior launchd restore: "
                            f"{barrier_exc}"
                        )
                        try:
                            _preserve_prior_bytes_nonloadably(
                                definition_path,
                                prior_bytes=restore_existing,
                                current_paths=paths,
                                expected_interpreter=interpreter,
                                authorized_recoveries=authorized_recoveries,
                            )
                        except Exception as safety_exc:
                            recovery_errors.append(
                                "critical: could not preserve the failed prior launchd restore "
                                f"nonloadably: {safety_exc}"
                            )
                    try:
                        current = launchd_snapshot()
                        if current.loaded:
                            _bootout_matching(
                                prior_identity.command if prior_identity else None,
                                current.pid,
                            )
                    except Exception as cleanup_exc:
                        recovery_errors.append(
                            f"failed prior launchd generation cleanup failed: {cleanup_exc}"
                        )
                    if rollback_barrier:
                        recovery_errors.append(
                            "prior launchd daemon could not be restored; its definition remains "
                            "persistently disabled"
                        )
            if prior_booted_out and prior_native.disabled and prior_native.pid is not None:
                recovery_errors.append(
                    "prior live-plus-disabled launchd state could not be safely reproduced; "
                    "the job remains stopped"
                )
        if (
            stopped_manual
            and not loaded_without_definition_is_current
            and not terminal_login_safety
        ):
            try:
                _restore_manual(paths, manual)
            except Exception as recovery_exc:
                recovery_errors.append(f"detached daemon restore failed: {recovery_exc}")
        if irreversible_takeover:
            residual = (
                f"; displaced definition remains non-loadable at {displaced_recovery}"
                if displaced_recovery is not None
                else "; displaced in-memory job could not be reconstructed and remains unloaded"
            )
            recovery_errors.append(residual.removeprefix("; "))
        detail = f"; {'; '.join(recovery_errors)}" if recovery_errors else ""
        if isinstance(exc, AutostartError):
            raise AutostartError(f"{exc}{detail}") from exc
        raise AutostartError(f"failed to enable launchd autostart: {exc}{detail}") from exc
    finally:
        close_reservations(reservations)
    recovery_warnings.extend(
        _post_commit_recovery_cleanup(
            definition_path,
            paths,
            interpreter,
            recovery=displaced_recovery,
            authorized_recoveries=authorized_recoveries,
        )
    )
    return _append_status_detail(result, recovery_warnings)


def disable_locked(
    *,
    paths: GlobalDataPaths,
    definition_path: Path,
    interpreter: Path,
    takeover: bool,
) -> AutostartStatus:
    _require_transaction_locks()
    _ensure_launchd_available()
    detached = daemon_status(paths)
    detached_remains = bool(
        detached.running and detached.state.get("manager") in {None, "detached"}
    )
    disabled_detail = (
        f"disabled; detached daemon pid {detached.state.get('pid')} remains running"
        if detached_remains
        else "disabled"
    )
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    recovery_snapshot = _owned_recovery_identities(
        definition_path, current_paths=paths, expected_interpreter=interpreter
    )
    authorized_recoveries = _authorized_recovery_snapshot(
        recovery_snapshot,
        takeover=takeover,
        target_identity=identity,
    )
    if not identity.owned and not identity.loaded:
        return AutostartStatus(
            False,
            False,
            False,
            False,
            True,
            definition_path,
            (
                f"not installed; detached daemon pid {detached.state.get('pid')} remains running"
                if detached_remains
                else "not installed"
            ),
            MANAGER,
        )
    if identity.owned and not identity.current_home_owned and not takeover:
        raise _foreign_owner_error(identity)
    if not identity.owned and identity.loaded:
        raise AutostartError(f"refusing to boot out unowned launchd label {LABEL}")
    native = launchd_snapshot()
    if native.loaded and not native.attributable:
        raise AutostartError(
            f"refusing to disable launchd label {LABEL}; its loaded program is not agent-collab"
        )
    native_home = _loaded_effective_home(native, detached)
    if native.loaded != identity.loaded or (
        native.loaded
        and (
            native.pid != identity.pid
            or native.command != identity.command
            or native_home != identity.effective_home
        )
    ):
        raise AutostartError(
            "refusing to disable launchd because the loaded job identity changed during discovery"
        )
    if detached.running and detached.state.get("manager") == MANAGER and not native.loaded:
        raise AutostartError(
            f"launchd-owned daemon pid {detached.state.get('pid')} is orphaned from its unloaded "
            "launchd target; stop it manually before disabling autostart"
        )
    _set_disabled(True)
    recovery: Optional[Path] = None
    recovery_warnings: list[str] = []
    if definition_path.exists():
        definition_snapshot = _definition_snapshot(definition_path)
        if definition_snapshot is None:
            raise AutostartError(f"cannot classify LaunchAgent definition: {definition_path}")
        parsed = definition_identity(
            definition_path, current_paths=paths, expected_interpreter=interpreter
        )
        if parsed is None:
            raise AutostartError(f"cannot classify LaunchAgent definition: {definition_path}")
        if not parsed.current_home_owned and not takeover:
            raise _foreign_owner_error(parsed)
        try:
            recovery = _quarantine_definition(
                definition_path,
                paths,
                interpreter,
                authorized_recoveries=authorized_recoveries,
                expected_content=definition_snapshot[0],
            )
        except RecoveryBytesLost as warning:
            recovery_warnings.append(str(warning))
    if native.loaded:
        _bootout_matching(native.command, native.pid)
    recovery_warnings.extend(
        _post_commit_recovery_cleanup(
            definition_path,
            paths,
            interpreter,
            recovery=recovery,
            authorized_recoveries=authorized_recoveries,
        )
    )
    result = AutostartStatus(
        False, False, False, False, True, definition_path, disabled_detail, MANAGER
    )
    notes = recovery_warnings + _foreign_recovery_notes(recovery_snapshot, paths)
    return _append_status_detail(result, notes)


def start_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> AutostartStatus:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    _require_current(identity)
    native = launchd_snapshot()
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not native.loaded:
        raise AutostartError(
            f"launchd-owned daemon pid {live.state.get('pid')} is orphaned from its unloaded "
            "launchd target; stop it manually before starting another generation"
        )
    if native.loaded and not native.attributable:
        raise AutostartError(
            f"refusing to start launchd label {LABEL}; its loaded program is not agent-collab"
        )
    if native.pid and identity.command:
        healthy, _, _ = readiness(
            host=identity.command.host,
            port=identity.command.port,
            paths=paths,
            expected_manager=MANAGER,
            expected_pid=native.pid,
        )
        if healthy:
            result = status(paths=paths, definition_path=definition_path, interpreter=interpreter)
            return _missing_definition_start_detail(result) if not identity.installed else result
        guidance = (
            "run 'agent-collab daemon autostart enable' first"
            if native.disabled
            else "use 'agent-collab daemon restart' or inspect daemon logs"
        )
        raise AutostartError(f"launchd daemon is live but unhealthy; {guidance}")
    if native.disabled:
        raise AutostartError(
            "launchd autostart is persistently disabled; run 'agent-collab daemon autostart enable'"
        )
    command = identity.command
    if command is None:
        raise AutostartError("cannot determine the launchd daemon endpoint")
    if live.running and live.state.get("manager") not in {None, "detached", MANAGER}:
        raise AutostartError(
            f"daemon pid {live.state.get('pid')} is owned by {live.state.get('manager')}; "
            "refusing launchd start"
        )
    stopped_detached: Optional[DaemonStatus] = None
    if live.running and live.state.get("manager") in {None, "detached"}:
        stop_daemon(paths, _lifecycle_locked=True)
        stopped_detached = live
    reservations = []
    candidate_attempted = False
    candidate_pids: set[int] = set()
    try:
        reservations = reserve_server_endpoint(command.host, command.port)
        close_reservations(reservations)
        candidate_attempted = True
        if native.loaded:
            _launchctl("kickstart", launchd_target())
        elif identity.installed:
            _launchctl("bootstrap", launchd_domain(), str(definition_path))
        else:
            raise AutostartError(
                "the in-memory launchd job is unavailable and its plist is missing; "
                "run autostart enable"
            )
        _wait_for_ready(
            paths,
            command.host,
            command.port,
            5.0,
            expected_command=command,
            observed_pids=candidate_pids,
        )
        result = status(paths=paths, definition_path=definition_path, interpreter=interpreter)
        _require_started_status(result, installed=identity.installed)
    except Exception as exc:
        errors = []
        if candidate_attempted:
            try:
                _cleanup_candidate(paths, command, candidate_pids)
                candidate_stopped = True
            except Exception as cleanup_exc:
                errors.append(f"candidate cleanup failed: {cleanup_exc}")
                candidate_stopped = False
        else:
            candidate_stopped = True
        if stopped_detached is not None and candidate_stopped:
            try:
                _restore_manual(paths, stopped_detached)
            except Exception as restore_exc:
                errors.append(f"detached daemon restore failed: {restore_exc}")
        detail = f"; {'; '.join(errors)}" if errors else ""
        raise AutostartError(f"{exc}{detail}") from exc
    finally:
        close_reservations(reservations)
    if not identity.installed:
        return _missing_definition_start_detail(result)
    return result


def stop_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> AutostartStatus:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    if identity.owned:
        _require_current(identity)
    native = launchd_snapshot()
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not native.loaded:
        raise AutostartError(
            f"launchd-owned daemon pid {live.state.get('pid')} is orphaned from its unloaded "
            "launchd target; stop it manually"
        )
    if native.loaded:
        if not native.attributable:
            raise AutostartError(f"refusing to boot out unowned launchd label {LABEL}")
        _bootout_matching(
            native.command,
            native.pid,
            expected_home=paths.home.resolve(),
            paths=paths,
        )
    detached = daemon_status(paths)
    if detached.running and detached.state.get("manager") in {None, "detached"}:
        stop_daemon(paths, _lifecycle_locked=True)
    return status(paths=paths, definition_path=definition_path, interpreter=interpreter)


def restart_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> AutostartStatus:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    _require_current(identity)
    native = launchd_snapshot()
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not native.loaded:
        raise AutostartError(
            f"launchd-owned daemon pid {live.state.get('pid')} is orphaned from its unloaded "
            "launchd target; stop it manually before restarting"
        )
    if not identity.installed:
        raise AutostartError(
            "cannot restart launchd without its plist; run 'agent-collab daemon autostart enable'"
        )
    if native.loaded and not native.attributable:
        raise AutostartError(
            f"refusing to restart launchd label {LABEL}; its loaded program is not agent-collab"
        )
    if native.disabled:
        raise AutostartError(
            "launchd autostart is persistently disabled; run 'agent-collab daemon autostart enable'"
        )
    definition = definition_identity(
        definition_path, current_paths=paths, expected_interpreter=interpreter
    )
    command = definition.command if definition is not None else None
    if command is None:
        raise AutostartError("cannot determine the launchd daemon endpoint")
    live_manager = live.state.get("manager") if live.running else None
    if live.running and live_manager not in {None, "detached", MANAGER}:
        raise AutostartError(
            f"daemon pid {live.state.get('pid')} is owned by {live_manager}; refusing launchd restart"
        )
    stopped_detached: Optional[DaemonStatus] = None
    if live.running and live_manager in {None, "detached"}:
        stop_daemon(paths, _lifecycle_locked=True)
        stopped_detached = live
    reservations = []
    candidate_attempted = False
    candidate_pids: set[int] = set()
    try:
        if native.loaded:
            _bootout_matching(native.command, native.pid)
        reservations = reserve_server_endpoint(command.host, command.port)
        close_reservations(reservations)
        candidate_attempted = True
        _launchctl("bootstrap", launchd_domain(), str(definition_path))
        _wait_for_ready(
            paths,
            command.host,
            command.port,
            5.0,
            expected_command=command,
            observed_pids=candidate_pids,
        )
        result = status(paths=paths, definition_path=definition_path, interpreter=interpreter)
        _require_enabled_healthy_status(result)
    except Exception as exc:
        errors = []
        if candidate_attempted:
            try:
                _cleanup_candidate(paths, command, candidate_pids)
                candidate_stopped = True
            except Exception as cleanup_exc:
                errors.append(f"candidate cleanup failed: {cleanup_exc}")
                candidate_stopped = False
        else:
            candidate_stopped = True
        if native.pid is not None and candidate_stopped:
            restore_pids: set[int] = set()
            try:
                _launchctl("bootstrap", launchd_domain(), str(definition_path))
                _wait_for_ready(
                    paths,
                    command.host,
                    command.port,
                    5.0,
                    expected_command=command,
                    observed_pids=restore_pids,
                )
            except Exception as restore_exc:
                errors.append(f"prior launchd daemon restore failed: {restore_exc}")
                try:
                    _cleanup_candidate(paths, command, restore_pids)
                except Exception as cleanup_exc:
                    candidate_stopped = False
                    errors.append(f"failed prior launchd generation cleanup failed: {cleanup_exc}")
        if stopped_detached is not None and candidate_stopped:
            try:
                _restore_manual(paths, stopped_detached)
            except Exception as restore_exc:
                errors.append(f"detached daemon restore failed: {restore_exc}")
        detail = f"; {'; '.join(errors)}" if errors else ""
        raise AutostartError(f"{exc}{detail}") from exc
    finally:
        close_reservations(reservations)
    return result


def quiesce_for_install_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> dict[str, object]:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    if identity.owned:
        _require_current(identity)
    native = launchd_snapshot()
    if native.loaded and not native.attributable:
        raise AutostartError("cannot upgrade while the launchd label is unattributable")
    disk = definition_identity(
        definition_path, current_paths=paths, expected_interpreter=interpreter
    )
    if native.loaded and disk is None:
        raise AutostartError(
            "cannot upgrade while the launchd job is loaded but its plist is missing"
        )
    if native.loaded and (native.command is None or disk is None or native.command != disk.command):
        raise AutostartError(
            "cannot upgrade while launchd's in-memory arguments differ from the plist; "
            "run 'agent-collab daemon restart' or 'agent-collab daemon autostart enable' first"
        )
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not native.loaded:
        raise AutostartError(
            f"cannot upgrade while launchd-owned daemon pid {live.state.get('pid')} is orphaned "
            "from its unloaded target; stop it manually first"
        )
    if native.pid and native.disabled:
        raise AutostartError(
            "cannot safely upgrade a live but persistently disabled LaunchAgent; run "
            "'agent-collab daemon autostart enable' first, then disable it again after upgrade"
        )
    snapshot: dict[str, object] = {
        "manager": MANAGER,
        "owned": identity.owned,
        "installed": identity.installed,
        "loaded": native.loaded,
        "running": native.pid is not None,
        "enabled": not native.disabled,
        "command": identity.command,
        "recoveries": {
            str(path): _read_optional(path)
            for path, _recovered in _owned_recovery_identities(
                definition_path,
                current_paths=paths,
                expected_interpreter=interpreter,
            )
        },
    }
    if identity.installed and not native.disabled:
        _set_disabled(True)
    if native.loaded:
        _bootout_matching(native.command, native.pid)
    return snapshot


def restore_after_install_locked(
    snapshot: Mapping[str, object],
    *,
    paths: GlobalDataPaths,
    definition_path: Path,
    interpreter: Path,
) -> AutostartStatus:
    _require_transaction_locks()
    if not snapshot.get("owned"):
        return status(paths=paths, definition_path=definition_path, interpreter=interpreter)
    if not snapshot.get("installed"):
        result = status(paths=paths, definition_path=definition_path, interpreter=interpreter)
        native = launchd_snapshot()
        recoveries = {
            str(path): _read_optional(path)
            for path, _recovered in _owned_recovery_identities(
                definition_path,
                current_paths=paths,
                expected_interpreter=interpreter,
            )
        }
        if result.installed or native.loaded or recoveries != snapshot.get("recoveries"):
            raise AutostartError(
                "recovery-only launchd registration changed or became loadable during install"
            )
        return result
    mutation_failed = not snapshot.get("mutation_succeeded", True)
    was_enabled = bool(snapshot.get("enabled"))
    was_running = bool(snapshot.get("running"))
    stopped_recovery: Optional[Path] = None
    candidate_pids: set[int] = set()
    command: Optional[ManagedCommand] = None
    try:
        if mutation_failed:
            _ensure_durable_install(interpreter)
        if was_running:
            snapshot_command = snapshot.get("command")
            if not isinstance(snapshot_command, ManagedCommand):
                raise AutostartError(
                    "cannot restore launchd daemon: prior command identity is missing"
                )
            command = snapshot_command
            if was_enabled:
                _set_disabled(False)
            current = launchd_snapshot()
            if current.loaded:
                if not current.attributable or current.command != command:
                    raise AutostartError("launchd target changed while restoring the prior daemon")
            else:
                _launchctl("bootstrap", launchd_domain(), str(definition_path))
            _wait_for_ready(
                paths,
                command.host,
                command.port,
                5.0,
                expected_command=command,
                observed_pids=candidate_pids,
            )
        elif was_enabled:
            command = snapshot.get("command")
            if not isinstance(command, ManagedCommand):
                raise AutostartError(
                    "cannot preserve stopped launchd intent: prior command identity is missing"
                )
            stopped_recovery = _quarantine_for_stopped_restore(definition_path)
            _set_disabled(False)
            current = launchd_snapshot()
            if current.loaded:
                _bootout_matching(command, current.pid)
                raise AutostartError(
                    "launchd auto-loaded while restoring previously stopped service intent"
                )
            atomic_rename_noreplace(stopped_recovery, definition_path)
            _fsync_directory(definition_path.parent)
            stopped_recovery = None
        result = status(paths=paths, definition_path=definition_path, interpreter=interpreter)
        final_native = launchd_snapshot()
        if result.installed != bool(snapshot.get("installed")):
            raise AutostartError(
                "restored launchd service did not preserve its prior installed state"
            )
        if result.enabled != was_enabled:
            raise AutostartError(
                "restored launchd service did not preserve its prior enabled state"
            )
        if snapshot.get("installed") and not result.definition_current:
            raise AutostartError("restored launchd plist is not current for this installation")
        if was_running and (
            not result.active
            or not result.healthy
            or final_native.pid is None
            or not final_native.attributable
            or final_native.command != command
        ):
            raise AutostartError(
                "restored launchd daemon did not remain active and PID-bound healthy"
            )
        if not was_running and final_native.loaded:
            raise AutostartError(
                "restored launchd service did not preserve its prior stopped intent"
            )
    except Exception as exc:
        errors = []
        barrier_restored = False
        try:
            _set_disabled(True)
            barrier_restored = True
        except Exception as barrier_exc:
            errors.append(f"critical: could not restore disabled upgrade barrier: {barrier_exc}")
            try:
                _preserve_prior_bytes_nonloadably(
                    definition_path,
                    prior_bytes=(
                        snapshot[0]
                        if (snapshot := _definition_snapshot(definition_path)) is not None
                        else None
                    ),
                    current_paths=paths,
                    expected_interpreter=interpreter,
                )
            except Exception as safety_exc:
                errors.append(f"critical: non-loadable recovery failed: {safety_exc}")
        try:
            if command is not None:
                _cleanup_candidate(paths, command, candidate_pids)
            else:
                candidate = launchd_snapshot()
                if candidate.loaded:
                    raise AutostartError("failed upgrade candidate command is unknown")
        except Exception as cleanup_exc:
            errors.append(f"candidate cleanup failed: {cleanup_exc}")
        if stopped_recovery is not None and barrier_restored:
            try:
                if not os.path.lexists(definition_path):
                    atomic_rename_noreplace(stopped_recovery, definition_path)
                    _fsync_directory(definition_path.parent)
                    stopped_recovery = None
            except OSError as recovery_exc:
                errors.append(f"stopped definition recovery failed: {recovery_exc}")
        detail = f"; {'; '.join(errors)}" if errors else ""
        error_type = SafeManagedRestoreError if not errors and barrier_restored else AutostartError
        raise error_type(
            f"could not restore launchd after install; autostart remains disabled: {exc}{detail}"
        ) from exc
    return result


def _quarantine_for_stopped_restore(path: Path) -> Path:
    """Temporarily make a stopped RunAtLoad definition non-loadable without data loss."""

    for candidate in recovery_paths(path):
        if os.path.lexists(candidate):
            continue
        try:
            atomic_rename_noreplace(path, candidate)
        except OSError as exc:
            raise AutostartError(
                f"could not make the stopped LaunchAgent non-loadable for restoration: {exc}"
            ) from exc
        _fsync_directory(path.parent)
        candidate.chmod(0o600)
        return candidate
    raise AutostartError(
        "cannot safely restore enabled-but-stopped launchd intent because both recovery slots "
        "are occupied"
    )


def _ensure_durable_install(interpreter: Path) -> None:
    result = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import agent_collab, agent_collab.cli",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AutostartError(
            "the post-install interpreter is unusable; prior launchd state cannot be restored"
        )


def launchd_snapshot() -> LaunchdSnapshot:
    disabled = _disabled_override()
    result = _launchctl("print", launchd_target(), check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").lower()
        if any(
            value in detail for value in ("could not find service", "not found", "no such process")
        ):
            return LaunchdSnapshot(False, None, None, False, disabled, result.stdout)
        raise AutostartError(
            f"launchctl print {launchd_target()} failed: "
            f"{(result.stderr or result.stdout or result.returncode).strip()}"
        )
    output = result.stdout or ""
    pid_match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", output)
    pid = int(pid_match.group(1)) if pid_match else None
    argv = _parse_launchctl_arguments(output)
    command = None
    attributable = False
    if argv:
        try:
            command = parse_managed_command(argv, platform_manager=MANAGER)
            attributable = True
        except AutostartError:
            pass
    return LaunchdSnapshot(
        True,
        pid,
        command,
        attributable,
        disabled,
        output,
        _parse_launchctl_home(output),
    )


def _parse_launchctl_arguments(output: str) -> list[str]:
    block = re.search(r"(?ms)^\s*arguments\s*=\s*\{\s*(.*?)^\s*\}\s*$", output)
    if not block:
        return []
    indexed: list[tuple[int, str]] = []
    for match in re.finditer(r"(?m)^\s*(\d+)\s*=\s*(.*?)\s*$", block.group(1)):
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
        indexed.append((int(match.group(1)), value))
    indexed.sort()
    if indexed:
        if [index for index, _ in indexed] != list(range(len(indexed))):
            return []
        return [value for _, value in indexed]
    values = []
    for line in block.group(1).splitlines():
        value = line.strip()
        if not value:
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
        values.append(value)
    return values


def _parse_launchctl_home(output: str) -> Optional[Path]:
    match = re.search(r"(?m)^\s*AGENT_COLLAB_HOME\s*=>\s*(.*?)\s*$", output)
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace(r"\"", '"').replace("\\\\", "\\")
    return Path(value).expanduser().resolve() if value else None


def _disabled_override() -> bool:
    result = _launchctl("print-disabled", launchd_domain(), check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"cannot query launchd disabled overrides: {detail}")
    output = result.stdout or ""
    block = re.search(r"(?ms)disabled services\s*=\s*\{\s*(.*?)^\s*\}\s*$", output)
    if not block:
        raise AutostartError("launchd disabled override output was malformed")
    observed: Optional[bool] = None
    for line in block.group(1).splitlines():
        value = line.strip()
        if not value:
            continue
        entry = re.fullmatch(r'(?:(?:"([^"]+)")|([^"\s]+))\s*=>\s*(\S+)', value)
        if not entry:
            if re.match(rf'^"?{re.escape(LABEL)}"?(?:\s|=>|$)', value):
                raise AutostartError("launchd disabled override output was ambiguous")
            continue
        label = entry.group(1) or entry.group(2)
        if label != LABEL:
            continue
        state = entry.group(3).lower()
        if state not in {"true", "false", "disabled", "enabled"}:
            raise AutostartError("launchd disabled override output was ambiguous")
        if observed is not None:
            raise AutostartError("launchd returned duplicate disabled overrides for agent-collab")
        observed = state in {"true", "disabled"}
    return bool(observed)


def _set_disabled(disabled: bool) -> None:
    action = "disable" if disabled else "enable"
    _launchctl(action, launchd_target())
    observed = _disabled_override()
    if observed != disabled:
        raise AutostartError(
            f"launchctl {action} did not establish the expected persisted override"
        )


def _wait_for_ready(
    paths: GlobalDataPaths,
    host: str,
    port: int,
    timeout: float,
    *,
    expected_command: Optional[ManagedCommand] = None,
    observed_pids: Optional[set[int]] = None,
) -> None:
    deadline = time.monotonic() + timeout
    detail = "launchd job has no running process"
    while time.monotonic() < deadline:
        before = launchd_snapshot()
        if observed_pids is not None and before.pid is not None:
            observed_pids.add(before.pid)
        command_matches = expected_command is None or before.command == expected_command
        if before.pid and before.attributable and command_matches:
            healthy, detail, _ = readiness(
                host=host,
                port=port,
                paths=paths,
                expected_manager=MANAGER,
                expected_pid=before.pid,
            )
            after = launchd_snapshot()
            if observed_pids is not None and after.pid is not None:
                observed_pids.add(after.pid)
            after_matches = expected_command is None or after.command == expected_command
            if healthy and after.pid == before.pid and after.attributable and after_matches:
                return
            if after.pid != before.pid:
                detail = "launchd process generation changed during readiness probe"
            elif not after_matches:
                detail = "launchd command changed during readiness probe"
        time.sleep(0.05)
    raise AutostartError(f"launchd daemon did not become healthy: {detail}")


def _bootout_and_wait(pid: Optional[int]) -> None:
    result = _launchctl("bootout", launchd_target(), check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        lowered = detail.lower()
        missing = any(
            value in lowered for value in ("could not find service", "not found", "no such process")
        )
        if pid is not None or not missing:
            raise AutostartError(f"launchctl bootout {launchd_target()} failed: {detail}")
    _wait_for_pid_exit(pid)


def _wait_for_pid_exit(pid: Optional[int]) -> None:
    if pid is None:
        return
    timeout = EXIT_TIMEOUT_SECONDS + SHUTDOWN_GRACE_SECONDS
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    raise AutostartError(f"launchd daemon pid {pid} did not exit within {timeout} seconds")


def _bootout_matching(
    expected_command: Optional[ManagedCommand],
    expected_pid: Optional[int] = None,
    *,
    expected_home: Optional[Path] = None,
    paths: Optional[GlobalDataPaths] = None,
) -> None:
    """Revalidate the loaded label immediately before asking launchd to remove it."""

    current = launchd_snapshot()
    if not current.loaded:
        _wait_for_pid_exit(expected_pid)
        return
    if expected_command is None or not current.attributable or current.command != expected_command:
        raise AutostartError(
            f"refusing to boot out launchd label {LABEL}; its loaded command changed"
        )
    if expected_pid is not None and current.pid != expected_pid:
        raise AutostartError(
            f"refusing to boot out launchd label {LABEL}; its process generation changed "
            f"from pid {expected_pid} to {current.pid}"
        )
    if expected_home is not None:
        if paths is None:
            raise AutostartError("launchd home revalidation requires daemon runtime paths")
        current_home = _loaded_effective_home(current, daemon_status(paths))
        if current_home != expected_home:
            raise AutostartError(
                f"refusing to boot out launchd label {LABEL}; its effective home changed"
            )
    _bootout_and_wait(current.pid)


def _cleanup_candidate(
    paths: GlobalDataPaths,
    expected_command: ManagedCommand,
    observed_pids: Optional[set[int]] = None,
) -> None:
    """Remove a failed candidate and prove any observed launchd generation exited."""

    current = launchd_snapshot()
    pids = set(observed_pids or ())
    expected_pid = current.pid
    if current.pid is not None:
        pids.add(current.pid)
    if not current.loaded:
        runtime = daemon_status(paths)
        if runtime.running and runtime.state.get("manager") == MANAGER:
            raw_pid = runtime.state.get("pid")
            if not isinstance(raw_pid, int) or raw_pid <= 0:
                raise AutostartError("failed candidate has an indeterminate launchd PID")
            expected_pid = raw_pid
            pids.add(raw_pid)
    _bootout_matching(expected_command, expected_pid)
    for pid in sorted(pids):
        _wait_for_pid_exit(pid)


def _require_enabled_healthy_status(result: AutostartStatus) -> None:
    if not (
        result.installed
        and result.enabled
        and result.active
        and result.healthy
        and result.definition_current
    ):
        raise AutostartError(
            "launchd candidate did not remain installed, enabled, current, active, and healthy"
        )


def _require_started_status(result: AutostartStatus, *, installed: bool) -> None:
    if not result.active or not result.healthy:
        raise AutostartError("launchd daemon did not remain active and PID-bound healthy")
    if installed and not (result.installed and result.enabled and result.definition_current):
        raise AutostartError(
            "launchd daemon did not retain its installed, enabled, current definition"
        )


def _ensure_launchd_available() -> None:
    result = _launchctl("print", launchd_domain(), check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "launchd GUI domain unavailable").strip()
        raise AutostartError(
            f"cannot use launchd GUI domain {launchd_domain()}: {detail}; "
            "log in graphically and retry (SSH-only sessions may not have this domain)"
        )


def _ensure_launchagents_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise AutostartError(f"LaunchAgents path is not a real directory: {path}")
        return
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def _lint_plist(content: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".plist") as candidate:
        candidate.write(content)
        candidate.flush()
        try:
            result = subprocess.run(
                ["plutil", "-lint", candidate.name], capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise AutostartError(f"plutil is unavailable: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"generated LaunchAgent plist failed plutil validation: {detail}")


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise AutostartError(f"launchctl is unavailable: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        remediation = ""
        lowered = detail.lower()
        if "not permitted" in lowered or "operation not permitted" in lowered:
            remediation = (
                "; allow agent-collab in System Settings > General > Login Items & Extensions"
            )
        raise AutostartError(f"launchctl {' '.join(args)} failed: {detail}{remediation}")
    return result


def _quarantine_definition(
    path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
    *,
    authorized_recoveries: Mapping[Path, str],
    expected_content: Optional[str] = None,
) -> Path:
    before = _definition_snapshot(path)
    if expected_content is None and before is not None:
        expected_content = before[0]
    if before is None or before[0] != expected_content:
        raise AutostartError("LaunchAgent definition changed before it could be quarantined")
    choices = recovery_paths(path)
    for candidate in choices:
        if os.path.lexists(candidate):
            try:
                _validated_recovery_identity(
                    candidate, current_paths=paths, expected_interpreter=interpreter
                )
            except AutostartError:
                continue
            if _read_optional(candidate) != authorized_recoveries.get(candidate):
                continue
            candidate.unlink()
        try:
            if _definition_snapshot(path) != before:
                raise AutostartError(
                    "LaunchAgent definition identity changed before quarantine rename"
                )
            atomic_rename_noreplace(path, candidate)
            _fsync_directory(path.parent)
            after = _definition_snapshot(candidate)
            if after is None or after != before:
                if after is not None and not os.path.lexists(path):
                    atomic_rename_noreplace(candidate, path)
                    _fsync_directory(path.parent)
                raise AutostartError(
                    "LaunchAgent definition identity changed while it was being quarantined"
                )
            _definition_snapshot(candidate, chmod_private=True)
            return candidate
        except OSError:
            continue
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise AutostartError(
            "could not quarantine the LaunchAgent definition and could not establish "
            f"a non-loadable login-safety state: {exc}"
        ) from exc
    raise RecoveryBytesLost(
        "critical: LaunchAgent recovery slots were unavailable; removed the canonical plist "
        "as a last-resort login-safety action and could not retain its exact bytes"
    )


def _remove_authorized_recoveries(
    path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
    *,
    authorized_recoveries: Mapping[Path, str],
) -> None:
    for candidate, _identity in _owned_recovery_identities(
        path,
        current_paths=paths,
        expected_interpreter=interpreter,
    ):
        if _read_optional(candidate) == authorized_recoveries.get(candidate):
            candidate.unlink(missing_ok=True)


def _post_commit_recovery_cleanup(
    path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
    *,
    recovery: Optional[Path] = None,
    authorized_recoveries: Mapping[Path, str],
) -> list[str]:
    warnings: list[str] = []
    if recovery is not None:
        try:
            recovery.unlink(missing_ok=True)
        except Exception as exc:
            warnings.append(
                f"warning: lifecycle commit succeeded, but recovery cleanup failed: {exc}"
            )
    try:
        _remove_authorized_recoveries(
            path, paths, interpreter, authorized_recoveries=authorized_recoveries
        )
    except Exception as exc:
        warnings.append(
            f"warning: lifecycle commit succeeded, but authorized recovery cleanup failed: {exc}"
        )
    return warnings


def _owned_recovery_identities(
    path: Path,
    *,
    current_paths: GlobalDataPaths,
    expected_interpreter: Path,
    strict: bool = False,
) -> list[tuple[Path, ManagedServiceIdentity]]:
    found: list[tuple[Path, ManagedServiceIdentity]] = []
    for candidate in recovery_paths(path):
        if not os.path.lexists(candidate):
            continue
        try:
            identity = _validated_recovery_identity(
                candidate,
                current_paths=current_paths,
                expected_interpreter=expected_interpreter,
            )
        except AutostartError:
            if strict:
                raise
            continue
        found.append((candidate, identity))
    return found


def _identity_key(
    identity: ManagedServiceIdentity,
) -> tuple[Optional[Path], Optional[ManagedCommand]]:
    return identity.effective_home, identity.command


def _authorized_recovery_snapshot(
    recoveries: list[tuple[Path, ManagedServiceIdentity]],
    *,
    takeover: bool,
    target_identity: Optional[ManagedServiceIdentity] = None,
    target_command: Optional[ManagedCommand] = None,
    target_home: Optional[Path] = None,
) -> dict[Path, str]:
    target_key = (
        _identity_key(target_identity)
        if target_identity is not None
        else (target_home, target_command)
    )
    authorized: dict[Path, str] = {}
    for candidate, identity in recoveries:
        is_target = (
            takeover and target_key != (None, None) and _identity_key(identity) == target_key
        )
        if identity.current_home_owned or is_target:
            content = _read_optional(candidate)
            if content is None:
                raise AutostartError(
                    f"LaunchAgent recovery disappeared during snapshot: {candidate}"
                )
            authorized[candidate] = content
    return authorized


def _foreign_recovery_notes(
    recoveries: list[tuple[Path, ManagedServiceIdentity]], paths: GlobalDataPaths
) -> list[str]:
    return [
        f"preserved foreign LaunchAgent recovery at {candidate} "
        f"(home {identity.effective_home or 'unknown'})"
        for candidate, identity in recoveries
        if identity.effective_home != paths.home.resolve()
    ]


def _recovery_notes(
    recoveries: list[tuple[Path, ManagedServiceIdentity]], paths: GlobalDataPaths
) -> list[str]:
    current_home = paths.home.resolve()
    return [
        (
            f"LaunchAgent recovery artifact is present at {candidate} (current home)"
            if identity.effective_home == current_home
            else f"preserved foreign LaunchAgent recovery at {candidate} "
            f"(home {identity.effective_home or 'unknown'})"
        )
        for candidate, identity in recoveries
    ]


def _with_recovery_detail(
    detail: str,
    recoveries: list[tuple[Path, ManagedServiceIdentity]],
    paths: GlobalDataPaths,
    collisions: tuple[Path, ...] = (),
) -> str:
    notes = _recovery_notes(recoveries, paths)
    notes.extend(
        f"preserved unowned LaunchAgent recovery collision at {path}" for path in collisions
    )
    return "; ".join([detail, *notes]) if notes else detail


def _recovery_collision_paths(
    path: Path, recoveries: list[tuple[Path, ManagedServiceIdentity]]
) -> tuple[Path, ...]:
    validated = {candidate for candidate, _identity in recoveries}
    return tuple(
        candidate
        for candidate in recovery_paths(path)
        if os.path.lexists(candidate) and candidate not in validated
    )


def _append_status_detail(status_value: AutostartStatus, notes: list[str]) -> AutostartStatus:
    if not notes:
        return status_value
    detail = "; ".join([status_value.detail, *notes]) if status_value.detail else "; ".join(notes)
    return AutostartStatus(
        status_value.installed,
        status_value.enabled,
        status_value.active,
        status_value.healthy,
        status_value.definition_current,
        status_value.definition_path,
        detail,
        status_value.manager,
    )


def _missing_definition_start_detail(status_value: AutostartStatus) -> AutostartStatus:
    return _append_status_detail(
        status_value,
        [
            "warning: installed=false; this start applies only to the current login session; "
            "run 'agent-collab daemon autostart enable' to restore future-login autostart"
        ],
    )


def _validated_recovery_identity(
    path: Path,
    *,
    current_paths: GlobalDataPaths,
    expected_interpreter: Path,
) -> ManagedServiceIdentity:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AutostartError(f"cannot inspect LaunchAgent recovery {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AutostartError(f"LaunchAgent recovery is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise AutostartError(f"LaunchAgent recovery is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise AutostartError(f"LaunchAgent recovery must have mode 0600: {path}")
    identity = definition_identity(
        path,
        current_paths=current_paths,
        expected_interpreter=expected_interpreter,
    )
    if identity is None:
        raise AutostartError(f"LaunchAgent recovery disappeared during inspection: {path}")
    return identity


def _remove_owned_definition(path: Path) -> None:
    snapshot = _definition_snapshot(path)
    if snapshot is None:
        return
    content = snapshot[0]
    parse_launchd_plist(content)
    path.unlink()


def _restore_manual(paths: GlobalDataPaths, manual: DaemonStatus) -> None:
    state = manual.state
    raw_workdir = state.get("default_workdir")
    start_daemon(
        paths,
        host=str(state.get("host") or "127.0.0.1"),
        port=_safe_port(state.get("port")),
        default_workdir=Path(str(raw_workdir)) if raw_workdir else None,
        _lifecycle_locked=True,
    )


def _runtime_state_home(status: DaemonStatus) -> Optional[Path]:
    raw_home = status.state.get("home")
    if not status.running or not raw_home:
        return None
    return Path(str(raw_home)).expanduser().resolve()


def _runtime_managed_command(status: DaemonStatus) -> Optional[ManagedCommand]:
    if not status.running or status.state.get("manager") != MANAGER:
        return None
    argv = status.state.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return None
    try:
        return parse_managed_command(argv, platform_manager=MANAGER)
    except AutostartError:
        return None


def _loaded_effective_home(native: LaunchdSnapshot, runtime_status: DaemonStatus) -> Optional[Path]:
    runtime_home = None
    if (
        native.pid is not None
        and runtime_status.running
        and runtime_status.state.get("manager") == MANAGER
        and runtime_status.state.get("pid") == native.pid
    ):
        runtime_home = _runtime_state_home(runtime_status)
    if native.effective_home is not None and runtime_home is not None:
        return native.effective_home if native.effective_home == runtime_home else None
    return native.effective_home or runtime_home


def _preserve_prior_bytes_nonloadably(
    path: Path,
    *,
    prior_bytes: Optional[str],
    current_paths: GlobalDataPaths,
    expected_interpreter: Path,
    authorized_recoveries: Optional[Mapping[Path, str]] = None,
) -> Optional[Path]:
    """Remove the canonical plist and retain prior bytes only in a recovery slot."""

    snapshot = _definition_snapshot(path)
    canonical = snapshot[0] if snapshot is not None else None
    if canonical is not None:
        parse_launchd_plist(canonical)
        path.unlink()
        _fsync_directory(path.parent)
    if prior_bytes is None:
        return None
    parse_launchd_plist(prior_bytes)
    choices = recovery_paths(path)
    for candidate in choices:
        if not os.path.lexists(candidate):
            continue
        try:
            _validated_recovery_identity(
                candidate,
                current_paths=current_paths,
                expected_interpreter=expected_interpreter,
            )
        except AutostartError:
            continue
        content = _read_optional(candidate)
        if content == prior_bytes:
            candidate.chmod(0o600)
            return candidate
    for candidate in choices:
        if not os.path.lexists(candidate):
            atomic_write_private_text(candidate, prior_bytes)
            return candidate
        try:
            identity = _validated_recovery_identity(
                candidate,
                current_paths=current_paths,
                expected_interpreter=expected_interpreter,
            )
        except AutostartError:
            continue
        authorized = authorized_recoveries or {}
        if identity.current_home_owned or _read_optional(candidate) == authorized.get(candidate):
            atomic_write_private_text(candidate, prior_bytes)
            return candidate
    raise AutostartError(
        "canonical plist was removed, but both non-loadable recovery slots are occupied; "
        "the exact prior definition could not be retained"
    )


def _require_current(identity: ManagedServiceIdentity) -> None:
    if not identity.owned:
        raise AutostartError("launchd registration is not owned by agent-collab")
    if not identity.current_home_owned:
        raise _foreign_owner_error(identity)


def _foreign_owner_error(identity: ManagedServiceIdentity) -> AutostartError:
    interpreter = identity.command.interpreter if identity.command else "unknown"
    if identity.effective_home is None or identity.command is None:
        return AutostartError(
            "the per-user launchd registration is attributable to agent-collab, but its home "
            "or interpreter identity is indeterminate; refusing native mutation until the "
            "loaded job and runtime state are reconciled"
        )
    return AutostartError(
        "the per-user launchd registration belongs to another agent-collab installation "
        f"(home {identity.effective_home or 'unknown'}, interpreter {interpreter}); "
        "use autostart enable --takeover or autostart disable --takeover explicitly"
    )


def _safe_port(value: object) -> int:
    try:
        return int(value or 8765)
    except (TypeError, ValueError):
        return 8765


def _require_transaction_locks() -> None:
    if not lifecycle_lock_held() or not registration_lock_held():
        raise RuntimeError("launchd lifecycle mutation requires lifecycle and registration locks")


def _home_has_token(home: Path) -> bool:
    try:
        resolved = AgentCollabHome(root=home, config_path=home / "config.toml")
        return bool(load_daemon_token(home=resolved))
    except Exception:
        return False


def _read_optional(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _definition_snapshot(
    path: Path, *, chmod_private: bool = False
) -> Optional[tuple[str, int, int]]:
    """Read one current-user regular definition without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AutostartError(f"cannot open LaunchAgent definition {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AutostartError(f"LaunchAgent definition is not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise AutostartError(f"LaunchAgent definition is not owned by the current user: {path}")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", newline="") as handle:
            content = handle.read()
        if chmod_private:
            os.fchmod(fd, 0o600)
        return content, info.st_dev, info.st_ino
    except OSError as exc:
        raise AutostartError(f"cannot read LaunchAgent definition {path}: {exc}") from exc
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "LABEL",
    "MANAGER",
    "PLIST_MARKER",
    "LaunchdSnapshot",
    "definition_identity",
    "disable_locked",
    "enable_locked",
    "inspect",
    "launchagent_path",
    "launchd_domain",
    "launchd_snapshot",
    "launchd_target",
    "parse_launchd_plist",
    "recovery_paths",
    "render_launchd_plist",
    "quiesce_for_install_locked",
    "restore_after_install_locked",
    "restart_locked",
    "start_locked",
    "status",
    "stop_locked",
]
