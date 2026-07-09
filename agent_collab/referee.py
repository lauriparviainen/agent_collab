from __future__ import annotations

from dataclasses import dataclass
import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import (
    DEFAULT_WORKFLOW,
    CollaborationConfig,
    builtin_config,
    load_config,
    validate_config,
    validate_workflow,
)
from .events import Event
from .logging import SessionLogger
from .paths import GlobalDataPaths
from .runners import AgentRunner, DryRunRunner, MockRunner, _mock_source, configured_runner
from .terminal import print_event


WORKFLOWS = set(builtin_config().workflows)

EventAppender = Callable[[Event], Awaitable[int]]


def _is_provider_session_event(event: Event) -> bool:
    """A provider-session bookkeeping event (carries a captured provider id)."""

    return isinstance(event.raw, dict) and bool(event.raw.get("provider_session_id"))


@dataclass
class RefereeInput:
    event: Event
    target: Optional[str] = None


@dataclass
class RefereeConfig:
    workflow: str = DEFAULT_WORKFLOW
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
    codex_options: Optional[Dict[str, Any]] = None
    claude_options: Optional[Dict[str, Any]] = None
    antigravity_options: Optional[Dict[str, Any]] = None
    # Exact backend-normalized options by agent. Provider buckets above remain
    # as the compatibility fallback for direct/non-daemon callers.
    agent_options: Optional[Dict[str, Dict[str, Any]]] = None
    # Resolved {agent_id: backend_id} carried from start validation so execution
    # uses exactly the selection the start response advertised (no re-resolution).
    agent_backends: Optional[Dict[str, str]] = None
    interactive: bool = False
    interactive_idle_timeout: float = 600.0
    input_queue: Optional[asyncio.Queue[RefereeInput]] = None
    status_callback: Optional[Callable[[str], Awaitable[None]]] = None
    event_appender_callback: Optional[Callable[[Optional[EventAppender]], Awaitable[None]]] = None
    turn_active_callback: Optional[Callable[[bool], Awaitable[None]]] = None


class Referee:
    def __init__(self, config: RefereeConfig, printer: Optional[Callable[[Event], None]] = None):
        self.config = config
        self.workdir = config.workdir.expanduser().resolve()
        self.log_dir = config.log_dir or GlobalDataPaths.resolve().session_dir
        self.printer = printer or (lambda event: print_event(event, config.color))
        self.collab_config = config.collab_config or load_config(self.workdir)
        if config.collab_config is not None:
            validate_config(self.collab_config)
        self._emit_lock: Optional[asyncio.Lock] = None

    def _runners(self) -> Dict[str, AgentRunner]:
        runners: Dict[str, AgentRunner] = {}
        for agent_id, agent in self.collab_config.agents.items():
            if not agent.enabled:
                continue
            if self.config.mock:
                name = agent.name or agent.id
                runners[agent_id] = MockRunner(name, source=_mock_source(agent.type, name))
            elif self.config.dry_run and agent.type != "mock":
                from .options import build_cli_command

                runners[agent_id] = DryRunRunner(
                    agent.id,
                    build_cli_command(agent, self._options_for(agent_id, agent.type)),
                    cwd=agent.cwd,
                    agent=agent,
                )
            else:
                runners[agent_id] = configured_runner(
                    agent,
                    self.config.verbose,
                    self._options_for(agent_id, agent.type),
                    self._backend_for(agent_id),
                )
        return runners

    def _backend_for(self, agent_id: str) -> Optional[str]:
        # Use the backend resolved once at start validation; falling back to
        # None lets configured_runner resolve from agent config for the direct
        # CLI path that never populated the map.
        if self.config.agent_backends:
            return self.config.agent_backends.get(agent_id)
        return None

    def _options_for(self, agent_id: str, agent_type: str) -> Dict[str, Any]:
        if self.config.agent_options is not None and agent_id in self.config.agent_options:
            return dict(self.config.agent_options[agent_id])
        if agent_type == "codex":
            return dict(self.config.codex_options or {})
        if agent_type == "claude":
            return dict(self.config.claude_options or {})
        if agent_type == "antigravity":
            return dict(self.config.antigravity_options or {})
        return {}

    def _sequence(self) -> List[str]:
        return list(self.collab_config.workflows[self.config.workflow].sequence)

    def _guardrails(self) -> str:
        return (
            "You are participating in an agent-collab supervised coding session.\n"
            "Do not invoke Claude, Codex, agent-collab, or another agent subprocess.\n"
            "Use read/analysis/review style unless the human explicitly asked for edits.\n"
            "Do not grant broad shell permissions automatically.\n"
        )

    def _recent_transcript(self, transcript: List[Event]) -> str:
        # SDK backends emit a provider-session status event (a bookkeeping id the
        # daemon persists); it is not conversation and must not leak into a peer
        # agent's handoff prompt, so filter it out before taking the recent window.
        visible = [event for event in transcript if not _is_provider_session_event(event)]
        return "\n".join(f"{event.source.upper()}: {event.text}" for event in visible[-12:])

    def _prompt_for(self, task: str, agent: str, turn: int, transcript: List[Event]) -> str:
        prior = self._recent_transcript(transcript)
        guardrails = self._guardrails()
        if turn == 1:
            role = "Lead agent: analyze the task and propose or perform the smallest useful next step."
        elif turn == 2:
            role = "Reviewer agent: critique, identify gaps, and improve the previous response."
        else:
            role = "Lead/reviser: produce a concise revision that accounts for the review."
        return f"{guardrails}\n{role}\n\nTASK:\n{task}\n\nRECENT TRANSCRIPT:\n{prior}\n"

    def _directed_prompt_for(self, task: str, agent: str, question: str, transcript: List[Event]) -> str:
        prior = self._recent_transcript(transcript)
        role = (
            "Directed agent: answer the referee's latest question using the current transcript. "
            "Keep the response scoped to that question."
        )
        return (
            f"{self._guardrails()}\n{role}\n\n"
            f"TASK:\n{task}\n\n"
            f"RECENT TRANSCRIPT:\n{prior}\n\n"
            f"DIRECTED QUESTION:\n{question}\n"
        )

    async def _emit(self, logger: SessionLogger, transcript: List[Event], event: Event) -> int:
        if self._emit_lock is None:
            self._emit_lock = asyncio.Lock()
        async with self._emit_lock:
            transcript.append(event)
            logger.write(event)
            self.printer(event)
            return len(transcript)

    async def _set_status(self, status: str) -> None:
        if self.config.status_callback is not None:
            await self.config.status_callback(status)

    async def _register_event_appender(self, appender: Optional[EventAppender]) -> None:
        if self.config.event_appender_callback is not None:
            await self.config.event_appender_callback(appender)

    async def _set_turn_active(self, active: bool) -> None:
        if self.config.turn_active_callback is not None:
            await self.config.turn_active_callback(active)

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

        await self._set_turn_active(True)
        try:
            try:
                await asyncio.wait_for(consume(), timeout=self.config.timeout)
            except asyncio.TimeoutError:
                await self._emit(
                    logger,
                    transcript,
                    Event.create("error", "error", f"{runner.name} turn exceeded timeout of {self.config.timeout}s"),
                )
        finally:
            await self._set_turn_active(False)

    async def _process_input_item(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
        item: RefereeInput,
    ) -> None:
        if not item.target:
            return
        await self._emit(logger, transcript, Event.create("referee", "status", f"directed turn: {item.target}"))
        prompt = self._directed_prompt_for(task, item.target, item.event.text, transcript)
        await self._run_agent_turn(logger, transcript, runners[item.target], prompt)

    async def _process_pending_inputs(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
    ) -> None:
        queue = self.config.input_queue
        if queue is None:
            return
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._process_input_item(logger, transcript, runners, task, item)
            finally:
                queue.task_done()

    async def _await_interactive_input(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
    ) -> None:
        queue = self.config.input_queue
        if queue is None:
            raise ValueError("interactive sessions require an input queue")
        timeout = max(0.0, float(self.config.interactive_idle_timeout))
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._emit(
                    logger,
                    transcript,
                    Event.create(
                        "referee",
                        "status",
                        f"interactive idle timeout after {timeout:g}s; closing session",
                        {"interactive_idle_timeout": timeout},
                    ),
                )
                return
            try:
                await self._process_input_item(logger, transcript, runners, task, item)
            finally:
                queue.task_done()
            await self._process_pending_inputs(logger, transcript, runners, task)

    async def _emit_final_summary(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        turns: int,
    ) -> None:
        await self._emit(
            logger,
            transcript,
            Event.create(
                "referee",
                "message",
                f"final summary: completed {turns} supervised turn(s). Logs: {logger.jsonl_path} and {logger.markdown_path}",
            ),
        )

    async def run(self, task: str) -> Dict[str, str]:
        validate_workflow(self.collab_config, self.config.workflow)
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
                    f"workflow={self.config.workflow} max_turns={self.config.max_turns} timeout={self.config.timeout}s workdir={self.workdir}",
                ),
            )
            if self.config.interactive:
                await self._register_event_appender(lambda event: self._emit(logger, transcript, event))
            try:
                for turn, agent_name in enumerate(sequence, start=1):
                    if self.config.interactive:
                        await self._process_pending_inputs(logger, transcript, runners, task)
                    await self._emit(logger, transcript, Event.create("referee", "status", f"turn {turn}: {agent_name}"))
                    prompt = self._prompt_for(task, agent_name, turn, transcript)
                    await self._run_agent_turn(logger, transcript, runners[agent_name], prompt)
                if self.config.interactive:
                    await self._process_pending_inputs(logger, transcript, runners, task)
                    await self._set_status("awaiting_input")
                    await self._await_interactive_input(logger, transcript, runners, task)
                    await self._emit_final_summary(logger, transcript, len(sequence))
                    await self._set_status("done")
                else:
                    await self._emit_final_summary(logger, transcript, len(sequence))
            finally:
                if self.config.interactive:
                    await self._register_event_appender(None)
            return {
                "session_id": logger.session_id,
                "jsonl_path": str(logger.jsonl_path),
                "markdown_path": str(logger.markdown_path),
            }


def run_sync(task: str, config: RefereeConfig) -> Dict[str, str]:
    return asyncio.run(Referee(config).run(task))
