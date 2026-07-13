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
from .outcomes import SessionFailure, TurnOutcome, TurnOutcomeRecord
from .paths import GlobalDataPaths
from .runners import (
    AgentRunner,
    BackendDryRunRunner,
    DryRunRunner,
    MockRunner,
    _mock_source,
    configured_runner,
)
from .terminal import print_event


WORKFLOWS = set(builtin_config().workflows)
RUNNER_CLEANUP_GRACE_SECONDS = 2.0

EventAppender = Callable[[Event], Awaitable[int]]
OutcomeCommitter = Callable[[TurnOutcomeRecord, Event], Awaitable[None]]


class RequiredTurnFailed(RuntimeError):
    """A required sequential/directed turn did not complete."""

    def __init__(self, record: TurnOutcomeRecord):
        self.record = record
        super().__init__(record.message or "A required turn did not complete")

    @property
    def failure(self) -> SessionFailure:
        return SessionFailure.from_record(self.record)


class RefereeStopSignal:
    """Daemon-owned stop cause registered before task cancellation."""

    def __init__(self) -> None:
        self._requested = False
        self._event: Optional[asyncio.Event] = None

    def request(self) -> None:
        self._requested = True
        if self._event is not None:
            self._event.set()

    def is_set(self) -> bool:
        return self._requested

    async def wait(self) -> None:
        if self._requested:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()


def _is_provider_session_event(event: Event) -> bool:
    """A live provider-session bookkeeping event (carries a captured id).

    Referee transcripts are constructed from live runner events and are never
    restored from JSONL. The trusted marker intentionally does not survive log
    serialization; a future resume feature must reconstruct identity from
    daemon-owned session state, never from provider-controlled ``raw`` keys.
    """

    return event.provider_session is not None


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
    # Exact backend-normalized options by agent.
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
    outcome_commit_callback: Optional[OutcomeCommitter] = None
    stop_signal: Optional[RefereeStopSignal] = None


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
        self.stop_signal = config.stop_signal or RefereeStopSignal()
        self._policy_cancel = RefereeStopSignal()
        self._next_turn_number = 1
        self._committed_turn_ids: set[str] = set()
        self._reaper_tasks: set[asyncio.Task] = set()

    def request_stop(self) -> None:
        self.stop_signal.request()

    def request_policy_cancel(self) -> None:
        self._policy_cancel.request()

    def _runners(self) -> Dict[str, AgentRunner]:
        runners: Dict[str, AgentRunner] = {}
        for agent_id, agent in self.collab_config.agents.items():
            if not agent.enabled:
                continue
            if self.config.mock:
                name = agent.name or agent.id
                runners[agent_id] = MockRunner(name, source=_mock_source(agent.type, name))
            elif self.config.dry_run and agent.type != "mock":
                from .backends import get_backend, resolve_backend_id

                backend_id = self._backend_for(agent_id) or resolve_backend_id(agent)
                backend = get_backend(agent.type, backend_id)
                options = self._options_for(agent_id)
                preview = backend.command_preview(agent, options, self.workdir)
                runners[agent_id] = (
                    DryRunRunner(agent.id, preview, cwd=agent.cwd)
                    if preview is not None
                    else BackendDryRunRunner(agent.id, f"{agent.type}_{backend_id}", cwd=agent.cwd)
                )
            else:
                runners[agent_id] = configured_runner(
                    agent,
                    self.config.verbose,
                    self._options_for(agent_id),
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

    def _options_for(self, agent_id: str) -> Dict[str, Any]:
        if self.config.agent_options is not None and agent_id in self.config.agent_options:
            return dict(self.config.agent_options[agent_id])
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
            role = (
                "Lead agent: analyze the task and propose or perform the smallest useful next step."
            )
        elif turn == 2:
            role = "Reviewer agent: critique, identify gaps, and improve the previous response."
        else:
            role = "Lead/reviser: produce a concise revision that accounts for the review."
        return f"{guardrails}\n{role}\n\nTASK:\n{task}\n\nRECENT TRANSCRIPT:\n{prior}\n"

    def _directed_prompt_for(
        self, task: str, agent: str, question: str, transcript: List[Event]
    ) -> str:
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

    def _allocate_occurrence(self) -> str:
        turn_id = f"turn-{self._next_turn_number}"
        self._next_turn_number += 1
        return turn_id

    def _canonical_backend(self, agent_id: str) -> str:
        if self.config.mock:
            return "mock"
        agent = self.collab_config.agents[agent_id]
        backend_id = self._backend_for(agent_id) or agent.backend or "cli"
        return f"{agent.type}_{backend_id}"

    async def _commit_outcome(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        record: TurnOutcomeRecord,
    ) -> None:
        if record.turn_id in self._committed_turn_ids:
            raise RuntimeError(f"outcome already committed for {record.turn_id}")
        if record.message:
            detail = f": {record.message} ({record.code})"
        else:
            detail = ""
        boundary = Event.create(
            "referee",
            "status",
            f"{record.turn_id} {record.agent_id} {record.outcome}{detail}",
            {"turn_outcome": record.to_dict()},
        )
        if self._emit_lock is None:
            self._emit_lock = asyncio.Lock()
        async with self._emit_lock:
            transcript.append(boundary)
            logger.write(boundary)
            if self.config.outcome_commit_callback is not None:
                await self.config.outcome_commit_callback(record, boundary)
            else:
                self.printer(boundary)
            self._committed_turn_ids.add(record.turn_id)

    async def _cancel_runner_bounded(self, runner_task: asyncio.Task) -> None:
        runner_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(runner_task), timeout=RUNNER_CLEANUP_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            self._adopt_runner_reaper(runner_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                self._adopt_runner_reaper(runner_task)
                raise
            # The awaited runner acknowledged our cancellation. This is the
            # expected cleanup result, not cancellation of the cleanup owner.
            _consume_task_result(runner_task)
        except Exception:
            # Awaiting the task retrieved its exception; the turn outcome is
            # still determined by the causal local timeout/stop/policy event.
            pass

    def _adopt_runner_reaper(self, runner_task: asyncio.Task) -> None:
        if runner_task.done():
            _consume_task_result(runner_task)
            return
        self._reaper_tasks.add(runner_task)
        runner_task.add_done_callback(self._reaper_tasks.discard)
        runner_task.add_done_callback(_consume_task_result)

    async def _run_agent_turn(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runner: AgentRunner,
        prompt: str,
        *,
        agent_id: str,
        stage_index: int,
        turn_id: str,
    ) -> TurnOutcomeRecord:
        async def emit(event: Event) -> None:
            await self._emit(logger, transcript, event)

        runner_task = asyncio.create_task(
            runner.run_turn(prompt, self.workdir, emit),
            name=f"agent-collab-{turn_id}-{agent_id}",
        )
        deadline_task = asyncio.create_task(asyncio.sleep(max(0, self.config.timeout)))
        stop_task = asyncio.create_task(self.stop_signal.wait())
        policy_task = asyncio.create_task(self._policy_cancel.wait())
        local_outcome: Optional[TurnOutcome] = None
        unexpected_cancel = False

        await self._set_turn_active(True)
        try:
            try:
                await asyncio.wait(
                    {runner_task, deadline_task, stop_task, policy_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                if runner_task.done():
                    pass
                elif self.stop_signal.is_set():
                    local_outcome = TurnOutcome("interrupted", "local_turn_interrupted")
                elif self._policy_cancel.is_set():
                    local_outcome = TurnOutcome("interrupted", "referee_turn_cancelled")
                else:
                    local_outcome = TurnOutcome("failed", "referee_cancelled_unexpected")
                    unexpected_cancel = True

            if runner_task.done():
                try:
                    outcome = runner_task.result()
                except asyncio.CancelledError:
                    outcome = local_outcome or TurnOutcome("failed", "referee_cancelled_unexpected")
                except Exception:
                    outcome = TurnOutcome("failed", "provider_transport_failed")
            else:
                if local_outcome is None:
                    if deadline_task.done():
                        local_outcome = TurnOutcome("timed_out", "local_turn_timed_out")
                    elif stop_task.done():
                        local_outcome = TurnOutcome("interrupted", "local_turn_interrupted")
                    elif policy_task.done():
                        local_outcome = TurnOutcome("interrupted", "referee_turn_cancelled")
                    else:
                        local_outcome = TurnOutcome("failed", "referee_cancelled_unexpected")
                await asyncio.shield(self._cancel_runner_bounded(runner_task))
                outcome = local_outcome

            record = TurnOutcomeRecord.from_outcome(
                turn_id=turn_id,
                stage_index=stage_index,
                agent_id=agent_id,
                backend=self._canonical_backend(agent_id),
                outcome=outcome,
            )
            await asyncio.shield(self._commit_outcome(logger, transcript, record))
            # A provider result that was already complete at arbitration keeps
            # its truthful outcome, but a concurrent registered stop still
            # ends this workflow now instead of launching another turn.
            if self.stop_signal.is_set():
                raise asyncio.CancelledError
            if unexpected_cancel:
                raise RequiredTurnFailed(record)
            return record
        finally:
            for task in (deadline_task, stop_task, policy_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(deadline_task, stop_task, policy_task, return_exceptions=True)
            await self._set_turn_active(False)

    async def _process_input_item(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
        item: RefereeInput,
    ) -> Optional[TurnOutcomeRecord]:
        if not item.target:
            return None
        await self._emit(
            logger, transcript, Event.create("referee", "status", f"directed turn: {item.target}")
        )
        prompt = self._directed_prompt_for(task, item.target, item.event.text, transcript)
        turn_id = self._allocate_occurrence()
        record = await self._run_agent_turn(
            logger,
            transcript,
            runners[item.target],
            prompt,
            agent_id=item.target,
            stage_index=self._next_turn_number - 1,
            turn_id=turn_id,
        )
        if record.outcome != "completed":
            raise RequiredTurnFailed(record)
        return record

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
            await self._emit(
                logger, transcript, Event.create("human", "message", task, {"task": task})
            )
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
                await self._register_event_appender(
                    lambda event: self._emit(logger, transcript, event)
                )
            try:
                for turn, agent_name in enumerate(sequence, start=1):
                    if self.config.interactive:
                        await self._process_pending_inputs(logger, transcript, runners, task)
                    await self._emit(
                        logger,
                        transcript,
                        Event.create("referee", "status", f"turn {turn}: {agent_name}"),
                    )
                    prompt = self._prompt_for(task, agent_name, turn, transcript)
                    turn_id = self._allocate_occurrence()
                    record = await self._run_agent_turn(
                        logger,
                        transcript,
                        runners[agent_name],
                        prompt,
                        agent_id=agent_name,
                        stage_index=turn,
                        turn_id=turn_id,
                    )
                    if record.outcome != "completed":
                        raise RequiredTurnFailed(record)
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


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except BaseException:
        pass
