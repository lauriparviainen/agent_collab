from __future__ import annotations

import asyncio
import contextlib
import copy
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union
import uuid

from .config import DEFAULT_WORKFLOW, load_config
from .events import Event, utc_timestamp
from .options import build_session_settings, describe_options, validate_start_options
from .paths import GlobalDataPaths
from .referee import Referee, RefereeConfig
from .session_index import SessionIndex


RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
INTERRUPTED = "interrupted"
TERMINAL_STATUSES = {DONE, FAILED, STOPPED, INTERRUPTED}


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
    codex_options: Optional[Dict[str, Any]] = None
    claude_options: Optional[Dict[str, Any]] = None


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
    ended_at: Optional[str] = None
    error: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EventBatch:
    session_id: str
    cursor: int
    events: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _ManagedSession:
    # request is None for sessions restored from the on-disk index.
    request: Optional[StartSessionRequest]
    state: SessionState
    events: List[Dict[str, Any]]
    condition: asyncio.Condition
    task: Optional[asyncio.Task] = None


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
        self._lifecycle_logger = lifecycle_logger
        self.default_workdir = Path(default_workdir).expanduser().resolve()
        self.default_log_dir = Path(default_log_dir).expanduser().resolve() if default_log_dir else None
        self._index = SessionIndex(Path(index_path)) if index_path else None
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
                state.status = INTERRUPTED
                state.error = "daemon restarted while session was running"
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
        workdir = Path(request.workdir).expanduser().resolve()
        if str(request.workdir) == ".":
            workdir = self.default_workdir
        log_dir = (
            Path(request.log_dir).expanduser().resolve()
            if request.log_dir
            else self.default_log_dir or GlobalDataPaths.resolve().session_dir
        )
        collab_config = load_config(workdir)
        normalized_options = validate_start_options(
            collab_config,
            request.workflow,
            request.codex_options,
            request.claude_options,
        )
        request.codex_options = normalized_options["codex_options"]
        request.claude_options = normalized_options["claude_options"]
        settings = build_session_settings(collab_config, request.workflow, normalized_options)
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
            settings=settings,
        )
        managed = _ManagedSession(
            request=request,
            state=state,
            events=[],
            condition=asyncio.Condition(),
        )
        self._sessions[session_id] = managed
        self._persist(state)
        managed.task = asyncio.create_task(self._run_session(managed), name=f"agent-collab-session-{session_id}")
        self._log_lifecycle(
            f"session {session_id} started workflow={state.workflow} max_turns={state.max_turns} "
            f"timeout={state.timeout}s mock={state.mock} dry_run={state.dry_run} workdir={state.workdir}"
        )
        return self._copy_state(state)

    def describe_options(self, workdir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        root = Path(workdir).expanduser().resolve() if workdir else self.default_workdir
        return describe_options(load_config(root), root)

    async def stop_session(self, session_id: str) -> SessionState:
        managed = self._get_managed(session_id)
        task = managed.task
        if managed.state.status in TERMINAL_STATUSES:
            return self._copy_state(managed.state)

        await self._set_status(managed, STOPPED)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self._copy_state(managed.state)

    def list_sessions(self) -> List[SessionState]:
        return [self._copy_state(managed.state) for managed in self._sessions.values()]

    def get_session(self, session_id: str) -> SessionState:
        return self._copy_state(self._get_managed(session_id).state)

    def read_events(self, session_id: str, cursor: int = 0) -> EventBatch:
        managed = self._get_managed(session_id)
        cursor = self._normalize_cursor(cursor)
        if managed.request is None and not managed.events:
            managed.events = _load_events_from_jsonl(Path(managed.state.jsonl_path))
        events = [copy.deepcopy(event) for event in managed.events[cursor:]]
        return EventBatch(session_id=session_id, cursor=len(managed.events), events=events)

    async def wait_events(self, session_id: str, cursor: int = 0, timeout_ms: int = 30000) -> EventBatch:
        managed = self._get_managed(session_id)
        cursor = min(self._normalize_cursor(cursor), len(managed.events))
        timeout = max(0, int(timeout_ms)) / 1000.0

        if len(managed.events) <= cursor and managed.state.status == RUNNING and timeout > 0:
            async with managed.condition:
                if len(managed.events) <= cursor and managed.state.status == RUNNING:
                    try:
                        await asyncio.wait_for(
                            managed.condition.wait_for(
                                lambda: len(managed.events) > cursor or managed.state.status != RUNNING
                            ),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        pass

        return self.read_events(session_id, cursor)

    async def _run_session(self, managed: _ManagedSession) -> None:
        request = managed.request
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
            collab_config=load_config(workdir),
            codex_options=dict(request.codex_options or {}),
            claude_options=dict(request.claude_options or {}),
        )

        try:
            result = await Referee(config, printer=lambda event: self._record_event(managed, event)).run(request.task)
            state.jsonl_path = result.get("jsonl_path", state.jsonl_path)
            state.markdown_path = result.get("markdown_path", state.markdown_path)
            self._persist(state)
            if state.status == RUNNING:
                await self._set_status(managed, DONE)
        except asyncio.CancelledError:
            await self._set_status(managed, STOPPED)
        except Exception as exc:
            self._record_event(
                managed,
                Event.create(
                    "error",
                    "error",
                    str(exc),
                    {"error": str(exc), "exception": exc.__class__.__name__},
                ),
            )
            if state.status != STOPPED:
                await self._set_status(managed, FAILED, error=str(exc))

    def _record_event(self, managed: _ManagedSession, event: Event) -> None:
        managed.events.append(event.to_dict())
        self._schedule_notify(managed)

    async def _set_status(self, managed: _ManagedSession, status: str, error: Optional[str] = None) -> None:
        now = utc_timestamp()
        managed.state.status = status
        managed.state.updated_at = now
        if status in TERMINAL_STATUSES:
            managed.state.ended_at = now
        if error is not None:
            managed.state.error = error
        self._persist(managed.state)
        async with managed.condition:
            managed.condition.notify_all()
        if status in TERMINAL_STATUSES:
            suffix = f" error={error}" if error else ""
            self._log_lifecycle(
                f"session {managed.state.session_id} {status} events={len(managed.events)} "
                f"logs={managed.state.jsonl_path}{suffix}"
            )

    def _schedule_notify(self, managed: _ManagedSession) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._notify(managed))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _notify(self, managed: _ManagedSession) -> None:
        async with managed.condition:
            managed.condition.notify_all()

    def _get_managed(self, session_id: str) -> _ManagedSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError(f"unknown session_id {session_id}") from exc

    def _validate_new_session_id(self, session_id: str) -> None:
        if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
            raise ValueError(f"invalid session_id {session_id!r}")
        if session_id in self._sessions:
            raise ValueError(f"session_id already exists: {session_id}")

    def _new_session_id(self) -> str:
        return f"daemon-{uuid.uuid4().hex[:16]}"

    def _normalize_cursor(self, cursor: int) -> int:
        cursor = int(cursor)
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        return cursor

    def _copy_state(self, state: SessionState) -> SessionState:
        return SessionState(**state.to_dict())

    def _log_lifecycle(self, message: str) -> None:
        if self._lifecycle_logger is not None:
            self._lifecycle_logger(message)


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
