from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, List, Optional

from .config import AgentConfig, ConfigError
from .events import Event, parse_claude_line, parse_codex_line
from .options import apply_agent_options


Parser = Callable[[str, bool], Optional[Event]]


class AgentRunner:
    name = "agent"

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        raise NotImplementedError


class DryRunRunner(AgentRunner):
    def __init__(self, name: str, command: List[str], cwd: Optional[str] = None):
        self.name = name
        self.command = command
        self.cwd = cwd

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        argv = self.command + [prompt]
        yield Event.create(
            "referee",
            "command",
            f"dry-run would execute in {run_dir}: {' '.join(argv)}",
            {"argv": argv, "workdir": str(run_dir)},
        )


class MockRunner(AgentRunner):
    def __init__(self, name: str):
        self.name = name

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        source = "claude" if self.name == "claude" else "codex"
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
    ):
        self.name = name
        self.command_prefix = command_prefix
        self.parser = parser
        self.verbose = verbose
        self.env = env or {}
        self.cwd = cwd

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        run_dir = _resolve_run_dir(workdir, self.cwd)
        argv = list(self.command_prefix) + [prompt]
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


def configured_runner(agent: AgentConfig, verbose: bool = False, options: Optional[Dict[str, object]] = None) -> AgentRunner:
    if agent.type == "mock":
        return MockRunner(agent.name or agent.id)
    if agent.type == "claude":
        parser = parse_claude_line
    elif agent.type == "codex":
        parser = parse_codex_line
    else:
        raise ConfigError(f"unsupported agent type for {agent.id!r}: {agent.type!r}")
    if not agent.command:
        raise ConfigError(f"agents.{agent.id}.command is required")
    command = apply_agent_options([agent.command] + list(agent.args), agent, options or {})
    return SubprocessRunner(
        agent.id,
        command,
        parser,
        verbose,
        env=dict(agent.env),
        cwd=agent.cwd,
    )


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


def _event_source(agent_name: str) -> str:
    return agent_name if agent_name in {"claude", "codex"} else "tool"
