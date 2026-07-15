from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import List, Mapping, Optional

HOME_ENV = "AGENT_COLLAB_HOME"


@dataclass(frozen=True)
class AgentCollabHome:
    root: Path
    config_path: Path

    @classmethod
    def resolve(cls, env: Optional[Mapping[str, str]] = None) -> "AgentCollabHome":
        environ = os.environ if env is None else env
        override = environ.get(HOME_ENV)
        root = Path(override) if override else Path.home() / ".agent-collab"
        root = root.expanduser().resolve()
        return cls(root=root, config_path=root / "config.toml")


@dataclass(frozen=True)
class GlobalDataPaths:
    home: Path
    data_dir: Path
    daemon_dir: Path
    session_dir: Path
    tmp_dir: Path
    session_index_path: Path
    daemon_start_lock_path: Path
    pid_path: Path
    state_path: Path
    usage_window_state_path: Path
    usage_window_workdir: Path
    token_path: Path
    daemon_log_path: Path
    daemon_stderr_path: Path

    @classmethod
    def from_home(cls, home: AgentCollabHome) -> "GlobalDataPaths":
        data_dir = home.root / "data"
        daemon_dir = data_dir / "daemon"
        return cls(
            home=home.root,
            data_dir=data_dir,
            daemon_dir=daemon_dir,
            session_dir=data_dir / "sessions",
            tmp_dir=data_dir / "tmp",
            session_index_path=data_dir / "session-index.json",
            daemon_start_lock_path=daemon_dir / "start.lock",
            pid_path=daemon_dir / "pid",
            state_path=daemon_dir / "state.json",
            usage_window_state_path=daemon_dir / "usage-window-state.json",
            usage_window_workdir=data_dir / "tmp" / "usage-windows",
            token_path=daemon_dir / "token",
            daemon_log_path=daemon_dir / "daemon.log",
            daemon_stderr_path=daemon_dir / "daemon.stderr.log",
        )

    @classmethod
    def resolve(cls, env: Optional[Mapping[str, str]] = None) -> "GlobalDataPaths":
        return cls.from_home(AgentCollabHome.resolve(env))

    def ensure_dirs(self) -> None:
        self.daemon_dir.mkdir(parents=True, exist_ok=True)
        self.daemon_dir.chmod(0o700)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.chmod(0o700)


def atomic_write_private_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with owner-only text in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def project_config_path(workdir: Path) -> Path:
    return workdir.expanduser().resolve() / ".agent-collab" / "config.toml"


def user_config_path(home: AgentCollabHome) -> Path:
    return home.config_path


def legacy_project_session_dirs(workdir: Path) -> List[Path]:
    root = workdir.expanduser().resolve()
    return [
        root / ".agent-collab" / "data" / "sessions",
        root / ".agent-collab" / "sessions",
    ]


def default_session_log_dirs(
    workdir: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> List[Path]:
    dirs = [GlobalDataPaths.resolve(env).session_dir]
    dirs.extend(legacy_project_session_dirs(workdir if workdir is not None else Path(".")))
    return dirs
