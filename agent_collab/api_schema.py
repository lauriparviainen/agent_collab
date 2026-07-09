"""Single source of truth for the daemon HTTP (REST) API contract.

This module holds the shared, typed request/response DTOs that the daemon server
and the CLI client both build against, a ``ROUTES`` registry enumerating every
REST route, and the API version constants. It is intentionally dependency-light
(only :data:`agent_collab.config.DEFAULT_WORKFLOW`) so both ``server_http`` and
``client`` can import it without a cycle.

See ``doc/tasks_open/stage-5.3-daemon-api-contract.md`` (Workstream A). This
first slice adds the DTOs + a contract test; wiring ``server_http``/``client``
onto these models is the following slice.

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
API_VERSION = 1
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


# --- Response DTOs ----------------------------------------------------------


@dataclass
class HealthModel:
    """``GET /health`` — open, unauthenticated liveness probe.

    ``api_version`` is ``None`` until the refactor slice makes the server emit
    it; ``to_dict`` omits it while ``None`` so it round-trips today's wire
    exactly.
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
            max_turns=int(data.get("max_turns", 3)),
            timeout=int(data.get("timeout", 900)),
            mock=bool(data.get("mock", False)),
            dry_run=bool(data.get("dry_run", False)),
            interactive=bool(data.get("interactive", False)),
            interactive_idle_timeout=float(data.get("interactive_idle_timeout", 600.0)),
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
        return cls(sessions=[SessionStateModel.from_dict(item) for item in data.get("sessions", [])])

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
    codex_options: Dict[str, Any] = field(default_factory=dict)
    claude_options: Dict[str, Any] = field(default_factory=dict)
    antigravity_options: Dict[str, Any] = field(default_factory=dict)
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
        "codex_options",
        "claude_options",
        "antigravity_options",
        "backend",
    )
    REQUIRED_FIELDS: ClassVar[Tuple[str, ...]] = ("task", "workdir")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StartSessionRequestModel":
        backend = data.get("backend")
        if backend is not None and not isinstance(backend, str):
            raise ValueError("backend must be a string")
        return cls(
            task=_required_str(data, "task"),
            workdir=_required_str(data, "workdir"),
            workflow=str(data.get("workflow", DEFAULT_WORKFLOW)),
            max_turns=int(data.get("max_turns", 3)),
            timeout=int(data.get("timeout", 900)),
            mock=bool(data.get("mock", False)),
            dry_run=bool(data.get("dry_run", False)),
            interactive=bool(data.get("interactive", False)),
            interactive_idle_timeout=float(data.get("interactive_idle_timeout", 600.0)),
            codex_options=_optional_object(data, "codex_options"),
            claude_options=_optional_object(data, "claude_options"),
            antigravity_options=_optional_object(data, "antigravity_options"),
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
            "codex_options": self.codex_options,
            "claude_options": self.claude_options,
            "antigravity_options": self.antigravity_options,
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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptionsRequestModel":
        return cls(workdir=_required_str(data, "workdir"))

    def to_dict(self) -> Dict[str, Any]:
        return {"workdir": self.workdir}


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


# --- Route registry ---------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One REST route: its wire method/path, the client method that calls it
    (``None`` for a server route the typed client does not wrap), and the
    request/response DTOs. ``dynamic_response`` marks ``/options``, whose body is
    the runtime authority and is not statically modeled."""

    method: str
    path: str
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
    Route("GET", "/health", "health", None, HealthModel),
    Route("POST", "/options", "describe_options", OptionsRequestModel, None, dynamic_response=True),
    Route("GET", "/options", None, OptionsRequestModel, None, dynamic_response=True),
    Route("POST", "/sessions", "start_session", StartSessionRequestModel, SessionStateModel),
    Route("GET", "/sessions", "list_sessions", None, SessionListModel),
    Route("GET", "/sessions/{session_id}", "get_session", None, SessionStateModel),
    Route("GET", "/sessions/{session_id}/events", "read_events", None, EventBatchModel),
    Route("GET", "/sessions/{session_id}/events/wait", "wait_events", None, EventBatchModel),
    Route("POST", "/sessions/{session_id}/messages", "post_message", PostMessageRequestModel, EventBatchModel),
    Route("GET", "/sessions/{session_id}/transcript", "read_transcript", None, TranscriptModel),
    Route("POST", "/sessions/{session_id}/stop", "stop_session", None, SessionStateModel),
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
    "StartSessionRequestModel",
    "OptionsRequestModel",
    "PostMessageRequestModel",
    "Route",
    "ROUTES",
    "SERVER_ONLY_ROUTES",
]
