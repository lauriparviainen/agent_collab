"""Linux systemd user-service backend for daemon autostart."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import sys
import time
from typing import Mapping, Optional

from .daemon_lifecycle import (
    account_home,
    lifecycle_lock_held,
    registration_lock_held,
)
from .config import load_daemon_token
from .daemon_service import (
    AutostartError,
    AutostartStatus,
    EndpointInUseError,
    ManagedCommand,
    ManagedServiceIdentity,
    READY_ENDPOINT_MISSING_DETAIL,
    SafeManagedRestoreError,
    absolute_path,
    atomic_rename_noreplace,
    close_reservations,
    endpoints_overlap,
    legacy_health,
    parse_managed_command,
    readiness,
    reserve_server_endpoint,
)
from .daemon_supervisor import daemon_status, start_daemon, stop_daemon
from .paths import AgentCollabHome, GlobalDataPaths, atomic_write_private_text


SERVICE_NAME = "agent-collab.service"
UNIT_MARKER = "# Managed by agent-collab. Do not edit."
INTERPRETER_MARKER = "# Agent-Collab-Interpreter: "
HOME_MARKER = "# Agent-Collab-Home: "
MANAGER = "systemd"
RECOVERY_SUFFIX = ".agent-collab-recovery"
RECOVERY_FALLBACK_SUFFIX = ".agent-collab-recovery-fallback"
LOST_UNIT_BYTES_DETAIL = (
    "critical: exact prior systemd unit bytes were lost because recovery slots were unavailable"
)


class _ReadyEndpointUnavailableError(AutostartError):
    """The managed generation predates the protected /ready route."""


def systemd_unit_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve the fresh unit path from the user manager's environment."""

    manager_env = _systemd_manager_environment()
    configured = manager_env.get("XDG_CONFIG_HOME")
    manager_home = manager_env.get("HOME")
    if configured:
        config_home = _validated_absolute_environment_path(configured, "XDG_CONFIG_HOME")
    elif manager_home:
        config_home = _validated_absolute_environment_path(manager_home, "HOME") / ".config"
    else:
        config_home = account_home() / ".config"
    return config_home.resolve() / "systemd" / "user" / SERVICE_NAME


def resolve_systemd_unit_path() -> Path:
    """Use an existing manager FragmentPath, otherwise the manager-derived path."""

    fresh = systemd_unit_path()
    result = _systemctl(
        "show", SERVICE_NAME, "--property=LoadState", "--property=FragmentPath", check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"cannot discover systemd unit FragmentPath: {detail}")
    properties: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            if key in {"LoadState", "FragmentPath"} and key in properties:
                if properties[key] != value:
                    raise AutostartError(
                        f"systemd unit discovery returned conflicting duplicate {key} values"
                    )
                continue
            properties[key] = value
    load_state = properties.get("LoadState")
    fragment = properties.get("FragmentPath")
    if fragment:
        path = Path(fragment)
        if not path.is_absolute():
            raise AutostartError(f"systemd returned a non-absolute FragmentPath: {fragment}")
        return path
    if load_state == "not-found":
        return fresh
    raise AutostartError(
        "systemd unit discovery was indeterminate (missing or conflicting FragmentPath)"
    )


def managed_unit_installed(
    unit_path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> bool:
    path = unit_path or resolve_systemd_unit_path()
    content = _definition_content(path)
    return bool(content is not None and content.startswith(UNIT_MARKER))


def render_systemd_unit(
    *,
    paths: GlobalDataPaths,
    interpreter: Path,
    env: Mapping[str, str],
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
) -> str:
    interpreter = absolute_path(interpreter)
    command = [
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
        command.extend(["--workdir", str(default_workdir.expanduser().resolve())])
    path_value = env.get("PATH") or os.defpath
    lines = [
        UNIT_MARKER,
        f"{INTERPRETER_MARKER}{interpreter}",
        f"{HOME_MARKER}{paths.home.resolve()}",
        "[Unit]",
        "Description=agent-collab local collaboration daemon",
        "StartLimitIntervalSec=30",
        "StartLimitBurst=3",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={' '.join(_systemd_quote(part, escape_dollar=True) for part in command)}",
        f"Environment={_systemd_quote(f'PATH={path_value}')}",
    ]
    lines.append(f"Environment={_systemd_quote(f'AGENT_COLLAB_HOME={paths.home.resolve()}')}")
    lines.extend(
        [
            "Restart=on-failure",
            "RestartSec=5",
            "TimeoutStopSec=10",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


def parse_systemd_unit(
    content: str,
    *,
    current_paths: Optional[GlobalDataPaths] = None,
    expected_interpreter: Optional[Path] = None,
) -> ManagedServiceIdentity:
    if not content.startswith(UNIT_MARKER):
        raise AutostartError("systemd definition is not owned by agent-collab")
    lines = content.splitlines()
    exec_lines = [line[len("ExecStart=") :] for line in lines if line.startswith("ExecStart=")]
    if not exec_lines:
        raise AutostartError("systemd definition is missing ExecStart")
    if len(exec_lines) != 1:
        raise AutostartError("systemd definition has duplicate ExecStart directives")
    exec_line = exec_lines[0]
    try:
        argv = [part.replace("%%", "%").replace("$$", "$") for part in shlex.split(exec_line)]
    except ValueError as exc:
        raise AutostartError(f"systemd definition has invalid ExecStart quoting: {exc}") from exc
    command = parse_managed_command(
        argv, platform_manager=MANAGER, allow_legacy_systemd_manager=True
    )
    marked_homes = [line[len(HOME_MARKER) :] for line in lines if line.startswith(HOME_MARKER)]
    environment_homes: list[str] = []
    for line in lines:
        if not line.startswith("Environment="):
            continue
        try:
            entries = shlex.split(line[len("Environment=") :])
        except ValueError as exc:
            raise AutostartError(
                f"systemd definition has invalid Environment quoting: {exc}"
            ) from exc
        for entry in entries:
            key, separator, value = entry.partition("=")
            if key != "AGENT_COLLAB_HOME":
                continue
            if not separator or not value:
                raise AutostartError("systemd definition has malformed AGENT_COLLAB_HOME metadata")
            environment_homes.append(value.replace("%%", "%"))
    if any(not value for value in marked_homes):
        raise AutostartError("systemd definition has malformed AGENT_COLLAB_HOME metadata")
    if len(marked_homes) > 1 or len(environment_homes) > 1:
        raise AutostartError("systemd definition has duplicate AGENT_COLLAB_HOME metadata")
    if marked_homes and not environment_homes:
        raise AutostartError("systemd definition has incomplete AGENT_COLLAB_HOME metadata")
    marked_home = Path(marked_homes[0]).expanduser().resolve() if marked_homes else None
    environment_home = (
        Path(environment_homes[0]).expanduser().resolve() if environment_homes else None
    )
    if marked_home is not None and marked_home != environment_home:
        raise AutostartError("systemd definition has conflicting AGENT_COLLAB_HOME metadata")
    effective_home = (
        environment_home
        if environment_home is not None
        else (account_home() / ".agent-collab").resolve()
    )
    current = False
    if current_paths is not None and expected_interpreter is not None:
        current = (
            effective_home == current_paths.home.resolve()
            and command.interpreter == absolute_path(expected_interpreter)
        )
    return ManagedServiceIdentity(
        MANAGER,
        "definition",
        True,
        current,
        True,
        False,
        False,
        command,
        effective_home,
    )


def inspect(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> ManagedServiceIdentity:
    content = _definition_content(definition_path)
    parsed = (
        parse_systemd_unit(content, current_paths=paths, expected_interpreter=interpreter)
        if content is not None
        else None
    )
    recoveries = _recovery_identities(
        definition_path,
        current_paths=paths,
        expected_interpreter=interpreter,
    )
    recovery_collisions = _recovery_collision_paths(definition_path, recoveries)
    active = _systemctl_truth("is-active", SERVICE_NAME)
    enabled = _systemctl_truth("is-enabled", SERVICE_NAME)
    pid = _systemd_main_pid() if active else None
    runtime = daemon_status(paths)
    runtime_command = _runtime_managed_command(runtime) if active else None
    runtime_home = _runtime_status_home(runtime) if active else None
    runtime_source = "runtime"
    if (
        active
        and pid is not None
        and (
            runtime_command is None
            or runtime_home is None
            or not runtime.running
            or runtime.state.get("manager") != MANAGER
            or runtime.state.get("pid") != pid
        )
    ):
        runtime_command, runtime_home = _systemd_process_identity(pid)
        runtime_source = "process"
    if parsed is not None:
        current = parsed.current_home_owned
        command = parsed.command
        if active:
            command = runtime_command
            current = bool(
                current
                and runtime_home == paths.home.resolve()
                and runtime_command is not None
                and runtime_command.interpreter == absolute_path(interpreter)
            )
        source = f"definition+{runtime_source}" if active else "definition"
        detail = "active" if active else "installed but inactive"
        if recoveries:
            source += "+recovery"
        detail = _with_recovery_detail(detail, recoveries, recovery_collisions)
        return ManagedServiceIdentity(
            MANAGER,
            source,
            True,
            current,
            True,
            active,
            enabled,
            command,
            runtime_home if active else parsed.effective_home,
            pid,
            detail,
        )
    if runtime.running and runtime.state.get("manager") == MANAGER:
        command = runtime_command if active else _runtime_managed_command(runtime)
        effective_home = runtime_home if active else _runtime_status_home(runtime)
        current = bool(
            effective_home == paths.home.resolve()
            and command is not None
            and command.interpreter == absolute_path(interpreter)
        )
        runtime_pid = int(runtime.state["pid"]) if runtime.state.get("pid") else None
        return ManagedServiceIdentity(
            MANAGER,
            "runtime",
            True,
            current,
            False,
            active,
            enabled,
            command,
            effective_home,
            pid if active and pid is not None else runtime_pid,
            _with_recovery_detail(
                "systemd runtime exists but the unit definition is missing",
                recoveries,
                recovery_collisions,
            ),
        )
    if active and pid is not None:
        process_command, process_home = _systemd_process_identity(pid)
        if process_command is not None and process_home is not None:
            return ManagedServiceIdentity(
                MANAGER,
                "process",
                True,
                bool(
                    process_home == paths.home.resolve()
                    and process_command.interpreter == absolute_path(interpreter)
                ),
                False,
                True,
                enabled,
                process_command,
                process_home,
                pid,
                _with_recovery_detail(
                    "active systemd process exists but the unit definition is missing",
                    recoveries,
                    recovery_collisions,
                ),
            )
        return ManagedServiceIdentity(
            MANAGER,
            "process-collision",
            False,
            False,
            False,
            True,
            enabled,
            None,
            None,
            pid,
            _with_recovery_detail(
                "active systemd MainPID is not attributable and the unit definition is missing",
                recoveries,
                recovery_collisions,
            ),
        )
    if recoveries:
        recovery, recovered = recoveries[0]
        detail = _with_recovery_detail(
            f"systemd unit is quarantined at {recovery}",
            recoveries[1:],
            recovery_collisions,
        )
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
            detail,
        )
    return ManagedServiceIdentity(
        MANAGER,
        "absent",
        False,
        False,
        False,
        active,
        enabled,
        None,
        None,
        pid,
        _with_recovery_detail("not installed", recoveries, recovery_collisions),
    )


def enable_autostart(
    *,
    paths: Optional[GlobalDataPaths] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
    readiness_timeout: float = 5.0,
    takeover: bool = False,
) -> AutostartStatus:
    _require_transaction_locks()
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    path = unit_path or resolve_systemd_unit_path()
    interpreter = absolute_path(interpreter or Path(sys.executable))
    _ensure_supported()
    _ensure_systemd_user_manager()
    _ensure_durable_install(interpreter)
    paths.ensure_dirs()
    expected = render_systemd_unit(
        paths=paths,
        interpreter=interpreter,
        env=environ,
        host=host,
        port=port,
        default_workdir=default_workdir,
    )
    candidate_identity = parse_systemd_unit(
        expected, current_paths=paths, expected_interpreter=interpreter
    )
    candidate_command = candidate_identity.command
    if candidate_command is None:
        raise AutostartError("rendered systemd candidate has no command identity")
    existing = _definition_content(path)
    disk_identity: Optional[ManagedServiceIdentity] = None
    if existing is not None and not existing.startswith(UNIT_MARKER):
        raise AutostartError(f"refusing to replace unmanaged systemd unit: {path}")
    recoveries = _recovery_identities(
        path,
        current_paths=paths,
        expected_interpreter=interpreter,
    )
    if existing is not None:
        disk_identity = parse_systemd_unit(
            existing, current_paths=paths, expected_interpreter=interpreter
        )
        if not disk_identity.current_home_owned and not takeover:
            raise AutostartError(
                "the per-user systemd registration belongs to another agent-collab installation "
                f"(home {disk_identity.effective_home}, interpreter "
                f"{disk_identity.command.interpreter}); "
                "re-run with --takeover to replace it"
            )
    elif recoveries:
        for _candidate, recovered in recoveries:
            if not recovered.current_home_owned and not takeover:
                raise AutostartError(
                    f"systemd recovery belongs to {recovered.effective_home}; re-run with "
                    "--takeover to replace it"
                )
    authorized_recoveries = _authorized_recovery_snapshot(
        recoveries,
        takeover=takeover,
        target_identity=(
            disk_identity or (recoveries[0][1] if existing is None and recoveries else None)
        ),
    )

    was_active = _systemctl_truth("is-active", SERVICE_NAME)
    was_enabled = _systemctl_truth("is-enabled", SERVICE_NAME)
    runtime = daemon_status(paths)
    manual = runtime if not was_active else None
    if manual is not None and manual.running:
        manual_manager = manual.state.get("manager")
        if manual_manager == MANAGER:
            raise AutostartError(
                f"systemd-owned daemon pid {manual.state.get('pid')} is orphaned from the "
                "inactive unit; stop it manually before enabling autostart"
            )
        if manual_manager not in {None, "detached"}:
            raise AutostartError(
                f"live daemon pid {manual.state.get('pid')} is owned by {manual_manager}; "
                "refusing systemd takeover"
            )
    active_command = _runtime_managed_command(runtime) if was_active else None
    active_pid = _systemd_main_pid() if was_active else None
    active_home = _runtime_status_home(runtime) if was_active else None
    if (
        was_active
        and active_pid is not None
        and (
            active_command is None
            or active_home is None
            or not runtime.running
            or runtime.state.get("pid") != active_pid
        )
    ):
        active_command, active_home = _systemd_process_identity(active_pid)
    active_attributable = bool(active_command is not None and active_home is not None)
    active_runtime_is_current = bool(
        was_active
        and active_attributable
        and active_home == paths.home.resolve()
        and active_command is not None
        and active_command.interpreter == interpreter
    )
    if was_active and not active_runtime_is_current:
        if not active_attributable:
            raise AutostartError(
                "the active systemd MainPID is not attributable to agent-collab; refusing takeover"
            )
        if not takeover:
            raise AutostartError(
                "the active systemd service cannot be proven to belong to this agent-collab "
                "home; re-run with --takeover to replace it explicitly"
            )
    reservations = []
    try:
        reservations = reserve_server_endpoint(host, port)
    except EndpointInUseError:
        claimed_by_native = bool(
            was_active
            and active_command
            and endpoints_overlap(host, port, active_command.host, active_command.port)
        )
        claimed_by_runtime = bool(
            manual
            and manual.running
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
    active_definition_mismatch = bool(
        was_active
        and disk_identity is not None
        and (active_command != disk_identity.command or active_home != disk_identity.effective_home)
    )
    changed = existing != expected or active_definition_mismatch
    recover_unhealthy = False
    if was_active and not changed:
        if active_pid is None:
            recover_unhealthy = True
        else:
            was_healthy, _detail = _health(
                host=host,
                port=port,
                paths=paths,
                expected_pid=active_pid,
            )
            recover_unhealthy = not was_healthy
    stopped_manual = False
    displaced_recovery: Optional[Path] = None
    lost_prior_bytes = False
    takeover_barrier_established = False
    candidate_written = False
    candidate_attempted = False
    candidate_pids: set[int] = set()
    irreversible_takeover = bool(
        takeover
        and (
            active_definition_mismatch
            or (disk_identity is None and was_active and not active_runtime_is_current)
            or (
                disk_identity is not None
                and not disk_identity.current_home_owned
                and (
                    disk_identity.effective_home is None
                    or not _home_has_token(disk_identity.effective_home)
                )
            )
        )
    )
    try:
        if irreversible_takeover:
            _systemctl("disable", SERVICE_NAME)
            if _systemctl_truth("is-enabled", SERVICE_NAME):
                raise AutostartError(
                    "could not establish the disabled barrier for irreversible takeover"
                )
            takeover_barrier_established = True
        if was_active and (changed or recover_unhealthy):
            _stop_managed_and_reap(paths, active_pid)
        if changed and existing is not None:
            displaced_recovery = _quarantine_unit(
                path,
                paths,
                interpreter,
                authorized_recoveries=authorized_recoveries,
                expected_content=existing,
            )
            lost_prior_bytes = displaced_recovery is None
            _systemctl("daemon-reload")
        if changed:
            atomic_write_private_text(path, expected)
            candidate_written = True
            _systemctl("daemon-reload")
        if manual and manual.running:
            stop_daemon(paths, _lifecycle_locked=True)
            stopped_manual = True
        _systemctl("enable", SERVICE_NAME)
        close_reservations(reservations)
        if was_active and (changed or recover_unhealthy):
            candidate_attempted = True
            _systemctl("start", SERVICE_NAME)
        elif not was_active:
            candidate_attempted = True
            _systemctl("start", SERVICE_NAME)
        _wait_for_health(host, port, paths, readiness_timeout, observed_pids=candidate_pids)
        result = autostart_status(paths=paths, unit_path=path, interpreter=interpreter, env=environ)
        if not (
            result.installed
            and result.enabled
            and result.active
            and result.healthy
            and result.definition_current
        ):
            raise AutostartError(
                "systemd candidate did not remain installed, enabled, current, active, and healthy"
            )
    except Exception as exc:
        close_reservations(reservations)
        recovery_errors = []
        candidate_cleanup_proven = False
        _capture_runtime_systemd_pid(paths, candidate_pids)
        if irreversible_takeover:
            if not takeover_barrier_established:
                raise AutostartError(
                    "could not establish the disabled systemd takeover barrier; "
                    f"the prior unit bytes were left untouched: {exc}"
                ) from exc
            try:
                if candidate_attempted:
                    _cleanup_failed_candidate(paths, path, candidate_command, candidate_pids)
                candidate_cleanup_proven = True
                current = _definition_content(path) if candidate_written else None
                if current is not None:
                    parse_systemd_unit(current)
                    path.unlink()
                    _fsync_directory(path.parent)
                _systemctl("daemon-reload", check=False)
            except Exception as recovery_exc:
                recovery_errors.append(f"candidate cleanup failed: {recovery_exc}")
            if displaced_recovery is not None:
                recovery_errors.append(
                    f"displaced unit remains non-loadable at {displaced_recovery}"
                )
            else:
                recovery_errors.append(
                    "displaced unit remains non-loadable, but its exact bytes were not retained"
                )
        else:
            try:
                _rollback_failed_enable(
                    path=path,
                    previous=existing,
                    changed=changed,
                    was_active=was_active,
                    was_enabled=was_enabled,
                    prior_identity=disk_identity,
                    current_paths=paths,
                    candidate_pids=candidate_pids,
                    candidate_attempted=candidate_attempted,
                    candidate_command=candidate_command,
                )
                candidate_cleanup_proven = True
                if displaced_recovery is not None:
                    displaced_recovery.unlink(missing_ok=True)
            except Exception as recovery_exc:
                recovery_errors.append(f"unit rollback failed: {recovery_exc}")
        if stopped_manual and manual is not None and candidate_cleanup_proven:
            try:
                _restore_manual_daemon(paths, manual.state)
            except Exception as recovery_exc:
                recovery_errors.append(f"manual daemon restore failed: {recovery_exc}")
        if recovery_errors:
            if lost_prior_bytes:
                recovery_errors.append(LOST_UNIT_BYTES_DETAIL)
            raise AutostartError(f"{exc}; {'; '.join(recovery_errors)}") from exc
        if lost_prior_bytes:
            raise AutostartError(f"{exc}; {LOST_UNIT_BYTES_DETAIL}") from exc
        if isinstance(exc, AutostartError):
            raise
        raise AutostartError(f"failed to enable daemon autostart: {exc}") from exc
    finally:
        close_reservations(reservations)
    cleanup_warnings = _post_commit_recovery_cleanup(
        path,
        paths,
        interpreter,
        recovery=displaced_recovery,
        authorized_recoveries=authorized_recoveries,
    )
    details = ([LOST_UNIT_BYTES_DETAIL] if lost_prior_bytes else []) + cleanup_warnings
    return _status_with_detail(result, "; ".join(details)) if details else result


def disable_autostart(
    *,
    paths: Optional[GlobalDataPaths] = None,
    unit_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    interpreter: Optional[Path] = None,
    takeover: bool = False,
) -> AutostartStatus:
    _require_transaction_locks()
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    path = unit_path or resolve_systemd_unit_path()
    _ensure_supported()
    detached = daemon_status(paths)
    detached_remains = bool(
        detached.running and detached.state.get("manager") in {None, "detached"}
    )
    disabled_detail = (
        f"disabled; detached daemon pid {detached.state.get('pid')} remains running"
        if detached_remains
        else "disabled"
    )
    live_manager = detached.state.get("manager") if detached.running else None
    if not path.exists():
        recovered = inspect(
            paths=paths,
            definition_path=path,
            interpreter=absolute_path(interpreter or Path(sys.executable)),
        )
        if recovered.source == "recovery":
            if not recovered.current_home_owned and not takeover:
                raise AutostartError(
                    "the systemd recovery definition belongs to another installation; "
                    "re-run with --takeover to remove it"
                )
            recovery_snapshot = _recovery_identities(
                path,
                current_paths=paths,
                expected_interpreter=absolute_path(interpreter or Path(sys.executable)),
            )
            authorized_recoveries = _authorized_recovery_snapshot(
                recovery_snapshot,
                takeover=takeover,
                target_identity=recovered,
            )
            _ensure_systemd_user_manager()
            _systemctl("disable", SERVICE_NAME, check=False)
            if _systemctl_truth("is-enabled", SERVICE_NAME):
                raise AutostartError(
                    "systemd unit remains persistently enabled after recovery disable"
                )
            result = AutostartStatus(False, False, False, False, True, path, disabled_detail)
            cleanup_warnings = _post_commit_recovery_cleanup(
                path,
                paths,
                absolute_path(interpreter or Path(sys.executable)),
                authorized_recoveries=authorized_recoveries,
            )
            return (
                _status_with_detail(result, "; ".join(cleanup_warnings))
                if cleanup_warnings
                else result
            )
        if recovered.loaded:
            if not recovered.owned:
                raise AutostartError(
                    "refusing to stop an unattributable active systemd service whose unit "
                    "definition is missing"
                )
            if not recovered.current_home_owned and not takeover:
                raise AutostartError(
                    "the active systemd service belongs to another agent-collab installation; "
                    "re-run with --takeover to stop it"
                )
            expected_interpreter = absolute_path(interpreter or Path(sys.executable))
            recovery_snapshot = _recovery_identities(
                path,
                current_paths=paths,
                expected_interpreter=expected_interpreter,
            )
            authorized_recoveries = _authorized_recovery_snapshot(
                recovery_snapshot,
                takeover=takeover,
                target_identity=recovered,
            )
            _ensure_systemd_user_manager()
            _systemctl("disable", SERVICE_NAME, check=False)
            if _systemctl_truth("is-enabled", SERVICE_NAME):
                raise AutostartError(
                    "systemd unit remains persistently enabled after missing-definition disable"
                )
            _stop_managed_and_reap(paths, recovered.pid)
            _systemctl("daemon-reload")
            result = AutostartStatus(False, False, False, False, True, path, disabled_detail)
            cleanup_warnings = _post_commit_recovery_cleanup(
                path,
                paths,
                expected_interpreter,
                authorized_recoveries=authorized_recoveries,
            )
            return (
                _status_with_detail(result, "; ".join(cleanup_warnings))
                if cleanup_warnings
                else result
            )
        if detached.running and live_manager == MANAGER:
            raise AutostartError(
                f"systemd-owned daemon pid {detached.state.get('pid')} is orphaned from the "
                "inactive unit; refusing to disable or signal it"
            )
        detail = (
            f"not installed; detached daemon pid {detached.state.get('pid')} remains running"
            if detached_remains
            else "not installed"
        )
        return AutostartStatus(False, False, False, False, True, path, detail)
    if not managed_unit_installed(path):
        raise AutostartError(f"refusing to remove unmanaged systemd unit: {path}")
    expected_interpreter = absolute_path(interpreter or Path(sys.executable))
    identity = inspect(
        paths=paths,
        definition_path=path,
        interpreter=expected_interpreter,
    )
    recovery_snapshot = _recovery_identities(
        path,
        current_paths=paths,
        expected_interpreter=expected_interpreter,
    )
    definition_content = _definition_content(path)
    if definition_content is None:
        raise AutostartError(f"systemd unit disappeared during disable: {path}")
    disk_identity = parse_systemd_unit(
        definition_content,
        current_paths=paths,
        expected_interpreter=expected_interpreter,
    )
    authorized_recoveries = _authorized_recovery_snapshot(
        recovery_snapshot,
        takeover=takeover,
        target_identity=disk_identity,
    )
    if not identity.current_home_owned and not takeover:
        raise AutostartError(
            "the per-user systemd registration belongs to another agent-collab installation; "
            "re-run with --takeover to remove it"
        )
    if identity.loaded and (identity.command is None or identity.effective_home is None):
        raise AutostartError(
            "refusing to stop an unattributable active systemd service during takeover"
        )
    if detached.running and live_manager == MANAGER and not identity.loaded:
        raise AutostartError(
            f"systemd-owned daemon pid {detached.state.get('pid')} is orphaned from the inactive "
            "unit; refusing to disable or signal it"
        )
    _ensure_systemd_user_manager()
    _systemctl("disable", SERVICE_NAME)
    if _systemctl_truth("is-enabled", SERVICE_NAME):
        raise AutostartError("systemd unit remains persistently enabled after disable")
    captured_pid = identity.pid if identity.loaded else None
    _stop_managed_and_reap(paths, captured_pid)
    recovery = _quarantine_unit(
        path,
        paths,
        expected_interpreter,
        authorized_recoveries=authorized_recoveries,
        expected_content=definition_content,
    )
    lost_prior_bytes = recovery is None
    _systemctl("daemon-reload")
    daemon_status(paths)
    detail = f"{disabled_detail}; {LOST_UNIT_BYTES_DETAIL}" if lost_prior_bytes else disabled_detail
    result = AutostartStatus(False, False, False, False, True, path, detail)
    cleanup_warnings = _post_commit_recovery_cleanup(
        path,
        paths,
        expected_interpreter,
        recovery=recovery,
        authorized_recoveries=authorized_recoveries,
    )
    return _status_with_detail(result, "; ".join(cleanup_warnings)) if cleanup_warnings else result


def autostart_status(
    *,
    paths: Optional[GlobalDataPaths] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    path = unit_path or resolve_systemd_unit_path()
    expected_interpreter = absolute_path(interpreter or Path(sys.executable))
    installed = managed_unit_installed(path)
    if not installed:
        recovered = inspect(
            paths=paths, definition_path=path, interpreter=interpreter or Path(sys.executable)
        )
        healthy = False
        detail = recovered.detail
        if (
            recovered.loaded
            and recovered.current_home_owned
            and recovered.command is not None
            and recovered.pid is not None
        ):
            healthy, detail = _health(
                host=recovered.command.host,
                port=recovered.command.port,
                paths=paths,
                expected_pid=recovered.pid,
            )
            after_pid = _systemd_main_pid()
            if after_pid != recovered.pid:
                healthy = False
                detail = "systemd MainPID changed during status readiness probe"
        return AutostartStatus(
            False,
            False,
            recovered.loaded,
            healthy,
            False,
            path,
            detail,
        )
    _ensure_supported()
    _ensure_systemd_user_manager()
    recoveries = _recovery_identities(
        path,
        current_paths=paths,
        expected_interpreter=expected_interpreter,
    )
    recovery_collisions = _recovery_collision_paths(path, recoveries)
    enabled = _systemctl_truth("is-enabled", SERVICE_NAME)
    manager_occupied = _systemctl_truth("is-active", SERVICE_NAME)
    recorded_interpreter = _recorded_interpreter(path)
    definition_current = bool(
        recorded_interpreter
        and recorded_interpreter.exists()
        and recorded_interpreter == expected_interpreter
    )
    token_home_owned = False
    parse_detail: Optional[str] = None
    try:
        content = _definition_content(path)
        if content is None:
            raise AutostartError(f"systemd unit disappeared during status: {path}")
        parsed = parse_systemd_unit(
            content,
            current_paths=paths,
            expected_interpreter=expected_interpreter,
        )
        definition_current = definition_current and parsed.current_home_owned
        token_home_owned = parsed.effective_home == paths.home.resolve()
        host = parsed.command.host if parsed.command else _state_host(paths)
        port = parsed.command.port if parsed.command else _state_port(paths)
    except (OSError, AutostartError) as exc:
        host, port = _state_host(paths), _state_port(paths)
        definition_current = False
        parse_detail = str(exc)
    pid = _systemd_main_pid() if manager_occupied else None
    active = pid is not None
    if active and token_home_owned:
        healthy, health_detail = _health(host=host, port=port, paths=paths, expected_pid=pid)
        detail = health_detail
        after_pid = _systemd_main_pid()
        if after_pid != pid:
            healthy = False
            detail = "systemd MainPID changed during status readiness probe"
    elif active:
        healthy = False
        detail = (
            "native registration belongs to another agent-collab home; authenticated health "
            "was not probed with this home's token"
        )
    elif manager_occupied:
        healthy = False
        detail = "systemd unit is transitioning without an attributable MainPID"
    else:
        healthy = False
        detail = "installed but inactive"
    if parse_detail:
        detail = f"{detail}; malformed managed definition: {parse_detail}"
    if recoveries:
        detail += "; recovery artifacts remain at " + ", ".join(
            str(recovery) for recovery, _identity in recoveries
        )
    if recovery_collisions:
        detail += "; preserved unowned systemd recovery collision at " + ", ".join(
            str(recovery) for recovery in recovery_collisions
        )
    return AutostartStatus(
        installed,
        enabled,
        active,
        bool(active and healthy),
        definition_current,
        path,
        detail,
    )


def start_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> AutostartStatus:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    _require_current(identity)
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not identity.loaded:
        raise AutostartError(
            f"systemd-owned daemon pid {live.state.get('pid')} is orphaned from the inactive unit; "
            "stop it manually before starting another generation"
        )
    if identity.loaded and identity.command and identity.pid is not None:
        healthy, _ = _health(
            host=identity.command.host,
            port=identity.command.port,
            paths=paths,
            expected_pid=identity.pid,
        )
        if healthy:
            return autostart_status(paths=paths, unit_path=definition_path, interpreter=interpreter)
        if not identity.enabled:
            raise AutostartError(
                "systemd daemon is live but autostart is persistently disabled; run "
                "'agent-collab daemon autostart enable' before attempting recovery"
            )
        raise AutostartError(
            "systemd daemon is live but unhealthy; use 'agent-collab daemon restart' "
            "or inspect daemon logs"
        )
    if not identity.enabled:
        raise AutostartError(
            "systemd autostart is persistently disabled; run 'agent-collab daemon autostart enable'"
        )
    stopped_detached = None
    if live.running and live.state.get("manager") in {None, "detached"}:
        stop_daemon(paths, _lifecycle_locked=True)
        stopped_detached = live
    command = identity.command
    if command is None:
        raise AutostartError("cannot determine the systemd daemon endpoint")
    reservations = []
    candidate_pids: set[int] = set()
    try:
        reservations = reserve_server_endpoint(command.host, command.port)
        close_reservations(reservations)
        _systemctl("start", SERVICE_NAME)
        _wait_for_health(command.host, command.port, paths, 5.0, observed_pids=candidate_pids)
        result = autostart_status(paths=paths, unit_path=definition_path, interpreter=interpreter)
        _require_started_status(result)
    except Exception as exc:
        errors = []
        candidate_stopped = False
        try:
            _cleanup_failed_manual_candidate(paths, definition_path, command, candidate_pids)
            candidate_stopped = True
        except Exception as cleanup_exc:
            errors.append(f"candidate systemd daemon cleanup failed: {cleanup_exc}")
        if stopped_detached is not None and candidate_stopped:
            try:
                _restore_manual_daemon(paths, stopped_detached.state)
            except Exception as restore_exc:
                errors.append(f"detached daemon restore failed: {restore_exc}")
        detail = f"; {'; '.join(errors)}" if errors else ""
        raise AutostartError(f"{exc}{detail}") from exc
    finally:
        close_reservations(reservations)
    return result


def stop_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> AutostartStatus:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    if identity.loaded and not identity.owned:
        raise AutostartError(
            "refusing to stop systemd because its active process is not attributable "
            "to agent-collab"
        )
    if identity.owned:
        _require_current(identity)
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not identity.loaded:
        raise AutostartError(
            f"systemd-owned daemon pid {live.state.get('pid')} is orphaned from the inactive unit; "
            "refusing to signal it"
        )
    if identity.loaded:
        _stop_managed_and_reap(paths, identity.pid)
    detached = daemon_status(paths)
    if detached.running and detached.state.get("manager") in {None, "detached"}:
        stop_daemon(paths, _lifecycle_locked=True)
    return autostart_status(paths=paths, unit_path=definition_path, interpreter=interpreter)


def restart_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> AutostartStatus:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    _require_current(identity)
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not identity.loaded:
        raise AutostartError(
            f"systemd-owned daemon pid {live.state.get('pid')} is orphaned from the inactive "
            "unit; stop it manually before restarting"
        )
    if not identity.enabled:
        raise AutostartError(
            "systemd autostart is persistently disabled; run 'agent-collab daemon autostart enable'"
        )
    content = _definition_content(definition_path)
    definition = (
        parse_systemd_unit(content, current_paths=paths, expected_interpreter=interpreter)
        if content is not None
        else None
    )
    if not identity.installed or definition is None or definition.command is None:
        raise AutostartError(
            "cannot restart systemd without its unit; run 'agent-collab daemon autostart enable'"
        )
    command = definition.command
    was_running = identity.loaded
    live_manager = live.state.get("manager") if live.running else None
    if live.running and live_manager not in {None, "detached", MANAGER}:
        raise AutostartError(
            f"daemon pid {live.state.get('pid')} is owned by {live_manager}; refusing systemd restart"
        )
    stopped_detached = None
    if live.running and live_manager in {None, "detached"}:
        stop_daemon(paths, _lifecycle_locked=True)
        stopped_detached = live
    reservations = []
    candidate_pids: set[int] = set()
    try:
        if was_running:
            _stop_managed_and_reap(paths, identity.pid)
        reservations = reserve_server_endpoint(command.host, command.port)
        close_reservations(reservations)
        _systemctl("start", SERVICE_NAME)
        _wait_for_health(command.host, command.port, paths, 5.0, observed_pids=candidate_pids)
        result = autostart_status(paths=paths, unit_path=definition_path, interpreter=interpreter)
        _require_started_status(result)
    except Exception as exc:
        errors = []
        candidate_stopped = False
        try:
            _cleanup_failed_manual_candidate(paths, definition_path, command, candidate_pids)
            candidate_stopped = True
        except Exception as cleanup_exc:
            errors.append(f"candidate systemd daemon cleanup failed: {cleanup_exc}")
        if was_running and candidate_stopped:
            try:
                _systemctl("start", SERVICE_NAME)
                _wait_for_health(command.host, command.port, paths, 5.0)
            except Exception as restore_exc:
                errors.append(f"prior systemd daemon restore failed: {restore_exc}")
        if stopped_detached is not None and candidate_stopped:
            try:
                _restore_manual_daemon(paths, stopped_detached.state)
            except Exception as restore_exc:
                errors.append(f"detached daemon restore failed: {restore_exc}")
        detail = f"; {'; '.join(errors)}" if errors else ""
        raise AutostartError(f"{exc}{detail}") from exc
    finally:
        close_reservations(reservations)
    return result


def _require_current(identity: ManagedServiceIdentity) -> None:
    if not identity.owned:
        raise AutostartError("systemd registration is not owned by agent-collab")
    if not identity.current_home_owned:
        interpreter = identity.command.interpreter if identity.command else "unknown"
        raise AutostartError(
            "the per-user systemd registration belongs to another agent-collab installation "
            f"(home {identity.effective_home or 'unknown'}, interpreter {interpreter})"
        )


def _require_started_status(result: AutostartStatus) -> None:
    if not (
        result.installed
        and result.enabled
        and result.active
        and result.healthy
        and result.definition_current
    ):
        raise AutostartError(
            "systemd daemon did not remain installed, enabled, current, active, and healthy"
        )


def quiesce_for_install_locked(
    *, paths: GlobalDataPaths, definition_path: Path, interpreter: Path
) -> dict[str, object]:
    _require_transaction_locks()
    identity = inspect(paths=paths, definition_path=definition_path, interpreter=interpreter)
    if identity.owned and not identity.current_home_owned:
        raise AutostartError(
            "the systemd registration belongs to another agent-collab installation"
        )
    content = _definition_content(definition_path)
    disk = (
        parse_systemd_unit(content, current_paths=paths, expected_interpreter=interpreter)
        if content is not None
        else None
    )
    if identity.loaded and (disk is None or identity.command is None):
        raise AutostartError(
            "cannot upgrade while the active systemd command cannot be attributed to the unit"
        )
    if identity.loaded and disk is not None and identity.command != disk.command:
        raise AutostartError(
            "cannot upgrade while systemd's running arguments differ from the unit; "
            "run 'agent-collab daemon restart' or 'agent-collab daemon autostart enable' first"
        )
    live = daemon_status(paths)
    if live.running and live.state.get("manager") == MANAGER and not identity.loaded:
        raise AutostartError(
            f"cannot upgrade while systemd-owned daemon pid {live.state.get('pid')} is orphaned "
            "from the inactive unit; stop it manually first"
        )
    snapshot: dict[str, object] = {
        "manager": MANAGER,
        "owned": identity.owned,
        "installed": identity.installed,
        "loaded": identity.loaded,
        "running": identity.loaded,
        "enabled": identity.enabled,
        "command": identity.command,
    }
    if identity.owned and identity.enabled:
        _systemctl("disable", SERVICE_NAME)
        if _systemctl_truth("is-enabled", SERVICE_NAME):
            raise AutostartError("could not establish the systemd upgrade autoload barrier")
    if identity.loaded:
        _stop_managed_and_reap(paths, identity.pid)
    return snapshot


def restore_after_install_locked(
    snapshot: Mapping[str, object],
    *,
    paths: GlobalDataPaths,
    definition_path: Path,
    interpreter: Path,
) -> AutostartStatus:
    _require_transaction_locks()
    mutation_failed = bool(snapshot.get("owned") and not snapshot.get("mutation_succeeded", True))
    was_running = bool(snapshot.get("running"))
    if mutation_failed and not was_running:
        if snapshot.get("enabled"):
            try:
                _ensure_durable_install(interpreter)
                if _systemctl_truth("is-active", SERVICE_NAME):
                    raise AutostartError(
                        "previously stopped systemd service became active during install recovery"
                    )
                _systemctl("enable", SERVICE_NAME)
                if not _systemctl_truth("is-enabled", SERVICE_NAME):
                    raise AutostartError("could not restore the prior systemd enabled state")
                if _systemctl_truth("is-active", SERVICE_NAME):
                    raise AutostartError(
                        "restoring systemd enablement unexpectedly started the stopped service"
                    )
                result = autostart_status(
                    paths=paths, unit_path=definition_path, interpreter=interpreter
                )
                _validate_restored_status(result, snapshot)
            except Exception as exc:
                _establish_disabled_stopped_after_failure(exc)
            return result
        _establish_disabled_stopped_after_failure(None)
        result = autostart_status(paths=paths, unit_path=definition_path, interpreter=interpreter)
        _validate_restored_status(result, snapshot)
        return result
    candidate_pids: set[int] = set()
    legacy_pid: Optional[int] = None
    snapshot_command = snapshot.get("command")
    try:
        if mutation_failed:
            _ensure_durable_install(interpreter)
        if was_running:
            _systemctl("start", SERVICE_NAME)
            if not isinstance(snapshot_command, ManagedCommand):
                raise AutostartError(
                    "cannot restore systemd daemon: prior command identity is missing"
                )
            try:
                _wait_for_health(
                    snapshot_command.host,
                    snapshot_command.port,
                    paths,
                    5.0,
                    observed_pids=candidate_pids,
                )
            except _ReadyEndpointUnavailableError:
                if not mutation_failed:
                    raise
                legacy_pid = _wait_for_legacy_health(
                    snapshot_command.host,
                    snapshot_command.port,
                    paths,
                    5.0,
                    observed_pids=candidate_pids,
                )
        if snapshot.get("enabled"):
            _systemctl("enable", SERVICE_NAME)
        result = autostart_status(paths=paths, unit_path=definition_path, interpreter=interpreter)
        if legacy_pid is not None:
            result = _validated_legacy_restore_status(result, paths, legacy_pid)
        _validate_restored_status(result, snapshot)
    except Exception as exc:
        try:
            if not isinstance(snapshot_command, ManagedCommand):
                raise AutostartError("failed candidate command identity is missing")
            _cleanup_failed_candidate(paths, definition_path, snapshot_command, candidate_pids)
        except Exception as cleanup_exc:
            raise AutostartError(
                "could not restore systemd after install and could not prove the service "
                f"disabled and stopped: {exc}; candidate cleanup failed: {cleanup_exc}"
            ) from exc
        raise SafeManagedRestoreError(
            f"could not restore systemd after install; autostart remains disabled: {exc}"
        ) from exc
    return result


def _validate_restored_status(result: AutostartStatus, snapshot: Mapping[str, object]) -> None:
    was_installed = bool(snapshot.get("installed"))
    was_running = bool(snapshot.get("running"))
    was_enabled = bool(snapshot.get("enabled"))
    if result.installed != was_installed:
        raise AutostartError("restored systemd service did not preserve prior installed state")
    if was_installed and not result.definition_current:
        raise AutostartError("restored systemd unit is not current for this installation")
    if result.enabled != was_enabled:
        raise AutostartError("restored systemd service did not preserve prior enabled state")
    if was_running and (not result.active or not result.healthy):
        raise AutostartError("restored systemd daemon did not remain active and PID-bound healthy")
    if not was_running and result.active:
        raise AutostartError("restored systemd service did not preserve its prior stopped intent")


def _validated_legacy_restore_status(
    result: AutostartStatus, paths: GlobalDataPaths, expected_pid: int
) -> AutostartStatus:
    """Carry a successful legacy /health proof across the final status check."""

    current_pid = _systemd_main_pid()
    runtime = daemon_status(paths)
    if (
        not result.active
        or current_pid != expected_pid
        or not runtime.running
        or runtime.state.get("manager") != MANAGER
        or runtime.state.get("pid") != expected_pid
    ):
        raise AutostartError(
            "legacy systemd daemon changed generation after its compatibility health probe"
        )
    return AutostartStatus(
        result.installed,
        result.enabled,
        result.active,
        True,
        result.definition_current,
        result.definition_path,
        "healthy (legacy authenticated endpoint compatibility)",
        result.manager,
    )


def _ensure_supported() -> None:
    if not sys.platform.startswith("linux"):
        raise AutostartError("daemon autostart currently requires Linux with systemd user services")


def _ensure_systemd_user_manager() -> None:
    result = _systemctl("show-environment", check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemd user manager unavailable").strip()
        raise AutostartError(f"cannot use systemd user services: {detail}")


def _systemd_manager_environment() -> dict[str, str]:
    result = _systemctl("show-environment", check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemd user manager unavailable").strip()
        raise AutostartError(f"cannot read systemd user-manager environment: {detail}")
    environment: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key:
            raise AutostartError("systemd user-manager environment was malformed")
        if key in {"HOME", "XDG_CONFIG_HOME"} and key in environment:
            raise AutostartError(
                f"systemd user-manager environment returned duplicate {key}; path is indeterminate"
            )
        environment[key] = value
    return environment


def _validated_absolute_environment_path(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(character in value for character in ("\0", "\n", "\r")):
        raise AutostartError(f"systemd user manager returned invalid {name}: {value!r}")
    return path


def _systemd_main_pid() -> Optional[int]:
    result = _systemctl("show", SERVICE_NAME, "--property=MainPID", "--value", check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"cannot determine systemd MainPID: {detail}")
    raw_pid = (result.stdout or "").strip()
    if not raw_pid:
        raise AutostartError("cannot determine systemd MainPID: empty systemctl output")
    try:
        pid = int(raw_pid)
    except ValueError as exc:
        raise AutostartError(
            f"cannot determine systemd MainPID: invalid value {raw_pid!r}"
        ) from exc
    if pid < 0:
        raise AutostartError(f"cannot determine systemd MainPID: invalid value {raw_pid!r}")
    return pid if pid > 0 else None


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
            "autostart requires a durable installed command; run ./agent_collab.sh install first"
        )


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AutostartError(f"cannot execute systemctl --user: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"systemctl --user {' '.join(args)} failed: {detail}")
    return result


def _systemctl_truth(action: str, service: str) -> bool:
    result = _systemctl(action, service, check=False)
    states = (result.stdout or "").splitlines()
    if len(states) != 1 or not states[0].strip():
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"systemctl --user {action} returned indeterminate state: {detail}")
    state = states[0].strip()
    if action == "is-active":
        if state in {"active", "reloading"} and result.returncode == 0:
            return True
        if state in {"activating", "deactivating", "maintenance", "refreshing"}:
            return True
        if state in {"inactive", "failed", "unknown"} and result.returncode != 0:
            return False
    elif action == "is-enabled":
        if state == "enabled" and result.returncode == 0:
            return True
        if state in {
            "disabled",
            "enabled-runtime",
            "linked",
            "linked-runtime",
            "alias",
            "masked",
            "masked-runtime",
            "static",
            "indirect",
            "generated",
            "transient",
            "not-found",
            "bad",
        }:
            return False
    else:
        raise AutostartError(f"unsupported systemctl truth query: {action}")
    raise AutostartError(
        f"systemctl --user {action} returned inconsistent state {state!r} "
        f"with exit {result.returncode}"
    )


def _stop_managed_and_reap(paths: GlobalDataPaths, expected_pid: Optional[int]) -> None:
    """Stop the unit, prove its captured generation exited, and reap matching stale state."""

    observed_pid = _systemd_main_pid()
    if observed_pid != expected_pid:
        raise AutostartError(
            "refusing to stop systemd service because its process generation changed "
            f"from MainPID {expected_pid or 0} to {observed_pid or 0}"
        )
    _systemctl("stop", SERVICE_NAME)
    if _systemctl_truth("is-active", SERVICE_NAME):
        raise AutostartError("systemd service remains active after stop")
    if expected_pid is not None:
        remaining_pid = _systemd_main_pid()
        if remaining_pid is not None:
            raise AutostartError(
                f"systemd service still reports MainPID {remaining_pid} after stop"
            )
        if _pid_alive(expected_pid):
            raise AutostartError(f"systemd service MainPID {expected_pid} remains alive after stop")
    daemon_status(paths)


def _capture_runtime_systemd_pid(paths: GlobalDataPaths, observed_pids: set[int]) -> None:
    runtime = daemon_status(paths)
    pid = runtime.state.get("pid")
    if runtime.running and runtime.state.get("manager") == MANAGER and isinstance(pid, int):
        observed_pids.add(pid)


def _prove_pids_exited(observed_pids: set[int]) -> None:
    for pid in sorted(observed_pids):
        if _pid_alive(pid):
            raise AutostartError(f"systemd candidate pid {pid} remains alive after cleanup")


def _establish_disabled_stopped_after_failure(cause: Optional[BaseException]) -> None:
    """Establish the installer fail-safe and preserve the original restoration error."""

    _systemctl("disable", "--now", SERVICE_NAME, check=False)
    if _systemctl_truth("is-enabled", SERVICE_NAME) or _systemctl_truth("is-active", SERVICE_NAME):
        suffix = f": {cause}" if cause is not None else ""
        raise AutostartError(
            "install did not complete and systemd could not prove the service disabled and "
            f"stopped; inspect the user unit before logging out{suffix}"
        ) from cause
    if cause is not None:
        raise SafeManagedRestoreError(
            "could not restore the prior enabled but stopped systemd intent; autostart remains "
            f"disabled and the daemon stopped: {cause}"
        ) from cause


def _wait_for_health(
    host: str,
    port: int,
    paths: GlobalDataPaths,
    timeout: float,
    *,
    observed_pids: Optional[set[int]] = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "not ready"
    while time.monotonic() < deadline:
        before_pid = _systemd_main_pid()
        if before_pid is None:
            last_detail = "systemd unit has no MainPID"
            time.sleep(0.05)
            continue
        if observed_pids is not None:
            observed_pids.add(before_pid)
        healthy, last_detail = _health(
            host=host,
            port=port,
            paths=paths,
            expected_pid=before_pid,
        )
        after_pid = _systemd_main_pid()
        if observed_pids is not None and after_pid is not None:
            observed_pids.add(after_pid)
        if healthy and after_pid == before_pid:
            return
        if after_pid != before_pid:
            last_detail = "systemd MainPID changed during readiness probe"
        time.sleep(0.05)
    message = f"daemon service did not become healthy: {last_detail}"
    if last_detail == READY_ENDPOINT_MISSING_DETAIL:
        raise _ReadyEndpointUnavailableError(message)
    raise AutostartError(message)


def _health(
    *, host: str, port: int, paths: GlobalDataPaths, expected_pid: Optional[int] = None
) -> tuple[bool, str]:
    healthy, detail, _ = readiness(
        host=host,
        port=port,
        paths=paths,
        expected_manager=MANAGER,
        expected_pid=expected_pid,
    )
    return healthy, detail


def _wait_for_legacy_health(
    host: str,
    port: int,
    paths: GlobalDataPaths,
    timeout: float,
    *,
    observed_pids: Optional[set[int]] = None,
) -> int:
    """Restore a pre-/ready release while still binding health to systemd MainPID state."""

    deadline = time.monotonic() + timeout
    detail = "legacy daemon is not ready"
    while time.monotonic() < deadline:
        before_pid = _systemd_main_pid()
        if observed_pids is not None and before_pid is not None:
            observed_pids.add(before_pid)
        runtime = daemon_status(paths)
        if (
            before_pid is not None
            and runtime.running
            and runtime.state.get("manager") == MANAGER
            and runtime.state.get("pid") == before_pid
        ):
            if not _pid_owns_listening_endpoint(before_pid, host, port):
                detail = "systemd MainPID does not own the legacy daemon endpoint"
                time.sleep(0.05)
                continue
            healthy, detail = legacy_health(host=host, port=port, paths=paths)
            after_pid = _systemd_main_pid()
            if observed_pids is not None and after_pid is not None:
                observed_pids.add(after_pid)
            after_runtime = daemon_status(paths)
            if (
                healthy
                and after_pid == before_pid
                and after_runtime.running
                and after_runtime.state.get("manager") == MANAGER
                and after_runtime.state.get("pid") == before_pid
                and _pid_owns_listening_endpoint(before_pid, host, port)
            ):
                return before_pid
            if after_pid != before_pid:
                detail = "systemd MainPID changed during legacy health probe"
        time.sleep(0.05)
    raise AutostartError(f"legacy systemd daemon did not become healthy: {detail}")


def _pid_owns_listening_endpoint(pid: int, host: str, port: int) -> bool:
    """Prove through procfs that one PID owns the socket reached by a legacy probe."""

    try:
        fd_entries = list((Path("/proc") / str(pid) / "fd").iterdir())
        expected = {
            sockaddr[0]
            for family, socktype, _protocol, _canonname, sockaddr in socket.getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
            if family in {socket.AF_INET, socket.AF_INET6} and socktype == socket.SOCK_STREAM
        }
    except (OSError, socket.gaierror):
        return False
    socket_inodes: set[str] = set()
    for entry in fd_entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            socket_inodes.add(target.removeprefix("socket:[").removesuffix("]"))
    if not socket_inodes or not expected:
        return False
    tables = (
        (Path("/proc/net/tcp"), socket.AF_INET),
        (Path("/proc/net/tcp6"), socket.AF_INET6),
    )
    for table, family in tables:
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A" or fields[9] not in socket_inodes:
                continue
            address_hex, separator, port_hex = fields[1].partition(":")
            if not separator:
                continue
            try:
                listening_port = int(port_hex, 16)
                listening_host = _decode_proc_address(address_hex, family)
            except (OSError, ValueError):
                continue
            if listening_port != port:
                continue
            if listening_host in {"0.0.0.0", "::"} or listening_host in expected:
                return True
    return False


def _decode_proc_address(value: str, family: int) -> str:
    raw = bytes.fromhex(value)
    if family == socket.AF_INET:
        if len(raw) != 4:
            raise ValueError("invalid procfs IPv4 address")
        packed = raw[::-1]
    else:
        if len(raw) != 16:
            raise ValueError("invalid procfs IPv6 address")
        packed = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
    return socket.inet_ntop(family, packed)


def _restore_manual_daemon(paths: GlobalDataPaths, state: Mapping[str, object]) -> None:
    host = str(state.get("host") or "127.0.0.1")
    try:
        port = int(state.get("port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    raw_workdir = state.get("default_workdir")
    workdir = Path(str(raw_workdir)) if raw_workdir else None
    try:
        start_daemon(
            paths,
            host=host,
            port=port,
            default_workdir=workdir,
            _lifecycle_locked=True,
        )
    except Exception as exc:
        raise AutostartError(
            f"service startup failed and the previous manual daemon could not be restored: {exc}"
        ) from exc


def _safe_port(value: object) -> int:
    try:
        return int(value or 8765)
    except (TypeError, ValueError):
        return 8765


def _require_transaction_locks() -> None:
    if not lifecycle_lock_held() or not registration_lock_held():
        raise RuntimeError("systemd lifecycle mutation requires lifecycle and registration locks")


def _runtime_managed_command(status) -> Optional[ManagedCommand]:
    if not status.running or status.state.get("manager") != MANAGER:
        return None
    argv = status.state.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return None
    try:
        return parse_managed_command(
            argv,
            platform_manager=MANAGER,
            allow_legacy_systemd_manager=True,
        )
    except AutostartError:
        return None


def _runtime_status_home(status) -> Optional[Path]:
    raw_home = status.state.get("home")
    if not status.running or not raw_home:
        return None
    return Path(str(raw_home)).expanduser().resolve()


def _systemd_process_identity(pid: int) -> tuple[Optional[ManagedCommand], Optional[Path]]:
    """Recover the running MainPID's typed command/home when caller-home state is absent."""

    try:
        raw_argv = Path(f"/proc/{pid}/cmdline").read_bytes()
        argv = [part.decode("utf-8") for part in raw_argv.split(b"\0") if part]
        command = parse_managed_command(
            argv,
            platform_manager=MANAGER,
            allow_legacy_systemd_manager=True,
        )
        environment: dict[str, str] = {}
        for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            if not entry or b"=" not in entry:
                continue
            key, value = entry.split(b"=", 1)
            environment[key.decode("utf-8")] = value.decode("utf-8")
        raw_home = environment.get("AGENT_COLLAB_HOME")
        effective_home = (
            Path(raw_home).expanduser().resolve()
            if raw_home
            else (account_home() / ".agent-collab").resolve()
        )
        return command, effective_home
    except (OSError, UnicodeDecodeError, AutostartError):
        return None, None


def _home_has_token(home: Path) -> bool:
    try:
        resolved = AgentCollabHome(root=home, config_path=home / "config.toml")
        return bool(load_daemon_token(home=resolved))
    except Exception:
        return False


def _rollback_failed_enable(
    *,
    path: Path,
    previous: Optional[str],
    changed: bool,
    was_active: bool,
    was_enabled: bool,
    prior_identity: Optional[ManagedServiceIdentity],
    current_paths: GlobalDataPaths,
    candidate_pids: set[int],
    candidate_attempted: bool,
    candidate_command: ManagedCommand,
) -> None:
    if candidate_attempted:
        _cleanup_failed_candidate(current_paths, path, candidate_command, candidate_pids)
    if changed:
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_private_text(path, previous)
        _systemctl("daemon-reload")
    if was_enabled and previous is not None:
        _systemctl("enable", SERVICE_NAME)
        if not _systemctl_truth("is-enabled", SERVICE_NAME):
            raise AutostartError("could not restore the prior systemd enabled state")
    elif not was_enabled:
        _systemctl("disable", SERVICE_NAME, check=False)
        if _systemctl_truth("is-enabled", SERVICE_NAME):
            raise AutostartError("could not restore the prior systemd disabled state")
    if was_active and previous is not None:
        _systemctl("start", SERVICE_NAME)
        command = prior_identity.command if prior_identity is not None else None
        if command is None:
            raise AutostartError("cannot authenticate restoration of the prior systemd service")
        prior_paths = current_paths
        if prior_identity and prior_identity.effective_home != current_paths.home.resolve():
            if prior_identity.effective_home is None:
                raise AutostartError("prior systemd home is unknown; rollback cannot be verified")
            prior_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(prior_identity.effective_home)}
            )
        _wait_for_health(command.host, command.port, prior_paths, 5.0)


def _cleanup_failed_candidate(
    paths: GlobalDataPaths,
    definition_path: Path,
    expected_command: ManagedCommand,
    observed_pids: set[int],
) -> None:
    """Disable a failed candidate, stop its current generation, and prove all observed PIDs gone."""

    _prove_cleanup_identity(paths, definition_path, expected_command)
    _systemctl("disable", SERVICE_NAME, check=False)
    if _systemctl_truth("is-enabled", SERVICE_NAME):
        raise AutostartError("could not prove the failed systemd candidate disabled")
    active = _systemctl_truth("is-active", SERVICE_NAME)
    current_pid = _systemd_main_pid()
    if current_pid is not None:
        observed_pids.add(current_pid)
    _capture_runtime_systemd_pid(paths, observed_pids)
    if active:
        if current_pid is None:
            raise AutostartError("active failed systemd candidate has no attributable MainPID")
        _prove_cleanup_identity(paths, definition_path, expected_command)
        _stop_managed_and_reap(paths, current_pid)
    if _systemctl_truth("is-active", SERVICE_NAME):
        raise AutostartError("could not prove the failed systemd candidate stopped")
    _capture_runtime_systemd_pid(paths, observed_pids)
    _prove_pids_exited(observed_pids)


def _cleanup_failed_manual_candidate(
    paths: GlobalDataPaths,
    definition_path: Path,
    expected_command: ManagedCommand,
    observed_pids: set[int],
) -> None:
    """Stop only the failed manual-start generation without changing login intent."""

    current_pid = _systemd_main_pid()
    if current_pid is not None:
        observed_pids.add(current_pid)
    _capture_runtime_systemd_pid(paths, observed_pids)
    _prove_cleanup_identity(paths, definition_path, expected_command)
    _stop_managed_and_reap(paths, current_pid)
    _capture_runtime_systemd_pid(paths, observed_pids)
    _prove_pids_exited(observed_pids)


def _prove_cleanup_identity(
    paths: GlobalDataPaths, definition_path: Path, expected_command: ManagedCommand
) -> None:
    current_pid = _systemd_main_pid()
    if current_pid is not None:
        runtime = daemon_status(paths)
        command = _runtime_managed_command(runtime)
        home = _runtime_status_home(runtime)
        if (
            not runtime.running
            or runtime.state.get("pid") != current_pid
            or runtime.state.get("manager") != MANAGER
            or command is None
            or home is None
        ):
            command, home = _systemd_process_identity(current_pid)
        if command != expected_command or home != paths.home.resolve():
            raise AutostartError(
                "refusing failed-candidate cleanup because the active systemd process "
                "identity changed"
            )
        return
    content = _definition_content(definition_path)
    if content is None:
        raise AutostartError(
            "refusing failed-candidate cleanup because its systemd definition disappeared"
        )
    identity = parse_systemd_unit(
        content,
        current_paths=paths,
        expected_interpreter=expected_command.interpreter,
    )
    if not identity.current_home_owned or identity.command != expected_command:
        raise AutostartError(
            "refusing failed-candidate cleanup because the systemd definition identity changed"
        )


def recovery_paths(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(path.name + RECOVERY_SUFFIX),
        path.with_name(path.name + RECOVERY_FALLBACK_SUFFIX),
    )


def _quarantine_unit(
    path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
    *,
    authorized_recoveries: Mapping[Path, str],
    expected_content: Optional[str] = None,
) -> Optional[Path]:
    before = _definition_snapshot(path)
    if expected_content is None and before is not None:
        expected_content = before[0]
    if before is None or before[0] != expected_content:
        raise AutostartError("systemd unit changed before it could be quarantined")
    for candidate in recovery_paths(path):
        if os.path.lexists(candidate):
            try:
                _validated_recovery_identity(
                    candidate,
                    current_paths=paths,
                    expected_interpreter=interpreter,
                )
            except AutostartError:
                continue
            if _read_optional(candidate) != authorized_recoveries.get(candidate):
                continue
            candidate.unlink()
        try:
            if _definition_snapshot(path) != before:
                raise AutostartError("systemd unit identity changed before quarantine rename")
            atomic_rename_noreplace(path, candidate)
            _fsync_directory(path.parent)
            after = _definition_snapshot(candidate)
            if after is None or after != before:
                if after is not None and not os.path.lexists(path):
                    atomic_rename_noreplace(candidate, path)
                    _fsync_directory(path.parent)
                raise AutostartError("systemd unit identity changed while it was being quarantined")
            _definition_snapshot(candidate, chmod_private=True)
            return candidate
        except OSError:
            continue
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise AutostartError(
            f"could not establish a non-loadable systemd recovery state: {exc}"
        ) from exc
    return None


def _remove_authorized_recoveries(
    path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
    *,
    authorized_recoveries: Mapping[Path, str],
) -> None:
    for candidate in recovery_paths(path):
        if not os.path.lexists(candidate):
            continue
        expected = authorized_recoveries.get(candidate)
        if expected is None:
            continue
        _validated_recovery_identity(
            candidate,
            current_paths=paths,
            expected_interpreter=interpreter,
        )
        if _read_optional(candidate) == expected:
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


def _authorized_recovery_snapshot(
    recoveries: list[tuple[Path, ManagedServiceIdentity]],
    *,
    takeover: bool,
    target_identity: Optional[ManagedServiceIdentity],
) -> dict[Path, str]:
    target_key = (
        (target_identity.effective_home, target_identity.command)
        if target_identity is not None
        else (None, None)
    )
    authorized: dict[Path, str] = {}
    for candidate, identity in recoveries:
        is_target = bool(
            takeover
            and target_key != (None, None)
            and (identity.effective_home, identity.command) == target_key
        )
        if identity.current_home_owned or is_target:
            content = _read_optional(candidate)
            if content is None:
                raise AutostartError(f"systemd recovery disappeared during snapshot: {candidate}")
            authorized[candidate] = content
    return authorized


def _recovery_identities(
    path: Path,
    *,
    current_paths: GlobalDataPaths,
    expected_interpreter: Path,
) -> list[tuple[Path, ManagedServiceIdentity]]:
    """Return owned recovery identities while preserving unrelated slot collisions."""

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
            continue
        found.append((candidate, identity))
    return found


def _recovery_collision_paths(
    path: Path, recoveries: list[tuple[Path, ManagedServiceIdentity]]
) -> tuple[Path, ...]:
    validated = {candidate for candidate, _identity in recoveries}
    return tuple(
        candidate
        for candidate in recovery_paths(path)
        if os.path.lexists(candidate) and candidate not in validated
    )


def _with_recovery_detail(
    detail: str,
    recoveries: list[tuple[Path, ManagedServiceIdentity]],
    collisions: tuple[Path, ...],
) -> str:
    notes = [f"systemd recovery artifact remains at {path}" for path, _identity in recoveries]
    notes.extend(f"preserved unowned systemd recovery collision at {path}" for path in collisions)
    return "; ".join([detail, *notes]) if notes else detail


def _validated_recovery_identity(
    path: Path,
    *,
    current_paths: GlobalDataPaths,
    expected_interpreter: Path,
) -> ManagedServiceIdentity:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AutostartError(f"cannot inspect systemd recovery {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AutostartError(f"systemd recovery is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise AutostartError(f"systemd recovery is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise AutostartError(f"systemd recovery must have mode 0600: {path}")
    content = _read_optional(path)
    if content is None:
        raise AutostartError(f"systemd recovery disappeared during inspection: {path}")
    return parse_systemd_unit(
        content,
        current_paths=current_paths,
        expected_interpreter=expected_interpreter,
    )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
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


def _status_with_detail(status: AutostartStatus, detail: str) -> AutostartStatus:
    combined = f"{status.detail}; {detail}" if status.detail else detail
    return AutostartStatus(
        status.installed,
        status.enabled,
        status.active,
        status.healthy,
        status.definition_current,
        status.definition_path,
        combined,
        status.manager,
    )


def _recorded_interpreter(path: Path) -> Optional[Path]:
    content = _definition_content(path) or ""
    for line in content.splitlines():
        if line.startswith(INTERPRETER_MARKER):
            return _absolute_path(Path(line[len(INTERPRETER_MARKER) :]))
    return None


def _absolute_path(path: Path) -> Path:
    """Make a path absolute without resolving a venv interpreter symlink."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _state(paths: GlobalDataPaths) -> Mapping[str, object]:
    try:
        value = json.loads(paths.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _state_host(paths: GlobalDataPaths) -> str:
    return str(_state(paths).get("host") or "127.0.0.1")


def _state_port(paths: GlobalDataPaths) -> int:
    try:
        return int(_state(paths).get("port") or 8765)
    except (TypeError, ValueError):
        return 8765


def _read_optional(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _definition_content(path: Path) -> Optional[str]:
    snapshot = _definition_snapshot(path)
    return snapshot[0] if snapshot is not None else None


def _definition_snapshot(
    path: Path, *, chmod_private: bool = False
) -> Optional[tuple[str, int, int]]:
    """Read one current-user regular unit without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AutostartError(f"cannot open systemd unit {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AutostartError(f"systemd unit is not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise AutostartError(f"systemd unit is not owned by the current user: {path}")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", newline="") as handle:
            content = handle.read()
        if chmod_private:
            os.fchmod(fd, 0o600)
        return content, info.st_dev, info.st_ino
    except OSError as exc:
        raise AutostartError(f"cannot read systemd unit {path}: {exc}") from exc
    finally:
        os.close(fd)


def _systemd_quote(value: str, *, escape_dollar: bool = False) -> str:
    if any(character in value for character in ("\n", "\r", "\0")):
        raise AutostartError("systemd unit values cannot contain newlines or NUL bytes")
    escaped = value.replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
