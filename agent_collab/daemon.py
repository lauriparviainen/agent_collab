from __future__ import annotations

import asyncio
import contextlib
import copy
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import uuid

from .config import (
    DEFAULT_WORKFLOW,
    CollaborationConfig,
    ConfigError,
    load_config,
    resolve_existing_workdir,
)
from .events import Event, compact_json, utc_timestamp
from .outcomes import CANONICAL_MESSAGES, SessionFailure, TurnOutcomeRecord
from .options import (
    StartOptionsError,
    build_session_settings,
    describe_options,
    normalize_start_options,
    resolve_workflow_members,
    validate_start_backends,
)
from .paths import GlobalDataPaths
from .referee import (
    EventAppender,
    ParallelStageFailed,
    Referee,
    RefereeConfig,
    RefereeInput,
    RefereeStopSignal,
    RequiredTurnFailed,
)
from .retention import (
    AWAITING_INPUT,
    DONE,
    FAILED,
    INTERRUPTED,
    LIVE_WAIT_STATUSES,
    RUNNING,
    STOPPED,
    TERMINAL_STATUSES,
    classify_transcript_paths,
    select_expired_sessions,
    transcript_unlink_blocker,
)
from .session_index import SessionIndex


MAX_FULL_TOOL_BYTES = 64 * 1024
MAX_FULL_TRANSCRIPT_BYTES = 1024 * 1024
CANONICAL_SAFE_ERRORS = frozenset(CANONICAL_MESSAGES.values())


class SessionNotFoundError(KeyError):
    """A caller supplied a session id that the manager does not own."""


class SessionRequestError(ValueError):
    """A session operation is invalid for caller-controlled state or input."""


@dataclass
class StartSessionRequest:
    task: str
    workflow: str = DEFAULT_WORKFLOW
    workdir: Union[str, Path] = Path(".")
    max_turns: int = 3
    timeout: int = 900
    mock: bool = False
    dry_run: bool = False
    verbose: bool = False
    color: bool = False
    log_dir: Optional[Union[str, Path]] = None
    session_id: Optional[str] = None
    backend_options: Optional[Dict[str, Dict[str, Any]]] = None
    backend: Optional[str] = None
    # Start-time workflow member selection ({slot: agent_id}); validated and
    # folded into the session's config snapshot by resolve_workflow_members.
    members: Optional[Dict[str, str]] = None
    interactive: bool = False
    interactive_idle_timeout: float = 600.0
    # Resolved {agent_id: backend_id}, computed once during start validation and
    # carried into execution; not a user input.
    resolved_backends: Optional[Dict[str, str]] = None
    # Exact backend-normalized options by agent id; runners consume this map.
    agent_options: Optional[Dict[str, Dict[str, Any]]] = None
    # The exact validated config snapshot from start, carried into execution so
    # the runner uses the same agents/types/backends the start response
    # advertised — never a possibly-divergent reload. Not a user input.
    collab_config: Optional[CollaborationConfig] = None
    # Scheduler-only exemption for its daemon-owned empty workdir. from_wire
    # never accepts or sets this flag, so external starts remain confined by
    # [workdir].restrict_workdir_roots.
    internal_workdir_exempt: bool = False

    @classmethod
    def from_wire(cls, data: Dict[str, Any]) -> "StartSessionRequest":
        """Build a request from a raw wire dict via the shared API DTO.

        The single validation/normalization path for the start payload, used by
        both the HTTP server (`POST /sessions`) and the in-daemon MCP backend
        (`SessionManagerToolBackend.start_session`) so the start shape is defined
        once in `api_schema.StartSessionRequestModel`. Raises `ValueError` on
        invalid input, which callers map to a 400 / MCP tool error. Non-user
        fields (`verbose`, `session_id`, `resolved_backends`, ...) keep their
        defaults and are never accepted off the wire.
        """
        from .api_schema import StartSessionRequestModel

        model = StartSessionRequestModel.from_dict(data)
        return cls(
            task=model.task,
            workflow=model.workflow,
            workdir=model.workdir,
            max_turns=model.max_turns,
            timeout=model.timeout,
            mock=model.mock,
            dry_run=model.dry_run,
            interactive=model.interactive,
            interactive_idle_timeout=model.interactive_idle_timeout,
            backend_options=model.backend_options,
            backend=model.backend,
            members=model.members,
        )


@dataclass
class SessionState:
    session_id: str
    status: str
    task: str
    workflow: str
    workdir: str
    jsonl_path: str
    markdown_path: str
    created_at: str
    updated_at: str
    max_turns: int = 3
    timeout: int = 900
    mock: bool = False
    dry_run: bool = False
    interactive: bool = False
    interactive_idle_timeout: float = 600.0
    ended_at: Optional[str] = None
    error: Optional[str] = None
    failure: Optional[Dict[str, Any]] = None
    # None means a legacy record had no outcome instrumentation; every new
    # session starts with a packed append-only list.
    turn_outcomes: Optional[List[Dict[str, Any]]] = None
    settings: Optional[Dict[str, Any]] = None
    # Honest session-level capability summary derived from the backends actually
    # in use (all false this stage); persisted so it survives daemon restart.
    capabilities: Optional[Dict[str, bool]] = None
    # Per-agent provider session identity captured from runner events, keyed by
    # workflow agent id: {agent_id: {backend, provider_session_id,
    # provider_session_kind}}. One uniform schema across providers (the provider's
    # own term lives in provider_session_kind). Persisted, but nothing resumes it
    # this stage — resume stays capability-false.
    agent_sessions: Optional[Dict[str, Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EventBatch:
    session_id: str
    cursor: int
    status: str
    terminal: bool
    error: Optional[str]
    failure: Optional[Dict[str, Any]]
    turn_outcomes: Optional[List[Dict[str, Any]]]
    events: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionResult:
    """The settled (or heartbeat) outcome returned by ``wait_result``.

    Mirrors ``api_schema.SessionResultModel``. ``answers`` is a list of per-agent
    answer dicts ({agent_id, text, event_id, timestamp}).
    """

    session_id: str
    status: str
    terminal: bool
    settled: bool
    cursor: int
    error: Optional[str]
    failure: Optional[Dict[str, Any]]
    turn_outcomes: Optional[List[Dict[str, Any]]]
    answers: List[Dict[str, Any]]
    markdown_path: str
    jsonl_path: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PruneSessionDetail:
    """One session's outcome in a prune run.

    ``disposition`` is one of ``pruned`` (apply removed it), ``preview``
    (would be removed), ``kept`` (protected by the keep count),
    ``skipped_no_timestamp`` (terminal but no usable timestamp),
    ``skipped_live`` (revalidation found a live task or non-terminal status),
    or ``failed`` (a removal step errored; the record stays and the next run
    retries). ``preserved_files`` lists (path, reason) pairs for transcripts
    outside the managed boundary — those are never touched even though the
    index record is removed.
    """

    session_id: str
    status: str
    disposition: str
    effective_at: Optional[str] = None
    removed_files: List[str] = field(default_factory=list)
    preserved_files: List[Dict[str, str]] = field(default_factory=list)
    bytes_reclaimed: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PruneResult:
    """The structured result of one preview or apply prune run."""

    apply: bool
    cutoff: str
    keep: int
    candidates: int
    pruned: int
    failed: int
    bytes_reclaimed: int
    unparseable_records: int
    sessions: List[PruneSessionDetail] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _TrackedInputQueue(asyncio.Queue):
    """``asyncio.Queue`` with a public unfinished counter and a task_done hook.

    ``unfinished`` mirrors the standard ``join()``/``task_done()`` accounting but
    is public so ``wait_result`` can read the in-flight input count without
    touching asyncio internals. ``task_done`` also fires an optional hook (wired
    to the session's coalesced ``_schedule_notify``) so a waiter wakes the moment
    the last in-flight input drains and the session settles.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._unfinished_public = 0
        self._task_done_hook: Optional[Callable[[], None]] = None

    def set_task_done_hook(self, hook: Optional[Callable[[], None]]) -> None:
        self._task_done_hook = hook

    def put_nowait(self, item: Any) -> None:
        super().put_nowait(item)
        self._unfinished_public += 1

    def task_done(self) -> None:
        super().task_done()
        if self._unfinished_public > 0:
            self._unfinished_public -= 1
        if self._task_done_hook is not None:
            self._task_done_hook()

    @property
    def unfinished(self) -> int:
        return self._unfinished_public


@dataclass
class _ManagedSession:
    # request is None for sessions restored from the on-disk index.
    request: Optional[StartSessionRequest]
    state: SessionState
    events: List[Dict[str, Any]]
    condition: asyncio.Condition
    input_queue: _TrackedInputQueue = field(default_factory=_TrackedInputQueue)
    appender_ready: asyncio.Event = field(default_factory=asyncio.Event)
    append_event: Optional[EventAppender] = None
    post_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_active: bool = False
    # True while the referee's interactive input loop is live and will consume a
    # posted message; set by the referee around _await_interactive_input and
    # cleared before it unwinds on idle timeout, failure, or stop. Guards both
    # the settled signal and post_message so a caller never posts into, nor sees
    # a false settle in, the awaiting_input -> terminal transition window.
    input_accepting: bool = False
    # Per-agent latest completed-turn answer pointer, keyed by agent id:
    # {agent_id: {text, event_id, timestamp}}. Recorded by the referee when a
    # completed outcome commits; failed/refused turns contribute nothing. In
    # memory only — restored sessions derive answers from persisted events.
    answer_ledger: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    referee: Optional[Referee] = None
    stop_signal: RefereeStopSignal = field(default_factory=RefereeStopSignal)
    # True while a coalesced watcher notification is scheduled but not yet
    # delivered; _schedule_notify skips scheduling another one meanwhile.
    notify_pending: bool = False


@dataclass(frozen=True)
class _PreparedSessionStart:
    workdir: Path
    log_dir: Path
    collab_config: CollaborationConfig
    normalized_options: Dict[str, Dict[str, Any]]
    agent_options: Dict[str, Dict[str, Any]]
    agent_backends: Dict[str, str]
    settings: Dict[str, Any]
    capabilities: Dict[str, bool]
    interactive_idle_timeout: float


class SessionManager:
    def __init__(
        self,
        lifecycle_logger: Optional[Callable[[str], None]] = None,
        default_workdir: Union[str, Path] = Path("."),
        default_log_dir: Optional[Union[str, Path]] = None,
        index_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self._sessions: Dict[str, _ManagedSession] = {}
        self._notify_tasks: Set[asyncio.Task] = set()
        # Manual and scheduled pruning serialize through this one lock so
        # runs never overlap or interleave their unlink/index phases.
        self._prune_lock: Optional[asyncio.Lock] = None
        self._lifecycle_logger = lifecycle_logger
        self.default_workdir = Path(default_workdir).expanduser().resolve()
        self.default_log_dir = (
            Path(default_log_dir).expanduser().resolve() if default_log_dir else None
        )
        self._index = SessionIndex(Path(index_path)) if index_path else None
        # Set by the HTTP server when the daemon's background model-catalog
        # refresher is running; a ``cached`` describe that served a stale or
        # missing catalog nudges it (never blocks on it).
        self.model_catalog_kick: Optional[Callable[[], None]] = None
        self._restore_from_index()

    def _restore_from_index(self) -> None:
        if self._index is None:
            return
        for session_id, record in sorted(self._index.load().items()):
            state = self._state_from_record(record)
            if state is None or state.session_id in self._sessions:
                continue
            if state.status not in TERMINAL_STATUSES:
                now = utc_timestamp()
                self._transition_state(state, INTERRUPTED)
                # The lost in-memory turn cannot be classified truthfully, so
                # retain the legacy unknown outcome/failure shape without
                # inventing a free-form error alongside the interrupted state.
                state.error = None
                state.updated_at = now
                state.ended_at = now
                self._persist(state)
                self._log_lifecycle(f"session {state.session_id} interrupted by daemon restart")
            self._sessions[state.session_id] = _ManagedSession(
                request=None,
                state=state,
                events=[],
                condition=asyncio.Condition(),
            )

    @staticmethod
    def _state_from_record(record: Dict[str, Any]) -> Optional[SessionState]:
        known = {field.name for field in fields(SessionState)}
        data = {key: value for key, value in record.items() if key in known}
        if not data.get("session_id") or not data.get("status"):
            return None
        if "failure" in data and data["failure"] is not None:
            try:
                data["failure"] = SessionFailure.from_dict(data["failure"]).to_dict()
            except (AttributeError, TypeError, ValueError):
                data["failure"] = None
        if "turn_outcomes" in data and data["turn_outcomes"] is not None:
            sanitized = []
            if isinstance(data["turn_outcomes"], list):
                for item in data["turn_outcomes"]:
                    try:
                        sanitized.append(TurnOutcomeRecord.from_dict(item).to_dict())
                    except (AttributeError, TypeError, ValueError):
                        continue
            data["turn_outcomes"] = sanitized
        try:
            return SessionState(**data)
        except TypeError:
            return None

    def _persist(self, state: SessionState) -> None:
        if self._index is None:
            return
        try:
            self._index.upsert(state.to_dict())
        except OSError as exc:
            self._log_lifecycle(f"failed to persist session index for {state.session_id}: {exc}")

    async def start_session(self, request: StartSessionRequest) -> SessionState:
        prepared = await asyncio.to_thread(self._prepare_session_start, request)
        workdir = prepared.workdir
        log_dir = prepared.log_dir
        request.backend_options = prepared.normalized_options
        request.agent_options = prepared.agent_options
        request.resolved_backends = prepared.agent_backends
        request.collab_config = prepared.collab_config
        request.interactive = bool(request.interactive)
        request.interactive_idle_timeout = prepared.interactive_idle_timeout

        session_id = request.session_id or self._new_session_id()
        self._validate_new_session_id(session_id)

        created_at = utc_timestamp()
        state = SessionState(
            session_id=session_id,
            status=RUNNING,
            task=request.task,
            workflow=request.workflow,
            workdir=str(workdir),
            jsonl_path=str(log_dir / f"{session_id}.jsonl"),
            markdown_path=str(log_dir / f"{session_id}.md"),
            created_at=created_at,
            updated_at=created_at,
            max_turns=int(request.max_turns),
            timeout=int(request.timeout),
            mock=bool(request.mock),
            dry_run=bool(request.dry_run),
            interactive=bool(request.interactive),
            interactive_idle_timeout=float(request.interactive_idle_timeout),
            settings=prepared.settings,
            capabilities=prepared.capabilities,
            turn_outcomes=[],
        )
        managed = _ManagedSession(
            request=request,
            state=state,
            events=[],
            condition=asyncio.Condition(),
        )
        # Draining the last in-flight input settles an awaiting_input session, so
        # a task_done must wake wait_result waiters via the coalesced notifier.
        managed.input_queue.set_task_done_hook(lambda: self._schedule_notify(managed))
        self._sessions[session_id] = managed
        self._persist(state)
        managed.task = asyncio.create_task(
            self._run_session(managed), name=f"agent-collab-session-{session_id}"
        )
        self._log_lifecycle(
            f"session {session_id} started workflow={state.workflow} max_turns={state.max_turns} "
            f"timeout={state.timeout}s mock={state.mock} dry_run={state.dry_run} workdir={state.workdir}"
        )
        return self._copy_state(state)

    def _prepare_session_start(self, request: StartSessionRequest) -> _PreparedSessionStart:
        """Load and validate start inputs outside the daemon event loop."""
        requested_workdir = (
            self.default_workdir if str(request.workdir) == "." else Path(request.workdir)
        )
        try:
            workdir = resolve_existing_workdir(requested_workdir)
            if request.internal_workdir_exempt:
                from .config import load_user_config

                collab_config = load_user_config()
            else:
                collab_config = load_config(workdir)
        except ConfigError as exc:
            raise SessionRequestError(str(exc)) from exc
        workflow = collab_config.workflows.get(request.workflow)
        if workflow is None:
            available = ", ".join(sorted(collab_config.workflows)) or "(none)"
            raise StartOptionsError(
                [
                    {
                        "path": "workflow",
                        "message": (
                            f"unknown workflow {request.workflow!r}; available: {available}"
                        ),
                    }
                ]
            )
        if request.members:
            # Fold the validated start-time member selection into this start's
            # fresh config snapshot: every later validation step, the settings
            # echo, and execution (which carries the snapshot) see the effective
            # members. The selection never comes from config, so #19's posture
            # (project config cannot alter execution) is untouched.
            effective = resolve_workflow_members(collab_config, request.workflow, request.members)
            if effective is not None:
                collab_config.workflows[request.workflow] = effective
                workflow = effective
        if workflow.parallel is not None:
            errors = []
            if request.interactive:
                errors.append(
                    {
                        "path": "interactive",
                        "message": (
                            f"workflow {request.workflow!r} is a parallel workflow; "
                            "interactive sessions are not supported — start it with "
                            "interactive=false"
                        ),
                    }
                )
            if int(request.max_turns) < 1:
                errors.append(
                    {
                        "path": "max_turns",
                        "message": (
                            f"workflow {request.workflow!r} is a parallel workflow; "
                            "max_turns must be at least 1"
                        ),
                    }
                )
            if errors:
                raise StartOptionsError(errors)
        log_dir = (
            Path(request.log_dir).expanduser().resolve()
            if request.log_dir
            else self.default_log_dir or GlobalDataPaths.resolve().session_dir
        )
        # The unknown-workflow case is handled above with a structured
        # ``workflow`` field error. Any ConfigError still raised here comes from
        # validating a *known* workflow (e.g. a disabled member agent); surface
        # its sanitized text as a 400 rather than letting it escape as a 500.
        try:
            selection = validate_start_backends(
                collab_config,
                request.workflow,
                request.backend,
                request.backend_options,
                health=None if (request.mock or request.dry_run) else self._backend_health,
            )
            normalized = normalize_start_options(
                collab_config,
                request.workflow,
                request.backend_options,
                agent_backends=selection.agent_backends,
            )
        except ConfigError as exc:
            raise SessionRequestError(str(exc)) from exc
        normalized_options = normalized.backend_options
        interactive = bool(request.interactive)
        interactive_idle_timeout = self._normalize_idle_timeout(request.interactive_idle_timeout)
        catalog_warnings = self._model_catalog_warnings(request, collab_config, selection)
        settings = build_session_settings(
            collab_config,
            request.workflow,
            normalized_options,
            agent_backends=selection.agent_backends,
            agent_options=normalized.agent_options,
            warnings=[*collab_config.warnings, *selection.warnings, *catalog_warnings],
            interactive=interactive,
            interactive_idle_timeout=interactive_idle_timeout,
            turn_timeout=int(request.timeout),
            workdir=workdir,
        )
        capabilities = self._session_capabilities(collab_config, selection.agent_backends)
        return _PreparedSessionStart(
            workdir=workdir,
            log_dir=log_dir,
            collab_config=collab_config,
            normalized_options=normalized_options,
            agent_options=dict(normalized.agent_options),
            agent_backends=dict(selection.agent_backends),
            settings=settings,
            capabilities=capabilities,
            interactive_idle_timeout=interactive_idle_timeout,
        )

    def _model_catalog_warnings(
        self,
        request: StartSessionRequest,
        config: CollaborationConfig,
        selection: Any,
    ) -> List[Dict[str, str]]:
        """Echo the cached-catalog warn-only default check into the start
        response's effective settings. Cache-only — a start never probes a
        catalog — and mock/dry-run starts skip it entirely so they stay
        hermetic. Never fails a start."""

        if request.mock or request.dry_run:
            return []
        try:
            from .model_catalog import start_catalog_warnings

            return start_catalog_warnings(config, selection.agent_backends)
        except Exception:
            return []

    def _session_capabilities(self, config: Any, agent_backends: Dict[str, str]) -> Dict[str, bool]:
        from . import backends as backend_registry

        per_agent = {
            agent_id: backend_registry.capabilities_for(config.agents[agent_id].type, backend_id)
            for agent_id, backend_id in agent_backends.items()
        }
        return backend_registry.summarize_session_capabilities(per_agent)

    def _backend_health(self, agent_type: str, backend_id: str) -> Any:
        # Start requests always re-probe fresh (bypass the TTL cache) so gating
        # never acts on stale state: install the CLI / sign in, then start works
        # with no daemon restart.
        from . import backends as backend_registry

        backend = backend_registry.get_backend(agent_type, backend_id)
        return backend_registry.HEALTH.health(backend, fresh=True)

    def describe_options(
        self,
        workdir: Optional[Union[str, Path]] = None,
        *,
        health_refresh: str = "cached",
        model_refresh: str = "cached",
    ) -> Dict[str, Any]:
        requested_workdir = Path(workdir) if workdir is not None else self.default_workdir
        try:
            root = resolve_existing_workdir(requested_workdir)
            config = load_config(root)
        except ConfigError as exc:
            raise SessionRequestError(str(exc)) from exc
        return describe_options(
            config, root, health_refresh=health_refresh, model_refresh=model_refresh
        )

    async def describe_options_async(
        self,
        workdir: Optional[Union[str, Path]] = None,
        *,
        health_refresh: str = "cached",
        model_refresh: str = "cached",
    ) -> Dict[str, Any]:
        result = await asyncio.to_thread(
            self.describe_options,
            workdir,
            health_refresh=health_refresh,
            model_refresh=model_refresh,
        )
        # ``cached`` permits nudging the daemon's background refresher when the
        # response served a stale or missing catalog; ``none`` is a strictly
        # local read that never triggers any activity. The inline read path
        # itself never probes in either mode.
        if model_refresh == "cached" and self.model_catalog_kick is not None:
            if _model_catalog_refresh_wanted(result):
                try:
                    self.model_catalog_kick()
                except Exception:
                    pass
        return result

    async def stop_session(self, session_id: str) -> SessionState:
        managed = self._get_managed(session_id)
        task = managed.task
        if managed.state.status in TERMINAL_STATUSES:
            return self._copy_state(managed.state)

        if task is not None and not task.done():
            managed.stop_signal.request()
            if managed.referee is not None:
                managed.referee.request_stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self._set_status(managed, STOPPED)
        else:
            await self._set_status(managed, STOPPED)
        return self._copy_state(managed.state)

    async def prune_sessions(
        self,
        *,
        apply: bool,
        retention: timedelta,
        keep: int = 0,
        now: Optional[datetime] = None,
    ) -> PruneResult:
        """Preview or apply retention over the in-memory session registry.

        Convergent deletion, no transaction: validated managed transcripts are
        unlinked first (in a worker thread, over a detached plan), then the
        selected records leave the index in one atomic rewrite and the
        registry. A crash or failure at any point leaves records that the
        next run re-selects and finishes — rerunning selection *is* the
        recovery mechanism. Failures never propagate out as exceptions for
        individual sessions; they are reported in the result.
        """

        if self._prune_lock is None:
            self._prune_lock = asyncio.Lock()
        async with self._prune_lock:
            current_time = now or datetime.now(timezone.utc)
            cutoff = current_time - retention
            session_dir = self._managed_session_dir()
            selection = select_expired_sessions(
                (managed.state.to_dict() for managed in self._sessions.values()),
                now=current_time,
                retention=retention,
                keep=keep,
            )

            details: List[PruneSessionDetail] = []
            for candidate in selection.kept:
                details.append(
                    PruneSessionDetail(
                        session_id=candidate.session_id,
                        status=candidate.status,
                        disposition="kept",
                        effective_at=candidate.effective_at.isoformat(),
                    )
                )
            for session_id in selection.skipped_no_timestamp:
                state = self._sessions[session_id].state
                details.append(
                    PruneSessionDetail(
                        session_id=session_id,
                        status=state.status,
                        disposition="skipped_no_timestamp",
                    )
                )

            # Revalidate on the event loop and build a detached unlink plan;
            # the worker thread never touches manager state.
            plans: List[Tuple[str, List[Path], List[Tuple[str, str]]]] = []
            candidates_by_id = {}
            for candidate in selection.expired:
                managed = self._sessions.get(candidate.session_id)
                if managed is None:
                    continue
                state = managed.state
                task = managed.task
                if state.status not in TERMINAL_STATUSES or (task is not None and not task.done()):
                    details.append(
                        PruneSessionDetail(
                            session_id=candidate.session_id,
                            status=state.status,
                            disposition="skipped_live",
                            effective_at=candidate.effective_at.isoformat(),
                        )
                    )
                    continue
                plan = classify_transcript_paths(state.to_dict(), session_dir)
                plans.append((candidate.session_id, plan.deletable, plan.preserved))
                candidates_by_id[candidate.session_id] = candidate

            outcomes = await asyncio.to_thread(_execute_transcript_unlinks, plans, apply)

            removable: List[str] = []
            for session_id, deletable, preserved in plans:
                candidate = candidates_by_id[session_id]
                removed, preserved_fs, size, error = outcomes[session_id]
                detail = PruneSessionDetail(
                    session_id=session_id,
                    status=candidate.status,
                    disposition="preview" if not apply else ("failed" if error else "pruned"),
                    effective_at=candidate.effective_at.isoformat(),
                    removed_files=removed,
                    preserved_files=[
                        {"path": path, "reason": reason}
                        for path, reason in preserved + preserved_fs
                    ],
                    bytes_reclaimed=size,
                    error=error,
                )
                details.append(detail)
                if apply and error is None:
                    removable.append(session_id)

            index_error: Optional[str] = None
            if apply and removable and self._index is not None:
                try:
                    self._index.remove_many(removable)
                except OSError as exc:
                    index_error = f"failed to rewrite session index: {exc}"
                    self._log_lifecycle(index_error)
            if apply and index_error is None:
                for session_id in removable:
                    self._sessions.pop(session_id, None)
            elif apply and index_error is not None:
                # Files may already be gone; the records stay so the next run
                # re-selects them and retries the index rewrite (convergence).
                for detail in details:
                    if detail.session_id in removable and detail.disposition == "pruned":
                        detail.disposition = "failed"
                        detail.error = index_error

            pruned = sum(1 for detail in details if detail.disposition == "pruned")
            failed = sum(1 for detail in details if detail.disposition == "failed")
            result = PruneResult(
                apply=apply,
                cutoff=cutoff.isoformat(),
                keep=keep,
                candidates=len(plans),
                pruned=pruned,
                failed=failed,
                bytes_reclaimed=sum(
                    detail.bytes_reclaimed
                    for detail in details
                    if detail.disposition in {"pruned", "preview"}
                ),
                unparseable_records=self._count_unparseable_index_records(),
                sessions=details,
            )
            if apply:
                self._log_lifecycle(
                    f"pruned {result.pruned} session(s), {result.failed} failure(s), "
                    f"{result.bytes_reclaimed} bytes reclaimed, cutoff {result.cutoff}"
                )
            return result

    def _managed_session_dir(self) -> Path:
        return self.default_log_dir or GlobalDataPaths.resolve().session_dir

    def _count_unparseable_index_records(self) -> int:
        """Index records that failed state restoration; reported, never deleted."""

        if self._index is None:
            return 0
        try:
            records = self._index.load()
        except OSError:
            return 0
        return sum(1 for session_id in records if session_id not in self._sessions)

    async def post_message(
        self,
        session_id: str,
        text: str,
        *,
        source: str = "referee",
        target: Optional[str] = None,
    ) -> EventBatch:
        managed = self._get_managed(session_id)
        message_text = self._normalize_message_text(text)
        message_source = self._normalize_message_source(source)

        async with managed.post_lock:
            self._validate_message_session(managed)
            original_target, resolved_target = self._resolve_message_target(managed, target)
            appender = await self._require_event_appender(managed)
            self._validate_message_session(managed)
            queued = bool(managed.turn_active)
            event = Event.create(
                message_source,
                "message",
                message_text,
                {
                    "source": message_source,
                    "target": original_target,
                    "resolved_target": resolved_target,
                    "queued": queued,
                },
            )
            cursor = await appender(event)
            managed.state.updated_at = event.timestamp
            self._persist(managed.state)
            await managed.input_queue.put(RefereeInput(event=event, target=resolved_target))
            return EventBatch(
                session_id=session_id,
                cursor=cursor,
                events=[event.to_dict()],
                **_outcome_view(managed.state),
            )

    def list_sessions(self) -> List[SessionState]:
        return [self._copy_state(managed.state) for managed in self._sessions.values()]

    def get_session(self, session_id: str) -> SessionState:
        return self._copy_state(self._get_managed(session_id).state)

    def read_events(
        self,
        session_id: str,
        cursor: int = 0,
        *,
        limit: Optional[int] = None,
        tool_output: str = "summary",
    ) -> EventBatch:
        managed = self._get_managed(session_id)
        if managed.request is None and not managed.events:
            managed.events = _load_events_from_jsonl(Path(managed.state.jsonl_path))
        events = managed.events[:]
        outcome_view = _outcome_view(managed.state)
        cursor = min(self._normalize_cursor(cursor), len(events))
        limit = self._normalize_limit(limit)
        tool_output = self._normalize_tool_output(tool_output)
        return _event_batch_from_snapshot(
            session_id, events, cursor, limit, tool_output, outcome_view
        )

    async def read_events_async(
        self,
        session_id: str,
        cursor: int = 0,
        *,
        limit: Optional[int] = None,
        tool_output: str = "summary",
    ) -> EventBatch:
        managed = self._get_managed(session_id)
        await self._load_restored_events(managed)
        # Session state belongs to the event-loop thread. Only a detached list
        # snapshot crosses into the worker used for potentially expensive event
        # projection/deep-copying; recorded event dicts are append-only.
        events = managed.events[:]
        outcome_view = _outcome_view(managed.state)
        cursor = min(self._normalize_cursor(cursor), len(events))
        limit = self._normalize_limit(limit)
        tool_output = self._normalize_tool_output(tool_output)
        return await asyncio.to_thread(
            _event_batch_from_snapshot,
            session_id,
            events,
            cursor,
            limit,
            tool_output,
            outcome_view,
        )

    async def _load_restored_events(self, managed: _ManagedSession) -> None:
        if managed.request is not None or managed.events:
            return
        path = Path(managed.state.jsonl_path)
        events = await asyncio.to_thread(_load_events_from_jsonl, path)
        # Another concurrent reader may have populated the cache while this
        # thread was reading. Never overwrite the event-loop-owned list then.
        if not managed.events:
            managed.events = events

    async def wait_events(
        self,
        session_id: str,
        cursor: int = 0,
        timeout_ms: int = 30000,
        *,
        tool_output: str = "summary",
    ) -> EventBatch:
        managed = self._get_managed(session_id)
        await self._load_restored_events(managed)
        cursor = min(self._normalize_cursor(cursor), len(managed.events))
        timeout = max(0, int(timeout_ms)) / 1000.0
        tool_output = self._normalize_tool_output(tool_output)

        if (
            len(managed.events) <= cursor
            and managed.state.status in LIVE_WAIT_STATUSES
            and timeout > 0
        ):
            async with managed.condition:
                if len(managed.events) <= cursor and managed.state.status in LIVE_WAIT_STATUSES:
                    try:
                        await asyncio.wait_for(
                            managed.condition.wait_for(
                                lambda: (
                                    len(managed.events) > cursor
                                    or managed.state.status not in LIVE_WAIT_STATUSES
                                )
                            ),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        pass

        return await self.read_events_async(session_id, cursor, tool_output=tool_output)

    async def wait_result(self, session_id: str, timeout_ms: int = 60000) -> SessionResult:
        """Block until the session is *settled*, then return its outcome.

        Settled := terminal, or ``awaiting_input`` while the referee is actively
        accepting input and none is pending or in flight. Same condition-wait
        shape as ``wait_events``; on timeout the caller gets a heartbeat
        (``settled: false``, no answers) and re-polls. Restored sessions have no
        live runner, are already terminal, and settle immediately.
        """

        managed = self._get_managed(session_id)
        await self._load_restored_events(managed)
        timeout = max(0, int(timeout_ms)) / 1000.0

        if not self._result_settled(managed) and timeout > 0:
            async with managed.condition:
                if not self._result_settled(managed):
                    try:
                        await asyncio.wait_for(
                            managed.condition.wait_for(lambda: self._result_settled(managed)),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        pass
        return self._build_result(managed)

    def _result_settled(self, managed: _ManagedSession) -> bool:
        status = managed.state.status
        if status in TERMINAL_STATUSES:
            return True
        # A live interactive session is settled only while it is genuinely parked
        # for input: status alone is not enough, because the referee leaves its
        # input loop (task_done already called) before the terminal transition.
        return (
            status == AWAITING_INPUT
            and managed.input_accepting
            and managed.input_queue.unfinished == 0
        )

    def _build_result(self, managed: _ManagedSession) -> SessionResult:
        state = managed.state
        settled = self._result_settled(managed)
        outcome = _outcome_view(state)
        return SessionResult(
            session_id=state.session_id,
            status=state.status,
            terminal=outcome["terminal"],
            settled=settled,
            cursor=len(managed.events),
            error=outcome["error"],
            failure=outcome["failure"],
            turn_outcomes=outcome["turn_outcomes"],
            # A heartbeat (not settled) carries no answers: partial output from an
            # in-flight or not-yet-parked turn must never masquerade as a result.
            answers=self._session_answers(managed) if settled else [],
            markdown_path=state.markdown_path,
            jsonl_path=state.jsonl_path,
        )

    def _session_answers(self, managed: _ManagedSession) -> List[Dict[str, Any]]:
        if managed.request is None:
            # Restored session: the in-memory ledger is gone, so derive a
            # best-effort answer per completed agent from the persisted events.
            return self._derive_restored_answers(managed)
        answers = []
        for agent_id, entry in managed.answer_ledger.items():
            answers.append(
                {
                    "agent_id": agent_id,
                    "text": _truncate_text(str(entry.get("text", "")), MAX_FULL_TOOL_BYTES),
                    "event_id": int(entry.get("event_id", 0)),
                    "timestamp": str(entry.get("timestamp", "")),
                }
            )
        return answers

    def _derive_restored_answers(self, managed: _ManagedSession) -> List[Dict[str, Any]]:
        # Reconstruct the per-agent completed-turn answer ledger from persisted
        # events, mirroring the live referee path exactly: a message event
        # becomes an agent's pending answer candidate; a turn-outcome boundary
        # commits that candidate only when the turn completed, and discards it
        # otherwise. Bounding each candidate to its turn span (delimited by the
        # boundary events) keeps a failed or interrupted follow-up turn's partial
        # output from overwriting the agent's last completed answer, so restored
        # and live sessions agree.
        ledger: Dict[str, Dict[str, Any]] = {}
        # Per agent within its current turn span: the last message seen and the
        # last final-marked message. Mirrors referee._find_turn_answer, which
        # prefers a backend-marked final message over later non-final ones.
        pending: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
        for event_id, event in enumerate(managed.events):
            raw = event.get("raw")
            outcome = raw.get("turn_outcome") if isinstance(raw, dict) else None
            if isinstance(outcome, dict):
                agent_id = outcome.get("agent_id")
                if isinstance(agent_id, str) and agent_id:
                    slot = pending.pop(agent_id, None)
                    if outcome.get("outcome") == "completed" and slot is not None:
                        chosen = slot["final"] or slot["last"]
                        if chosen is not None:
                            ledger[agent_id] = chosen
                continue
            agent_id = event.get("agent_id")
            if (
                isinstance(agent_id, str)
                and agent_id
                and event.get("type") == "message"
                and event.get("source") != "error"
                and str(event.get("text", "")).strip()
            ):
                entry = {
                    "agent_id": agent_id,
                    "text": _truncate_text(str(event.get("text", "")), MAX_FULL_TOOL_BYTES),
                    "event_id": event_id,
                    "timestamp": str(event.get("timestamp", "")),
                }
                slot = pending.setdefault(agent_id, {"last": None, "final": None})
                slot["last"] = entry
                if isinstance(raw, dict) and raw.get("final"):
                    slot["final"] = entry
        return list(ledger.values())

    def read_transcript(self, session_id: str, *, tool_output: str = "summary") -> str:
        managed = self._get_managed(session_id)
        tool_output = self._normalize_tool_output(tool_output)
        if tool_output == "full":
            return _read_full_transcript(Path(managed.state.markdown_path))

        events = _load_events_from_jsonl(Path(managed.state.jsonl_path))
        if not events:
            events = managed.events[:]
        return _render_transcript(session_id, events, tool_output)

    async def read_transcript_async(self, session_id: str, *, tool_output: str = "summary") -> str:
        managed = self._get_managed(session_id)
        tool_output = self._normalize_tool_output(tool_output)
        if tool_output == "full":
            path = Path(managed.state.markdown_path)
            return await asyncio.to_thread(_read_full_transcript, path)

        path = Path(managed.state.jsonl_path)
        events = await asyncio.to_thread(_load_events_from_jsonl, path)
        if not events:
            # Snapshot on the event loop; the worker never touches managed
            # session state or its live event list.
            events = managed.events[:]
        return await asyncio.to_thread(
            _render_transcript,
            session_id,
            events,
            tool_output,
        )

    async def _run_session(self, managed: _ManagedSession) -> None:
        request = managed.request
        if request is None:
            return
        state = managed.state
        workdir = Path(state.workdir)
        log_dir = Path(state.jsonl_path).parent
        config = RefereeConfig(
            workflow=request.workflow,
            max_turns=int(request.max_turns),
            timeout=int(request.timeout),
            dry_run=bool(request.dry_run),
            mock=bool(request.mock),
            verbose=bool(request.verbose),
            color=bool(request.color),
            workdir=workdir,
            log_dir=log_dir,
            session_id=state.session_id,
            # Reuse the config validated at start; only reload if it was lost
            # (e.g. an index-restored request, which never runs anyway).
            collab_config=request.collab_config or load_config(workdir),
            agent_options={
                key: dict(value) for key, value in (request.agent_options or {}).items()
            },
            agent_backends=dict(request.resolved_backends or {}),
            interactive=bool(request.interactive),
            interactive_idle_timeout=float(request.interactive_idle_timeout),
            input_queue=managed.input_queue,
            status_callback=lambda status: self._set_status(managed, status),
            event_appender_callback=lambda appender: self._set_event_appender(managed, appender),
            turn_active_callback=lambda active: self._set_turn_active(managed, active),
            input_accepting_callback=lambda accepting: self._set_input_accepting(
                managed, accepting
            ),
            outcome_commit_callback=lambda record, event: self._record_turn_outcome(
                managed, record, event
            ),
            answer_commit_callback=lambda answer: self._record_session_answer(managed, answer),
            stop_signal=managed.stop_signal,
        )

        try:
            referee = Referee(config, printer=lambda event: self._record_event(managed, event))
            managed.referee = referee
            result = await referee.run(request.task)
            state.jsonl_path = result.get("jsonl_path", state.jsonl_path)
            state.markdown_path = result.get("markdown_path", state.markdown_path)
            self._persist(state)
            if state.status in LIVE_WAIT_STATUSES and not managed.stop_signal.is_set():
                await self._set_status(managed, DONE)
        except asyncio.CancelledError:
            # Explicit stop has one publisher: stop_session, after this task
            # settles. A cancellation without that registered cause is a
            # canonical daemon failure.
            if not managed.stop_signal.is_set():
                failure = SessionFailure(code="referee_cancelled_unexpected")
                await self._set_status(managed, FAILED, failure=failure)
        except RequiredTurnFailed as exc:
            await self._set_status(managed, FAILED, failure=exc.failure)
        except ParallelStageFailed as exc:
            await self._set_status(managed, FAILED, failure=exc.failure)
        except Exception as exc:
            self._record_event(
                managed,
                Event.create(
                    "error",
                    "error",
                    "Unexpected session failure",
                    {"exception": exc.__class__.__name__},
                ),
            )
            failure = SessionFailure(code="provider_transport_failed")
            await self._set_status(managed, FAILED, failure=failure)
        finally:
            managed.referee = None

    def _record_event(self, managed: _ManagedSession, event: Event) -> None:
        managed.events.append(event.to_dict())
        self._maybe_capture_provider_session(managed, event)
        self._schedule_notify(managed)

    async def _record_turn_outcome(
        self,
        managed: _ManagedSession,
        record: TurnOutcomeRecord,
        boundary_event: Event,
    ) -> None:
        outcomes = list(managed.state.turn_outcomes or [])
        if any(item.get("turn_id") == record.turn_id for item in outcomes):
            raise RuntimeError(f"duplicate turn outcome {record.turn_id}")
        outcomes.append(record.to_dict())
        managed.state.turn_outcomes = outcomes
        managed.state.updated_at = boundary_event.timestamp
        # Outcome state and its boundary cursor entry are mutated together on
        # the event-loop thread before the single watcher notification.
        managed.events.append(boundary_event.to_dict())
        self._maybe_capture_provider_session(managed, boundary_event)
        self._persist(managed.state)
        self._schedule_notify(managed)

    def _maybe_capture_provider_session(self, managed: _ManagedSession, event: Event) -> None:
        # CLI and SDK runners emit a status event with trusted in-process
        # identity metadata (see backends.common.sdk.provider_session_event).
        # Record it into central session state under one uniform schema; this is
        # capture only — nothing resumes it and capabilities stay honest.
        identity = event.provider_session
        if identity is None:
            return
        session_id = identity.get("provider_session_id")
        agent_id = identity.get("agent_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(agent_id, str)
            or not agent_id
        ):
            return
        request = managed.request
        resolved = request.resolved_backends if request else None
        collab_config = request.collab_config if request else None
        backend_id = resolved.get(agent_id) if resolved else None
        agent = collab_config.agents.get(agent_id) if collab_config else None
        # Session identity is accepted only from a selected agent and its
        # configured provider source. Never let a malformed backend event create
        # arbitrary state entries or impersonate another provider.
        if not backend_id or agent is None or event.source != agent.type:
            return
        entry: Dict[str, Any] = {"backend": backend_id}
        entry["provider_session_id"] = session_id
        kind = identity.get("provider_session_kind")
        if isinstance(kind, str) and kind:
            entry["provider_session_kind"] = kind
        sessions = dict(managed.state.agent_sessions or {})
        if sessions.get(agent_id) == entry:
            return
        sessions[agent_id] = entry
        managed.state.agent_sessions = sessions
        managed.state.updated_at = utc_timestamp()
        self._persist(managed.state)

    async def _set_event_appender(
        self, managed: _ManagedSession, appender: Optional[EventAppender]
    ) -> None:
        managed.append_event = appender
        if appender is None:
            managed.appender_ready.clear()
        else:
            managed.appender_ready.set()
        async with managed.condition:
            managed.condition.notify_all()

    async def _set_turn_active(self, managed: _ManagedSession, active: bool) -> None:
        managed.turn_active = bool(active)
        async with managed.condition:
            managed.condition.notify_all()

    async def _set_input_accepting(self, managed: _ManagedSession, accepting: bool) -> None:
        # Deliberately no internal await: the referee clears this from a finally
        # that can run under cancellation (stop), and an await-free coroutine has
        # no cancellation checkpoint. The coalesced notifier wakes wait_result.
        managed.input_accepting = bool(accepting)
        self._schedule_notify(managed)

    async def _record_session_answer(
        self, managed: _ManagedSession, answer: Dict[str, Any]
    ) -> None:
        agent_id = answer.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return
        managed.answer_ledger[agent_id] = {
            "text": str(answer.get("text", "")),
            "event_id": int(answer.get("event_id", 0)),
            "timestamp": str(answer.get("timestamp", "")),
        }

    @staticmethod
    def _transition_state(state: SessionState, status: str) -> bool:
        current = state.status
        current_terminal = current in TERMINAL_STATUSES
        requested_terminal = status in TERMINAL_STATUSES
        if current_terminal:
            return current == status
        if current not in LIVE_WAIT_STATUSES:
            return False
        if status not in LIVE_WAIT_STATUSES and not requested_terminal:
            return False
        state.status = status
        return True

    async def _set_status(
        self,
        managed: _ManagedSession,
        status: str,
        error: Optional[str] = None,
        failure: Optional[SessionFailure] = None,
    ) -> bool:
        if managed.state.status in TERMINAL_STATUSES:
            return managed.state.status == status
        if not self._transition_state(managed.state, status):
            return False
        now = utc_timestamp()
        managed.state.updated_at = now
        if status in TERMINAL_STATUSES:
            managed.state.ended_at = now
        if failure is not None:
            managed.state.failure = failure.to_dict()
            managed.state.error = failure.message
        elif error is not None:
            # Callers retaining this compatibility path must still select a
            # canonical safe message.
            managed.state.error = error if error in CANONICAL_SAFE_ERRORS else None
        self._persist(managed.state)
        async with managed.condition:
            managed.condition.notify_all()
        if status in TERMINAL_STATUSES:
            suffix = f" error={error}" if error else ""
            self._log_lifecycle(
                f"session {managed.state.session_id} {status} events={len(managed.events)} "
                f"logs={managed.state.jsonl_path}{suffix}"
            )
        return True

    def _schedule_notify(self, managed: _ManagedSession) -> None:
        """Wake watchers once for any burst of events recorded before it runs.

        Events are appended before this is called and watchers re-check their
        cursor under the condition, so one pending notification covers every
        event recorded until it is delivered; only an event recorded after
        delivery starts needs (and gets) a new one.
        """

        if managed.notify_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        managed.notify_pending = True
        task = loop.create_task(self._notify(managed))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _notify(self, managed: _ManagedSession) -> None:
        managed.notify_pending = False
        async with managed.condition:
            managed.condition.notify_all()

    def _get_managed(self, session_id: str) -> _ManagedSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise SessionNotFoundError(f"unknown session_id {session_id}") from exc

    def _validate_new_session_id(self, session_id: str) -> None:
        if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
            raise SessionRequestError(f"invalid session_id {session_id!r}")
        if session_id in self._sessions:
            raise SessionRequestError(f"session_id already exists: {session_id}")

    def _new_session_id(self) -> str:
        return f"daemon-{uuid.uuid4().hex[:16]}"

    def _normalize_cursor(self, cursor: int) -> int:
        cursor = int(cursor)
        if cursor < 0:
            raise SessionRequestError("cursor must be >= 0")
        return cursor

    def _normalize_limit(self, limit: Optional[int]) -> Optional[int]:
        if limit is None:
            return None
        try:
            normalized = int(limit)
        except (TypeError, ValueError) as exc:
            raise SessionRequestError("limit must be an integer") from exc
        if normalized < 1:
            raise SessionRequestError("limit must be >= 1")
        return normalized

    def _normalize_tool_output(self, tool_output: Any) -> str:
        if tool_output not in {"summary", "full"}:
            raise SessionRequestError("tool_output must be 'summary' or 'full'")
        return str(tool_output)

    def _normalize_idle_timeout(self, value: Any) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise SessionRequestError("interactive_idle_timeout must be a number") from exc
        if timeout < 0:
            raise SessionRequestError("interactive_idle_timeout must be >= 0")
        return timeout

    def _normalize_message_text(self, text: Any) -> str:
        if not isinstance(text, str):
            raise SessionRequestError("text is required")
        value = text.strip()
        if not value:
            raise SessionRequestError("text is required")
        return value

    def _normalize_message_source(self, source: Any) -> str:
        value = "referee" if source is None else source
        if not isinstance(value, str) or value not in {"human", "referee"}:
            raise SessionRequestError("source must be 'human' or 'referee'")
        return str(value)

    def _validate_message_session(self, managed: _ManagedSession) -> None:
        state = managed.state
        if managed.request is None:
            raise SessionRequestError("session is read-only because it has no live runner")
        if state.status not in LIVE_WAIT_STATUSES:
            raise SessionRequestError(f"session is not live: {state.status}")
        if not state.interactive or not managed.request.interactive:
            raise SessionRequestError("session was not started with interactive input enabled")
        # Once the referee has left its input loop (idle timeout, a failed
        # directed turn, or stop) but has not yet transitioned to terminal, the
        # queue no longer has a consumer; reject rather than enqueue input that
        # would never be read. Mid-turn posts during planned stages (status
        # running) stay allowed — they are drained at the next turn boundary.
        if state.status == AWAITING_INPUT and not managed.input_accepting:
            raise SessionRequestError("session is no longer accepting input")

    async def _require_event_appender(self, managed: _ManagedSession) -> EventAppender:
        if managed.append_event is not None:
            return managed.append_event
        task = managed.task
        if task is None or task.done():
            raise SessionRequestError("session is read-only because it has no live runner")
        try:
            await asyncio.wait_for(managed.appender_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError as exc:
            raise SessionRequestError("session is not ready for input") from exc
        if managed.append_event is None:
            raise SessionRequestError("session is read-only because it has no live runner")
        return managed.append_event

    def _resolve_message_target(
        self, managed: _ManagedSession, target: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        if target is None:
            return None, None
        if not isinstance(target, str):
            raise SessionRequestError("target must be a string")
        selector = target.strip()
        if not selector:
            raise SessionRequestError("target must not be empty")

        agents = self._session_agent_refs(managed)
        enabled_ids = tuple(agent_id for agent_id, _agent_type in agents)
        needle = selector.lower()
        id_matches = [agent_id for agent_id, _agent_type in agents if agent_id.lower() == needle]
        if id_matches:
            return selector, id_matches[0]
        type_matches = [
            agent_id
            for agent_id, agent_type in agents
            if agent_type.lower() == needle and agent_type
        ]
        if len(type_matches) == 1:
            return selector, type_matches[0]
        valid = ", ".join(enabled_ids) if enabled_ids else "(none)"
        if len(type_matches) > 1:
            raise SessionRequestError(
                f"ambiguous agent type {selector!r}; valid agent ids: {valid}"
            )
        raise SessionRequestError(f"unknown target {selector!r}; valid agent ids: {valid}")

    def _session_agent_refs(self, managed: _ManagedSession) -> Tuple[Tuple[str, str], ...]:
        settings = managed.state.settings if isinstance(managed.state.settings, dict) else {}
        agents = settings.get("agents") if isinstance(settings.get("agents"), dict) else {}
        refs = []
        for agent_id, agent in agents.items():
            agent_type = ""
            if isinstance(agent, dict):
                agent_type = str(agent.get("type") or "")
            refs.append((str(agent_id), agent_type))
        return tuple(refs)

    def _copy_state(self, state: SessionState) -> SessionState:
        return SessionState(**state.to_dict())

    def _log_lifecycle(self, message: str) -> None:
        if self._lifecycle_logger is not None:
            self._lifecycle_logger(message)


def _model_catalog_refresh_wanted(payload: Dict[str, Any]) -> bool:
    """True when a ``cached`` describe served a non-authoritative catalog
    (missing, stale, or a cached failed observation) for an enabled,
    discovery-supported backend — the signal to nudge the daemon's background
    refresher."""

    backends = payload.get("backends")
    if not isinstance(backends, dict):
        return False
    for entry in backends.values():
        if not isinstance(entry, dict):
            continue
        catalog = entry.get("model_catalog") or {}
        policy = entry.get("policy") or {}
        if not catalog.get("supported") or not policy.get("enabled"):
            continue
        if not catalog.get("authoritative"):
            return True
    return False


def _execute_transcript_unlinks(
    plans: List[Tuple[str, List[Path], List[Tuple[str, str]]]],
    apply: bool,
) -> Dict[str, Tuple[List[str], List[Tuple[str, str]], int, Optional[str]]]:
    """Unlink (or, for preview, only inspect) planned managed transcripts.

    Runs in a worker thread over a detached plan. Returns per session id:
    (removed paths, filesystem-preserved (path, reason) pairs, bytes, error).
    A missing file counts as already absent; symlinks and special files are
    preserved and reported, never followed or unlinked. For a preview, bytes
    are the size that an apply would reclaim and nothing is modified.
    """

    outcomes: Dict[str, Tuple[List[str], List[Tuple[str, str]], int, Optional[str]]] = {}
    for session_id, deletable, _preserved in plans:
        removed: List[str] = []
        preserved_fs: List[Tuple[str, str]] = []
        size = 0
        error: Optional[str] = None
        for path in deletable:
            blocker = transcript_unlink_blocker(path)
            if blocker is not None:
                preserved_fs.append((str(path), blocker))
                continue
            try:
                file_size = os.lstat(path).st_size
            except FileNotFoundError:
                continue
            except OSError as exc:
                error = str(exc)
                continue
            if not apply:
                size += file_size
                removed.append(str(path))
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                error = str(exc)
                continue
            size += file_size
            removed.append(str(path))
        outcomes[session_id] = (removed, preserved_fs, size, error)
    return outcomes


def _load_events_from_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _event_batch_from_snapshot(
    session_id: str,
    events: List[Dict[str, Any]],
    cursor: int,
    limit: Optional[int],
    tool_output: str,
    outcome_view: Dict[str, Any],
) -> EventBatch:
    end = len(events) if limit is None else min(len(events), cursor + limit)
    projected = [
        _project_event(event, event_id, tool_output)
        for event_id, event in enumerate(events[cursor:end], start=cursor)
    ]
    return EventBatch(session_id=session_id, cursor=end, events=projected, **outcome_view)


def _outcome_view(state: SessionState) -> Dict[str, Any]:
    return {
        "status": state.status,
        "terminal": state.status in TERMINAL_STATUSES,
        "error": state.error,
        "failure": copy.deepcopy(state.failure),
        "turn_outcomes": copy.deepcopy(state.turn_outcomes),
    }


def _read_full_transcript(path: Path) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return _truncate_text(text, MAX_FULL_TRANSCRIPT_BYTES)


def _render_transcript(
    session_id: str,
    events: List[Dict[str, Any]],
    tool_output: str,
) -> str:
    parts = [f"# agent-collab session {session_id}\n\n"]
    for event_id, event in enumerate(events):
        projected = _project_event(event, event_id, tool_output)
        label = str(projected.get("source", "error")).upper()
        agent_id = projected.get("agent_id")
        if agent_id and agent_id != projected.get("source"):
            label += f" ({agent_id})"
        event_type = str(projected.get("type", "status"))
        text = str(projected.get("text", ""))
        parts.append(f"## {label} `{event_type}`\n\n{text}\n\n")
    return "".join(parts)


def _project_event(event: Dict[str, Any], event_id: int, tool_output: str) -> Dict[str, Any]:
    projected = copy.deepcopy(event)
    if projected.get("source") != "tool":
        return projected
    if tool_output == "summary":
        projected["text"] = _tool_event_summary(projected, event_id)
        projected["raw"] = None
        return projected

    projected["text"] = _truncate_text(str(projected.get("text", "")), MAX_FULL_TOOL_BYTES)
    raw = projected.get("raw")
    encoded = _json_bytes(raw)
    if len(encoded) > MAX_FULL_TOOL_BYTES:
        preview = encoded[:MAX_FULL_TOOL_BYTES].decode("utf-8", errors="replace")
        omitted = len(encoded) - MAX_FULL_TOOL_BYTES
        projected["raw"] = {
            "truncated": True,
            "preview": preview,
            "omitted_bytes": omitted,
            "message": f"+{omitted} bytes truncated, see transcript file",
        }
    return projected


def _tool_event_summary(event: Dict[str, Any], event_id: int) -> str:
    name, args = _tool_identity(event.get("raw"))
    text = str(event.get("text", ""))
    if not name:
        first = text.strip().split(None, 1)
        name = first[0] if first else str(event.get("type", "tool"))
        if args is None and len(first) > 1:
            args = first[1]
    digest = ""
    if args not in (None, "", {}, []):
        digest = " " + compact_json(args, limit=120)
    result_size = max(len(text.encode("utf-8")), len(_json_bytes(event.get("raw"))))
    return f"[event {event_id}] {name}{digest} — result {result_size} bytes"


def _tool_identity(value: Any) -> Tuple[str, Any]:
    if isinstance(value, dict):
        name = value.get("name") or value.get("tool_name")
        args = next(
            (value[key] for key in ("input", "arguments", "args", "command") if key in value),
            None,
        )
        if isinstance(name, str) and name:
            return name, args
        for key in ("item", "message", "content", "tool_call", "tool_calls"):
            if key in value:
                nested_name, nested_args = _tool_identity(value[key])
                if nested_name:
                    return nested_name, nested_args
    elif isinstance(value, list):
        for item in value:
            name, args = _tool_identity(item)
            if name:
                return name, args
    return "", None


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8", errors="replace")


def _truncate_text(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    omitted = len(encoded) - byte_limit
    preview = encoded[:byte_limit].decode("utf-8", errors="ignore")
    return f"{preview}\n+{omitted} bytes truncated, see transcript file"
