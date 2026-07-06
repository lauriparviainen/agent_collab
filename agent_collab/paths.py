from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class DataPaths:
    workdir: Path
    root: Path
    data_dir: Path
    daemon_dir: Path
    session_dir: Path
    pid_path: Path
    state_path: Path
    daemon_log_path: Path
    daemon_stderr_path: Path

    @classmethod
    def from_workdir(cls, workdir: Path) -> "DataPaths":
        root = workdir.expanduser().resolve()
        data_dir = root / ".agent-collab" / "data"
        daemon_dir = data_dir / "daemon"
        session_dir = data_dir / "sessions"
        return cls(
            workdir=root,
            root=root / ".agent-collab",
            data_dir=data_dir,
            daemon_dir=daemon_dir,
            session_dir=session_dir,
            pid_path=daemon_dir / "pid",
            state_path=daemon_dir / "state.json",
            daemon_log_path=daemon_dir / "daemon.log",
            daemon_stderr_path=daemon_dir / "daemon.stderr.log",
        )

    def ensure_daemon_dirs(self) -> None:
        self.daemon_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)


def legacy_session_dir(workdir: Path) -> Path:
    return workdir.expanduser().resolve() / ".agent-collab" / "sessions"


def default_session_log_dirs(workdir: Path) -> List[Path]:
    root = workdir.expanduser().resolve()
    return [DataPaths.from_workdir(root).session_dir, legacy_session_dir(root)]
