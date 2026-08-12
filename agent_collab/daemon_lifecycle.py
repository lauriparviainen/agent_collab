"""Cross-process locks for daemon and native-manager lifecycle transactions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import time
from typing import Iterator, Optional

from .paths import GlobalDataPaths, atomic_write_private_text


class LifecycleBusyError(RuntimeError):
    pass


_held_lifecycle: ContextVar[bool] = ContextVar("agent_collab_lifecycle_held", default=False)
_held_registration: ContextVar[bool] = ContextVar("agent_collab_registration_held", default=False)


def account_home() -> Path:
    """Return the real login account home, independent of caller environment."""

    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def registration_lock_path(manager: str, *, home: Optional[Path] = None) -> Path:
    root = (home or account_home()).resolve()
    if manager == "launchd":
        return (
            root
            / "Library"
            / "Application Support"
            / "io.github.lauriparviainen.agent-collab"
            / "launchd-registration.lock"
        )
    if manager == "systemd":
        return (
            root
            / ".local"
            / "state"
            / "io.github.lauriparviainen.agent-collab"
            / "systemd-registration.lock"
        )
    raise ValueError(f"unsupported service manager: {manager!r}")


@contextmanager
def lifecycle_transaction(
    operation: str, paths: Optional[GlobalDataPaths] = None
) -> Iterator[None]:
    """Acquire the home-scoped top-level lifecycle lock without waiting."""

    if _held_lifecycle.get() or _held_registration.get():
        raise RuntimeError("lifecycle transactions are non-reentrant")
    paths = paths or GlobalDataPaths.resolve()
    paths.ensure_dirs()
    with _private_lock(paths.lifecycle_lock_path, operation):
        token = _held_lifecycle.set(True)
        try:
            yield
        finally:
            _held_lifecycle.reset(token)


@contextmanager
def registration_transaction(
    manager: str,
    operation: str,
    *,
    lock_path: Optional[Path] = None,
) -> Iterator[None]:
    """Acquire the OS-account global registration lock after lifecycle lock."""

    if not _held_lifecycle.get():
        raise RuntimeError("registration lock requires the lifecycle lock first")
    if _held_registration.get():
        raise RuntimeError("registration transactions are non-reentrant")
    path = lock_path or registration_lock_path(manager)
    _ensure_private_application_directory(path.parent)
    with _private_lock(path, operation, validate_existing=True):
        token = _held_registration.set(True)
        try:
            yield
        finally:
            _held_registration.reset(token)


def lifecycle_lock_held() -> bool:
    return _held_lifecycle.get()


def registration_lock_held() -> bool:
    return _held_registration.get()


@contextmanager
def _private_lock(path: Path, operation: str, *, validate_existing: bool = False) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if validate_existing:
        _validate_private_regular(path, allow_missing=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LifecycleBusyError(f"cannot open lifecycle lock {path}: {exc}") from exc
    sidecar = path.with_name(path.name + ".owner.json")
    owner = {
        "operation": operation,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    try:
        os.fchmod(fd, 0o600)
        _validate_fd(fd, path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            busy = _read_busy_operation(sidecar)
            detail = f" ({busy} is in progress)" if busy else ""
            raise LifecycleBusyError(
                f"another agent-collab daemon lifecycle operation is already in progress{detail}; retry after it finishes"
            ) from exc
        atomic_write_private_text(sidecar, json.dumps(owner, sort_keys=True) + "\n")
        try:
            yield
        finally:
            try:
                current = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = None
            if current == owner:
                sidecar.unlink(missing_ok=True)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _ensure_private_application_directory(path: Path) -> None:
    """Create the app-specific directory and reject unsafe existing objects."""

    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise LifecycleBusyError(f"registration lock directory is not a real directory: {path}")
        if info.st_uid != os.getuid():
            raise LifecycleBusyError(
                f"registration lock directory is not owned by this user: {path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise LifecycleBusyError(
                f"registration lock directory is not owner-only (0700): {path}"
            )
        return
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def _validate_private_regular(path: Path, *, allow_missing: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise LifecycleBusyError(f"registration lock is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise LifecycleBusyError(f"registration lock is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LifecycleBusyError(f"registration lock is not owner-only (0600): {path}")


def _validate_fd(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise LifecycleBusyError(f"unsafe lifecycle lock: {path}")


def _read_busy_operation(sidecar: Path) -> Optional[str]:
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    operation = value.get("operation")
    pid = value.get("pid")
    started_at = value.get("started_at")
    if not isinstance(operation, str) or not operation.strip():
        return None
    if not isinstance(pid, int) or pid <= 0 or not isinstance(started_at, (int, float)):
        return None
    return operation


__all__ = [
    "LifecycleBusyError",
    "account_home",
    "lifecycle_lock_held",
    "lifecycle_transaction",
    "registration_lock_held",
    "registration_lock_path",
    "registration_transaction",
]
