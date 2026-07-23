from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Union

from .config import AgentConfig, ConfigError
from .events import Event
from .outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome


ParserResult = Optional[Union[Event, Iterable[Event]]]
Parser = Callable[[str, bool], ParserResult]
CommandBuilder = Callable[[Path], List[str]]
AsyncEventSink = Callable[[Event], Awaitable[None]]

# Provider CLIs emit one JSON object per line. Tool results and large diffs can
# legitimately make one event much larger than asyncio's 64 KiB default, but
# the daemon still needs a finite bound against a broken or hostile child.
DEFAULT_STREAM_LIMIT_BYTES = 8 * 1024 * 1024
SUBPROCESS_TERMINATE_GRACE_SECONDS = 1.0
SUBPROCESS_KILL_GRACE_SECONDS = 1.0


class AgentRunner:
    name = "agent"

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        raise NotImplementedError

    def conversation_active(self) -> bool:
        """True when the runner holds provider-side context the next ``run_turn``
        will continue, so the referee sends a delta continuation prompt instead of
        re-sending guardrails, task, and window. Default False: stateless runners
        (every CLI and mock runner) rebuild context from the prompt each turn."""

        return False

    async def close(self) -> None:
        """Release any client or subprocess held across turns. Default no-op;
        must be idempotent and concurrency-safe against an in-flight or adopted
        ``run_turn`` (a backend that keeps state serializes the two internally)."""

        return None


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

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        command = self.command_builder(run_dir) if self.command_builder else list(self.command)
        argv = command + [prompt]
        await emit(
            Event.create(
                "referee",
                "command",
                f"dry-run would execute in {run_dir}: {' '.join(argv)}",
                {"argv": argv, "workdir": str(run_dir)},
            )
        )
        return TurnOutcome("completed")


class BackendDryRunRunner(AgentRunner):
    """Dry-run representation for an in-process backend with no subprocess argv."""

    def __init__(self, name: str, backend_name: str, cwd: Optional[str] = None):
        self.name = name
        self.backend_name = backend_name
        self.cwd = cwd

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        await emit(
            Event.create(
                "referee",
                "status",
                f"dry-run would execute {self.backend_name} in {run_dir}",
                {"backend": self.backend_name, "workdir": str(run_dir)},
            )
        )
        return TurnOutcome("completed")


class MockRunner(AgentRunner):
    def __init__(self, name: str, source: Optional[str] = None):
        self.name = name
        # Message/status events are attributed to the simulated provider so an
        # antigravity mock emits antigravity-sourced events, not a codex fallback.
        self.source = source or _mock_source("", name)

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        source = self.source
        await emit(Event.create(source, "status", f"mock {self.name} received prompt in {workdir}"))
        await asyncio.sleep(0.03)
        await emit(Event.create("tool", "tool_call", f"mock {self.name} inspects repository state"))
        await asyncio.sleep(0.03)
        summary = prompt.strip().splitlines()[0][:120]
        await emit(Event.create(source, "message", f"Mock {self.name} response for: {summary}"))
        return TurnOutcome("completed")


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
        source: Optional[str] = None,
        clean_eof_fallback: bool = False,
    ):
        self.name = name
        self.command_prefix = command_prefix
        self.parser = parser
        self.verbose = verbose
        self.env = env or {}
        self.cwd = cwd
        self.command_builder = command_builder
        # Provider stderr is attributed by provider type. The runner name is the
        # configured agent id (a display label like "reviewer"), so it can only
        # serve as a fallback source for callers that do not pass the provider.
        if source is not None and source not in PROVIDER_SOURCES:
            raise ValueError(f"source must be one of {sorted(PROVIDER_SOURCES)}, got {source!r}")
        self.source = source if source is not None else _event_source(name)
        if isinstance(stream_limit, bool) or not isinstance(stream_limit, int) or stream_limit <= 0:
            raise ValueError("stream_limit must be a positive integer")
        self.stream_limit = stream_limit
        self.clean_eof_fallback = bool(clean_eof_fallback)

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        command_prefix = (
            self.command_builder(run_dir) if self.command_builder else list(self.command_prefix)
        )
        argv = command_prefix + [prompt]
        await emit(
            Event.create(
                "referee",
                "command",
                f"starting {self.name}: {' '.join(argv[:-1])} <prompt>",
                {"argv": argv, "workdir": str(run_dir)},
            )
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
            await emit(
                Event.create(
                    "error",
                    "error",
                    f"{self.name} command not found: {argv[0]}",
                    {"error": str(exc), "fatal": True},
                )
            )
            return TurnOutcome("failed", "provider_transport_failed")

        queue: asyncio.Queue[Optional[Event]] = asyncio.Queue()
        evidence = TerminalEvidenceAccumulator()
        process_exit_code: Optional[int] = None
        exception_code: Optional[str] = None
        produced_message = False

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
                evidence.extend(_take_parser_evidence(self.parser))
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
                            await queue.put(
                                Event.create(
                                    self.source,
                                    "status",
                                    f"{self.name} stderr: {line}",
                                    {"line": line},
                                )
                            )
                        continue
                    await queue.put(
                        Event.create(
                            "error", "error", f"{self.name} stderr: {line}", {"line": line}
                        )
                    )

        async def terminate_process() -> None:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()), SUBPROCESS_TERMINATE_GRACE_SECONDS
                )
                return
            except asyncio.TimeoutError:
                pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()), SUBPROCESS_KILL_GRACE_SECONDS
                )
            except asyncio.TimeoutError:
                # Ownership transfers to the loop; do not let an anomalous
                # platform wait prevent timeout/interruption recording.
                asyncio.create_task(process.wait())

        async def wait_done() -> None:
            nonlocal process_exit_code, exception_code
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
                process_exit_code = code
                if self.verbose:
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
                exception_code = "provider_output_invalid"
                await queue.put(
                    Event.create(
                        "error",
                        "error",
                        f"{self.name} output transport failed: {exc}",
                        {"error": str(exc), "stream_limit": self.stream_limit, "fatal": True},
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
                if event.type == "message" and event.source != "error" and event.text.strip():
                    produced_message = True
                await emit(event)
        finally:
            if not done_task.done():
                done_task.cancel()
                await asyncio.gather(done_task, return_exceptions=True)
        return evidence.resolve(
            process_exit_code=process_exit_code,
            exception_code=exception_code,
            clean_eof_fallback=self.clean_eof_fallback,
            produced_message=produced_message,
        )


def _take_parser_evidence(parser: Parser) -> Iterable[TerminalEvidence]:
    take = getattr(parser, "take_terminal_evidence", None)
    if not callable(take):
        return ()
    evidence = take()
    if evidence is None:
        return ()
    if isinstance(evidence, TerminalEvidence):
        return (evidence,)
    return tuple(item for item in evidence if isinstance(item, TerminalEvidence))


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
