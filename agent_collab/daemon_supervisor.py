from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .events import utc_timestamp
from .paths import GlobalDataPaths, atomic_write_private_text


@dataclass
class DaemonStatus:
    running: bool
    state: Dict[str, Any]
    message: str


class DaemonSupervisorError(RuntimeError):
    pass


# Default seconds to wait for a freshly spawned daemon to pass the protected
# readiness probe. Overridable for slow cold starts (first venv import on a
# cold filesystem cache can exceed 3s on small machines).
DEFAULT_READY_TIMEOUT_SECONDS = 3.0
READY_TIMEOUT_ENV = "AGENT_COLLAB_DAEMON_READY_TIMEOUT"

IDENTITY_MATCH = "match"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_UNKNOWN = "unknown"


def start_daemon(
    paths: Optional[GlobalDataPaths] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
) -> Dict[str, Any]:
    paths = paths or GlobalDataPaths.resolve()
    paths.ensure_dirs()
    with _daemon_start_lock(paths):
        return _start_daemon_locked(paths, host, port, default_workdir)


def run_managed_daemon(
    paths: Optional[GlobalDataPaths] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_workdir: Optional[Path] = None,
    *,
    redirect_logs: bool = True,
) -> None:
    """Run a foreground daemon whose process lifecycle is owned by systemd."""

    paths = paths or GlobalDataPaths.resolve()
    paths.ensure_dirs()
    pid = os.getpid()
    argv = [sys.executable, *sys.argv]
    with _daemon_start_lock(paths):
        state = _read_state(paths)
        existing_pid = _state_pid(state) or _read_pid(paths)
        if existing_pid is not None and existing_pid != pid and _is_running(existing_pid):
            identity = _daemon_identity_status(existing_pid, state)
            if identity != IDENTITY_MISMATCH:
                raise DaemonSupervisorError(
                    f"global agent-collab daemon already running with pid {existing_pid}"
                )
        _remove_pid_state(paths)
        state = _build_state(
            paths,
            pid,
            host,
            port,
            argv,
            default_workdir,
            manager="systemd",
        )
        _write_state(paths, state)
        atomic_write_private_text(paths.pid_path, f"{pid}\n")

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _managed_sigterm)
    try:
        if redirect_logs:
            _redirect_managed_logs(paths)
        from .server_http import run_server

        run_server(
            host,
            port,
            default_workdir=default_workdir or paths.home,
            session_log_dir=paths.session_dir,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        _remove_owned_pid_state(paths, pid, manager="systemd")


def _start_daemon_locked(
    paths: GlobalDataPaths,
    host: str,
    port: int,
    default_workdir: Optional[Path],
) -> Dict[str, Any]:
    state = _read_state(paths)
    pid = _state_pid(state) or _read_pid(paths)
    if pid is not None:
        if _is_running(pid):
            identity = _daemon_identity_status(pid, state)
            if identity == IDENTITY_MATCH:
                raise DaemonSupervisorError(
                    f"global agent-collab daemon already running on {host}:{port} with pid {pid}"
                )
            if identity == IDENTITY_UNKNOWN:
                raise DaemonSupervisorError(
                    f"live pid {pid} cannot be attributed; refusing to start a second daemon"
                )
            _remove_pid_state(paths)
        else:
            _remove_pid_state(paths)
    try:
        paths.token_path.unlink()
    except FileNotFoundError:
        pass

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
        "--session-log-dir",
        str(paths.session_dir),
    ]
    if default_workdir is not None:
        argv.extend(["--workdir", str(Path(default_workdir).expanduser().resolve())])
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parent.parent)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not pythonpath else source_root + os.pathsep + pythonpath
    process = subprocess.Popen(
        argv,
        cwd=str(paths.home),
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
        state = _build_state(paths, process.pid, host, port, argv, default_workdir)
        _write_state(paths, state)
        atomic_write_private_text(paths.pid_path, f"{process.pid}\n")
        return state
    except Exception:
        _terminate_process(process)
        _remove_pid_state(paths)
        raise


def daemon_status(paths: Optional[GlobalDataPaths] = None) -> DaemonStatus:
    paths = paths or GlobalDataPaths.resolve()
    state = _read_state(paths)
    pid = _state_pid(state) or _read_pid(paths)
    if pid is None:
        return DaemonStatus(False, state, "global agent-collab daemon is not running")
    if _is_running(pid):
        identity = _daemon_identity_status(pid, state)
        if identity == IDENTITY_MATCH:
            return DaemonStatus(
                True, state, f"global agent-collab daemon is running with pid {pid}"
            )
        if identity == IDENTITY_UNKNOWN:
            return DaemonStatus(
                False,
                state,
                f"live pid {pid} cannot be attributed to the daemon; state was preserved",
            )
        _remove_pid_state(paths)
        return DaemonStatus(
            False,
            state,
            f"removed stale agent-collab daemon state; live pid {pid} belongs to another process",
        )
    _remove_pid_state(paths)
    return DaemonStatus(False, state, f"removed stale agent-collab daemon state for pid {pid}")


def stop_daemon(
    paths: Optional[GlobalDataPaths] = None, grace_seconds: float = 3.0
) -> DaemonStatus:
    paths = paths or GlobalDataPaths.resolve()
    state = _read_state(paths)
    pid = _state_pid(state) or _read_pid(paths)
    if state.get("manager") == "systemd":
        if pid is not None and _is_running(pid):
            raise DaemonSupervisorError(
                "daemon process is owned by systemd; stop it through systemctl --user"
            )
        _remove_pid_state(paths)
        return DaemonStatus(False, state, "removed stale systemd-owned daemon state")
    if pid is None:
        return DaemonStatus(False, state, "global agent-collab daemon is not running")
    if not _is_running(pid):
        _remove_pid_state(paths)
        return DaemonStatus(False, state, f"removed stale agent-collab daemon state for pid {pid}")

    identity = _daemon_identity_status(pid, state)
    if identity == IDENTITY_UNKNOWN:
        raise DaemonSupervisorError(f"live pid {pid} cannot be attributed; refusing to signal it")
    if identity == IDENTITY_MISMATCH:
        _remove_pid_state(paths)
        return DaemonStatus(
            False,
            state,
            f"refused to signal unattributed live pid {pid}; removed stale daemon state",
        )

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _is_running(pid):
            _remove_pid_state(paths)
            return DaemonStatus(False, state, f"agent-collab daemon stopped pid {pid}")
        identity = _daemon_identity_status(pid, state)
        if identity == IDENTITY_MISMATCH:
            _remove_pid_state(paths)
            return DaemonStatus(False, state, f"agent-collab daemon stopped pid {pid}")
        if identity == IDENTITY_UNKNOWN:
            raise DaemonSupervisorError(
                f"identity for pid {pid} became unavailable after SIGTERM; refusing further signals"
            )
        time.sleep(0.05)

    if _is_running(pid):
        identity = _daemon_identity_status(pid, state)
        if identity == IDENTITY_MISMATCH:
            _remove_pid_state(paths)
            return DaemonStatus(
                False,
                state,
                f"agent-collab daemon stopped; recycled pid {pid} was not signaled",
            )
        if identity == IDENTITY_UNKNOWN:
            raise DaemonSupervisorError(f"identity for pid {pid} is unavailable; refusing SIGKILL")
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _is_running(pid):
            break
        identity = _daemon_identity_status(pid, state)
        if identity == IDENTITY_MISMATCH:
            break
        if identity == IDENTITY_UNKNOWN:
            raise DaemonSupervisorError(f"identity for pid {pid} became unavailable after SIGKILL")
        time.sleep(0.05)
    if _is_running(pid) and _daemon_identity_status(pid, state) == IDENTITY_MATCH:
        raise DaemonSupervisorError(f"failed to stop agent-collab daemon pid {pid}")
    _remove_pid_state(paths)
    return DaemonStatus(False, state, f"agent-collab daemon killed pid {pid}")


def tail_daemon_log(
    paths: Optional[GlobalDataPaths] = None, tail: int = 100, stderr: bool = False
) -> str:
    paths = paths or GlobalDataPaths.resolve()
    path = paths.daemon_stderr_path if stderr else paths.daemon_log_path
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    count = max(0, int(tail))
    return "\n".join(lines[-count:] if count else lines)


def _build_state(
    paths: GlobalDataPaths,
    pid: int,
    host: str,
    port: int,
    argv: Any,
    default_workdir: Optional[Path] = None,
    manager: str = "detached",
) -> Dict[str, Any]:
    return {
        "pid": pid,
        "host": host,
        "port": port,
        "home": str(paths.home),
        "default_workdir": str(Path(default_workdir).expanduser().resolve())
        if default_workdir
        else None,
        "data_dir": str(paths.data_dir),
        "daemon_dir": str(paths.daemon_dir),
        "session_dir": str(paths.session_dir),
        "daemon_log_path": str(paths.daemon_log_path),
        "daemon_stderr_path": str(paths.daemon_stderr_path),
        "server_url": f"http://{host}:{port}",
        "mcp_url": f"http://{host}:{port}/mcp",
        "started_at": utc_timestamp(),
        "manager": manager,
        "argv": list(argv),
        "process_identity": _read_process_identity(pid),
    }


def _managed_sigterm(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _redirect_managed_logs(paths: GlobalDataPaths) -> None:
    """Redirect foreground-service output after ensuring private log paths exist."""

    for stream_fd, path in (
        (sys.stdout.fileno(), paths.daemon_log_path),
        (sys.stderr.fileno(), paths.daemon_stderr_path),
    ):
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.dup2(fd, stream_fd)
        finally:
            os.close(fd)


def _remove_owned_pid_state(paths: GlobalDataPaths, pid: int, *, manager: str) -> None:
    state = _read_state(paths)
    if _state_pid(state) == pid and state.get("manager") == manager:
        _remove_pid_state(paths)


@contextmanager
def _daemon_start_lock(paths: GlobalDataPaths) -> Iterator[None]:
    """Serialize the full daemon check/spawn/readiness/state transaction."""

    fd = os.open(paths.daemon_start_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DaemonSupervisorError(
                "global agent-collab daemon start already in progress"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_state(paths: GlobalDataPaths) -> Dict[str, Any]:
    if not paths.state_path.exists():
        return {}
    try:
        data = json.loads(paths.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(paths: GlobalDataPaths, state: Dict[str, Any]) -> None:
    atomic_write_private_text(paths.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _read_pid(paths: GlobalDataPaths) -> Optional[int]:
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


def _daemon_identity_matches(pid: int, state: Dict[str, Any]) -> bool:
    """Return true only when the live PID is attributable to the stored daemon."""

    return _daemon_identity_status(pid, state) == IDENTITY_MATCH


def _daemon_identity_status(pid: int, state: Dict[str, Any]) -> str:
    """Classify a live PID as the daemon, another process, or unverifiable."""

    actual = _read_process_identity(pid)
    if actual is None:
        return IDENTITY_UNKNOWN

    expected = state.get("process_identity")
    if isinstance(expected, dict):
        expected_source = expected.get("source")
        actual_source = actual.get("source")
        if expected_source != actual_source:
            # Evidence from procfs and ps uses different clocks and command
            # representations. It cannot prove either a match or a mismatch.
            return IDENTITY_UNKNOWN
        return IDENTITY_MATCH if expected == actual else IDENTITY_MISMATCH

    # Compatibility for daemon state written before process identities were
    # persisted. Exact argv verification is sufficient to reject an unrelated
    # process that inherited a recycled PID.
    expected_argv = state.get("argv")
    if not isinstance(expected_argv, list) or not all(
        isinstance(part, str) for part in expected_argv
    ):
        return IDENTITY_UNKNOWN
    actual_argv = actual.get("argv")
    if not isinstance(actual_argv, list):
        return IDENTITY_UNKNOWN
    return IDENTITY_MATCH if actual_argv == expected_argv else IDENTITY_MISMATCH


def _read_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Read a stable process identity from procfs, with a portable ps fallback."""

    proc_root = Path("/proc") / str(pid)
    try:
        stat = (proc_root / "stat").read_text(encoding="utf-8", errors="replace")
        fields = stat.rsplit(")", 1)[1].strip().split()
        start_time = fields[19]
        raw_argv = (proc_root / "cmdline").read_bytes().split(b"\0")
        argv = [os.fsdecode(part) for part in raw_argv if part]
        if argv:
            return {
                "source": "procfs",
                "start_time": start_time,
                "argv": argv,
            }
    except (IndexError, OSError):
        pass

    try:
        started = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        command = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started_text = started.stdout.strip() if started.returncode == 0 else ""
    command_text = command.stdout.strip() if command.returncode == 0 else ""
    if not started_text or not command_text:
        return None
    return {
        "source": "ps",
        "start_time": started_text,
        "command": command_text,
    }


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


def _ready_timeout_seconds() -> float:
    """The configured readiness timeout; invalid values fail loudly."""

    raw = os.environ.get(READY_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_READY_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    # `not value > 0` (rather than `value <= 0`) also rejects NaN.
    if not value > 0:
        raise DaemonSupervisorError(
            f"{READY_TIMEOUT_ENV} must be a positive number of seconds, got {raw!r}"
        )
    return value


def _wait_for_ready(
    process: subprocess.Popen,
    host: str,
    port: int,
    paths: GlobalDataPaths,
    timeout: Optional[float] = None,
) -> None:
    if timeout is None:
        timeout = _ready_timeout_seconds()
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        code = poll()
        if code is not None:
            message = f"agent-collab daemon exited during startup with code {code}"
            stderr_tail = tail_daemon_log(paths, tail=20, stderr=True)
            if stderr_tail:
                message += f": {stderr_tail}"
            raise DaemonSupervisorError(message)
        try:
            from .config import load_daemon_token
            from .paths import AgentCollabHome

            home = AgentCollabHome(root=paths.home, config_path=paths.home / "config.toml")
            token = load_daemon_token(home=home)
            if not token:
                raise OSError("daemon token is not ready")
            display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            request = Request(
                f"http://{display_host}:{port}/sessions",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=0.2) as response:
                if response.status != 200:
                    raise OSError(f"protected readiness probe returned {response.status}")
                response.read()
            time.sleep(0.05)
            code = poll()
            if code is None:
                return
            raise DaemonSupervisorError(
                f"agent-collab daemon exited during startup with code {code}"
            )
        except (OSError, HTTPError, URLError) as exc:
            last_error = exc
            time.sleep(0.05)
    suffix = f": {last_error}" if last_error else ""
    raise DaemonSupervisorError(
        f"agent-collab daemon did not become ready at {host}:{port}{suffix}"
    )


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


def _remove_pid_state(paths: GlobalDataPaths) -> None:
    for path in (paths.pid_path, paths.state_path, paths.token_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
