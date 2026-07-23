from __future__ import annotations

from dataclasses import dataclass, replace
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
    workflow_members,
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


class ParallelStageFailed(RuntimeError):
    """A parallel stage ended without an accepted member review."""

    def __init__(self, stage_index: int):
        self.stage_index = stage_index
        self.failure = SessionFailure(
            code="parallel_stage_no_accepted_member",
            stage_index=stage_index,
        )
        super().__init__(self.failure.message)


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
    # True while the interactive input loop is live and will consume a posted
    # message; cleared before the loop unwinds on idle timeout, failure, or stop.
    input_accepting_callback: Optional[Callable[[bool], Awaitable[None]]] = None
    outcome_commit_callback: Optional[OutcomeCommitter] = None
    # Records one agent's answer for a completed turn: {agent_id, text,
    # event_id, timestamp}. Never called for a non-completed turn.
    answer_commit_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
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
            # Carry the session's authoritative per-turn deadline into backend
            # command construction. Most backends do not need it, but provider
            # CLIs with their own print deadline can keep both limits aligned.
            runtime_agent = replace(agent, timeout=max(0, int(self.config.timeout)))
            if self.config.mock:
                name = agent.name or agent.id
                runners[agent_id] = MockRunner(name, source=_mock_source(agent.type, name))
            elif self.config.dry_run and agent.type != "mock":
                from .backends import get_backend, resolve_backend_id

                backend_id = self._backend_for(agent_id) or resolve_backend_id(agent)
                backend = get_backend(agent.type, backend_id)
                options = self._options_for(agent_id)
                preview = backend.command_preview(runtime_agent, options, self.workdir)
                runners[agent_id] = (
                    DryRunRunner(agent.id, preview, cwd=agent.cwd)
                    if preview is not None
                    else BackendDryRunRunner(agent.id, f"{agent.type}_{backend_id}", cwd=agent.cwd)
                )
            else:
                runners[agent_id] = configured_runner(
                    runtime_agent,
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

    def _stages(self) -> List[List[str]]:
        workflow = self.collab_config.workflows[self.config.workflow]
        if workflow.parallel is not None:
            return [workflow_members(workflow)]
        return [[agent_id] for agent_id in workflow_members(workflow)]

    def _sole_workflow_agent(self) -> Optional[str]:
        """The single agent participating in this workflow, or None when it has
        more than one distinct member. A solo workflow has exactly one, so an
        untargeted post routes to it like a direct message."""

        members = workflow_members(self.collab_config.workflows[self.config.workflow])
        distinct = list(dict.fromkeys(members))
        return distinct[0] if len(distinct) == 1 else None

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

    def _parallel_prompt_for(self, task: str, transcript: List[Event]) -> str:
        prior = self._recent_transcript(transcript)
        role = "Reviewer agent: critique, identify gaps, and improve the previous response."
        return f"{self._guardrails()}\n{role}\n\nTASK:\n{task}\n\nRECENT TRANSCRIPT:\n{prior}\n"

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

    async def _set_input_accepting(self, accepting: bool) -> None:
        if self.config.input_accepting_callback is not None:
            await self.config.input_accepting_callback(accepting)

    async def _record_answer(self, answer: Dict[str, Any]) -> None:
        if self.config.answer_commit_callback is not None:
            await self.config.answer_commit_callback(answer)

    def _find_turn_answer(
        self, transcript: List[Event], span_start: int, agent_id: str
    ) -> Optional[Dict[str, Any]]:
        """The answer for one completed turn: the agent's final-marked message
        event when the backend marked one, else its last message event in the
        turn's span. Filtering by ``agent_id`` isolates the turn even when a
        parallel stage interleaves peers into the shared transcript. Returns None
        when the turn emitted no usable message (contributes no answer)."""

        answer_index: Optional[int] = None
        final_index: Optional[int] = None
        for index in range(span_start, len(transcript)):
            event = transcript[index]
            if (
                event.agent_id == agent_id
                and event.type == "message"
                and event.source != "error"
                and event.text.strip()
            ):
                answer_index = index
                if isinstance(event.raw, dict) and event.raw.get("final"):
                    final_index = index
        chosen = final_index if final_index is not None else answer_index
        if chosen is None:
            return None
        event = transcript[chosen]
        return {
            "agent_id": agent_id,
            "text": event.text,
            "event_id": chosen,
            "timestamp": event.timestamp,
        }

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
        answer: Optional[Dict[str, Any]] = None,
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
            agent_id=record.agent_id,
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
        # The answer is recorded inside the same shielded call as the outcome so
        # a cancellation (stop) landing on the shield's awaiter never lets a
        # completed outcome commit without its ledger entry.
        if answer is not None:
            await self._record_answer(answer)

    async def _cancel_runner_bounded(self, runner_task: asyncio.Task) -> None:
        runner_task.cancel()
        try:
            done, _pending = await asyncio.wait({runner_task}, timeout=RUNNER_CLEANUP_GRACE_SECONDS)
        except asyncio.CancelledError:
            self._adopt_runner_reaper(runner_task)
            raise
        if done:
            _consume_task_result(runner_task)
        else:
            self._adopt_runner_reaper(runner_task)

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
        manage_turn_active: bool = True,
        event_observer: Optional[Callable[[Event], None]] = None,
    ) -> TurnOutcomeRecord:
        # The event span for this turn's answer starts here, before the runner
        # emits anything. transcript index == daemon event id (appended in
        # lockstep), so a recorded answer event_id is a valid read_events cursor.
        answer_span_start = len(transcript)

        async def emit(event: Event) -> None:
            # Workflow ownership is authoritative. Backends cannot attribute
            # their stream to another configured member.
            event.agent_id = agent_id
            if event_observer is not None:
                event_observer(event)
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

        if manage_turn_active:
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
            # Compute the answer before committing (a sync, cancellation-free
            # step): a failed/refused turn contributes nothing, and the boundary
            # about to be appended is a status event the message filter ignores.
            answer = (
                self._find_turn_answer(transcript, answer_span_start, agent_id)
                if record.outcome == "completed"
                else None
            )
            # Commit the outcome and record its answer atomically under the shield
            # so a stop cancellation never lands a completed outcome without its
            # ledger entry.
            await asyncio.shield(self._commit_outcome(logger, transcript, record, answer))
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
            if manage_turn_active:
                await self._set_turn_active(False)

    async def _run_parallel_stage(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
        members: List[str],
        stage_index: int,
    ) -> None:
        snapshot = list(transcript)
        prompt = self._parallel_prompt_for(task, snapshot)
        produced_messages: set[str] = set()

        def observe(agent_id: str, event: Event) -> None:
            if event.type == "message" and event.source != "error" and event.text.strip():
                produced_messages.add(agent_id)

        occurrences = [(agent_id, self._allocate_occurrence()) for agent_id in members]
        await self._set_turn_active(True)
        member_tasks: List[asyncio.Task] = []
        try:
            member_tasks = [
                asyncio.create_task(
                    self._run_agent_turn(
                        logger,
                        transcript,
                        runners[agent_id],
                        prompt,
                        agent_id=agent_id,
                        stage_index=stage_index,
                        turn_id=turn_id,
                        manage_turn_active=False,
                        event_observer=lambda event, member=agent_id: observe(member, event),
                    ),
                    name=f"agent-collab-stage-{stage_index}-{agent_id}",
                )
                for agent_id, turn_id in occurrences
            ]
            try:
                results = await asyncio.gather(*member_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for member_task in member_tasks:
                    if not member_task.done():
                        member_task.cancel()
                await asyncio.gather(*member_tasks, return_exceptions=True)
                raise
        finally:
            await self._set_turn_active(False)

        if self.stop_signal.is_set():
            raise asyncio.CancelledError

        records: Dict[str, TurnOutcomeRecord] = {}
        for (agent_id, _turn_id), result in zip(occurrences, results):
            if isinstance(result, TurnOutcomeRecord):
                records[agent_id] = result
            elif isinstance(result, RequiredTurnFailed):
                records[agent_id] = result.record
            elif isinstance(result, BaseException):
                raise result
            else:
                raise RuntimeError("parallel member returned an invalid turn result")

        accepted = [
            agent_id
            for agent_id in members
            if records[agent_id].outcome == "completed" and agent_id in produced_messages
        ]
        member_outcomes = {agent_id: records[agent_id].outcome for agent_id in members}
        await self._emit(
            logger,
            transcript,
            Event.create(
                "referee",
                "status",
                f"stage {stage_index} (parallel) completed: "
                f"{len(accepted)}/{len(members)} accepted",
                {
                    "stage": stage_index,
                    "parallel": True,
                    "members": member_outcomes,
                    "accepted_members": accepted,
                },
            ),
        )
        if not accepted:
            raise ParallelStageFailed(stage_index)

    async def _process_input_item(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
        item: RefereeInput,
    ) -> Optional[TurnOutcomeRecord]:
        # An untargeted post runs a turn of the sole agent in a solo session
        # (a cost-bearing behavior change); multi-agent sessions keep the
        # append-only behavior (target=None is recorded but runs no turn).
        target = item.target or self._sole_workflow_agent()
        if not target:
            return None
        await self._emit(
            logger,
            transcript,
            Event.create(
                "referee",
                "status",
                f"directed turn: {target}",
                agent_id=target,
            ),
        )
        prompt = self._directed_prompt_for(task, target, item.event.text, transcript)
        turn_id = self._allocate_occurrence()
        record = await self._run_agent_turn(
            logger,
            transcript,
            runners[target],
            prompt,
            agent_id=target,
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
                # Stop accepting BEFORE the closing emit: this branch has decided
                # to leave the loop, and _emit awaits, so a post landing during
                # that await would enqueue input no one will ever consume. The
                # callback is await-free, so the flag drops with no suspension.
                await self._set_input_accepting(False)
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
        stages = self._stages()[: max(0, self.config.max_turns)]

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
                for turn, stage in enumerate(stages, start=1):
                    if self.config.interactive:
                        await self._process_pending_inputs(logger, transcript, runners, task)
                    if len(stage) > 1:
                        await self._emit(
                            logger,
                            transcript,
                            Event.create(
                                "referee",
                                "status",
                                f"stage {turn} (parallel): {', '.join(stage)}",
                            ),
                        )
                        await self._run_parallel_stage(
                            logger,
                            transcript,
                            runners,
                            task,
                            stage,
                            turn,
                        )
                        continue
                    agent_name = stage[0]
                    await self._emit(
                        logger,
                        transcript,
                        Event.create(
                            "referee",
                            "status",
                            f"turn {turn}: {agent_name}",
                            agent_id=agent_name,
                        ),
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
                    # Accept input before announcing awaiting_input, and clear it
                    # before any unwinding: the finally runs the moment the loop
                    # exits (idle timeout, a failed directed turn, or a stop
                    # cancellation), so the awaiting_input -> terminal window is
                    # never seen as settled and never accepts an unread post.
                    await self._set_input_accepting(True)
                    await self._set_status("awaiting_input")
                    try:
                        await self._await_interactive_input(logger, transcript, runners, task)
                    finally:
                        await self._set_input_accepting(False)
                    await self._emit_final_summary(logger, transcript, len(stages))
                    await self._set_status("done")
                else:
                    await self._emit_final_summary(logger, transcript, len(stages))
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
