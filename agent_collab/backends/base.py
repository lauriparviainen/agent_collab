"""Backend abstraction shared by every execution mechanism.

A *provider* is an agent ``type`` (``claude``, ``codex``, ``antigravity``);
a *backend* is *how* a turn is executed (``cli`` subprocess today, ``sdk``
next). The registry in :mod:`agent_collab.backends` is keyed by
``(agent_type, backend_id)``; each entry is an :class:`AgentBackend` that knows
how to build an :class:`~agent_collab.runners.AgentRunner` for that pair, report
its :class:`BackendCapabilities`, and :meth:`AgentBackend.probe` its live
health.

Nothing in this module imports the runtime runner/config modules at import
time; annotations are stringized via ``from __future__ import annotations`` so
the base install and default ``cli`` backend stay standard-library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import AgentConfig
    from ..runners import AgentRunner


# Health status values (a probed (agent_type, backend_id) pair).
HEALTH_OK = "ok"
HEALTH_UNAVAILABLE = "unavailable"
HEALTH_UNKNOWN = "unknown"

# Credential status values (best-effort, side-effect free).
CREDENTIALS_OK = "ok"
CREDENTIALS_MISSING = "missing"
CREDENTIALS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend can actually do for a session.

    Every capability defaults to ``False`` and stays ``False`` for every backend
    in this stage: the flags exist so later stages can turn a running code path
    ``True`` without a schema change, never so a provider brand can imply one.
    """

    resume: bool = False  # provider-side session/thread continuation
    interrupt: bool = False  # mid-turn stop
    tool_gate: bool = False  # programmatic tool approve/deny

    def to_dict(self) -> Dict[str, bool]:
        return {
            "resume": self.resume,
            "interrupt": self.interrupt,
            "tool_gate": self.tool_gate,
        }


@dataclass(frozen=True)
class BackendHealth:
    """A probed status for one ``(agent_type, backend_id)`` pair.

    ``status`` gates a start; ``credentials`` only warns unless it is definitely
    ``missing``. ``version``/``reason``/``checked_at`` are diagnostic surface for
    ``describe_options`` and are never treated as authoritative gating detail.
    """

    status: str = HEALTH_UNKNOWN
    reason: Optional[str] = None
    credentials: str = CREDENTIALS_UNKNOWN
    version: Optional[str] = None
    checked_at: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.status == HEALTH_OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "credentials": self.credentials,
            "version": self.version,
            "checked_at": self.checked_at,
        }


class BackendUnavailable(Exception):
    """A backend cannot build a runner (missing binary, SDK extra, or creds).

    Carries machine-readable fields plus an actionable ``hint`` so callers can
    surface an install/sign-in instruction instead of a mid-session crash.
    """

    def __init__(
        self,
        agent_type: str,
        backend_id: str,
        reason: str,
        hint: Optional[str] = None,
    ) -> None:
        self.agent_type = agent_type
        self.backend_id = backend_id
        self.reason = reason
        self.hint = hint
        message = reason if not hint else f"{reason} ({hint})"
        super().__init__(message)


@runtime_checkable
class AgentBackend(Protocol):
    """Factory + health surface for one ``(agent_type, backend_id)`` pair."""

    id: str  # "cli", "sdk"
    agent_type: str  # "claude", "codex", "antigravity"
    capabilities: BackendCapabilities

    def probe(self) -> BackendHealth:
        """Return a fresh, side-effect-free health snapshot (never a model call)."""
        ...

    def create_runner(
        self,
        agent: "AgentConfig",
        verbose: bool,
        options: Dict[str, Any],
    ) -> "AgentRunner":
        """Build the runner that executes a turn for ``agent`` on this backend."""
        ...
