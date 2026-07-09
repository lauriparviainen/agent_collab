from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, List, Optional

from .config import AgentConfig, ConfigError
from .events import Event


Parser = Callable[[str, bool], Optional[Event]]


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
        agent: Optional[AgentConfig] = None,
    ):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.agent = agent

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        command = list(self.command)
        if self.agent is not None:
            from .options import apply_runtime_workdir_args

            command = apply_runtime_workdir_args(command, self.agent, run_dir)
        argv = command + [prompt]
        yield Event.create(
            "referee",
            "command",
            f"dry-run would execute in {run_dir}: {' '.join(argv)}",
            {"argv": argv, "workdir": str(run_dir)},
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
        agent: Optional[AgentConfig] = None,
    ):
        self.name = name
        self.command_prefix = command_prefix
        self.parser = parser
        self.verbose = verbose
        self.env = env or {}
        self.cwd = cwd
        self.agent = agent

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        command_prefix = list(self.command_prefix)
        if self.agent is not None:
            from .options import apply_runtime_workdir_args

            command_prefix = apply_runtime_workdir_args(command_prefix, self.agent, run_dir)
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
            )
        except FileNotFoundError as exc:
            yield Event.create("error", "error", f"{self.name} command not found: {argv[0]}", {"error": str(exc)})
            return

        queue: asyncio.Queue[Optional[Event]] = asyncio.Queue()

        async def read_stdout() -> None:
            assert process.stdout is not None
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                event = self.parser(line, self.verbose)
                if event is not None:
                    await queue.put(event)

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for raw_line in process.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    if _is_noisy_stderr(line):
                        if self.verbose:
                            await queue.put(Event.create(_event_source(self.name), "status", f"{self.name} stderr: {line}", {"line": line}))
                        continue
                    await queue.put(Event.create("error", "error", f"{self.name} stderr: {line}", {"line": line}))

        async def wait_done() -> None:
            stdout_task = asyncio.create_task(read_stdout())
            stderr_task = asyncio.create_task(read_stderr())
            code = await process.wait()
            await stdout_task
            await stderr_task
            await queue.put(Event.create("referee", "status", f"{self.name} exited with code {code}", {"code": code}))
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
                process.terminate()


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


PROVIDER_SOURCES = {"claude", "codex", "antigravity"}


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
