from __future__ import annotations

from dataclasses import dataclass, replace
import asyncio
from pathlib import Path
import re
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
)

from .approvals import APPROVAL_PARK_EXCLUSION_FACTOR, DEFAULT_APPROVAL_DEADLINE_SECONDS

from .config import (
    DEFAULT_WORKFLOW,
    CollaborationConfig,
    builtin_config,
    load_config,
    validate_config,
    validate_workflow,
    workflow_members,
)
from .events import Event, harvest_message_text, utc_timestamp
from .logging import SessionLogger
from .retention import is_valid_session_id
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
# Extra time after the close bound for adopted close reapers (worker CLOSE
# protocol can take longer than the 2s grace) before plan private roots go.
REAPER_DRAIN_SECONDS = 8.0

EventAppender = Callable[[Event], Awaitable[int]]
# (record, boundary, planned_completed_stages=None, persist=True)
OutcomeCommitter = Callable[..., Awaitable[None]]


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
    """Daemon-owned stop and turn-interrupt cause registered before cancellation."""

    def __init__(self) -> None:
        self._requested = False
        self._session_stopping = False
        self._turn_interrupt = False
        self._event: Optional[asyncio.Event] = None

    def mark_session_stopping(self) -> None:
        """Session will terminate stopped; do not abort the in-flight runner yet."""

        self._session_stopping = True

    def request(self) -> None:
        self._requested = True
        self._session_stopping = True
        if self._event is not None:
            self._event.set()

    def mark_turn_interrupt(self) -> None:
        """Record an operator turn-interrupt without waking the in-flight wait."""

        self._turn_interrupt = True

    def request_turn_interrupt(self) -> None:
        """Ask the current turn to abort without stopping the session."""

        self._turn_interrupt = True
        if self._event is not None:
            self._event.set()

    def turn_interrupt_requested(self) -> bool:
        return self._turn_interrupt

    def consume_turn_interrupt(self) -> None:
        """Clear the one-shot turn interrupt after the park decision."""

        self._turn_interrupt = False
        if self._event is not None and not self._requested:
            self._event.clear()

    def is_set(self) -> bool:
        return self._requested

    def session_stopping(self) -> bool:
        return self._session_stopping or self._requested

    async def wait(self) -> None:
        if self._requested or self._turn_interrupt:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()


# Daemon-authored bookkeeping text from provider_session_event(). The trusted
# in-process marker does not survive JSONL; resume must recognize the same
# status lines without reading untrusted raw identity keys.
_PROVIDER_SESSION_TEXT_RE = re.compile(
    r"^(?P<source>\S+) (?P<kind>session|thread|conversation|response)_id=\S+$"
)


def _is_provider_session_event(event: Event) -> bool:
    """True for live marked events and restored daemon bookkeeping status lines.

    The trusted marker intentionally does not survive log serialization. Restored
    transcripts are recognized by ``type==status`` plus the daemon text shape
    (``{source} {kind}_id={id}``). Provider-controlled ``raw`` keys are never
    treated as a capture source.
    """

    if event.provider_session is not None:
        return True
    if event.type != "status":
        return False
    text = event.text or ""
    match = _PROVIDER_SESSION_TEXT_RE.fullmatch(text)
    return match is not None and match.group("source") == event.source


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
    sandbox: Optional[str] = None
    sandbox_plan: Optional[Any] = None
    # Session-manager approval registry: worker frames register through
    # ``approval_callback``; turn teardown denies or abandons via the two
    # release hooks. All three are no-ops when unset (CLI/mock/direct runs).
    approval_callback: Optional[Callable[[Mapping[str, Any]], Any]] = None
    turn_approval_release_callback: Optional[Callable[[str, str], Awaitable[None]]] = None
    turn_approval_abandon_callback: Optional[Callable[[str], Awaitable[None]]] = None
    # Fail-closed tool-approval deadline (seconds). Expiry auto-denies.
    approval_deadline: float = DEFAULT_APPROVAL_DEADLINE_SECONDS
    # Clock exclusion: pending request ids for this turn. None = never parked
    # (fail-closed: the turn clock keeps running).
    pending_turn_approvals: Optional[Callable[[str], Sequence[str]]] = None
    approval_generation: Optional[Callable[[], int]] = None
    wait_approval_generation: Optional[Callable[[int], Awaitable[Any]]] = None
    resume: bool = False
    resume_events: Optional[List[Event]] = None
    resume_watermarks: Optional[Dict[str, int]] = None
    resume_phase: Optional[Dict[str, Any]] = None
    resume_descriptors: Optional[Dict[str, Dict[str, Any]]] = None
    resume_turn_outcomes: Optional[List[Dict[str, Any]]] = None
    prompt_handoff_callback: Optional[Callable[[str, int], Awaitable[None]]] = None
    phase_commit_callback: Optional[Callable[[int, bool], Awaitable[None]]] = None


# Poll when a park is active but no generation waiter is wired (fail-closed).
_PARK_CLOCK_POLL_SECONDS = 0.05


class _TurnBudget:
    """Remaining turn timeout plus fail-closed park-exclusion caps."""

    def __init__(self, timeout: float, approval_deadline: float) -> None:
        self.remaining = max(0.0, float(timeout))
        self.approval_deadline = max(0.0, float(approval_deadline))
        self.exclusion_budget = APPROVAL_PARK_EXCLUSION_FACTOR * self.approval_deadline
        self.request_caps: Dict[str, float] = {}

    def sync_caps(self, pending: Sequence[str]) -> None:
        pending_set = set(pending)
        for request_id in list(self.request_caps):
            if request_id not in pending_set:
                del self.request_caps[request_id]
        for request_id in pending:
            self.request_caps.setdefault(request_id, self.approval_deadline)

    def exclusion_slice(self, pending: Sequence[str]) -> Optional[float]:
        if not pending or self.exclusion_budget <= 0:
            return None
        caps = [
            self.request_caps[request_id]
            for request_id in pending
            if self.request_caps.get(request_id, 0) > 0
        ]
        if not caps:
            return None
        return min(self.exclusion_budget, min(caps))

    def consume_exclusion(self, pending: Sequence[str], elapsed: float) -> None:
        used = max(0.0, float(elapsed))
        self.exclusion_budget = max(0.0, self.exclusion_budget - used)
        for request_id in pending:
            if request_id in self.request_caps:
                self.request_caps[request_id] = max(0.0, self.request_caps[request_id] - used)

    def consume_running(self, elapsed: float) -> None:
        self.remaining = max(0.0, self.remaining - max(0.0, float(elapsed)))


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
        self._next_turn_number = 1
        self._committed_turn_ids: set[str] = set()
        self._seed_resume_turn_ids()
        self._reaper_tasks: set[asyncio.Task] = set()
        # Per-agent prompt-snapshot watermark: the transcript length captured when
        # the agent's last prompt was *built*. The next continuation delta is
        # transcript[watermark:] minus the agent's own events. Never advanced to
        # completion-time length, so peer events emitted mid-turn stay in the delta.
        self._agent_watermarks: Dict[str, int] = {}
        self._ensure_session_id()
        self._direct_sandbox_plan = config.sandbox_plan is None
        self.sandbox_plan = config.sandbox_plan
        if self.sandbox_plan is None:
            self.sandbox_plan = self._resolve_direct_sandbox_plan()
        self._live_runners: Dict[str, AgentRunner] = {}
        self._in_flight_runner_tasks: Set[asyncio.Task] = set()
        self._in_flight_agents: Dict[asyncio.Task, str] = {}
        # HOST CREATE_PRIVATE roots survive only after the session starts live
        # work. Dry-run, mock, preflight failure, and other never-live starts
        # must roll them back with cleanup_failed_start_roots.
        self._plan_went_live = False

    def _ensure_session_id(self) -> None:
        current = self.config.session_id
        if current:
            if not is_valid_session_id(current):
                raise ValueError(f"invalid session_id {current!r}")
            return
        stamp = utc_timestamp().replace(":", "").replace("+00:00", "Z")
        self.config.session_id = f"{stamp}-session"

    def _resolve_direct_sandbox_plan(self) -> Any:
        from . import backends as backend_registry
        from .paths import AgentCollabHome
        from .sandbox.plan import SandboxOperatorConfig, resolve_session_plan
        from .sandbox.policy import resolve_sandbox_policy
        from .sandbox.specs import NoLocalEffectsSandboxAdapter

        policy = resolve_sandbox_policy(
            self.config.sandbox,
            self.collab_config.system.sandbox_default,
            self.collab_config.system.sandbox_override,
        )
        workflow = self.collab_config.workflows[self.config.workflow]
        selected = list(dict.fromkeys(workflow_members(workflow)))
        agents = {}
        command_previews = {}
        for agent_id in selected:
            agent = self.collab_config.agents[agent_id]
            if self.config.mock or agent.type == "mock":
                adapter = NoLocalEffectsSandboxAdapter()
            else:
                backend_id = self._backend_for(agent_id) or backend_registry.resolve_backend_id(
                    agent
                )
                backend = backend_registry.get_backend(agent.type, backend_id)
                adapter = backend.sandbox_adapter
                options = self._options_for(agent_id)
                if not options:
                    options = dict(backend.normalize_options(agent, {}))
                preview = backend.command_preview(agent, options, self.workdir)
                if preview is not None:
                    command_previews[agent_id] = tuple(preview)
            agents[agent_id] = (agent.cwd, dict(agent.env), adapter)
        system = self.collab_config.system
        return resolve_session_plan(
            policy=policy,
            workspace_path=self.workdir,
            agents=agents,
            command_previews=command_previews,
            operator=SandboxOperatorConfig(
                extra_readable_dirs=tuple(system.sandbox_extra_readable_dirs),
                extra_writable_dirs=tuple(system.sandbox_extra_writable_dirs),
                alias_audit_max_entries=system.sandbox_alias_audit_max_entries,
                alias_audit_timeout_seconds=system.sandbox_alias_audit_timeout_seconds,
                scratch_root=system.sandbox_scratch_root,
                agent_collab_home=AgentCollabHome.resolve().root,
            ),
            session_id=self.config.session_id,
        )

    def request_stop(self) -> None:
        self.stop_signal.request()

    def request_turn_interrupt(self) -> None:
        self.stop_signal.request_turn_interrupt()

    async def interrupt_in_flight(self) -> bool:
        """Ask every live runner to interrupt its active turn. True if any issued."""

        requested = False
        for runner in list(self._live_runners.values()):
            try:
                if await runner.interrupt_request():
                    requested = True
            except Exception:
                continue
        return requested

    def in_flight_runner_tasks(self) -> List[asyncio.Task]:
        return [task for task in self._in_flight_runner_tasks if not task.done()]

    def in_flight_agent_ids(self) -> FrozenSet[str]:
        return frozenset(
            agent_id for task, agent_id in self._in_flight_agents.items() if not task.done()
        )

    async def _preflight_direct_sandbox_plan(self) -> None:
        """Run the engine control omitted by daemon-owned prepared starts.

        SessionManager preflights every OS-enforced agent while preparing a
        daemon start. A directly constructed Referee resolves its own plan and
        therefore must perform the same recursive-read-only control before it
        builds or launches any runner.
        """

        if not self._direct_sandbox_plan or self.config.mock or self.config.dry_run:
            return

        from .sandbox.bubblewrap import discover_bubblewrap
        from .sandbox.specs import SandboxEnforcement
        from .sandbox.supervisor import SandboxSupervisor

        plans = getattr(self.sandbox_plan, "agents", None)
        if plans is None:
            return
        enforced = [
            plan for plan in plans.values() if plan.enforcement is SandboxEnforcement.OS_ENFORCED
        ]
        if not enforced:
            return
        installation = await asyncio.to_thread(discover_bubblewrap)
        supervisor = SandboxSupervisor(installation)
        for plan in enforced:
            await supervisor.preflight(plan)

    def _runners(self) -> Dict[str, AgentRunner]:
        runners: Dict[str, AgentRunner] = {}
        selected = set(workflow_members(self.collab_config.workflows[self.config.workflow]))
        for agent_id, agent in self.collab_config.agents.items():
            if not agent.enabled or agent_id not in selected:
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
                    DryRunRunner(
                        agent.id,
                        preview,
                        cwd=agent.cwd,
                        sandbox_plan=self.sandbox_plan.agents.get(agent_id),
                        resume_finalizer=getattr(backend, "finalize_cli_invocation", None),
                        ownership_flags=getattr(backend, "cli_ownership_flags", ()),
                    )
                    if preview is not None
                    else BackendDryRunRunner(agent.id, f"{agent.type}_{backend_id}", cwd=agent.cwd)
                )
            else:
                runners[agent_id] = configured_runner(
                    runtime_agent,
                    self.config.verbose,
                    self._options_for(agent_id),
                    self._backend_for(agent_id),
                    self.sandbox_plan.agents.get(agent_id),
                )
            if self.config.approval_callback is not None:
                from .backends import capabilities_for, resolve_backend_id

                backend_id = self._backend_for(agent_id) or resolve_backend_id(agent)
                if capabilities_for(agent.type, backend_id).tool_gate:
                    runners[agent_id].set_approval_callback(self.config.approval_callback)
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

    def _stage_role(self, turn: int) -> str:
        if turn == 1:
            return (
                "Lead agent: analyze the task and propose or perform the smallest useful next step."
            )
        if turn == 2:
            return "Reviewer agent: critique, identify gaps, and improve the previous response."
        return "Lead/reviser: produce a concise revision that accounts for the review."

    _DIRECTED_ROLE = (
        "Directed agent: answer the referee's latest question using the current transcript. "
        "Keep the response scoped to that question."
    )

    def _prompt_for(self, task: str, agent: str, turn: int, transcript: List[Event]) -> str:
        prior = self._recent_transcript(transcript)
        return (
            f"{self._guardrails()}\n{self._stage_role(turn)}\n\n"
            f"TASK:\n{task}\n\nRECENT TRANSCRIPT:\n{prior}\n"
        )

    def _parallel_prompt_for(self, task: str, transcript: List[Event]) -> str:
        prior = self._recent_transcript(transcript)
        role = "Reviewer agent: critique, identify gaps, and improve the previous response."
        return f"{self._guardrails()}\n{role}\n\nTASK:\n{task}\n\nRECENT TRANSCRIPT:\n{prior}\n"

    def _directed_prompt_for(
        self, task: str, agent: str, question: str, transcript: List[Event]
    ) -> str:
        prior = self._recent_transcript(transcript)
        return (
            f"{self._guardrails()}\n{self._DIRECTED_ROLE}\n\n"
            f"TASK:\n{task}\n\n"
            f"RECENT TRANSCRIPT:\n{prior}\n\n"
            f"DIRECTED QUESTION:\n{question}\n"
        )

    def _continuation_delta(self, transcript: List[Event], watermark: int, agent_id: str) -> str:
        # Events the provider has not seen since this agent's last prompt was
        # built: transcript[watermark:] minus the agent's own output (already in
        # its provider context) and the provider-session bookkeeping id. No size
        # cap — the watermark advances only over the full snapshot, so nothing is
        # silently dropped.
        visible = [
            event
            for event in transcript[watermark:]
            if event.agent_id != agent_id and not _is_provider_session_event(event)
        ]
        return "\n".join(f"{event.source.upper()}: {event.text}" for event in visible)

    def _continuation_prompt(self, role: str, delta: str, question: Optional[str] = None) -> str:
        # A continuation turn: the provider thread already holds guardrails, task,
        # and prior context, so send only the role note, the new-events delta, and
        # the directed question when present. No guardrails/task/window re-send.
        prompt = f"{role}\n\nNEW EVENTS SINCE YOUR LAST TURN:\n{delta}\n"
        if question is not None:
            prompt += f"\nDIRECTED QUESTION:\n{question}\n"
        return prompt

    def _build_turn_prompt(
        self,
        runner: AgentRunner,
        transcript: List[Event],
        agent_id: str,
        role: str,
        stateless_prompt: Callable[[], str],
        *,
        question: Optional[str] = None,
    ) -> str:
        # Prompt-snapshot semantics: capture the transcript length at build time
        # and advance this agent's watermark to it, whether or not continuity is
        # active this turn, so the delta stays correct once continuity engages.
        snapshot = len(transcript)
        if runner.conversation_active():
            delta = self._continuation_delta(
                transcript, self._agent_watermarks.get(agent_id, 0), agent_id
            )
            prompt = self._continuation_prompt(role, delta, question)
        else:
            prompt = stateless_prompt()
        self._agent_watermarks[agent_id] = snapshot
        return prompt

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

    async def _commit_phase(self, completed_stages: int, parked: bool) -> None:
        if self.config.phase_commit_callback is not None:
            await self.config.phase_commit_callback(int(completed_stages), bool(parked))

    async def _register_event_appender(self, appender: Optional[EventAppender]) -> None:
        if self.config.event_appender_callback is not None:
            await self.config.event_appender_callback(appender)

    async def _set_turn_active(self, active: bool) -> None:
        if self.config.turn_active_callback is not None:
            await self.config.turn_active_callback(active)

    async def _release_turn_approvals(self, turn_id: str, reason: str) -> None:
        if self.config.turn_approval_release_callback is not None:
            await self.config.turn_approval_release_callback(turn_id, reason)

    async def _abandon_turn_approvals(self, turn_id: str) -> None:
        if self.config.turn_approval_abandon_callback is not None:
            await self.config.turn_approval_abandon_callback(turn_id)

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
            "text": harvest_message_text(event.text, event.raw),
            "event_id": chosen,
            "timestamp": event.timestamp,
        }

    def _seed_resume_turn_ids(self) -> None:
        """Continue turn-id allocation after persisted outcomes so resume cannot collide."""

        if not self.config.resume:
            return
        highest = 0
        for item in self.config.resume_turn_outcomes or []:
            if not isinstance(item, dict):
                continue
            turn_id = item.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            self._committed_turn_ids.add(turn_id)
            if turn_id.startswith("turn-"):
                suffix = turn_id[5:]
                if suffix.isdigit():
                    highest = max(highest, int(suffix))
        if highest:
            self._next_turn_number = highest + 1

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
        *,
        planned_completed_stages: Optional[int] = None,
        persist: bool = True,
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
                await self.config.outcome_commit_callback(
                    record, boundary, planned_completed_stages, persist
                )
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

    async def _close_runners_bounded(self, runners: Dict[str, AgentRunner]) -> None:
        """Close every runner within a bound, shielded from teardown.

        A runner's ``close()`` releases any client/subprocess held across turns;
        it must be idempotent and concurrency-safe against an in-flight or adopted
        ``run_turn``. A close that hangs or raises must never hang teardown or
        alter an already-committed outcome, so uncooperative closes are adopted as
        background reapers exactly like a non-cooperative ``run_turn`` task. Called
        from ``run()``'s finally under ``asyncio.shield`` so it completes on normal
        exit, failure, and stop cancellation alike.
        """

        close_tasks = [
            asyncio.create_task(runner.close(), name=f"agent-collab-close-{agent_id}")
            for agent_id, runner in runners.items()
        ]
        if not close_tasks:
            return
        try:
            done, pending = await asyncio.wait(close_tasks, timeout=RUNNER_CLEANUP_GRACE_SECONDS)
        except asyncio.CancelledError:
            for task in close_tasks:
                self._adopt_runner_reaper(task)
            raise
        for task in done:
            _consume_task_result(task)
        for task in pending:
            self._adopt_runner_reaper(task)

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
        planned_completed_stages: Optional[int] = None,
        persist_outcome: bool = True,
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

        runner.bind_turn(turn_id=turn_id, agent_id=agent_id)
        if self.config.prompt_handoff_callback is not None:
            cursor = self._agent_watermarks.get(agent_id, len(transcript))
            await self.config.prompt_handoff_callback(agent_id, cursor)
        runner_task = asyncio.create_task(
            runner.run_turn(prompt, self.workdir, emit),
            name=f"agent-collab-{turn_id}-{agent_id}",
        )
        self._in_flight_runner_tasks.add(runner_task)
        self._in_flight_agents[runner_task] = agent_id
        budget = _TurnBudget(self.config.timeout, self.config.approval_deadline)
        stop_task = asyncio.create_task(self.stop_signal.wait())
        local_outcome: Optional[TurnOutcome] = None
        unexpected_cancel = False
        deadline_fired = False

        if manage_turn_active:
            await self._set_turn_active(True)
        try:
            try:
                deadline_fired = await self._wait_turn_with_park_exclusion(
                    runner_task, stop_task, budget, turn_id
                )
            except asyncio.CancelledError:
                if runner_task.done():
                    pass
                elif self.stop_signal.is_set():
                    local_outcome = TurnOutcome("interrupted", "local_turn_interrupted")
                else:
                    local_outcome = TurnOutcome("failed", "referee_cancelled_unexpected")
                    unexpected_cancel = True

            if runner_task.done():
                try:
                    outcome = runner_task.result()
                except asyncio.CancelledError:
                    outcome = local_outcome or TurnOutcome("failed", "referee_cancelled_unexpected")
                    await self._release_turn_approvals(turn_id, "stop")
                except Exception:
                    outcome = TurnOutcome("failed", "provider_transport_failed")
                    await self._release_turn_approvals(turn_id, "worker_loss")
                else:
                    if getattr(runner, "_turn_ended_locally", False):
                        await self._release_turn_approvals(turn_id, "protocol_error")
                    else:
                        await self._abandon_turn_approvals(turn_id)
            else:
                if local_outcome is None:
                    if deadline_fired or (budget.remaining <= 0 and not stop_task.done()):
                        local_outcome = TurnOutcome("timed_out", "local_turn_timed_out")
                        release_reason = "turn_deadline"
                    elif stop_task.done():
                        local_outcome = TurnOutcome("interrupted", "local_turn_interrupted")
                        release_reason = "stop"
                    else:
                        local_outcome = TurnOutcome("failed", "referee_cancelled_unexpected")
                        release_reason = "stop"
                elif self.stop_signal.is_set():
                    release_reason = "stop"
                else:
                    release_reason = "turn_deadline"
                # Deny pending approvals before cancelling the runner so
                # awaiting_approval cannot outlive this turn.
                await self._release_turn_approvals(turn_id, release_reason)
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
            await asyncio.shield(
                self._commit_outcome(
                    logger,
                    transcript,
                    record,
                    answer,
                    planned_completed_stages=(
                        planned_completed_stages if record.outcome == "completed" else None
                    ),
                    persist=persist_outcome,
                )
            )
            # A provider result that was already complete at arbitration keeps
            # its truthful outcome, but a concurrent registered stop still
            # ends this workflow now instead of launching another turn.
            if self.stop_signal.session_stopping():
                raise asyncio.CancelledError
            if unexpected_cancel:
                raise RequiredTurnFailed(record)
            return record
        finally:
            self._in_flight_runner_tasks.discard(runner_task)
            self._in_flight_agents.pop(runner_task, None)
            if not stop_task.done():
                stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            if manage_turn_active:
                await self._set_turn_active(False)

    def _pending_turn_approval_ids(self, turn_id: str) -> List[str]:
        query = self.config.pending_turn_approvals
        if query is None:
            return []
        try:
            ids = query(turn_id)
        except Exception:
            return []
        if not ids:
            return []
        return [item for item in ids if isinstance(item, str) and item]

    def _make_park_wait_task(self) -> Optional[asyncio.Task[Any]]:
        waiter = self.config.wait_approval_generation
        if waiter is None:
            return None
        gen = 0
        getter = self.config.approval_generation
        if getter is not None:
            try:
                gen = int(getter())
            except Exception:
                return None
        return asyncio.create_task(waiter(gen))

    async def _wait_turn_with_park_exclusion(
        self,
        runner_task: asyncio.Task[Any],
        stop_task: asyncio.Task[Any],
        budget: _TurnBudget,
        turn_id: str,
    ) -> bool:
        """Wait until the runner, stop, or remaining turn budget wins.

        Parked intervals are excluded from the turn clock, capped at one
        approval deadline per request and twice that deadline per turn.
        Ambiguity (no registry, query failure, unknown/empty pending set)
        resumes the clock. Returns True when the local turn budget expired.
        """

        loop = asyncio.get_running_loop()
        # Let an already-complete runner win timeout=0 arbitration.
        await asyncio.sleep(0)
        while not runner_task.done() and not stop_task.done():
            pending = self._pending_turn_approval_ids(turn_id)
            budget.sync_caps(pending)
            slice_cap = budget.exclusion_slice(pending)
            parked = slice_cap is not None
            if not parked and budget.remaining <= 0:
                return True
            duration = slice_cap if parked else budget.remaining
            park_task = self._make_park_wait_task()
            if parked and park_task is None:
                duration = min(duration, _PARK_CLOCK_POLL_SECONDS)
            sleep_task = asyncio.create_task(asyncio.sleep(max(0.0, float(duration))))
            waiters = {runner_task, stop_task, sleep_task}
            if park_task is not None:
                waiters.add(park_task)
            started = loop.time()
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (sleep_task, park_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (sleep_task, park_task) if task is not None),
                    return_exceptions=True,
                )
            elapsed = max(0.0, loop.time() - started)
            if parked:
                budget.consume_exclusion(pending, elapsed)
            else:
                budget.consume_running(elapsed)
        return False

    async def _run_parallel_stage(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
        members: List[str],
        stage_index: int,
    ) -> bool:
        snapshot = list(transcript)
        prompt = self._parallel_prompt_for(task, snapshot)
        # Every member shares this one prompt built from the snapshot; advance
        # each watermark to the snapshot length so the prompt-snapshot invariant
        # (watermark == build-time transcript length) holds for every prompt
        # build, not only the two sequential sites. A parallel workflow is a
        # single non-interactive stage today, so nothing reads these yet; keeping
        # the invariant total is what stays correct if a member ever continues
        # its provider thread on a later turn.
        for agent_id in members:
            self._agent_watermarks[agent_id] = len(snapshot)
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
                        persist_outcome=False,
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

        if self.stop_signal.session_stopping():
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
            if self.stop_signal.turn_interrupt_requested():
                return False
            raise ParallelStageFailed(stage_index)
        return True

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
        prompt = self._build_turn_prompt(
            runners[target],
            transcript,
            target,
            self._DIRECTED_ROLE,
            lambda: self._directed_prompt_for(task, target, item.event.text, transcript),
            question=item.event.text,
        )
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
            if self.stop_signal.turn_interrupt_requested():
                return record
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
            if self.stop_signal.turn_interrupt_requested():
                return

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
            if self.stop_signal.turn_interrupt_requested():
                self.stop_signal.consume_turn_interrupt()
            await self._process_pending_inputs(logger, transcript, runners, task)
            if self.stop_signal.turn_interrupt_requested():
                self.stop_signal.consume_turn_interrupt()

    async def _park_after_turn_interrupt(
        self,
        logger: SessionLogger,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        task: str,
        completed_stages: int,
        total_stages: int,
    ) -> Dict[str, str]:
        """Abandon remaining planned stages and park at awaiting_input."""

        await self._commit_phase(completed_stages, True)
        self.stop_signal.consume_turn_interrupt()
        if self.config.interactive:
            await self._set_input_accepting(True)
            await self._set_status("awaiting_input")
            try:
                await self._await_interactive_input(logger, transcript, runners, task)
            finally:
                await self._set_input_accepting(False)
            await self._commit_phase(completed_stages, False)
            await self._emit_final_summary(logger, transcript, total_stages)
            await self._set_status("done")
        return {
            "session_id": logger.session_id,
            "jsonl_path": str(logger.jsonl_path),
            "markdown_path": str(logger.markdown_path),
        }

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

    def _cleanup_sandbox_plan_private_roots(self) -> None:
        """Best-effort cleanup for CREATE_PRIVATE_DIRECTORY roots on the plan.

        Live session end keeps HOST trajectories and removes SESSION roots.
        Never-live starts (dry-run, mock, preflight failure, or stages never
        started) roll back HOST and SESSION via cleanup_failed_start_roots.
        """

        plan = self.sandbox_plan
        if self._plan_went_live:
            cleanup = getattr(plan, "cleanup_created_session_private_roots", None)
        else:
            cleanup = getattr(plan, "cleanup_failed_start_roots", None)
            if not callable(cleanup):
                cleanup = getattr(plan, "cleanup_created_session_private_roots", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass

    async def _drain_close_reapers(self) -> None:
        """Wait briefly for adopted close reapers before plan private-root rmtree."""

        pending = [task for task in self._reaper_tasks if not task.done()]
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=REAPER_DRAIN_SECONDS)
        except Exception:
            pass

    async def _await_task_until(self, task: "asyncio.Task[Any]", timeout: float) -> None:
        """Wait for *task* until it finishes or *timeout* elapses.

        CancelledError at any await must not end the wait early: concurrent
        ``stop_session()`` calls each ``task.cancel()`` the referee run, and an
        unshielded fallback would let the third cancel skip straight to plan
        private-root rmtree while close/drain is still mid-flight. Always use
        ``asyncio.shield`` and loop until done or the deadline.

        Reuse one ``asyncio.wait`` task across cancels. A fresh
        ``shield(wait(...))`` each iteration would orphan the previous wait
        until its timeout, so a cancel storm fans out unbounded waiters.
        """

        if task.done() or timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        waiter: Optional[asyncio.Task[Any]] = None
        try:
            while not task.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                if waiter is None or waiter.done():
                    waiter = asyncio.create_task(
                        asyncio.wait({task}, timeout=remaining),
                        name="agent-collab-await-task-until",
                    )
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
        finally:
            if waiter is not None and not waiter.done():
                waiter.cancel()
                try:
                    await waiter
                except (asyncio.CancelledError, Exception):
                    pass

    async def run(self, task: str) -> Dict[str, str]:
        validate_workflow(self.collab_config, self.config.workflow)
        if not self.workdir.exists() or not self.workdir.is_dir():
            raise ValueError(f"workdir does not exist or is not a directory: {self.workdir}")

        transcript: List[Event] = []
        runners: Dict[str, AgentRunner] = {}
        stages = self._stages()[: max(0, self.config.max_turns)]
        if self.config.resume:
            transcript = list(self.config.resume_events or [])
            if self.config.resume_watermarks:
                self._agent_watermarks.update(self.config.resume_watermarks)

        try:
            await self._preflight_direct_sandbox_plan()
            runners = self._runners()
            self._seed_resume_runners(runners)
            self._live_runners = runners
            if not self.config.mock and not self.config.dry_run:
                self._plan_went_live = True
            return await self._run_stages(task, transcript, runners, stages)
        finally:
            # Close every runner within a bound, shielded so it runs on normal
            # exit, failure, and stop cancellation alike; a hanging/failing close
            # never hangs teardown. A cancel that lands purely during this
            # cleanup (the stages already finished and committed their outcomes)
            # must not retroactively convert the run into a cancellation: swallow
            # it so the body's own result or exception is what the daemon sees.
            # The shielded close keeps running/adopted in the background. A
            # genuine mid-stage stop still propagates — that CancelledError comes
            # from the body, not from this await.
            # Track close/drain as tasks so cancel of a shielded await cannot
            # proceed to plan rmtree while those coroutines are still mid-flight
            # (shield alone continues them but resumes this finally immediately).
            # _await_task_until keeps waiting through repeated cancels until the
            # bound elapses — no unshielded fallback that a third cancel can skip.
            close_task = asyncio.create_task(
                self._close_runners_bounded(runners),
                name="agent-collab-close-runners",
            )
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                pass
            if not close_task.done():
                await self._await_task_until(
                    close_task,
                    RUNNER_CLEANUP_GRACE_SECONDS + REAPER_DRAIN_SECONDS,
                )
            # Drain any per-runner close reapers adopted inside the bound wait.
            drain_task = asyncio.create_task(
                self._drain_close_reapers(),
                name="agent-collab-drain-close-reapers",
            )
            try:
                await asyncio.shield(drain_task)
            except asyncio.CancelledError:
                pass
            if not drain_task.done():
                await self._await_task_until(drain_task, REAPER_DRAIN_SECONDS)
            self._cleanup_sandbox_plan_private_roots()
            self._live_runners = {}
            self._in_flight_runner_tasks.clear()
            self._in_flight_agents.clear()

    def _seed_resume_runners(self, runners: Dict[str, AgentRunner]) -> None:
        descriptors = self.config.resume_descriptors or {}
        if not self.config.resume or not descriptors:
            return
        for agent_id, runner in runners.items():
            descriptor = descriptors.get(agent_id)
            if isinstance(descriptor, dict):
                runner.seed_resume_descriptor(descriptor)

    async def _run_stages(
        self,
        task: str,
        transcript: List[Event],
        runners: Dict[str, AgentRunner],
        stages: List[List[str]],
    ) -> Dict[str, str]:
        with SessionLogger(self.log_dir, task, self.config.session_id) as logger:
            if not self.config.resume:
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
            # Always register so daemon-minted approval events stay in lockstep
            # with the referee transcript (event_id == transcript index).
            await self._register_event_appender(lambda event: self._emit(logger, transcript, event))
            try:
                phase = dict(self.config.resume_phase or {})
                completed_stages = int(phase.get("completed_stages") or 0)
                parked = bool(phase.get("parked_in_input_loop"))
                if self.config.resume and parked and self.config.interactive:
                    await self._commit_phase(completed_stages, True)
                    await self._set_input_accepting(True)
                    await self._set_status("awaiting_input")
                    try:
                        await self._await_interactive_input(logger, transcript, runners, task)
                    finally:
                        await self._set_input_accepting(False)
                    await self._commit_phase(completed_stages, False)
                    await self._emit_final_summary(logger, transcript, len(stages))
                    await self._set_status("done")
                    return {
                        "session_id": logger.session_id,
                        "jsonl_path": str(logger.jsonl_path),
                        "markdown_path": str(logger.markdown_path),
                    }
                last_completed = completed_stages
                for turn, stage in enumerate(stages, start=1):
                    if self.config.resume and turn <= completed_stages:
                        continue
                    if self.config.interactive:
                        await self._process_pending_inputs(logger, transcript, runners, task)
                        if self.stop_signal.turn_interrupt_requested():
                            return await self._park_after_turn_interrupt(
                                logger,
                                transcript,
                                runners,
                                task,
                                last_completed,
                                len(stages),
                            )
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
                        accepted = await self._run_parallel_stage(
                            logger,
                            transcript,
                            runners,
                            task,
                            stage,
                            turn,
                        )
                        if accepted:
                            last_completed = turn
                            await self._commit_phase(turn, False)
                        if self.stop_signal.turn_interrupt_requested():
                            return await self._park_after_turn_interrupt(
                                logger,
                                transcript,
                                runners,
                                task,
                                last_completed,
                                len(stages),
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
                    prompt = self._build_turn_prompt(
                        runners[agent_name],
                        transcript,
                        agent_name,
                        self._stage_role(turn),
                        lambda name=agent_name, turn=turn: self._prompt_for(
                            task, name, turn, transcript
                        ),
                    )
                    turn_id = self._allocate_occurrence()
                    record = await self._run_agent_turn(
                        logger,
                        transcript,
                        runners[agent_name],
                        prompt,
                        agent_id=agent_name,
                        stage_index=turn,
                        turn_id=turn_id,
                        planned_completed_stages=turn,
                    )
                    if self.stop_signal.turn_interrupt_requested():
                        if record.outcome == "completed":
                            last_completed = turn
                        return await self._park_after_turn_interrupt(
                            logger,
                            transcript,
                            runners,
                            task,
                            last_completed,
                            len(stages),
                        )
                    if record.outcome != "completed":
                        raise RequiredTurnFailed(record)
                    last_completed = turn
                if self.config.interactive:
                    await self._process_pending_inputs(logger, transcript, runners, task)
                    if self.stop_signal.turn_interrupt_requested():
                        return await self._park_after_turn_interrupt(
                            logger,
                            transcript,
                            runners,
                            task,
                            last_completed,
                            len(stages),
                        )
                    # Accept input before announcing awaiting_input, and clear it
                    # before any unwinding: the finally runs the moment the loop
                    # exits (idle timeout, a failed directed turn, or a stop
                    # cancellation), so the awaiting_input -> terminal window is
                    # never seen as settled and never accepts an unread post.
                    await self._commit_phase(len(stages), True)
                    await self._set_input_accepting(True)
                    await self._set_status("awaiting_input")
                    try:
                        await self._await_interactive_input(logger, transcript, runners, task)
                    finally:
                        await self._set_input_accepting(False)
                    await self._commit_phase(len(stages), False)
                    await self._emit_final_summary(logger, transcript, len(stages))
                    await self._set_status("done")
                else:
                    await self._emit_final_summary(logger, transcript, len(stages))
            finally:
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
