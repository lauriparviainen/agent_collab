"""Fail-before-provider Bubblewrap establishment and process supervision."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import sys
import tempfile
import time
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .bubblewrap import (
    BootstrapHandles,
    BubblewrapInstallation,
    BubblewrapStatusParser,
    build_bubblewrap_argv,
    discover_bubblewrap,
)
from .paths import (
    GitProtectionRecord,
    MountInfoEntry,
    MountOperation,
    PinnedIdentity,
    audit_aliases,
    component_contains,
    parse_mountinfo,
)
from .plan import ResolvedSandboxPlan
from .specs import PathAccess, PathOrigin, SandboxFailure, SandboxPolicy, SandboxSupport

PROOF_FRAME_LIMIT = 16 * 1024
ESTABLISHMENT_TIMEOUT_SECONDS = 15.0
TERMINATE_GRACE_SECONDS = 1.0
KILL_GRACE_SECONDS = 1.0
DIAGNOSTIC_LIMIT = 16 * 1024
RECURSIVE_READ_ONLY_REJECTED_EXIT = 42

_RECURSIVE_READ_ONLY_CONTROL = """
from pathlib import Path

root = Path.cwd()
writable = False
for target in (root / "direct-write", root / "nested" / "nested-write"):
    try:
        target.write_text("write must fail", encoding="utf-8")
        writable = True
    except OSError:
        pass
raise SystemExit(42 if writable else 0)
"""


@dataclass
class _PinnedProcess:
    pid: int
    proc_fd: int
    start_time: int
    pidfd: Optional[int]

    def close(self) -> None:
        for descriptor in (self.pidfd, self.proc_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class SupervisedProcess:
    """The Process subset consumed by ``SubprocessRunner``."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        *,
        scratch: Path,
        scratch_files: Sequence[Any],
        status_parser: BubblewrapStatusParser,
        status_task: asyncio.Task[None],
        diagnostic_tasks: Sequence[asyncio.Task[bytes]],
        pins: Sequence[_PinnedProcess],
    ) -> None:
        self._process = process
        self.stdout = stdout
        self.stderr = stderr
        self._scratch = scratch
        self._scratch_files = list(scratch_files)
        self._status_parser = status_parser
        self._status_task = status_task
        self._diagnostic_tasks = tuple(diagnostic_tasks)
        self._pins = tuple(pins)
        self._returncode: Optional[int] = None
        self._completion_task = asyncio.create_task(
            self._complete(),
            name="sandbox-process-completion",
        )

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode if self._returncode is None else self._returncode

    async def wait(self) -> int:
        # Multiple runner paths may wait while cancellation escalation is in
        # progress. Shield the one owned completion task so cancelling a
        # reader/supervisor task cannot prematurely tear down proof resources
        # or turn a later wait into an assertion failure.
        return await asyncio.shield(self._completion_task)

    async def _complete(self) -> int:
        try:
            code = await self._process.wait()
            self._returncode = code
            await self._status_task
            self._status_parser.reconcile(code)
            await asyncio.gather(*self._diagnostic_tasks, return_exceptions=True)
            await _wait_pins(self._pins, KILL_GRACE_SECONDS)
            return code
        finally:
            self._close_resources()

    def terminate(self) -> None:
        _signal_group(self._process.pid, signal.SIGTERM)

    def kill(self) -> None:
        _signal_group(self._process.pid, signal.SIGKILL)

    def _close_resources(self) -> None:
        for file_object in self._scratch_files:
            try:
                file_object.close()
            except Exception:
                pass
        for pin in self._pins:
            pin.close()
        try:
            shutil.rmtree(self._scratch)
        except FileNotFoundError:
            pass
        except OSError:
            # The process tree is already owned/reaped. Cleanup can be retried
            # safely by the daemon's ordinary runtime cleanup on a later pass.
            pass


class SandboxSupervisor:
    def __init__(
        self,
        installation: Optional[BubblewrapInstallation] = None,
    ) -> None:
        self.installation = installation or discover_bubblewrap()

    async def launch_cli(
        self,
        plan: ResolvedSandboxPlan,
        command_prefix: Sequence[str],
        prompt: str,
        *,
        stream_limit: int,
    ) -> SupervisedProcess:
        if plan.policy.effective is not SandboxPolicy.READ_ONLY:
            raise SandboxFailure(
                "outer_sandbox_policy_invalid",
                "SandboxSupervisor received a non-read-only plan",
                phase="launch",
            )
        scratch: Optional[Path] = None
        try:
            scratch = _allocate_scratch(plan)
            rendered_prompt = plan.render_prompt(prompt, scratch)
            prepared = plan.prepare_inner(command_prefix)
            inner = (
                *_resolve_inner_executable(prepared, plan.context.inherited_environment),
                rendered_prompt,
            )
            process, _worker = await self._launch(
                plan,
                scratch,
                inner,
                stream_limit=stream_limit,
                with_worker=False,
            )
            return process
        except SandboxFailure:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise
        except asyncio.TimeoutError as exc:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise SandboxFailure(
                "outer_sandbox_bootstrap_failed",
                "the sandbox timed out while establishing the isolated process",
                phase="establishment",
            ) from exc
        except OSError as exc:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise SandboxFailure(
                "outer_sandbox_bootstrap_failed",
                "the sandbox could not allocate or start its isolated process",
                phase="launch",
            ) from exc
        except BaseException:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise

    async def launch_sdk_worker(
        self,
        plan: ResolvedSandboxPlan,
        *,
        stream_limit: int,
    ) -> Tuple[SupervisedProcess, socket.socket]:
        """Establish Bubblewrap and return the process plus the daemon worker socket."""

        if plan.policy.effective is not SandboxPolicy.READ_ONLY:
            raise SandboxFailure(
                "outer_sandbox_policy_invalid",
                "SandboxSupervisor received a non-read-only plan",
                phase="launch",
            )
        if plan.support is not SandboxSupport.SDK_WORKER:
            raise SandboxFailure(
                "outer_sandbox_policy_invalid",
                "SandboxSupervisor.launch_sdk_worker requires sdk_worker support",
                phase="launch",
            )
        scratch: Optional[Path] = None
        try:
            scratch = _allocate_scratch(plan)
            # Keep the venv path: Path.resolve() follows bin/python -> /usr/bin
            # and loses site-packages (openai_codex lives only in the venv).
            # -I isolates from cwd/PYTHONPATH so a reviewed workspace cannot
            # shadow agent_collab.sandbox.sdk_worker via sys.path injection.
            python = _worker_python_executable()
            # Placeholder fd replaced after the worker socketpair is created.
            inner = (
                python,
                "-I",
                "-m",
                "agent_collab.sandbox.sdk_worker",
                "--worker-fd",
                "3",
            )
            return await self._launch(
                plan,
                scratch,
                inner,
                stream_limit=stream_limit,
                with_worker=True,
            )
        except SandboxFailure:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise
        except asyncio.TimeoutError as exc:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise SandboxFailure(
                "outer_sandbox_bootstrap_failed",
                "the sandbox timed out while establishing the isolated process",
                phase="establishment",
            ) from exc
        except OSError as exc:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise SandboxFailure(
                "outer_sandbox_bootstrap_failed",
                "the sandbox could not allocate or start its isolated process",
                phase="launch",
            ) from exc
        except BaseException:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise

    async def preflight(self, plan: ResolvedSandboxPlan) -> None:
        """Fresh credential-free use of the real builder, protocol, and proof."""

        true_path = shutil.which("true")
        if true_path is None:
            raise SandboxFailure(
                "outer_sandbox_engine_incompatible",
                "the inert preflight executable is unavailable",
            )
        scratch = _allocate_scratch(plan)
        process: Optional[SupervisedProcess] = None
        try:
            await _verify_recursive_read_only_control(self.installation, scratch)
            process, _worker = await self._launch(
                plan,
                scratch,
                (str(Path(true_path).resolve()),),
                stream_limit=64 * 1024,
                with_worker=False,
            )
            code = await process.wait()
            if code != 0:
                raise SandboxFailure(
                    "outer_sandbox_engine_incompatible",
                    "the Bubblewrap credential-free control returned nonzero",
                )
        except BaseException:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
                except BaseException:
                    pass
            shutil.rmtree(scratch, ignore_errors=True)
            raise

    async def _launch(
        self,
        plan: ResolvedSandboxPlan,
        scratch: Path,
        inner: Sequence[str],
        *,
        stream_limit: int,
        with_worker: bool,
    ) -> Tuple[SupervisedProcess, Optional[socket.socket]]:
        # The session plan is immutable, but host inode aliases and nested
        # mounts are not. Repeat the bounded host audit immediately before
        # every preflight and provider launch so a hard link planted after
        # start validation cannot bridge writable state into protected data.
        await asyncio.to_thread(
            audit_aliases,
            plan.operations,
            plan.git_records,
            accounting_peer_roots=plan.accounting_peer_roots,
            max_entries=plan.alias_audit_max_entries,
            timeout_seconds=plan.alias_audit_timeout_seconds,
            log=plan.alias_audit_log,
        )
        status_read, status_write = os.pipe2(os.O_CLOEXEC)
        proof_parent, proof_child = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM | socket.SOCK_CLOEXEC,
        )
        worker_parent: Optional[socket.socket] = None
        worker_child: Optional[socket.socket] = None
        if with_worker:
            worker_parent, worker_child = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM | socket.SOCK_CLOEXEC,
            )
        provider_input = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        stdout_read, stdout_write = os.pipe2(os.O_CLOEXEC)
        stderr_read, stderr_write = os.pipe2(os.O_CLOEXEC)
        worker_fd = None if worker_child is None else worker_child.fileno()
        handles = BootstrapHandles(
            status=status_write,
            proof=proof_child.fileno(),
            provider_stdin=provider_input,
            provider_stdout=stdout_write,
            provider_stderr=stderr_write,
            worker=worker_fd,
        )
        effective_inner = list(inner)
        if with_worker:
            if worker_fd is None:
                raise SandboxFailure(
                    "outer_sandbox_bootstrap_failed",
                    "the sandbox worker control socket was not allocated",
                    phase="launch",
                )
            # Replace the placeholder --worker-fd argument with the real child fd.
            try:
                flag_index = effective_inner.index("--worker-fd")
                effective_inner[flag_index + 1] = str(worker_fd)
            except (ValueError, IndexError) as exc:
                raise SandboxFailure(
                    "outer_sandbox_inner_command_invalid",
                    "the SDK worker command is missing --worker-fd",
                    phase="launch",
                ) from exc
        argv = build_bubblewrap_argv(self.installation, plan, scratch, handles, effective_inner)
        process: Optional[asyncio.subprocess.Process] = None
        parent_close = [
            status_read,
            status_write,
            provider_input,
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        ]
        scratch_files: list[Any] = []
        pins: list[_PinnedProcess] = []
        status_task: Optional[asyncio.Task[None]] = None
        diagnostic_tasks: list[asyncio.Task[bytes]] = []
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(plan.context.cwd),
                env=dict(plan.context.inherited_environment),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                pass_fds=handles.pass_fds(),
                start_new_session=True,
                limit=stream_limit,
            )
            for descriptor in (status_write, provider_input, stdout_write, stderr_write):
                os.close(descriptor)
                parent_close.remove(descriptor)
            proof_child.close()
            if worker_child is not None:
                worker_child.close()
                worker_child = None

            status_reader, status_file = await _reader_from_fd(status_read, limit=64 * 1024)
            stdout_reader, stdout_file = await _reader_from_fd(stdout_read, limit=stream_limit)
            stderr_reader, stderr_file = await _reader_from_fd(stderr_read, limit=stream_limit)
            scratch_files.extend((status_file, stdout_file, stderr_file))
            for descriptor in (status_read, stdout_read, stderr_read):
                parent_close.remove(descriptor)

            parser = BubblewrapStatusParser()
            child_ready = asyncio.Event()
            status_task = asyncio.create_task(
                _drain_status(status_reader, parser, child_ready),
                name="sandbox-bwrap-status",
            )
            assert process.stdout is not None and process.stderr is not None
            diagnostic_tasks = [
                asyncio.create_task(_bounded_diagnostic(process.stdout)),
                asyncio.create_task(_bounded_diagnostic(process.stderr)),
            ]
            nonce = secrets.token_hex(32)
            proof_parent.setblocking(False)
            loop = asyncio.get_running_loop()
            await _send_frame(
                loop,
                proof_parent,
                {"type": "sandbox_challenge", "version": 1, "nonce": nonce},
            )
            hello_task = asyncio.create_task(_recv_frame(loop, proof_parent))
            try:
                await asyncio.wait_for(
                    asyncio.gather(child_ready.wait(), asyncio.shield(hello_task)),
                    ESTABLISHMENT_TIMEOUT_SECONDS,
                )
                hello = hello_task.result()
                if hello.get("type") == "sandbox_error":
                    raise SandboxFailure(
                        "outer_sandbox_bootstrap_failed",
                        "the isolated bootstrap rejected its startup contract",
                        phase="establishment",
                    )
                _validate_hello(hello, nonce, handles.roles())
                if parser.child_pid is None:
                    raise SandboxFailure(
                        "outer_sandbox_status_invalid",
                        "Bubblewrap did not identify its namespace reaper",
                        phase="establishment",
                    )
                reaper = _pin_process(parser.child_pid)
                pins.append(reaper)
                bootstrap = await _correlate_bootstrap(
                    reaper,
                    int(hello["pid"]),
                    launched_at=time.monotonic(),
                )
                pins.append(bootstrap)
                _verify_establishment(plan, scratch, reaper, bootstrap)
                await _send_frame(
                    loop,
                    proof_parent,
                    {
                        "type": "sandbox_ack",
                        "version": 1,
                        "nonce": nonce,
                        "verified": True,
                    },
                )
                nonce = ""
                proof_parent.close()
            except BaseException as exc:
                if not hello_task.done():
                    hello_task.cancel()
                await asyncio.gather(hello_task, return_exceptions=True)
                if isinstance(exc, asyncio.TimeoutError):
                    raise SandboxFailure(
                        "outer_sandbox_bootstrap_failed",
                        "the sandbox timed out before establishment proof completed",
                        phase="establishment",
                    ) from exc
                raise

            supervised = SupervisedProcess(
                process,
                stdout_reader,
                stderr_reader,
                scratch=scratch,
                scratch_files=scratch_files,
                status_parser=parser,
                status_task=status_task,
                diagnostic_tasks=diagnostic_tasks,
                pins=pins,
            )
            owned_worker = worker_parent
            worker_parent = None
            return supervised, owned_worker
        except BaseException:
            proof_parent.close()
            proof_child.close()
            if worker_parent is not None:
                worker_parent.close()
            if worker_child is not None:
                worker_child.close()
            if process is not None:
                await _terminate_failed_launch(process)
            if status_task is not None:
                if not status_task.done():
                    status_task.cancel()
            for task in diagnostic_tasks:
                if not task.done():
                    task.cancel()
            for pin in pins:
                pin.close()
            for file_object in scratch_files:
                try:
                    file_object.close()
                except Exception:
                    pass
            if status_task is not None:
                try:
                    await asyncio.gather(status_task, return_exceptions=True)
                except BaseException:
                    pass
            try:
                await asyncio.gather(*diagnostic_tasks, return_exceptions=True)
            except BaseException:
                pass
            raise
        finally:
            for descriptor in parent_close:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _adopt_process_wait(process: asyncio.subprocess.Process) -> None:
    """Own a final process wait after cancellation interrupts bounded cleanup."""

    async def reap() -> None:
        try:
            await process.wait()
        except BaseException:
            pass

    asyncio.create_task(reap())


async def _terminate_failed_launch(process: asyncio.subprocess.Process) -> None:
    """Escalate and retain wait ownership despite repeated cancellation."""

    _signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    except BaseException:
        pass
    if process.returncode is not None:
        return
    _signal_group(process.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _adopt_process_wait(process)
    except BaseException:
        _adopt_process_wait(process)


def _allocate_scratch(plan: ResolvedSandboxPlan) -> Path:
    if plan.scratch_anchor is None:
        raise SandboxFailure(
            "outer_sandbox_scratch_anchor_invalid",
            "the sandbox plan has no private scratch anchor",
            phase="launch",
        )
    root: Optional[Path] = None
    try:
        root = Path(tempfile.mkdtemp(prefix="turn-", dir=plan.scratch_anchor))
        os.chmod(root, 0o700)
        for name, mode in (
            ("system-tmp", 0o1777),
            ("system-var-tmp", 0o1777),
            ("tmp", 0o700),
            ("xdg-cache", 0o700),
            ("xdg-config", 0o700),
            ("xdg-data", 0o700),
            ("xdg-state", 0o700),
        ):
            path = root / name
            path.mkdir(mode=mode)
            os.chmod(path, mode)
        return root
    except OSError as exc:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise SandboxFailure(
            "outer_sandbox_scratch_anchor_invalid",
            "the sandbox could not allocate private scratch space",
            phase="launch",
        ) from exc


async def _verify_recursive_read_only_control(
    installation: BubblewrapInstallation,
    scratch: Path,
) -> None:
    """Version-gate recursive read-only binds with a private nested mount."""

    control = scratch / "recursive-read-only-control"
    source = control / "source"
    nested_target = source / "nested"
    nested_source = control / "nested-source"
    control.mkdir(mode=0o700)
    source.mkdir(mode=0o700)
    nested_target.mkdir(mode=0o700)
    nested_source.mkdir(mode=0o700)
    argv = (
        str(installation.executable),
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
        str(source),
        str(source),
        "--bind",
        str(nested_source),
        str(nested_target),
        "--ro-bind",
        str(source),
        str(source),
        "--chdir",
        str(source),
        "--",
        str(Path(sys.executable).resolve()),
        "-I",
        "-S",
        "-c",
        _RECURSIVE_READ_ONLY_CONTROL,
    )
    process: Optional[asyncio.subprocess.Process] = None
    stderr_task: Optional[asyncio.Task[bytes]] = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            limit=DIAGNOSTIC_LIMIT,
        )
        assert process.stderr is not None
        stderr_task = asyncio.create_task(_bounded_diagnostic(process.stderr))
        try:
            code = await asyncio.wait_for(
                process.wait(),
                ESTABLISHMENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await asyncio.gather(process.wait(), return_exceptions=True)
            raise SandboxFailure(
                "outer_sandbox_bootstrap_failed",
                "the recursive read-only control timed out",
                phase="preflight",
            ) from exc
        if code == RECURSIVE_READ_ONLY_REJECTED_EXIT:
            raise SandboxFailure(
                "outer_sandbox_recursive_read_only_unavailable",
                "Bubblewrap did not make a nested bind mount read-only",
                phase="preflight",
            )
        if code != 0:
            raise SandboxFailure(
                "outer_sandbox_engine_incompatible",
                "the recursive read-only control failed",
                phase="preflight",
            )
        if (source / "direct-write").exists() or (nested_source / "nested-write").exists():
            raise SandboxFailure(
                "outer_sandbox_recursive_read_only_unavailable",
                "the recursive read-only control contradicted its host markers",
                phase="preflight",
            )
    except FileNotFoundError as exc:
        raise SandboxFailure(
            "outer_sandbox_engine_missing",
            "Bubblewrap disappeared before the recursive read-only control",
            phase="preflight",
        ) from exc
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.gather(process.wait(), return_exceptions=True)
        if stderr_task is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)
        shutil.rmtree(control, ignore_errors=True)


def _worker_python_executable() -> str:
    """Absolute interpreter path that preserves a virtualenv entrypoint.

    ``sys.executable`` under a venv is often a symlink into ``/usr/bin``. Fully
    resolving it drops ``pyvenv.cfg`` association and site-packages. Use an
    absolute path that stops before following the final system binary.
    """

    raw = Path(sys.executable)
    if not raw.is_absolute():
        found = shutil.which(str(raw))
        raw = Path(found) if found is not None else raw.absolute()
    current = raw
    # Walk through relative symlink components inside the venv bin dir only.
    seen: set[Path] = set()
    while current.is_symlink() and current not in seen:
        seen.add(current)
        target = current.readlink()
        if target.is_absolute():
            # Symlink leaves the venv tree (common: bin/python3.12 -> /usr/bin).
            # Keep the venv path so Python still loads the environment.
            return str(current)
        current = current.parent / target
    return str(current)


def _resolve_inner_executable(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> Tuple[str, ...]:
    if not command:
        raise SandboxFailure(
            "outer_sandbox_inner_command_invalid",
            "the provider command is empty",
            phase="launch",
        )
    first = command[0]
    try:
        if os.path.isabs(first):
            resolved = Path(first).resolve(strict=True)
        else:
            found = shutil.which(first, path=environment.get("PATH"))
            if found is None:
                raise SandboxFailure(
                    "outer_sandbox_inner_command_invalid",
                    "the provider executable could not be resolved",
                    phase="launch",
                )
            resolved = Path(found).resolve(strict=True)
    except SandboxFailure:
        raise
    except (OSError, RuntimeError) as exc:
        raise SandboxFailure(
            "outer_sandbox_inner_command_invalid",
            "the provider executable could not be resolved",
            phase="launch",
        ) from exc
    if not resolved.is_file():
        raise SandboxFailure(
            "outer_sandbox_inner_command_invalid",
            "the provider executable is not a regular file",
            phase="launch",
        )
    return (str(resolved), *command[1:])


async def _reader_from_fd(
    descriptor: int,
    *,
    limit: int,
) -> Tuple[asyncio.StreamReader, Any]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=limit)
    protocol = asyncio.StreamReaderProtocol(reader)
    file_object = os.fdopen(descriptor, "rb", buffering=0)
    await loop.connect_read_pipe(lambda: protocol, file_object)
    return reader, file_object


async def _drain_status(
    reader: asyncio.StreamReader,
    parser: BubblewrapStatusParser,
    child_ready: asyncio.Event,
) -> None:
    while True:
        data = await reader.read(4096)
        if not data:
            parser.finish()
            return
        parser.feed(data)
        if parser.child_pid is not None:
            child_ready.set()


async def _bounded_diagnostic(reader: asyncio.StreamReader) -> bytes:
    value = bytearray()
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return bytes(value)
        remaining = DIAGNOSTIC_LIMIT - len(value)
        if remaining > 0:
            value.extend(chunk[:remaining])


def _json_pairs(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SandboxFailure(
                "outer_sandbox_protocol_invalid",
                "the bootstrap protocol contained a duplicate field",
                phase="establishment",
            )
        result[key] = value
    return result


async def _send_frame(
    loop: asyncio.AbstractEventLoop,
    channel: socket.socket,
    value: Mapping[str, Any],
) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not raw or len(raw) > PROOF_FRAME_LIMIT:
        raise SandboxFailure(
            "outer_sandbox_protocol_invalid",
            "the bootstrap protocol frame exceeded its limit",
            phase="establishment",
        )
    await loop.sock_sendall(channel, struct.pack(">I", len(raw)) + raw)


async def _recv_exact(
    loop: asyncio.AbstractEventLoop,
    channel: socket.socket,
    length: int,
) -> bytes:
    result = bytearray()
    while len(result) < length:
        value = await loop.sock_recv(channel, length - len(result))
        if not value:
            raise SandboxFailure(
                "outer_sandbox_protocol_invalid",
                "the bootstrap proof socket closed prematurely",
                phase="establishment",
            )
        result.extend(value)
    return bytes(result)


async def _recv_frame(
    loop: asyncio.AbstractEventLoop,
    channel: socket.socket,
) -> Dict[str, Any]:
    length = struct.unpack(">I", await _recv_exact(loop, channel, 4))[0]
    if length == 0 or length > PROOF_FRAME_LIMIT:
        raise SandboxFailure(
            "outer_sandbox_protocol_invalid",
            "the bootstrap protocol frame had an invalid length",
            phase="establishment",
        )
    raw = await _recv_exact(loop, channel, length)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxFailure(
            "outer_sandbox_protocol_invalid",
            "the bootstrap protocol contained invalid JSON",
            phase="establishment",
        ) from exc
    if not isinstance(value, dict):
        raise SandboxFailure(
            "outer_sandbox_protocol_invalid",
            "the bootstrap protocol frame was not an object",
            phase="establishment",
        )
    return value


def _validate_hello(hello: Mapping[str, Any], nonce: str, roles: Mapping[str, int]) -> None:
    if set(hello) != {"type", "version", "nonce", "pid", "fd_roles"}:
        raise SandboxFailure(
            "outer_sandbox_protocol_invalid",
            "the bootstrap hello fields were not exact",
            phase="establishment",
        )
    pid = hello["pid"]
    if (
        hello["type"] != "sandbox_hello"
        or hello["version"] != 1
        or hello["nonce"] != nonce
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or hello["fd_roles"] != roles
    ):
        raise SandboxFailure(
            "outer_sandbox_protocol_invalid",
            "the bootstrap hello did not match this launch",
            phase="establishment",
        )


def _pin_process(pid: int) -> _PinnedProcess:
    try:
        proc_fd = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        start_time = _process_stat_start(_read_at(proc_fd, "stat"))
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd = pidfd_open(pid, 0) if callable(pidfd_open) else None
        return _PinnedProcess(pid, proc_fd, start_time, pidfd)
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_proc_identity_unavailable",
            "the namespace process identity could not be pinned",
            phase="establishment",
        ) from exc


def _read_at(directory_fd: int, name: str) -> str:
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        chunks = bytearray()
        while True:
            value = os.read(descriptor, 65536)
            if not value:
                break
            chunks.extend(value)
            if len(chunks) > 4 * 1024 * 1024:
                raise SandboxFailure(
                    "outer_sandbox_proc_identity_unavailable",
                    "a proc identity file exceeded its bound",
                    phase="establishment",
                )
        return bytes(chunks).decode("utf-8", errors="strict")
    finally:
        os.close(descriptor)


def _process_stat_start(text: str) -> int:
    close = text.rfind(")")
    fields = text[close + 2 :].split()
    if close < 0 or len(fields) < 20:
        raise SandboxFailure(
            "outer_sandbox_proc_identity_unavailable",
            "a proc stat record was malformed",
            phase="establishment",
        )
    return int(fields[19])


def _status_fields(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            result[key] = value.strip()
    return result


async def _correlate_bootstrap(
    reaper: _PinnedProcess,
    local_pid: int,
    *,
    launched_at: float,
) -> _PinnedProcess:
    del launched_at
    candidates: list[int] = []
    try:
        children = _read_at(reaper.proc_fd, f"task/{reaper.pid}/children")
        candidates = [int(item) for item in children.split()]
    except OSError:
        candidates = _same_uid_proc_children(reaper.pid)
    if not candidates:
        # The children file can be momentarily empty just after hello visibility.
        for _ in range(20):
            await asyncio.sleep(0.01)
            try:
                children = _read_at(reaper.proc_fd, f"task/{reaper.pid}/children")
                candidates = [int(item) for item in children.split()]
            except OSError:
                candidates = _same_uid_proc_children(reaper.pid)
            if candidates:
                break
    matched: list[_PinnedProcess] = []
    for candidate in candidates:
        try:
            pin = _pin_process(candidate)
            fields = _status_fields(_read_at(pin.proc_fd, "status"))
            nspid = [int(item) for item in fields.get("NSpid", "").split()]
            parent = int(fields.get("PPid", "0"))
            uid = int(fields.get("Uid", "-1").split()[0])
            if (
                parent == reaper.pid
                and uid == os.getuid()
                and nspid
                and nspid[-1] == local_pid
                and _process_stat_start(_read_at(pin.proc_fd, "stat")) == pin.start_time
            ):
                matched.append(pin)
            else:
                pin.close()
        except (OSError, ValueError, SandboxFailure):
            continue
    if len(matched) != 1:
        for item in matched:
            item.close()
        raise SandboxFailure(
            "outer_sandbox_proc_identity_unavailable",
            "the bootstrap could not be correlated to exactly one pinned reaper child",
            phase="establishment",
        )
    return matched[0]


def _same_uid_proc_children(parent_pid: int) -> list[int]:
    result: list[int] = []
    uid = os.getuid()
    try:
        proc_fd = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_proc_identity_unavailable",
            "neither proc children interface nor pinned proc scan is available",
            phase="establishment",
        ) from exc
    try:
        for name in os.listdir(proc_fd):
            if not name.isdigit():
                continue
            try:
                candidate_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    dir_fd=proc_fd,
                )
                try:
                    fields = _status_fields(_read_at(candidate_fd, "status"))
                    if (
                        int(fields.get("PPid", "0")) == parent_pid
                        and int(fields.get("Uid", "-1").split()[0]) == uid
                    ):
                        result.append(int(name))
                finally:
                    os.close(candidate_fd)
            except (OSError, ValueError):
                continue
    finally:
        os.close(proc_fd)
    return result


def _verify_establishment(
    plan: ResolvedSandboxPlan,
    scratch: Path,
    reaper: _PinnedProcess,
    bootstrap: _PinnedProcess,
) -> None:
    reaper_fields = _status_fields(_read_at(reaper.proc_fd, "status"))
    reaper_nspid = [int(item) for item in reaper_fields.get("NSpid", "").split()]
    if not reaper_nspid or reaper_nspid[-1] != 1:
        raise SandboxFailure(
            "outer_sandbox_namespace_invalid",
            "Bubblewrap's pinned child was not namespace PID 1",
            phase="establishment",
        )
    fields = _status_fields(_read_at(bootstrap.proc_fd, "status"))
    for capability in ("CapEff", "CapPrm", "CapAmb"):
        try:
            if int(fields.get(capability, "1"), 16) != 0:
                raise SandboxFailure(
                    "outer_sandbox_capabilities_present",
                    "the bootstrap retained Linux capabilities",
                    phase="establishment",
                )
        except ValueError as exc:
            raise SandboxFailure(
                "outer_sandbox_proc_identity_unavailable",
                "capability status could not be parsed",
                phase="establishment",
            ) from exc

    coverage = _verify_coverage_plan(plan)
    mountinfo = parse_mountinfo(_read_at(bootstrap.proc_fd, "mountinfo"))
    _verify_mount(Path("/"), Path("/"), PathAccess.READ_ONLY, mountinfo, bootstrap)
    _verify_mount(scratch, scratch, PathAccess.WRITABLE, mountinfo, bootstrap)
    for operation in plan.operations:
        _verify_mount(
            operation.source,
            operation.destination,
            operation.access,
            mountinfo,
            bootstrap,
        )
        if operation.access is PathAccess.READ_ONLY and (
            PathOrigin.WORKSPACE in operation.origins
            or PathOrigin.GIT_METADATA in operation.origins
        ):
            for nested in mountinfo:
                if (
                    nested.mountpoint != operation.destination
                    and component_contains(operation.destination, nested.mountpoint)
                    and nested.writable
                ):
                    raise SandboxFailure(
                        "outer_sandbox_mount_proof_failed",
                        "a writable submount remained below protected coverage",
                        phase="establishment",
                    )
    cwd = os.readlink(f"/proc/self/fd/{bootstrap.proc_fd}/cwd")
    if Path(cwd) != plan.context.cwd:
        raise SandboxFailure(
            "outer_sandbox_cwd_mismatch",
            "the bootstrap current directory did not match the resolved plan",
            phase="establishment",
        )
    for _operation, records in coverage:
        for record in records:
            inside = _namespace_path(bootstrap, record.destination)
            value = os.stat(inside, follow_symlinks=False)
            if (
                not stat.S_ISDIR(value.st_mode)
                or PinnedIdentity.from_stat(value) != record.identity
            ):
                raise SandboxFailure(
                    "outer_sandbox_git_identity_mismatch",
                    "a logical Git root did not retain its pinned identity in the namespace",
                    phase="establishment",
                )


def _verify_coverage_plan(
    plan: ResolvedSandboxPlan,
) -> Tuple[Tuple[MountOperation, Tuple[GitProtectionRecord, ...]], ...]:
    """Prove the frozen logical-Git-root to coverage-operation mapping."""

    if not plan.operations:
        raise SandboxFailure(
            "outer_sandbox_mount_proof_failed",
            "the sandbox plan omitted its workspace coverage operation",
            phase="establishment",
        )
    workspace_index = len(plan.operations) - 1
    workspace_operation = plan.operations[workspace_index]
    if (
        workspace_operation.destination != plan.context.workspace
        or PathOrigin.WORKSPACE not in workspace_operation.origins
        or workspace_operation.access is not PathAccess.READ_ONLY
    ):
        raise SandboxFailure(
            "outer_sandbox_mount_proof_failed",
            "the final filesystem operation was not the protected workspace",
            phase="establishment",
        )

    coverage_indices = [
        index
        for index, operation in enumerate(plan.operations)
        if PathOrigin.WORKSPACE in operation.origins or PathOrigin.GIT_METADATA in operation.origins
    ]
    assignments: Dict[int, list[GitProtectionRecord]] = {index: [] for index in coverage_indices}
    for record in plan.git_records:
        workspace_record = component_contains(plan.context.workspace, record.destination)
        matches = []
        for index in coverage_indices:
            operation = plan.operations[index]
            if workspace_record:
                category_matches = index == workspace_index
            else:
                category_matches = (
                    PathOrigin.GIT_METADATA in operation.origins
                    and PathOrigin.WORKSPACE not in operation.origins
                )
            if (
                category_matches
                and component_contains(operation.destination, record.destination)
                and record.destination in operation.covered_paths
            ):
                matches.append(index)
        if len(matches) != 1:
            raise SandboxFailure(
                "outer_sandbox_mount_proof_failed",
                "a logical Git root did not map to exactly one planned coverage anchor",
                phase="establishment",
            )
        assignments[matches[0]].append(record)

    result = []
    for index in coverage_indices:
        operation = plan.operations[index]
        records = tuple(assignments[index])
        expected_paths = {record.destination for record in records}
        expected_roles = {record.role for record in records}
        if (
            operation.access is not PathAccess.READ_ONLY
            or set(operation.covered_paths) != expected_paths
            or set(operation.git_roles) != expected_roles
        ):
            raise SandboxFailure(
                "outer_sandbox_mount_proof_failed",
                "a coverage operation did not match the frozen logical Git mapping",
                phase="establishment",
            )
        if index == workspace_index:
            expected_origins = (
                {PathOrigin.WORKSPACE, PathOrigin.GIT_METADATA}
                if records
                else {PathOrigin.WORKSPACE}
            )
            if set(operation.origins) != expected_origins:
                raise SandboxFailure(
                    "outer_sandbox_mount_proof_failed",
                    "workspace coverage origins did not match the frozen mapping",
                    phase="establishment",
                )
        else:
            if set(operation.origins) != {PathOrigin.GIT_METADATA} or not records:
                raise SandboxFailure(
                    "outer_sandbox_mount_proof_failed",
                    "an external Git coverage operation was missing or unplanned",
                    phase="establishment",
                )
            for writable_index, writable in enumerate(plan.operations):
                if (
                    writable.access is PathAccess.WRITABLE
                    and component_contains(writable.destination, operation.destination)
                    and writable_index >= index
                ):
                    raise SandboxFailure(
                        "outer_sandbox_mount_proof_failed",
                        "a protected Git anchor did not narrow its writable ancestor",
                        phase="establishment",
                    )
        result.append((operation, records))
    return tuple(result)


def _effective_mount(
    destination: Path,
    entries: Sequence[MountInfoEntry],
) -> MountInfoEntry:
    exact = [item for item in entries if item.mountpoint == destination]
    if not exact:
        raise SandboxFailure(
            "outer_sandbox_mount_proof_failed",
            "a planned mount destination was absent",
            phase="establishment",
        )
    return max(exact, key=lambda item: item.mount_id)


def _verify_mount(
    source: Path,
    destination: Path,
    access: PathAccess,
    entries: Sequence[MountInfoEntry],
    bootstrap: _PinnedProcess,
) -> None:
    mount = _effective_mount(destination, entries)
    if access is PathAccess.READ_ONLY and mount.writable:
        raise SandboxFailure(
            "outer_sandbox_mount_proof_failed",
            "a planned read-only mount was effectively writable",
            phase="establishment",
        )
    if access is PathAccess.WRITABLE and not mount.writable:
        raise SandboxFailure(
            "outer_sandbox_mount_proof_failed",
            "a planned writable mount was not effectively writable",
            phase="establishment",
        )
    host = os.stat(source, follow_symlinks=False)
    inside = os.stat(_namespace_path(bootstrap, destination), follow_symlinks=False)
    if PinnedIdentity.from_stat(host) != PinnedIdentity.from_stat(inside):
        raise SandboxFailure(
            "outer_sandbox_mount_proof_failed",
            "a planned mount did not expose the pinned source identity",
            phase="establishment",
        )


def _namespace_path(process: _PinnedProcess, destination: Path) -> str:
    relative = str(destination).lstrip("/")
    base = f"/proc/self/fd/{process.proc_fd}/root"
    # The final ``root`` component is a procfs magic link. Traverse through it
    # before applying ``follow_symlinks=False`` so the namespace root's actual
    # identity is compared rather than the magic-link inode.
    return f"{base}/." if not relative else f"{base}/{relative}"


def _signal_group(pid: int, value: signal.Signals) -> None:
    try:
        os.killpg(pid, value)
    except ProcessLookupError:
        pass


async def _wait_pins(pins: Sequence[_PinnedProcess], timeout: float) -> None:
    pidfds = {item.pidfd for item in pins if item.pidfd is not None}
    if not pidfds:
        return

    def wait() -> None:
        poller = select.poll()
        for descriptor in pidfds:
            assert descriptor is not None
            poller.register(descriptor, select.POLLIN)
        pending = set(pidfds)
        deadline = time.monotonic() + timeout
        terminal = select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            events = poller.poll(max(1, int(remaining * 1000)))
            if not events:
                return
            for descriptor, flags in events:
                if descriptor in pending and flags & terminal:
                    pending.remove(descriptor)
                    poller.unregister(descriptor)

    await asyncio.to_thread(wait)
