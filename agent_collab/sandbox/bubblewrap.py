"""Bubblewrap discovery, JSON-status parsing, and deterministic argv building."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import platform
import re
import shutil
import sys
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .plan import ResolvedSandboxPlan
from .specs import PathAccess, SandboxFailure

STATUS_LINE_LIMIT = 16 * 1024
STATUS_STREAM_LIMIT = 64 * 1024
MINIMUM_BWRAP_VERSION = (0, 6, 0)
GIT_REDIRECT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_INDEX_FILE",
)


@dataclass(frozen=True)
class BubblewrapInstallation:
    executable: Path
    version: str


@dataclass(frozen=True)
class BootstrapHandles:
    status: int
    proof: int
    provider_stdin: int
    provider_stdout: int
    provider_stderr: int
    worker: Optional[int] = None

    def roles(self) -> Dict[str, int]:
        values = {
            "proof": self.proof,
            "provider_stdin": self.provider_stdin,
            "provider_stdout": self.provider_stdout,
            "provider_stderr": self.provider_stderr,
        }
        if self.worker is not None:
            values["worker"] = self.worker
        return dict(sorted(values.items()))

    def pass_fds(self) -> Tuple[int, ...]:
        return tuple(
            dict.fromkeys(
                [
                    self.status,
                    self.proof,
                    self.provider_stdin,
                    self.provider_stdout,
                    self.provider_stderr,
                    *([] if self.worker is None else [self.worker]),
                ]
            )
        )


class BubblewrapStatusParser:
    """Bounded streaming parser for Bubblewrap's JSON-lines status protocol."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.total = 0
        self.child_pid: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.eof = False

    def feed(self, data: bytes) -> list[Mapping[str, Any]]:
        if self.eof:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status bytes arrived after EOF",
                phase="establishment",
            )
        self.total += len(data)
        if self.total > STATUS_STREAM_LIMIT:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status stream exceeded its limit",
                phase="establishment",
            )
        self.buffer.extend(data)
        objects: list[Mapping[str, Any]] = []
        while b"\n" in self.buffer:
            raw, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            if len(raw) > STATUS_LINE_LIMIT:
                raise SandboxFailure(
                    "outer_sandbox_status_invalid",
                    "Bubblewrap status line exceeded its limit",
                    phase="establishment",
                )
            objects.append(self._parse_line(bytes(raw)))
        if len(self.buffer) > STATUS_LINE_LIMIT:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status line exceeded its limit",
                phase="establishment",
            )
        return objects

    def finish(self) -> None:
        self.eof = True
        if self.buffer:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status ended with an incomplete line",
                phase="establishment",
            )
        if self.child_pid is None:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status ended before child-pid",
                phase="establishment",
            )

    def reconcile(self, process_code: int) -> None:
        if self.exit_code is not None and self.exit_code != process_code:
            raise SandboxFailure(
                "outer_sandbox_status_contradiction",
                "Bubblewrap status exit-code contradicted the reaped process",
                phase="cleanup",
            )

    def _parse_line(self, raw: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status contained invalid JSON",
                phase="establishment",
            ) from exc
        if not isinstance(value, dict):
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap status line was not an object",
                phase="establishment",
            )
        if self.child_pid is None and not value:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap's first status object did not contain child-pid",
                phase="establishment",
            )
        if "child-pid" in value:
            child_pid = value["child-pid"]
            if (
                self.child_pid is not None
                or isinstance(child_pid, bool)
                or not isinstance(child_pid, int)
                or child_pid <= 0
            ):
                raise SandboxFailure(
                    "outer_sandbox_status_invalid",
                    "Bubblewrap status contained an invalid or repeated child-pid",
                    phase="establishment",
                )
            self.child_pid = child_pid
        elif self.child_pid is None:
            raise SandboxFailure(
                "outer_sandbox_status_invalid",
                "Bubblewrap's first status object did not contain child-pid",
                phase="establishment",
            )
        if "exit-code" in value:
            exit_code = value["exit-code"]
            if (
                self.exit_code is not None
                or isinstance(exit_code, bool)
                or not isinstance(exit_code, int)
            ):
                raise SandboxFailure(
                    "outer_sandbox_status_invalid",
                    "Bubblewrap status contained an invalid or repeated exit-code",
                    phase="cleanup",
                )
            self.exit_code = exit_code
        return value


def discover_bubblewrap(command: str = "bwrap") -> BubblewrapInstallation:
    if platform.system() != "Linux":
        raise SandboxFailure(
            "outer_sandbox_platform_unsupported",
            "OS-enforced read-only sandboxing is currently available only on Linux",
        )
    resolved = shutil.which(command)
    if resolved is None:
        raise SandboxFailure(
            "outer_sandbox_engine_missing",
            "Bubblewrap is required for an OS-enforced read-only session",
            remediation=("Install the bubblewrap package and ensure bwrap is on PATH.",),
        )
    import subprocess

    try:
        result = subprocess.run(
            [resolved, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxFailure(
            "outer_sandbox_engine_incompatible",
            "Bubblewrap version discovery failed",
        ) from exc
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
    version_tuple = tuple(int(item) for item in match.groups()) if match else (0, 0, 0)
    if version_tuple < MINIMUM_BWRAP_VERSION:
        raise SandboxFailure(
            "outer_sandbox_engine_incompatible",
            "Bubblewrap is older than the minimum compatible version",
        )
    return BubblewrapInstallation(Path(resolved).resolve(), ".".join(map(str, version_tuple)))


def build_bubblewrap_argv(
    installation: BubblewrapInstallation,
    plan: ResolvedSandboxPlan,
    scratch: Path,
    handles: BootstrapHandles,
    inner_command: Sequence[str],
) -> Tuple[str, ...]:
    if not inner_command or not Path(inner_command[0]).is_absolute():
        raise SandboxFailure(
            "outer_sandbox_inner_command_invalid",
            "the sandbox inner executable must be an absolute path",
            phase="launch",
        )
    bootstrap = Path(__file__).with_name("bootstrap.py").resolve(strict=True)
    interpreter = Path(sys.executable).resolve(strict=True)
    system_tmp = scratch / "system-tmp"
    system_var_tmp = scratch / "system-var-tmp"
    private_tmp = scratch / "tmp"
    environment: Dict[str, str] = {
        **dict(plan.spec.environment.set_values),
        "TMPDIR": str(private_tmp),
        "TMP": str(private_tmp),
        "TEMP": str(private_tmp),
        "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
        "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
        "XDG_DATA_HOME": str(scratch / "xdg-data"),
        "XDG_STATE_HOME": str(scratch / "xdg-state"),
    }
    for name in plan.spec.environment.private_tmp_names:
        environment[name] = str(private_tmp)
    unset = set(plan.spec.environment.unset_names) | set(GIT_REDIRECT_ENV)
    argv = [
        str(installation.executable),
        "--json-status-fd",
        str(handles.status),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        str(scratch),
        str(scratch),
        "--bind",
        str(system_tmp),
        "/tmp",
        "--bind",
        str(system_var_tmp),
        "/var/tmp",
    ]
    for operation in plan.operations:
        argv.extend(
            [
                "--ro-bind" if operation.access is PathAccess.READ_ONLY else "--bind",
                str(operation.source),
                str(operation.destination),
            ]
        )
    for name in sorted(unset):
        argv.extend(["--unsetenv", name])
    for name in sorted(environment):
        argv.extend(["--setenv", name, environment[name]])
    argv.extend(
        [
            "--chdir",
            str(plan.context.cwd),
            "--",
            str(interpreter),
            "-I",
            "-S",
            str(bootstrap),
            "--protocol-version",
            "1",
            "--proof-fd",
            str(handles.proof),
        ]
    )
    if handles.worker is not None:
        argv.extend(["--worker-fd", str(handles.worker)])
    argv.extend(
        [
            "--provider-stdin-fd",
            str(handles.provider_stdin),
            "--provider-stdout-fd",
            str(handles.provider_stdout),
            "--provider-stderr-fd",
            str(handles.provider_stderr),
            "--",
            *inner_command,
        ]
    )
    return tuple(argv)
