"""Backend registry + resolution.

A *backend* is the mechanism that executes an agent turn (``cli`` subprocess
today, ``sdk`` next). The registry is keyed by ``(agent_type, backend_id)``;
resolution picks the effective backend id with *most specific wins*:

    start-request backend > ``agents.<id>.backend`` > built-in default ``cli``.

Registration is a module-level dict populated by the built-in backends at
import time — adding a backend is one new module plus one factory here, with no
entry-point/plugin machinery. Nothing else in the package imports this module at
import time (callers use function-level imports), so the base install and the
default ``cli`` backend stay standard-library only.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from .base import (
    CREDENTIALS_MISSING,
    CREDENTIALS_OK,
    CREDENTIALS_UNKNOWN,
    HEALTH_OK,
    HEALTH_UNAVAILABLE,
    HEALTH_UNKNOWN,
    AgentBackend,
    BackendCapabilities,
    BackendHealth,
    BackendUnavailable,
)
from .health import HealthCache

DEFAULT_BACKEND = "cli"

_REGISTRY: Dict[Tuple[str, str], AgentBackend] = {}

# Short-TTL cache shared by describe_options (cached) and start gating (fresh).
HEALTH = HealthCache()


def register(backend: AgentBackend) -> None:
    _REGISTRY[(backend.agent_type, backend.id)] = backend


def unregister(agent_type: str, backend_id: str) -> None:
    _REGISTRY.pop((agent_type, backend_id), None)


def get_backend(agent_type: str, backend_id: str) -> AgentBackend:
    try:
        return _REGISTRY[(agent_type, backend_id)]
    except KeyError as exc:
        registered = registered_backends(agent_type)
        raise KeyError(
            f"no backend {backend_id!r} registered for agent type {agent_type!r}; "
            f"registered: {registered}"
        ) from exc


def is_registered(agent_type: str, backend_id: str) -> bool:
    return (agent_type, backend_id) in _REGISTRY


def registered_backends(agent_type: str) -> List[str]:
    return sorted(bid for (atype, bid) in _REGISTRY if atype == agent_type)


def registered_agent_types() -> List[str]:
    return sorted({atype for (atype, _bid) in _REGISTRY})


def resolve_backend_id(agent: "object", request_backend: Optional[str] = None) -> str:
    """Most-specific-wins resolution; does not validate registration."""

    agent_backend = getattr(agent, "backend", None)
    return request_backend or agent_backend or DEFAULT_BACKEND


def capabilities_for(agent_type: str, backend_id: str) -> BackendCapabilities:
    backend = _REGISTRY.get((agent_type, backend_id))
    return backend.capabilities if backend is not None else BackendCapabilities()


def summarize_session_capabilities(
    per_agent: Mapping[str, BackendCapabilities],
    captured_session_ids: FrozenSet[str] = frozenset(),
) -> Dict[str, bool]:
    """AND per-agent runtime facts into a session-level summary.

    ``resumable`` is true only if *every* agent's backend has ``resume`` **and**
    that agent actually captured a provider session id; ``interruptible`` only if
    every agent's backend has ``interrupt``. Built and tested against the
    all-``false`` reality this stage; a later stage flips inputs without touching
    this reducer. An empty agent set is not resumable/interruptible.
    """

    agents = list(per_agent.items())
    resumable = bool(agents) and all(
        cap.resume and agent_id in captured_session_ids for agent_id, cap in agents
    )
    interruptible = bool(agents) and all(cap.interrupt for _agent_id, cap in agents)
    return {"resumable": resumable, "interruptible": interruptible}


def health(backend: AgentBackend, *, fresh: bool = False) -> BackendHealth:
    return HEALTH.health(backend, fresh=fresh)


def _register_builtins() -> None:
    from .cli import build_cli_backends

    for backend in build_cli_backends():
        register(backend)


_register_builtins()
