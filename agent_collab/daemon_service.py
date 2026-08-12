"""Shared service-manager identities and process-bound readiness helpers."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import socket
import sys
from typing import Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_daemon_token
from .paths import AgentCollabHome, GlobalDataPaths


MANAGERS = frozenset({"systemd", "launchd"})
READY_ENDPOINT_MISSING_DETAIL = "protected readiness endpoint is unavailable (HTTP 404)"


class AutostartError(RuntimeError):
    pass


class SafeManagedRestoreError(AutostartError):
    """Managed restoration failed after its candidate was proven disabled and stopped."""


class EndpointInUseError(AutostartError):
    pass


def atomic_rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename one path without replacing a concurrent destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable")
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 1)
    elif sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "renamex_np is unavailable")
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unsupported")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


@dataclass(frozen=True)
class ManagedCommand:
    interpreter: Path
    manager: str
    host: str
    port: int
    default_workdir: Optional[Path]


@dataclass(frozen=True)
class ManagedServiceIdentity:
    manager: str
    source: str
    owned: bool
    current_home_owned: bool
    installed: bool
    loaded: bool
    enabled: bool
    command: Optional[ManagedCommand]
    effective_home: Optional[Path]
    pid: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True, init=False)
class AutostartStatus:
    installed: bool
    enabled: bool
    active: bool
    healthy: bool
    definition_current: bool
    definition_path: Path
    detail: str = ""
    manager: str = "systemd"

    def __init__(
        self,
        installed: bool,
        enabled: bool,
        active: bool,
        healthy: bool,
        definition_current: bool,
        definition_path: Optional[Path] = None,
        detail: str = "",
        manager: str = "systemd",
        *,
        unit_path: Optional[Path] = None,
    ) -> None:
        path = definition_path if definition_path is not None else unit_path
        if path is None:
            raise TypeError("definition_path is required")
        object.__setattr__(self, "installed", bool(installed))
        object.__setattr__(self, "enabled", bool(enabled))
        object.__setattr__(self, "active", bool(active))
        object.__setattr__(self, "healthy", bool(healthy))
        object.__setattr__(self, "definition_current", bool(definition_current))
        object.__setattr__(self, "definition_path", Path(path))
        object.__setattr__(self, "detail", detail)
        object.__setattr__(self, "manager", manager)

    @property
    def unit_path(self) -> Path:
        """Compatibility alias for callers predating the neutral status name."""

        return self.definition_path


def absolute_path(path: Path) -> Path:
    """Make a path absolute without resolving a venv interpreter symlink."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def canonical_home(paths: GlobalDataPaths) -> Path:
    return paths.home.expanduser().resolve()


def parse_managed_command(
    argv: Sequence[str],
    *,
    platform_manager: str,
    allow_legacy_systemd_manager: bool = False,
) -> ManagedCommand:
    """Parse the exact hidden foreground command used by native definitions."""

    if len(argv) < 5:
        raise AutostartError("managed definition has an incomplete ProgramArguments command")
    if list(argv[1:5]) != ["-m", "agent_collab.cli", "daemon", "run"]:
        raise AutostartError("managed definition does not use the agent-collab foreground command")
    values: dict[str, str] = {}
    index = 5
    while index < len(argv):
        flag = argv[index]
        if flag not in {"--manager", "--host", "--port", "--workdir"}:
            raise AutostartError(f"managed definition has unknown argument: {flag}")
        if flag in values:
            raise AutostartError(f"managed definition repeats argument: {flag}")
        if index + 1 >= len(argv):
            raise AutostartError(f"managed definition is missing a value for {flag}")
        values[flag] = argv[index + 1]
        index += 2
    manager = values.get("--manager")
    if manager is None and platform_manager == "systemd" and allow_legacy_systemd_manager:
        manager = "systemd"
    if manager != platform_manager or manager not in MANAGERS:
        raise AutostartError(
            f"managed definition has manager {manager!r}; expected {platform_manager!r}"
        )
    host = values.get("--host")
    raw_port = values.get("--port")
    if not host or raw_port is None:
        raise AutostartError("managed definition must specify --host and --port")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise AutostartError(f"managed definition has invalid port: {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise AutostartError(f"managed definition has invalid port: {port}")
    raw_workdir = values.get("--workdir")
    return ManagedCommand(
        interpreter=absolute_path(Path(argv[0])),
        manager=manager,
        host=host,
        port=port,
        default_workdir=absolute_path(Path(raw_workdir)) if raw_workdir else None,
    )


def state(paths: GlobalDataPaths) -> Mapping[str, object]:
    try:
        value = json.loads(paths.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def state_host(paths: GlobalDataPaths) -> str:
    return str(state(paths).get("host") or "127.0.0.1")


def state_port(paths: GlobalDataPaths) -> int:
    try:
        return int(state(paths).get("port") or 8765)
    except (TypeError, ValueError):
        return 8765


def readiness(
    *,
    host: str,
    port: int,
    paths: GlobalDataPaths,
    expected_manager: Optional[str] = None,
    expected_pid: Optional[int] = None,
) -> tuple[bool, str, Optional[dict[str, object]]]:
    """Authenticate and validate the immutable serving-process identity."""

    home = AgentCollabHome(root=paths.home, config_path=paths.home / "config.toml")
    try:
        token = load_daemon_token(home=home)
        if not token:
            return False, "daemon token is not ready", None
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        request = Request(
            f"http://{display_host}:{port}/ready",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urlopen(request, timeout=0.2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or not isinstance(payload, dict):
            return False, f"protected readiness probe returned {response.status}", None
        pid = int(payload.get("pid", 0))
        manager = str(payload.get("manager", ""))
        if pid <= 0 or manager not in {"detached", *MANAGERS}:
            return False, "protected readiness probe returned an invalid identity", payload
        if expected_pid is not None and pid != expected_pid:
            return False, f"readiness pid {pid} does not match managed pid {expected_pid}", payload
        if expected_manager is not None and manager != expected_manager:
            return (
                False,
                f"readiness manager {manager!r} does not match {expected_manager!r}",
                payload,
            )
        return True, "healthy", payload
    except HTTPError as exc:
        if exc.code == 404:
            return False, READY_ENDPOINT_MISSING_DETAIL, None
        return False, str(exc), None
    except (OSError, TypeError, ValueError, json.JSONDecodeError, URLError) as exc:
        return False, str(exc), None


def legacy_health(*, host: str, port: int, paths: GlobalDataPaths) -> tuple[bool, str]:
    """Probe the authenticated route used before process-identity readiness existed."""

    try:
        home = AgentCollabHome(root=paths.home, config_path=paths.home / "config.toml")
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
        if response.status != 200:
            return False, f"legacy authenticated probe returned {response.status}"
        return True, "healthy through legacy authenticated endpoint"
    except (OSError, HTTPError, URLError) as exc:
        return False, str(exc)


def reserve_server_endpoint(host: str, port: int) -> list[socket.socket]:
    """Reserve the sockets asyncio.start_server would need, or fail as occupied."""

    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror as exc:
        raise AutostartError(f"cannot resolve daemon listen endpoint {host}:{port}: {exc}") from exc
    reservations: list[socket.socket] = []
    seen: set[tuple[object, ...]] = set()
    try:
        for family, kind, protocol, _canon, address in addresses:
            key = (family, kind, protocol, *address)
            if key in seen:
                continue
            seen.add(key)
            sock = socket.socket(family, kind, protocol)
            reservations.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind(address)
            except OSError as exc:
                raise EndpointInUseError(
                    f"daemon endpoint {host}:{port} is occupied by an unattributable listener"
                ) from exc
        return reservations
    except Exception:
        close_reservations(reservations)
        raise


def close_reservations(reservations: list[socket.socket]) -> None:
    while reservations:
        reservations.pop().close()


def endpoints_overlap(host_a: str, port_a: int, host_b: str, port_b: int) -> bool:
    """Return whether two listen specifications can address the same socket."""

    if int(port_a) != int(port_b):
        return False
    if host_a == "" or host_b == "":
        return True
    host_a = host_a.removeprefix("[").removesuffix("]")
    host_b = host_b.removeprefix("[").removesuffix("]")
    try:
        addresses_a = {
            (family, sockaddr[0])
            for family, _kind, _proto, _canon, sockaddr in socket.getaddrinfo(
                host_a, port_a, type=socket.SOCK_STREAM
            )
        }
        addresses_b = {
            (family, sockaddr[0])
            for family, _kind, _proto, _canon, sockaddr in socket.getaddrinfo(
                host_b, port_b, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror:
        return host_a == host_b
    wildcard_a = {
        family
        for family, address in addresses_a
        if (family, address) in {(socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")}
    }
    wildcard_b = {
        family
        for family, address in addresses_b
        if (family, address) in {(socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")}
    }
    if wildcard_a:
        return bool(wildcard_a & {family for family, _ in addresses_b})
    if wildcard_b:
        return bool(wildcard_b & {family for family, _ in addresses_a})
    return bool(addresses_a & addresses_b)


__all__ = [
    "AutostartError",
    "AutostartStatus",
    "EndpointInUseError",
    "MANAGERS",
    "ManagedCommand",
    "ManagedServiceIdentity",
    "absolute_path",
    "canonical_home",
    "close_reservations",
    "endpoints_overlap",
    "parse_managed_command",
    "readiness",
    "reserve_server_endpoint",
    "state",
    "state_host",
    "state_port",
]
