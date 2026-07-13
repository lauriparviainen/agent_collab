"""Single source of truth for the daemon HTTP (REST) API contract.

This module holds the shared, typed request/response DTOs that the daemon server
and the CLI client both build against, a ``ROUTES`` registry enumerating every
REST route, and the API version constants. It is intentionally dependency-light
(only :data:`agent_collab.config.DEFAULT_WORKFLOW`) so both ``server_http`` and
``client`` can import it without a cycle.

See ``doc/tasks_closed/stage-5.3-daemon-api-contract.md`` (Workstream A).
``./agent_collab_dev.sh build`` generates the documentation artifacts under
``doc/daemon_api_doc/`` from these DTOs and :data:`ROUTES`.

Deliberate non-coverage (documented, not accidental):

* ``/options`` responses are the **runtime authority** — configured agents,
  workflows, per-agent option schemas and backend health, all resolved per
  workdir by ``options.describe_options``. They are NOT modeled as fixed fields
  here; the response is an opaque ``dict`` passthrough (``dynamic_response`` on
  the route). Only the ``/options`` *request* (``workdir``) has a fixed shape.
* ``/mcp`` is JSON-RPC-in-HTTP, not a REST resource, and is absent from
  ``ROUTES``. Its tool inputs already have ``inputSchema`` in ``mcp_tools`` and
  its JSON-RPC error bodies keep their own shape (not :class:`ErrorModel`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from .config import DEFAULT_WORKFLOW


# --- Versioning -------------------------------------------------------------
# Explicit contract version so the client can detect a daemon mismatch cleanly
# instead of via defensive shape guesses. The refactor slice makes the server
# emit ``api_version`` in ``GET /health`` and the ``X-Agent-Collab-API`` header
# on every REST response, and has the client assert a compatible *major* on
# connect. Bump on a breaking wire change.
API_VERSION = 2
API_VERSION_HEADER = "X-Agent-Collab-API"


# --- Validation helpers (mirror the server's ad-hoc checks) -----------------
# These raise ``ValueError`` (which the server maps to 400) rather than the
# server's ``HttpError`` so this module stays free of a server import cycle.


def _required_str(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _optional_object(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    # Matches ``server_http._optional_payload`` / ``mcp_tools._optional_payload``:
    # an absent or null option block normalizes to an empty object.
    value = data.get(key)
    return {} if value is None else value


def _integer(data: Dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _number(data: Dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc


def _tool_output(data: Dict[str, Any]) -> str:
    value = data.get("tool_output", "summary")
    if not isinstance(value, str) or value not in {"summary", "full"}:
        raise ValueError("tool_output must be 'summary' or 'full'")
    return value


# --- Response DTOs ----------------------------------------------------------


@dataclass
class HealthModel:
    """``GET /health`` — open, unauthenticated liveness probe.

    ``api_version`` is optional only so the typed client can still parse a
    pre-versioning daemon response; current servers always emit it.
    """

    status: str
    sessions: int
    api_version: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthModel":
        version = data.get("api_version")
        return cls(
            status=str(data["status"]),
            sessions=int(data["sessions"]),
            api_version=int(version) if version is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "sessions": self.sessions}
        if self.api_version is not None:
            out["api_version"] = self.api_version
        return out


@dataclass
class SessionStateModel:
    """A daemon session's state; mirrors ``daemon.SessionState.to_dict()``.

    ``settings`` and ``capabilities`` stay opaque dicts on purpose — ``settings``
    is built dynamically per workflow/backend and its inner shape is the
    ``/options`` runtime authority's concern, not a static field list.
    """

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
    settings: Optional[Dict[str, Any]] = None
    capabilities: Optional[Dict[str, bool]] = None
    # Per-agent provider session identity (backend + provider_session_id +
    # provider_session_kind), keyed by agent id. Opaque dict like ``settings``.
    agent_sessions: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionStateModel":
        return cls(
            session_id=str(data["session_id"]),
            status=str(data["status"]),
            task=str(data.get("task", "")),
            workflow=str(data.get("workflow", "")),
            workdir=str(data.get("workdir", "")),
            jsonl_path=str(data.get("jsonl_path", "")),
            markdown_path=str(data.get("markdown_path", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            max_turns=_integer(data, "max_turns", 3),
            timeout=_integer(data, "timeout", 900),
            mock=bool(data.get("mock", False)),
            dry_run=bool(data.get("dry_run", False)),
            interactive=bool(data.get("interactive", False)),
            interactive_idle_timeout=_number(data, "interactive_idle_timeout", 600.0),
            ended_at=data.get("ended_at"),
            error=data.get("error"),
            settings=data.get("settings"),
            capabilities=data.get("capabilities"),
            agent_sessions=data.get("agent_sessions"),
        )

    def to_dict(self) -> Dict[str, Any]:
        # Emit every field (including ``None`` ones) to match ``asdict`` on the
        # wire today.
        return {
            "session_id": self.session_id,
            "status": self.status,
            "task": self.task,
            "workflow": self.workflow,
            "workdir": self.workdir,
            "jsonl_path": self.jsonl_path,
            "markdown_path": self.markdown_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "max_turns": self.max_turns,
            "timeout": self.timeout,
            "mock": self.mock,
            "dry_run": self.dry_run,
            "interactive": self.interactive,
            "interactive_idle_timeout": self.interactive_idle_timeout,
            "ended_at": self.ended_at,
            "error": self.error,
            "settings": self.settings,
            "capabilities": self.capabilities,
            "agent_sessions": self.agent_sessions,
        }


@dataclass
class SessionListModel:
    """``GET /sessions`` — ``{"sessions": [SessionStateModel, ...]}``."""

    sessions: List[SessionStateModel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionListModel":
        return cls(
            sessions=[SessionStateModel.from_dict(item) for item in data.get("sessions", [])]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"sessions": [item.to_dict() for item in self.sessions]}


@dataclass
class EventModel:
    """One event; mirrors ``events.Event.to_dict()``. ``raw`` is opaque."""

    timestamp: str
    source: str
    type: str
    text: str
    raw: Any = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventModel":
        return cls(
            timestamp=str(data["timestamp"]),
            source=str(data["source"]),
            type=str(data["type"]),
            text=str(data["text"]),
            raw=data.get("raw"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "type": self.type,
            "text": self.text,
            "raw": self.raw,
        }


@dataclass
class EventBatchModel:
    """``.../events`` and ``.../events/wait`` (and the ``.../messages`` reply)."""

    session_id: str
    cursor: int
    events: List[EventModel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventBatchModel":
        return cls(
            session_id=str(data["session_id"]),
            cursor=int(data["cursor"]),
            events=[EventModel.from_dict(item) for item in data.get("events", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cursor": self.cursor,
            "events": [item.to_dict() for item in self.events],
        }


@dataclass
class TranscriptModel:
    """``GET /sessions/{id}/transcript`` — ``{"transcript": "..."}``."""

    transcript: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TranscriptModel":
        return cls(transcript=str(data.get("transcript", "")))

    def to_dict(self) -> Dict[str, Any]:
        return {"transcript": self.transcript}


@dataclass
class ErrorModel:
    """The single REST error envelope: ``{"error": ..., "details"?: [...]}``.

    Covers every non-2xx REST response and the transport-level ``/mcp`` errors
    (bad Origin/method/protocol), which render through the same ``{"error": ...}``
    envelope (no ``details``). ``details`` is therefore optional. It does NOT
    cover JSON-RPC error objects inside a 200/202 ``/mcp`` body — those keep
    their JSON-RPC shape.
    """

    error: Any
    details: Optional[List[Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ErrorModel":
        details = data.get("details")
        return cls(
            error=data.get("error", data),
            details=list(details) if isinstance(details, list) else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.error}
        if self.details is not None:
            out["details"] = self.details
        return out


@dataclass
class PruneSessionDetailModel:
    """One session's outcome in a prune run; mirrors ``daemon.PruneSessionDetail``.

    ``disposition`` values are documented on the daemon dataclass (``pruned``,
    ``preview``, ``kept``, ``skipped_no_timestamp``, ``skipped_live``,
    ``failed``); like ``SessionStateModel.status`` the DTO does not reject
    unknown future values, the generated schema documents the enum.
    """

    session_id: str
    status: str
    disposition: str
    effective_at: Optional[str] = None
    removed_files: List[str] = field(default_factory=list)
    preserved_files: List[Dict[str, str]] = field(default_factory=list)
    bytes_reclaimed: int = 0
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PruneSessionDetailModel":
        return cls(
            session_id=str(data["session_id"]),
            status=str(data.get("status", "")),
            disposition=str(data["disposition"]),
            effective_at=data.get("effective_at"),
            removed_files=[str(item) for item in data.get("removed_files", [])],
            preserved_files=[dict(item) for item in data.get("preserved_files", [])],
            bytes_reclaimed=_integer(data, "bytes_reclaimed", 0),
            error=data.get("error"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "disposition": self.disposition,
            "effective_at": self.effective_at,
            "removed_files": self.removed_files,
            "preserved_files": self.preserved_files,
            "bytes_reclaimed": self.bytes_reclaimed,
            "error": self.error,
        }


@dataclass
class PruneResultModel:
    """``POST /sessions/prune`` response; mirrors ``daemon.PruneResult``."""

    apply: bool
    cutoff: str
    keep: int
    candidates: int
    pruned: int
    failed: int
    bytes_reclaimed: int
    unparseable_records: int
    sessions: List[PruneSessionDetailModel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PruneResultModel":
        return cls(
            apply=bool(data["apply"]),
            cutoff=str(data["cutoff"]),
            keep=_integer(data, "keep", 0),
            candidates=_integer(data, "candidates", 0),
            pruned=_integer(data, "pruned", 0),
            failed=_integer(data, "failed", 0),
            bytes_reclaimed=_integer(data, "bytes_reclaimed", 0),
            unparseable_records=_integer(data, "unparseable_records", 0),
            sessions=[PruneSessionDetailModel.from_dict(item) for item in data.get("sessions", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "apply": self.apply,
            "cutoff": self.cutoff,
            "keep": self.keep,
            "candidates": self.candidates,
            "pruned": self.pruned,
            "failed": self.failed,
            "bytes_reclaimed": self.bytes_reclaimed,
            "unparseable_records": self.unparseable_records,
            "sessions": [item.to_dict() for item in self.sessions],
        }


# --- Request DTOs -----------------------------------------------------------

# Fields carried on ``daemon.StartSessionRequest`` that are NOT wire inputs:
# resolved/derived state or process-local flags. The API request DTO must never
# expose these. The contract test asserts this set is exactly the daemon
# dataclass minus the wire fields, so a new field forces a wire/non-user
# decision.
NON_USER_START_FIELDS: Tuple[str, ...] = (
    "verbose",
    "color",
    "log_dir",
    "session_id",
    "resolved_backends",
    "agent_options",
    "collab_config",
)


@dataclass
class StartSessionRequestModel:
    """``POST /sessions`` request. The single definition of the start wire shape.

    ``WIRE_FIELDS`` / ``REQUIRED_FIELDS`` mirror the ``agent_collab_start`` MCP
    ``inputSchema`` and ``mcp_tools._start_payload``; the contract test keeps all
    three in lockstep (the "quadruplication" guard).
    """

    task: str
    workdir: str
    workflow: str = DEFAULT_WORKFLOW
    max_turns: int = 3
    timeout: int = 900
    mock: bool = False
    dry_run: bool = False
    interactive: bool = False
    interactive_idle_timeout: float = 600.0
    backend_options: Dict[str, Any] = field(default_factory=dict)
    backend: Optional[str] = None

    WIRE_FIELDS: ClassVar[Tuple[str, ...]] = (
        "task",
        "workflow",
        "workdir",
        "max_turns",
        "timeout",
        "mock",
        "dry_run",
        "interactive",
        "interactive_idle_timeout",
        "backend_options",
        "backend",
    )
    REQUIRED_FIELDS: ClassVar[Tuple[str, ...]] = ("task", "workdir")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StartSessionRequestModel":
        unknown = sorted(set(data) - set(cls.WIRE_FIELDS))
        if unknown:
            raise ValueError(f"unknown start field {unknown[0]!r}")
        backend = data.get("backend")
        if backend is not None and not isinstance(backend, str):
            raise ValueError("backend must be a string")
        return cls(
            task=_required_str(data, "task"),
            workdir=_required_str(data, "workdir"),
            workflow=str(data.get("workflow", DEFAULT_WORKFLOW)),
            max_turns=_integer(data, "max_turns", 3),
            timeout=_integer(data, "timeout", 900),
            mock=bool(data.get("mock", False)),
            dry_run=bool(data.get("dry_run", False)),
            interactive=bool(data.get("interactive", False)),
            interactive_idle_timeout=_number(data, "interactive_idle_timeout", 600.0),
            backend_options=_optional_object(data, "backend_options"),
            backend=str(backend) if backend is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "task": self.task,
            "workflow": self.workflow,
            "workdir": self.workdir,
            "max_turns": self.max_turns,
            "timeout": self.timeout,
            "mock": self.mock,
            "dry_run": self.dry_run,
            "interactive": self.interactive,
            "interactive_idle_timeout": self.interactive_idle_timeout,
            "backend_options": self.backend_options,
        }
        if self.backend is not None:
            out["backend"] = self.backend
        return out


@dataclass
class OptionsRequestModel:
    """``POST /options`` request. ``workdir`` is required and non-blank.

    Requiring it here fixes ``client.describe_options()``'s current no-payload
    path (it sends ``{}`` and the server 400s).
    """

    workdir: str
    health_refresh: str = "cached"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptionsRequestModel":
        health_refresh = data.get("health_refresh", "cached")
        if not isinstance(health_refresh, str) or health_refresh not in {"cached", "fresh"}:
            raise ValueError("health_refresh must be 'cached' or 'fresh'")
        return cls(workdir=_required_str(data, "workdir"), health_refresh=health_refresh)

    def to_dict(self) -> Dict[str, Any]:
        return {"workdir": self.workdir, "health_refresh": self.health_refresh}


@dataclass
class PostMessageRequestModel:
    """``POST /sessions/{id}/messages`` request."""

    text: str
    source: str = "referee"
    target: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PostMessageRequestModel":
        text = _required_str(data, "text")
        raw_source = data.get("source")
        source = "referee" if raw_source is None else raw_source
        if not isinstance(source, str) or source not in {"human", "referee"}:
            raise ValueError("source must be 'human' or 'referee'")
        target = data.get("target")
        if target is not None and not isinstance(target, str):
            raise ValueError("target must be a string")
        return cls(text=text, source=source, target=target)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"source": self.source, "text": self.text}
        if self.target is not None:
            out["target"] = self.target
        return out


@dataclass
class PruneSessionsRequestModel:
    """``POST /sessions/prune`` request.

    ``older_than`` overrides the configured retention for one invocation; the
    duration grammar is validated here (via ``retention.parse_duration``) so a
    bad value 400s before reaching the manager. Unknown fields are rejected —
    this endpoint deletes data, so a mistyped field must never be ignored.
    """

    apply: bool = False
    older_than: Optional[str] = None
    keep: int = 0

    WIRE_FIELDS: ClassVar[Tuple[str, ...]] = ("apply", "older_than", "keep")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PruneSessionsRequestModel":
        from .retention import parse_duration

        unknown = sorted(set(data) - set(cls.WIRE_FIELDS))
        if unknown:
            raise ValueError(f"unknown prune field {unknown[0]!r}")
        older_than = data.get("older_than")
        if older_than is not None:
            parse_duration(older_than)
            older_than = str(older_than).strip()
        keep = _integer(data, "keep", 0)
        if keep < 0:
            raise ValueError("keep must be >= 0")
        return cls(apply=bool(data.get("apply", False)), older_than=older_than, keep=keep)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"apply": self.apply, "keep": self.keep}
        if self.older_than is not None:
            out["older_than"] = self.older_than
        return out


@dataclass
class ReadEventsRequestModel:
    """Query for ``GET .../events``.

    ``limit`` makes a one-event full-fidelity re-fetch possible after a summary
    reports an absolute event id.
    """

    cursor: int = 0
    limit: Optional[int] = None
    tool_output: str = "summary"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReadEventsRequestModel":
        cursor = _integer(data, "cursor", 0)
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        limit = None
        if data.get("limit") is not None:
            limit = _integer(data, "limit", 0)
            if limit < 1:
                raise ValueError("limit must be >= 1")
        return cls(cursor=cursor, limit=limit, tool_output=_tool_output(data))

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"cursor": self.cursor, "tool_output": self.tool_output}
        if self.limit is not None:
            out["limit"] = self.limit
        return out


@dataclass
class WaitEventsRequestModel:
    """Query for ``GET .../events/wait``."""

    cursor: int = 0
    timeout_ms: int = 30000
    tool_output: str = "summary"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WaitEventsRequestModel":
        cursor = _integer(data, "cursor", 0)
        timeout_ms = _integer(data, "timeout_ms", 30000)
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be >= 0")
        return cls(cursor=cursor, timeout_ms=timeout_ms, tool_output=_tool_output(data))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cursor": self.cursor,
            "timeout_ms": self.timeout_ms,
            "tool_output": self.tool_output,
        }


@dataclass
class TranscriptRequestModel:
    """Query for ``GET .../transcript``."""

    tool_output: str = "summary"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TranscriptRequestModel":
        return cls(tool_output=_tool_output(data))

    def to_dict(self) -> Dict[str, Any]:
        return {"tool_output": self.tool_output}


# --- Route registry ---------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One REST route: its wire method/path, the client method that calls it
    (``None`` for a server route the typed client does not wrap), and the
    request/response DTOs. ``dynamic_response`` marks ``/options``, whose body is
    the runtime authority and is not statically modeled."""

    method: str
    path: str
    handler: str
    client_method: Optional[str]
    request_model: Optional[type] = None
    response_model: Optional[type] = None
    dynamic_response: bool = False


# The complete REST surface of the daemon. Every server route lives here so the
# registry is a true single source of truth, not just the client-wrapped subset.
# ``GET /options`` carries ``workdir`` as a query param and is not wrapped by the
# client (which uses ``POST /options`` -> ``describe_options``), so its
# ``client_method`` is ``None``. ``/mcp`` is intentionally excluded (JSON-RPC,
# not REST). ``SERVER_ONLY_ROUTES`` below pins the set that legitimately has no
# client method, so a route silently losing its client method is caught.
ROUTES: Tuple[Route, ...] = (
    Route("GET", "/health", "health", "health", None, HealthModel),
    Route(
        "POST",
        "/options",
        "options",
        "describe_options",
        OptionsRequestModel,
        None,
        dynamic_response=True,
    ),
    Route("GET", "/options", "options", None, OptionsRequestModel, None, dynamic_response=True),
    Route(
        "POST",
        "/sessions",
        "start_session",
        "start_session",
        StartSessionRequestModel,
        SessionStateModel,
    ),
    Route("GET", "/sessions", "list_sessions", "list_sessions", None, SessionListModel),
    Route("GET", "/sessions/{session_id}", "get_session", "get_session", None, SessionStateModel),
    Route(
        "GET",
        "/sessions/{session_id}/events",
        "read_events",
        "read_events",
        ReadEventsRequestModel,
        EventBatchModel,
    ),
    Route(
        "GET",
        "/sessions/{session_id}/events/wait",
        "wait_events",
        "wait_events",
        WaitEventsRequestModel,
        EventBatchModel,
    ),
    Route(
        "POST",
        "/sessions/{session_id}/messages",
        "post_message",
        "post_message",
        PostMessageRequestModel,
        EventBatchModel,
    ),
    Route(
        "GET",
        "/sessions/{session_id}/transcript",
        "read_transcript",
        "read_transcript",
        TranscriptRequestModel,
        TranscriptModel,
    ),
    Route(
        "POST",
        "/sessions/{session_id}/stop",
        "stop_session",
        "stop_session",
        None,
        SessionStateModel,
    ),
    Route(
        "POST",
        "/sessions/prune",
        "prune_sessions",
        "prune_sessions",
        PruneSessionsRequestModel,
        PruneResultModel,
    ),
)

# REST routes that legitimately have no typed-client method (server/manual-curl
# only). The contract test pins this exactly, so any *other* route missing its
# client method fails.
SERVER_ONLY_ROUTES: Tuple[Tuple[str, str], ...] = (("GET", "/options"),)


__all__ = [
    "API_VERSION",
    "API_VERSION_HEADER",
    "NON_USER_START_FIELDS",
    "HealthModel",
    "SessionStateModel",
    "SessionListModel",
    "EventModel",
    "EventBatchModel",
    "TranscriptModel",
    "ErrorModel",
    "PruneResultModel",
    "PruneSessionDetailModel",
    "StartSessionRequestModel",
    "OptionsRequestModel",
    "PostMessageRequestModel",
    "PruneSessionsRequestModel",
    "ReadEventsRequestModel",
    "WaitEventsRequestModel",
    "TranscriptRequestModel",
    "Route",
    "ROUTES",
    "SERVER_ONLY_ROUTES",
]
