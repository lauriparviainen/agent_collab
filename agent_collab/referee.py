from __future__ import annotations

from dataclasses import dataclass
import asyncio
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import CollaborationConfig, builtin_config, load_config, validate_config, validate_mode
from .events import Event
from .logging import SessionLogger
from .runners import AgentRunner, DryRunRunner, MockRunner, configured_runner
from .terminal import print_event


MODES = set(builtin_config().modes)


@dataclass
class RefereeConfig:
    mode: str = "claude-leads"
    max_turns: int = 3
    timeout: int = 900
    dry_run: bool = False
    mock: bool = False
    verbose: bool = False
    color: bool = True
    workdir: Path = Path(".")
    log_dir: Optional[Path] = None
    session_id: Optional[str] = None
    collab_config: Optional[CollaborationConfig] = None


class Referee:
    def __init__(self, config: RefereeConfig, printer: Optional[Callable[[Event], None]] = None):
        self.config = config
        self.workdir = config.workdir.expanduser().resolve()
        self.log_dir = config.log_dir or (self.workdir / ".agent-collab" / "sessions")
        self.printer = printer or (lambda event: print_event(event, config.color))
        self.collab_config = config.collab_config or load_config(self.workdir)
        if config.collab_config is not None:
            validate_config(self.collab_config)

    def _runners(self) -> Dict[str, AgentRunner]:
        runners: Dict[str, AgentRunner] = {}
        for agent_id, agent in self.collab_config.agents.items():
            if not agent.enabled:
                continue
            if self.config.mock:
                runners[agent_id] = MockRunner(agent.name or agent.id)
            elif self.config.dry_run and agent.type != "mock":
                runners[agent_id] = DryRunRunner(agent.id, [agent.command or agent.id] + list(agent.args), cwd=agent.cwd)
            else:
                runners[agent_id] = configured_runner(agent, self.config.verbose)
        return runners

    def _sequence(self) -> List[str]:
        return list(self.collab_config.modes[self.config.mode].sequence)

    def _prompt_for(self, task: str, agent: str, turn: int, transcript: List[Event]) -> str:
        prior = "\n".join(f"{event.source.upper()}: {event.text}" for event in transcript[-12:])
        guardrails = (
            "You are participating in an agent-collab supervised coding session.\n"
            "Do not invoke Claude, Codex, agent-collab, or another agent subprocess.\n"
            "Use read/analysis/review style unless the human explicitly asked for edits.\n"
            "Do not grant broad shell permissions automatically.\n"
        )
        if turn == 1:
            role = "Lead agent: analyze the task and propose or perform the smallest useful next step."
        elif turn == 2:
            role = "Reviewer agent: critique, identify gaps, and improve the previous response."
        else:
            role = "Lead/reviser: produce a concise revision that accounts for the review."
        return f"{guardrails}\n{role}\n\nTASK:\n{task}\n\nRECENT TRANSCRIPT:\n{prior}\n"

    async def _emit(self, logger: SessionLogger, transcript: List[Event], event: Event) -> None:
        transcript.append(event)
        logger.write(event)
        self.printer(event)

    async def _run_agent_turn(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runner: AgentRunner,
        prompt: str,
    ) -> None:
        async def consume() -> None:
            async for event in runner.run(prompt, self.workdir):
                await self._emit(logger, transcript, event)

        try:
            await asyncio.wait_for(consume(), timeout=self.config.timeout)
        except asyncio.TimeoutError:
            await self._emit(
                logger,
                transcript,
                Event.create("error", "error", f"{runner.name} turn exceeded timeout of {self.config.timeout}s"),
            )

    async def run(self, task: str) -> Dict[str, str]:
        validate_mode(self.collab_config, self.config.mode)
        if not self.workdir.exists() or not self.workdir.is_dir():
            raise ValueError(f"workdir does not exist or is not a directory: {self.workdir}")

        transcript: List[Event] = []
        runners = self._runners()
        sequence = self._sequence()[: max(0, self.config.max_turns)]

        with SessionLogger(self.log_dir, task, self.config.session_id) as logger:
            await self._emit(logger, transcript, Event.create("human", "message", task, {"task": task}))
            await self._emit(
                logger,
                transcript,
                Event.create(
                    "referee",
                    "status",
                    f"mode={self.config.mode} max_turns={self.config.max_turns} timeout={self.config.timeout}s workdir={self.workdir}",
                ),
            )
            for turn, agent_name in enumerate(sequence, start=1):
                await self._emit(logger, transcript, Event.create("referee", "status", f"turn {turn}: {agent_name}"))
                prompt = self._prompt_for(task, agent_name, turn, transcript)
                await self._run_agent_turn(logger, transcript, runners[agent_name], prompt)
            await self._emit(
                logger,
                transcript,
                Event.create(
                    "referee",
                    "message",
                    f"final summary: completed {len(sequence)} supervised turn(s). Logs: {logger.jsonl_path} and {logger.markdown_path}",
                ),
            )
            return {
                "session_id": logger.session_id,
                "jsonl_path": str(logger.jsonl_path),
                "markdown_path": str(logger.markdown_path),
            }


def run_sync(task: str, config: RefereeConfig) -> Dict[str, str]:
    return asyncio.run(Referee(config).run(task))
