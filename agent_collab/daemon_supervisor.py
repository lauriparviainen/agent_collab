from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from .events import utc_timestamp
from .paths import DataPaths


@dataclass
class DaemonStatus:
    running: bool
    state: Dict[str, Any]
    message: str


class DaemonSupervisorError(RuntimeError):
    pass


def start_daemon(workdir: Path, host: str = "127.0.0.1", port: int = 8765) -> Dict[str, Any]:
    paths = DataPaths.from_workdir(workdir)
    paths.ensure_daemon_dirs()
    state = _read_state(paths)
    pid = _state_pid(state) or _read_pid(paths)
    if pid is not None:
        if _is_running(pid):
            raise DaemonSupervisorError(f"agent-collab daemon already running for {paths.workdir} on {host}:{port} with pid {pid}")
        _remove_pid_state(paths)

    stdout = paths.daemon_log_path.open("a", encoding="utf-8")
    stderr = paths.daemon_stderr_path.open("a", encoding="utf-8")
    argv = [
        sys.executable,
        "-m",
        "agent_collab.cli",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--workdir",
        str(paths.workdir),
        "--session-log-dir",
        str(paths.session_dir),
    ]
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parent.parent)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not pythonpath else source_root + os.pathsep + pythonpath
    process = subprocess.Popen(
        argv,
        cwd=str(paths.workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    stdout.close()
    stderr.close()
    try:
        _wait_for_ready(process, host, port, paths)
    except Exception:
        _terminate_process(process)
        raise
    state = _build_state(paths, process.pid, host, port, argv)
    _write_state(paths, state)
    paths.pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    return state


def daemon_status(workdir: Path) -> DaemonStatus:
    paths = DataPaths.from_workdir(workdir)
    state = _read_state(paths)
    pid = _state_pid(state) or _read_pid(paths)
    if pid is None:
        return DaemonStatus(False, state, f"agent-collab daemon is not running for {paths.workdir}")
    if _is_running(pid):
        return DaemonStatus(True, state, f"agent-collab daemon is running with pid {pid}")
    _remove_pid_state(paths)
    return DaemonStatus(False, state, f"removed stale agent-collab daemon state for pid {pid}")


def stop_daemon(workdir: Path, grace_seconds: float = 3.0) -> DaemonStatus:
    paths = DataPaths.from_workdir(workdir)
    state = _read_state(paths)
    pid = _state_pid(state) or _read_pid(paths)
    if pid is None:
        return DaemonStatus(False, state, f"agent-collab daemon is not running for {paths.workdir}")
    if not _is_running(pid):
        _remove_pid_state(paths)
        return DaemonStatus(False, state, f"removed stale agent-collab daemon state for pid {pid}")

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _is_running(pid):
            _remove_pid_state(paths)
            return DaemonStatus(False, state, f"agent-collab daemon stopped pid {pid}")
        time.sleep(0.05)

    if _is_running(pid):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _is_running(pid):
            break
        time.sleep(0.05)
    if _is_running(pid):
        raise DaemonSupervisorError(f"failed to stop agent-collab daemon pid {pid}")
    _remove_pid_state(paths)
    return DaemonStatus(False, state, f"agent-collab daemon killed pid {pid}")


def tail_daemon_log(workdir: Path, tail: int = 100, stderr: bool = False) -> str:
    paths = DataPaths.from_workdir(workdir)
    path = paths.daemon_stderr_path if stderr else paths.daemon_log_path
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    count = max(0, int(tail))
    return "\n".join(lines[-count:] if count else lines)


def _build_state(paths: DataPaths, pid: int, host: str, port: int, argv: Any) -> Dict[str, Any]:
    return {
        "pid": pid,
        "host": host,
        "port": port,
        "workdir": str(paths.workdir),
        "data_dir": str(paths.data_dir),
        "daemon_dir": str(paths.daemon_dir),
        "session_dir": str(paths.session_dir),
        "daemon_log_path": str(paths.daemon_log_path),
        "daemon_stderr_path": str(paths.daemon_stderr_path),
        "server_url": f"http://{host}:{port}",
        "mcp_url": f"http://{host}:{port}/mcp",
        "started_at": utc_timestamp(),
        "argv": list(argv),
    }


def _read_state(paths: DataPaths) -> Dict[str, Any]:
    if not paths.state_path.exists():
        return {}
    try:
        data = json.loads(paths.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(paths: DataPaths, state: Dict[str, Any]) -> None:
    paths.state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_pid(paths: DataPaths) -> Optional[int]:
    try:
        text = paths.pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _state_pid(state: Dict[str, Any]) -> Optional[int]:
    try:
        pid = int(state.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if _is_zombie(pid):
        return False
    return True


def _is_zombie(pid: int) -> bool:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        state = stat.rsplit(")", 1)[1].strip().split()[0]
    except IndexError:
        return False
    return state == "Z"


def _wait_for_ready(process: subprocess.Popen, host: str, port: int, paths: DataPaths, timeout: float = 3.0) -> None:
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        code = poll()
        if code is not None:
            message = f"agent-collab daemon exited during startup with code {code}"
            stderr_tail = tail_daemon_log(paths.workdir, tail=20, stderr=True)
            if stderr_tail:
                message += f": {stderr_tail}"
            raise DaemonSupervisorError(message)
        try:
            with socket.create_connection((host, port), timeout=0.2):
                time.sleep(0.05)
                code = poll()
                if code is None:
                    return
                raise DaemonSupervisorError(f"agent-collab daemon exited during startup with code {code}")
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    suffix = f": {last_error}" if last_error else ""
    raise DaemonSupervisorError(f"agent-collab daemon did not become ready at {host}:{port}{suffix}")


def _terminate_process(process: subprocess.Popen) -> None:
    poll = getattr(process, "poll", None)
    terminate = getattr(process, "terminate", None)
    kill = getattr(process, "kill", None)
    try:
        if callable(poll) and poll() is not None:
            return
        if callable(terminate):
            terminate()
            deadline = time.monotonic() + 1.0
            while callable(poll) and poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        if callable(poll) and poll() is None and callable(kill):
            kill()
    except OSError:
        return


def _remove_pid_state(paths: DataPaths) -> None:
    for path in (paths.pid_path, paths.state_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
