"""Linux systemd user-service registration for the global daemon."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_daemon_token
from .daemon_supervisor import daemon_status, start_daemon, stop_daemon
from .paths import AgentCollabHome, GlobalDataPaths, atomic_write_private_text


SERVICE_NAME = "agent-collab.service"
UNIT_MARKER = "# Managed by agent-collab. Do not edit."
INTERPRETER_MARKER = "# Agent-Collab-Interpreter: "


class AutostartError(RuntimeError):
    pass


@dataclass(frozen=True)
class AutostartStatus:
    installed: bool
    enabled: bool
    active: bool
    healthy: bool
    definition_current: bool
    unit_path: Path
    detail: str = ""


def systemd_unit_path(env: Optional[Mapping[str, str]] = None) -> Path:
    environ = os.environ if env is None else env
    configured = environ.get("XDG_CONFIG_HOME")
    home = Path(environ.get("HOME") or Path.home()).expanduser()
    config_home = Path(configured).expanduser() if configured else home / ".config"
    return config_home.resolve() / "systemd" / "user" / SERVICE_NAME


def managed_unit_installed(
    unit_path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> bool:
    path = unit_path or systemd_unit_path(env)
    try:
        return path.read_text(encoding="utf-8").startswith(UNIT_MARKER)
    except OSError:
        return False


def systemd_owns_daemon(
    paths: Optional[GlobalDataPaths] = None,
    unit_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    if managed_unit_installed(unit_path, env):
        return True
    paths = paths or GlobalDataPaths.resolve(env)
    status = daemon_status(paths)
    return bool(status.running and status.state.get("manager") == "systemd")


def render_systemd_unit(
    *,
    paths: GlobalDataPaths,
    interpreter: Path,
    env: Mapping[str, str],
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
) -> str:
    interpreter = interpreter.expanduser().resolve()
    command = [
        str(interpreter),
        "-m",
        "agent_collab.cli",
        "daemon",
        "run",
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
    home_override = env.get("AGENT_COLLAB_HOME")
    if home_override:
        lines.append(
            f"Environment={_systemd_quote(f'AGENT_COLLAB_HOME={Path(home_override).expanduser().resolve()}')}"
        )
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
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    path = unit_path or systemd_unit_path(environ)
    interpreter = (interpreter or Path(sys.executable)).expanduser().resolve()
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
    existing = _read_optional(path)
    if existing is not None and not existing.startswith(UNIT_MARKER):
        raise AutostartError(f"refusing to replace unmanaged systemd unit: {path}")

    was_active = _systemctl_truth("is-active", SERVICE_NAME)
    manual = daemon_status(paths) if not was_active else None
    changed = existing != expected
    if changed:
        atomic_write_private_text(path, expected)
        _systemctl("daemon-reload")

    stopped_manual = False
    try:
        if manual and manual.running:
            stop_daemon(paths)
            stopped_manual = True
        _systemctl("enable", SERVICE_NAME)
        if was_active and changed:
            _systemctl("restart", SERVICE_NAME)
        elif not was_active:
            _systemctl("start", SERVICE_NAME)
        _wait_for_health(host, port, paths, readiness_timeout)
    except Exception as exc:
        recovery_errors = []
        try:
            _rollback_failed_enable(
                path=path,
                previous=existing,
                changed=changed,
                was_active=was_active,
            )
        except Exception as recovery_exc:
            recovery_errors.append(f"unit rollback failed: {recovery_exc}")
        if stopped_manual and manual is not None:
            try:
                _restore_manual_daemon(paths, manual.state)
            except Exception as recovery_exc:
                recovery_errors.append(f"manual daemon restore failed: {recovery_exc}")
        if recovery_errors:
            raise AutostartError(f"{exc}; {'; '.join(recovery_errors)}") from exc
        if isinstance(exc, AutostartError):
            raise
        raise AutostartError(f"failed to enable daemon autostart: {exc}") from exc
    return autostart_status(paths=paths, unit_path=path, interpreter=interpreter, env=environ)


def disable_autostart(
    *,
    paths: Optional[GlobalDataPaths] = None,
    unit_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    path = unit_path or systemd_unit_path(environ)
    _ensure_supported()
    if not path.exists():
        if systemd_owns_daemon(paths=paths, unit_path=path, env=environ):
            _ensure_systemd_user_manager()
            _systemctl("stop", SERVICE_NAME)
            _systemctl("disable", SERVICE_NAME, check=False)
            _systemctl("daemon-reload")
            daemon_status(paths)
            return AutostartStatus(False, False, False, False, True, path, "disabled")
        return AutostartStatus(False, False, False, False, True, path, "not installed")
    if not managed_unit_installed(path):
        raise AutostartError(f"refusing to remove unmanaged systemd unit: {path}")
    _ensure_systemd_user_manager()
    _systemctl("disable", "--now", SERVICE_NAME)
    path.unlink()
    _systemctl("daemon-reload")
    daemon_status(paths)
    return AutostartStatus(False, False, False, False, True, path, "disabled")


def autostart_status(
    *,
    paths: Optional[GlobalDataPaths] = None,
    unit_path: Optional[Path] = None,
    interpreter: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AutostartStatus:
    environ = dict(os.environ if env is None else env)
    paths = paths or GlobalDataPaths.resolve(environ)
    path = unit_path or systemd_unit_path(environ)
    installed = managed_unit_installed(path)
    if not installed:
        return AutostartStatus(False, False, False, False, True, path, "not installed")
    _ensure_supported()
    _ensure_systemd_user_manager()
    enabled = _systemctl_truth("is-enabled", SERVICE_NAME)
    active = _systemctl_truth("is-active", SERVICE_NAME)
    expected_interpreter = (interpreter or Path(sys.executable)).expanduser().resolve()
    recorded_interpreter = _recorded_interpreter(path)
    definition_current = bool(
        recorded_interpreter
        and recorded_interpreter.exists()
        and recorded_interpreter == expected_interpreter
    )
    healthy, detail = _health(host=_state_host(paths), port=_state_port(paths), paths=paths)
    return AutostartStatus(
        installed,
        enabled,
        active,
        bool(active and healthy),
        definition_current,
        path,
        detail,
    )


def start_systemd_daemon(paths: Optional[GlobalDataPaths] = None) -> AutostartStatus:
    paths = paths or GlobalDataPaths.resolve()
    _ensure_supported()
    _ensure_systemd_user_manager()
    _systemctl("start", SERVICE_NAME)
    _wait_for_health(_state_host(paths), _state_port(paths), paths, 5.0)
    return autostart_status(paths=paths)


def stop_systemd_daemon(paths: Optional[GlobalDataPaths] = None) -> AutostartStatus:
    paths = paths or GlobalDataPaths.resolve()
    _ensure_supported()
    _ensure_systemd_user_manager()
    _systemctl("stop", SERVICE_NAME)
    daemon_status(paths)
    return autostart_status(paths=paths)


def restart_systemd_daemon(paths: Optional[GlobalDataPaths] = None) -> AutostartStatus:
    paths = paths or GlobalDataPaths.resolve()
    _ensure_supported()
    _ensure_systemd_user_manager()
    _systemctl("restart", SERVICE_NAME)
    _wait_for_health(_state_host(paths), _state_port(paths), paths, 5.0)
    return autostart_status(paths=paths)


def _ensure_supported() -> None:
    if not sys.platform.startswith("linux"):
        raise AutostartError("daemon autostart currently requires Linux with systemd user services")


def _ensure_systemd_user_manager() -> None:
    result = _systemctl("show-environment", check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemd user manager unavailable").strip()
        raise AutostartError(f"cannot use systemd user services: {detail}")


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
    result = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AutostartError(f"systemctl --user {' '.join(args)} failed: {detail}")
    return result


def _systemctl_truth(action: str, service: str) -> bool:
    return _systemctl(action, service, check=False).returncode == 0


def _wait_for_health(host: str, port: int, paths: GlobalDataPaths, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "not ready"
    while time.monotonic() < deadline:
        healthy, last_detail = _health(host=host, port=port, paths=paths)
        if healthy:
            return
        time.sleep(0.05)
    raise AutostartError(f"daemon service did not become healthy: {last_detail}")


def _health(*, host: str, port: int, paths: GlobalDataPaths) -> tuple[bool, str]:
    home = AgentCollabHome(root=paths.home, config_path=paths.home / "config.toml")
    try:
        token = load_daemon_token(home=home)
        if not token:
            return False, "daemon token is not ready"
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        request = Request(
            f"http://{display_host}:{port}/sessions",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urlopen(request, timeout=0.2) as response:
            response.read()
            if response.status == 200:
                return True, "healthy"
            return False, f"protected health probe returned {response.status}"
    except (OSError, HTTPError, URLError) as exc:
        return False, str(exc)


def _restore_manual_daemon(paths: GlobalDataPaths, state: Mapping[str, object]) -> None:
    host = str(state.get("host") or "127.0.0.1")
    try:
        port = int(state.get("port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    raw_workdir = state.get("default_workdir")
    workdir = Path(str(raw_workdir)) if raw_workdir else None
    try:
        start_daemon(paths, host=host, port=port, default_workdir=workdir)
    except Exception as exc:
        raise AutostartError(
            f"service startup failed and the previous manual daemon could not be restored: {exc}"
        ) from exc


def _rollback_failed_enable(
    *, path: Path, previous: Optional[str], changed: bool, was_active: bool
) -> None:
    _systemctl("disable", "--now", SERVICE_NAME, check=False)
    if changed:
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_private_text(path, previous)
        _systemctl("daemon-reload", check=False)
    if was_active and previous is not None:
        _systemctl("enable", SERVICE_NAME, check=False)
        _systemctl("restart", SERVICE_NAME, check=False)


def _recorded_interpreter(path: Path) -> Optional[Path]:
    content = _read_optional(path) or ""
    for line in content.splitlines():
        if line.startswith(INTERPRETER_MARKER):
            return Path(line[len(INTERPRETER_MARKER) :]).expanduser().resolve()
    return None


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
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _systemd_quote(value: str, *, escape_dollar: bool = False) -> str:
    if any(character in value for character in ("\n", "\r", "\0")):
        raise AutostartError("systemd unit values cannot contain newlines or NUL bytes")
    escaped = value.replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
