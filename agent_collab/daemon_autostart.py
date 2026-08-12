"""Platform-neutral daemon autostart and managed lifecycle facade."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator, Mapping, Optional

from .daemon_lifecycle import (
    LifecycleBusyError,
    lifecycle_transaction,
    registration_transaction,
)
from .daemon_service import (
    AutostartError,
    AutostartStatus,
    ManagedServiceIdentity,
    absolute_path,
    endpoints_overlap,
)
from .paths import GlobalDataPaths


def selected_manager(platform: Optional[str] = None) -> str:
    value = sys.platform if platform is None else platform
    if value.startswith("linux"):
        return "systemd"
    if value == "darwin":
        return "launchd"
    raise AutostartError(
        "daemon autostart is supported on Linux with systemd and macOS with launchd"
    )


def _backend(manager: Optional[str] = None):
    chosen = manager or selected_manager()
    if chosen == "systemd":
        from . import daemon_autostart_systemd as backend
    elif chosen == "launchd":
        from . import daemon_autostart_launchd as backend
    else:
        raise AutostartError(f"unsupported service manager: {chosen}")
    return backend


def _definition_path(backend, explicit: Optional[Path]) -> Path:
    if explicit is not None:
        return explicit
    if backend.MANAGER == "systemd":
        return backend.resolve_systemd_unit_path()
    return backend.launchagent_path()


def enable_autostart(
    *,
    paths: Optional[GlobalDataPaths] = None,
    definition_path: Optional[Path] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
    readiness_timeout: float = 5.0,
    takeover: bool = False,
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    interpreter = absolute_path(interpreter or Path(sys.executable))
    explicit = definition_path or unit_path
    backend = _backend()
    operation = "daemon autostart enable"
    try:
        with lifecycle_transaction(operation, paths):
            with registration_transaction(
                backend.MANAGER,
                operation,
            ):
                _ensure_durable_install(interpreter)
                path = _definition_path(backend, explicit)
                if backend.MANAGER == "systemd":
                    return backend.enable_autostart(
                        paths=paths,
                        unit_path=path,
                        interpreter=interpreter,
                        env=environ,
                        host=host,
                        port=port,
                        default_workdir=default_workdir,
                        readiness_timeout=readiness_timeout,
                        takeover=takeover,
                    )
                return backend.enable_locked(
                    paths=paths,
                    definition_path=path,
                    interpreter=interpreter,
                    env=environ,
                    host=host,
                    port=port,
                    default_workdir=default_workdir,
                    readiness_timeout=readiness_timeout,
                    takeover=takeover,
                )
    except LifecycleBusyError as exc:
        raise AutostartError(str(exc)) from exc


def disable_autostart(
    *,
    paths: Optional[GlobalDataPaths] = None,
    definition_path: Optional[Path] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    takeover: bool = False,
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    interpreter = absolute_path(interpreter or Path(sys.executable))
    explicit = definition_path or unit_path
    backend = _backend()
    operation = "daemon autostart disable"
    try:
        with lifecycle_transaction(operation, paths):
            with registration_transaction(
                backend.MANAGER,
                operation,
            ):
                path = _definition_path(backend, explicit)
                if backend.MANAGER == "systemd":
                    return backend.disable_autostart(
                        paths=paths,
                        unit_path=path,
                        interpreter=interpreter,
                        env=environ,
                        takeover=takeover,
                    )
                return backend.disable_locked(
                    paths=paths,
                    definition_path=path,
                    interpreter=interpreter,
                    takeover=takeover,
                )
    except LifecycleBusyError as exc:
        raise AutostartError(str(exc)) from exc


def autostart_status(
    *,
    paths: Optional[GlobalDataPaths] = None,
    definition_path: Optional[Path] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    interpreter = absolute_path(interpreter or Path(sys.executable))
    explicit = definition_path or unit_path
    backend = _backend()
    _reject_cross_platform_runtime(paths, backend.MANAGER)
    path = _definition_path(backend, explicit)
    if backend.MANAGER == "systemd":
        return backend.autostart_status(
            paths=paths, unit_path=path, interpreter=interpreter, env=environ
        )
    return backend.status(paths=paths, definition_path=path, interpreter=interpreter)


def managed_service_identity(
    *,
    paths: Optional[GlobalDataPaths] = None,
    definition_path: Optional[Path] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ManagedServiceIdentity:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    interpreter = absolute_path(interpreter or Path(sys.executable))
    explicit = definition_path or unit_path
    backend = _backend()
    _reject_cross_platform_runtime(paths, backend.MANAGER)
    path = _definition_path(backend, explicit)
    return backend.inspect(paths=paths, definition_path=path, interpreter=interpreter)


def service_manager_owns_daemon(
    paths: Optional[GlobalDataPaths] = None,
    definition_path: Optional[Path] = None,
    unit_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    try:
        identity = managed_service_identity(
            paths=paths,
            definition_path=definition_path,
            unit_path=unit_path,
            env=env,
        )
    except AutostartError:
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            return False
        raise
    return bool(
        identity.current_home_owned
        and (identity.installed or identity.loaded or identity.pid is not None)
    )


def _reject_cross_platform_runtime(paths: GlobalDataPaths, manager: str) -> None:
    from .daemon_supervisor import daemon_status

    runtime = daemon_status(paths)
    recorded_manager = runtime.state.get("manager") if runtime.running else None
    if recorded_manager in {"systemd", "launchd"} and recorded_manager != manager:
        raise AutostartError(
            f"live daemon pid {runtime.state.get('pid')} is owned by {recorded_manager}, but "
            f"this platform uses {manager}; refusing native routing or raw signalling. "
            f"Stop the attributable process manually, then remove stale state at {paths.state_path}"
        )


def start_managed_daemon(
    *, paths: Optional[GlobalDataPaths] = None, interpreter: Optional[Path] = None
) -> AutostartStatus:
    return _managed_lifecycle("start", paths=paths, interpreter=interpreter)


def stop_managed_daemon(
    *, paths: Optional[GlobalDataPaths] = None, interpreter: Optional[Path] = None
) -> AutostartStatus:
    return _managed_lifecycle("stop", paths=paths, interpreter=interpreter)


def restart_managed_daemon(
    *, paths: Optional[GlobalDataPaths] = None, interpreter: Optional[Path] = None
) -> AutostartStatus:
    return _managed_lifecycle("restart", paths=paths, interpreter=interpreter)


def start_detached_daemon(
    *,
    paths: Optional[GlobalDataPaths] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
    interpreter: Optional[Path] = None,
):
    from .daemon_supervisor import start_daemon

    paths = paths or GlobalDataPaths.resolve()
    try:
        backend = _backend()
    except AutostartError:
        return start_daemon(
            paths,
            host=host,
            port=port,
            default_workdir=default_workdir,
            interpreter=interpreter,
        )
    operation = "daemon start"
    try:
        with lifecycle_transaction(operation, paths):
            with registration_transaction(backend.MANAGER, operation):
                identity = backend.inspect(
                    paths=paths,
                    definition_path=_definition_path(backend, None),
                    interpreter=absolute_path(interpreter or Path(sys.executable)),
                )
                _reject_reserved_endpoint(identity, host, port)
                return start_daemon(
                    paths,
                    host=host,
                    port=port,
                    default_workdir=default_workdir,
                    interpreter=interpreter,
                    _lifecycle_locked=True,
                )
    except LifecycleBusyError as exc:
        raise AutostartError(str(exc)) from exc


def restart_detached_daemon(
    *,
    paths: Optional[GlobalDataPaths] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
    interpreter: Optional[Path] = None,
):
    from .daemon_supervisor import start_daemon, stop_daemon

    paths = paths or GlobalDataPaths.resolve()
    try:
        backend = _backend()
    except AutostartError:
        with lifecycle_transaction("daemon restart", paths):
            stop_daemon(paths, _lifecycle_locked=True)
            return start_daemon(
                paths,
                host=host,
                port=port,
                default_workdir=default_workdir,
                interpreter=interpreter,
                _lifecycle_locked=True,
            )
    operation = "daemon restart"
    try:
        with lifecycle_transaction(operation, paths):
            with registration_transaction(backend.MANAGER, operation):
                identity = backend.inspect(
                    paths=paths,
                    definition_path=_definition_path(backend, None),
                    interpreter=absolute_path(interpreter or Path(sys.executable)),
                )
                _reject_reserved_endpoint(identity, host, port)
                stop_daemon(paths, _lifecycle_locked=True)
                return start_daemon(
                    paths,
                    host=host,
                    port=port,
                    default_workdir=default_workdir,
                    interpreter=interpreter,
                    _lifecycle_locked=True,
                )
    except LifecycleBusyError as exc:
        raise AutostartError(str(exc)) from exc


def _reject_reserved_endpoint(identity: ManagedServiceIdentity, host: str, port: int) -> None:
    if identity.current_home_owned and (identity.installed or identity.loaded):
        raise AutostartError(
            f"the daemon lifecycle is reserved by {identity.manager}; use the managed lifecycle"
        )
    if identity.loaded and not identity.owned:
        raise AutostartError(
            f"the {identity.manager} registration is occupied by an unattributable job"
        )
    if identity.owned and (identity.installed or identity.loaded) and identity.command is None:
        raise AutostartError(
            f"the {identity.manager} registration has an indeterminate command identity; "
            "refusing detached lifecycle mutation"
        )
    if (
        identity.owned
        and (identity.installed or identity.loaded)
        and identity.command
        and endpoints_overlap(host, port, identity.command.host, identity.command.port)
    ):
        raise AutostartError(
            f"the requested endpoint {host}:{port} is reserved by the per-user "
            f"{identity.manager} registration for {identity.effective_home or 'another home'}"
        )


def _managed_lifecycle(
    action: str,
    *,
    paths: Optional[GlobalDataPaths],
    interpreter: Optional[Path],
) -> AutostartStatus:
    paths = paths or GlobalDataPaths.resolve()
    interpreter = absolute_path(interpreter or Path(sys.executable))
    backend = _backend()
    operation = f"daemon {action}"
    try:
        with lifecycle_transaction(operation, paths):
            with registration_transaction(backend.MANAGER, operation):
                path = _definition_path(backend, None)
                function = getattr(backend, f"{action}_locked")
                return function(paths=paths, definition_path=path, interpreter=interpreter)
    except LifecycleBusyError as exc:
        raise AutostartError(str(exc)) from exc


def managed_definition_installed(
    *, paths: Optional[GlobalDataPaths] = None, interpreter: Optional[Path] = None
) -> bool:
    try:
        return managed_service_identity(paths=paths, interpreter=interpreter).installed
    except AutostartError:
        return False


@contextmanager
def service_transaction(
    operation: str,
    *,
    paths: Optional[GlobalDataPaths] = None,
    interpreter: Optional[Path] = None,
) -> Iterator[tuple[Optional[object], Optional[Path], GlobalDataPaths, Path]]:
    """Hold lifecycle then per-user manager locks for install/uninstall envelopes."""

    paths = paths or GlobalDataPaths.resolve()
    interpreter = absolute_path(interpreter or Path(sys.executable))
    try:
        with lifecycle_transaction(operation, paths):
            try:
                backend = _backend()
            except AutostartError:
                yield None, None, paths, interpreter
                return
            with registration_transaction(backend.MANAGER, operation):
                yield backend, _definition_path(backend, None), paths, interpreter
    except LifecycleBusyError as exc:
        raise AutostartError(str(exc)) from exc


def quiesce_for_install_locked(
    backend: object,
    definition_path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
) -> dict[str, object]:
    return backend.quiesce_for_install_locked(
        paths=paths, definition_path=definition_path, interpreter=interpreter
    )


def restore_after_install_locked(
    backend: object,
    snapshot: Mapping[str, object],
    definition_path: Path,
    paths: GlobalDataPaths,
    interpreter: Path,
) -> AutostartStatus:
    return backend.restore_after_install_locked(
        snapshot,
        paths=paths,
        definition_path=definition_path,
        interpreter=interpreter,
    )


def _ensure_durable_install(interpreter: Path) -> None:
    result = subprocess.run(
        [str(interpreter), "-I", "-c", "import agent_collab, agent_collab.cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AutostartError(
            "autostart requires a durable installed command; run ./agent_collab.sh install first"
        )


# Compatibility names for integrations using the original Linux-only module.
def managed_unit_installed(
    unit_path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> bool:
    if selected_manager() != "systemd":
        return False
    return _backend("systemd").managed_unit_installed(unit_path=unit_path, env=env)


systemd_owns_daemon = service_manager_owns_daemon
start_systemd_daemon = start_managed_daemon
stop_systemd_daemon = stop_managed_daemon
restart_systemd_daemon = restart_managed_daemon


def render_systemd_unit(**kwargs):
    return _backend("systemd").render_systemd_unit(**kwargs)


def render_launchd_plist(**kwargs):
    return _backend("launchd").render_launchd_plist(**kwargs)


SERVICE_NAME = "agent-collab.service"


__all__ = [
    "AutostartError",
    "AutostartStatus",
    "ManagedServiceIdentity",
    "SERVICE_NAME",
    "autostart_status",
    "disable_autostart",
    "enable_autostart",
    "managed_definition_installed",
    "managed_service_identity",
    "managed_unit_installed",
    "render_launchd_plist",
    "render_systemd_unit",
    "quiesce_for_install_locked",
    "restart_managed_daemon",
    "restart_detached_daemon",
    "restart_systemd_daemon",
    "selected_manager",
    "service_transaction",
    "service_manager_owns_daemon",
    "start_managed_daemon",
    "start_detached_daemon",
    "start_systemd_daemon",
    "stop_managed_daemon",
    "stop_systemd_daemon",
    "systemd_owns_daemon",
    "restore_after_install_locked",
]
