from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Iterable, List, Optional, Union

from .config import AgentConfig, ConfigError
from .events import Event


ParserResult = Optional[Union[Event, Iterable[Event]]]
Parser = Callable[[str, bool], ParserResult]
CommandBuilder = Callable[[Path], List[str]]

# Provider CLIs emit one JSON object per line. Tool results and large diffs can
# legitimately make one event much larger than asyncio's 64 KiB default, but
# the daemon still needs a finite bound against a broken or hostile child.
DEFAULT_STREAM_LIMIT_BYTES = 8 * 1024 * 1024


class AgentRunner:
    name = "agent"

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        raise NotImplementedError


class DryRunRunner(AgentRunner):
    def __init__(
        self,
        name: str,
        command: List[str],
        cwd: Optional[str] = None,
        command_builder: Optional[CommandBuilder] = None,
    ):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.command_builder = command_builder

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        command = self.command_builder(run_dir) if self.command_builder else list(self.command)
        argv = command + [prompt]
        yield Event.create(
            "referee",
            "command",
            f"dry-run would execute in {run_dir}: {' '.join(argv)}",
            {"argv": argv, "workdir": str(run_dir)},
        )


class BackendDryRunRunner(AgentRunner):
    """Dry-run representation for an in-process backend with no subprocess argv."""

    def __init__(self, name: str, backend_name: str, cwd: Optional[str] = None):
        self.name = name
        self.backend_name = backend_name
        self.cwd = cwd

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        yield Event.create(
            "referee",
            "status",
            f"dry-run would execute {self.backend_name} in {run_dir}",
            {"backend": self.backend_name, "workdir": str(run_dir)},
        )


class MockRunner(AgentRunner):
    def __init__(self, name: str, source: Optional[str] = None):
        self.name = name
        # Message/status events are attributed to the simulated provider so an
        # antigravity mock emits antigravity-sourced events, not a codex fallback.
        self.source = source or _mock_source("", name)

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        source = self.source
        yield Event.create(source, "status", f"mock {self.name} received prompt in {workdir}")
        await asyncio.sleep(0.03)
        yield Event.create("tool", "tool_call", f"mock {self.name} inspects repository state")
        await asyncio.sleep(0.03)
        summary = prompt.strip().splitlines()[0][:120]
        yield Event.create(source, "message", f"Mock {self.name} response for: {summary}")


class SubprocessRunner(AgentRunner):
    def __init__(
        self,
        name: str,
        command_prefix: List[str],
        parser: Parser,
        verbose: bool = False,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        command_builder: Optional[CommandBuilder] = None,
        stream_limit: int = DEFAULT_STREAM_LIMIT_BYTES,
    ):
        self.name = name
        self.command_prefix = command_prefix
        self.parser = parser
        self.verbose = verbose
        self.env = env or {}
        self.cwd = cwd
        self.command_builder = command_builder
        if isinstance(stream_limit, bool) or not isinstance(stream_limit, int) or stream_limit <= 0:
            raise ValueError("stream_limit must be a positive integer")
        self.stream_limit = stream_limit

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        command_prefix = self.command_builder(run_dir) if self.command_builder else list(self.command_prefix)
        argv = command_prefix + [prompt]
        yield Event.create(
            "referee",
            "command",
            f"starting {self.name}: {' '.join(argv[:-1])} <prompt>",
            {"argv": argv, "workdir": str(run_dir)},
        )
        try:
            env = os.environ.copy()
            env.update(self.env)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(run_dir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.stream_limit,
            )
        except FileNotFoundError as exc:
            yield Event.create("error", "error", f"{self.name} command not found: {argv[0]}", {"error": str(exc)})
            return

        queue: asyncio.Queue[Optional[Event]] = asyncio.Queue()

        async def read_line(reader: asyncio.StreamReader, stream: str) -> Optional[bytes]:
            try:
                return await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                return exc.partial or None
            except asyncio.LimitOverrunError as exc:
                kind = "JSONL event" if stream == "stdout" else "line"
                raise RuntimeError(
                    f"{stream} {kind} exceeded the {self.stream_limit}-byte transport limit"
                ) from exc

        async def read_stdout() -> None:
            assert process.stdout is not None

            async def queue_parsed(parsed: ParserResult) -> None:
                if parsed is None:
                    return
                events = (parsed,) if isinstance(parsed, Event) else parsed
                for event in events:
                    if not isinstance(event, Event):
                        raise TypeError("parser returned a non-Event value")
                    await queue.put(event)

            while True:
                raw_line = await read_line(process.stdout, "stdout")
                if raw_line is None:
                    finish = getattr(self.parser, "finish", None)
                    if callable(finish):
                        await queue_parsed(finish())
                    return
                line = raw_line.decode("utf-8", errors="replace")
                await queue_parsed(self.parser(line, self.verbose))

        async def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                raw_line = await read_line(process.stderr, "stderr")
                if raw_line is None:
                    return
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    if _is_noisy_stderr(line):
                        if self.verbose:
                            await queue.put(Event.create(_event_source(self.name), "status", f"{self.name} stderr: {line}", {"line": line}))
                        continue
                    await queue.put(Event.create("error", "error", f"{self.name} stderr: {line}", {"line": line}))

        async def terminate_process() -> None:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            # A LimitOverrunError pauses the subprocess pipe transport. Waiting
            # for process exit before consuming that buffered pipe can deadlock,
            # so drain both pipes concurrently with reaping the child.
            tasks = [asyncio.create_task(process.wait())]
            tasks.extend(
                asyncio.create_task(stream.read())
                for stream in (process.stdout, process.stderr)
                if stream is not None
            )
            _done, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)

        async def wait_done() -> None:
            stdout_task = asyncio.create_task(read_stdout())
            stderr_task = asyncio.create_task(read_stderr())
            process_task = asyncio.create_task(process.wait())
            tasks = (stdout_task, stderr_task, process_task)
            try:
                done, _pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                # FIRST_EXCEPTION returns as soon as a reader fails, or after all
                # tasks complete normally. Retrieving results propagates reader
                # and parser failures into this supervisor.
                for task in done:
                    task.result()
                code = process_task.result()
                await queue.put(
                    Event.create(
                        "referee",
                        "status",
                        f"{self.name} exited with code {code}",
                        {"code": code},
                    )
                )
            except asyncio.CancelledError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await terminate_process()
                raise
            except Exception as exc:
                await queue.put(
                    Event.create(
                        "error",
                        "error",
                        f"{self.name} output transport failed: {exc}",
                        {"error": str(exc), "stream_limit": self.stream_limit},
                    )
                )
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await terminate_process()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # An over-limit read leaves bytes buffered in StreamReader.
                # Once the child is reaped, drain both pipes so asyncio can
                # close their transports before the owning event loop exits.
                for stream in (process.stdout, process.stderr):
                    if stream is None:
                        continue
                    try:
                        await stream.read()
                    except Exception:
                        pass
                await queue.put(None)

        done_task = asyncio.create_task(wait_done())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not done_task.done():
                done_task.cancel()
                await asyncio.gather(done_task, return_exceptions=True)


def configured_runner(
    agent: AgentConfig,
    verbose: bool = False,
    options: Optional[Dict[str, object]] = None,
    backend_id: Optional[str] = None,
) -> AgentRunner:
    """Build the runner for an agent by delegating to its resolved backend.

    ``backend_id`` is the effective backend resolved once at start validation and
    carried through execution; when omitted (e.g. the direct CLI path), it is
    resolved from ``agents.<id>.backend`` or the built-in default. ``mock`` agents
    keep their runner-level handling and ignore backend selection.
    """

    if agent.type == "mock":
        name = agent.name or agent.id
        return MockRunner(name, source=_mock_source(agent.type, name))

    from .backends import get_backend, resolve_backend_id

    resolved = backend_id or resolve_backend_id(agent)
    try:
        backend = get_backend(agent.type, resolved)
    except KeyError as exc:
        raise ConfigError(
            f"agents.{agent.id}.backend {resolved!r} is not registered for type {agent.type!r}"
        ) from exc
    normalized = dict(backend.normalize_options(agent, dict(options or {})))
    return backend.create_runner(agent, verbose, normalized)


def _resolve_run_dir(workdir: Path, configured_cwd: Optional[str]) -> Path:
    if not configured_cwd:
        return workdir
    path = Path(configured_cwd).expanduser()
    if not path.is_absolute():
        path = workdir / path
    return path


def _is_noisy_stderr(line: str) -> bool:
    return (
        line == "Reading additional input from stdin..."
        or " WARN " in line
        or line.startswith("WARN ")
    )


PROVIDER_SOURCES = {"claude", "codex", "antigravity", "xai"}


def _event_source(agent_name: str) -> str:
    return agent_name if agent_name in PROVIDER_SOURCES else "tool"


def _mock_source(agent_type: str, agent_name: str) -> str:
    """Attribute mock events to the simulated provider.

    Prefers the agent's type, falls back to its name, and keeps the historical
    ``codex`` default so a plain ``mock`` agent is unchanged.
    """

    for candidate in (agent_type, agent_name):
        if candidate in PROVIDER_SOURCES:
            return candidate
    return "codex"
