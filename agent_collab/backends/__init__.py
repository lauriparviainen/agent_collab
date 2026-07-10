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
    BackendOptionError,
    BackendCapabilities,
    BackendHealth,
    BackendUnavailable,
    OptionSpec,
)
from .common.health import HealthCache

DEFAULT_BACKEND = "cli"

_REGISTRY: Dict[Tuple[str, str], AgentBackend] = {}

# Short-TTL cache shared by describe_options (cached) and start gating (fresh).
HEALTH = HealthCache()


def register(backend: AgentBackend) -> None:
    _validate_backend_contract(backend)
    name = backend_name(backend.agent_type, backend.id)
    for agent_type, backend_id in _REGISTRY:
        if (agent_type, backend_id) != (backend.agent_type, backend.id):
            if backend_name(agent_type, backend_id) == name:
                raise ValueError(
                    f"canonical backend name {name!r} collides with "
                    f"({agent_type!r}, {backend_id!r})"
                )
    _REGISTRY[(backend.agent_type, backend.id)] = backend


def _validate_backend_contract(backend: AgentBackend) -> None:
    agent_type = getattr(backend, "agent_type", None)
    backend_id = getattr(backend, "id", None)
    if not isinstance(agent_type, str) or not agent_type:
        raise TypeError("backend.agent_type must be a non-empty string")
    if not isinstance(backend_id, str) or not backend_id:
        raise TypeError("backend.id must be a non-empty string")
    if not isinstance(getattr(backend, "capabilities", None), BackendCapabilities):
        raise TypeError(f"backend ({agent_type}, {backend_id}) must declare BackendCapabilities")
    for method in (
        "probe",
        "option_schema",
        "normalize_options",
        "settings_summary",
        "command_preview",
        "create_runner",
    ):
        if not callable(getattr(backend, method, None)):
            raise TypeError(f"backend ({agent_type}, {backend_id}) is missing required method {method}()")
    brand_color = getattr(backend, "brand_color", None)
    if not (
        isinstance(brand_color, str)
        and len(brand_color) == 7
        and brand_color.startswith("#")
        and all(char in "0123456789abcdefABCDEF" for char in brand_color[1:])
    ):
        raise TypeError(
            f"backend ({agent_type}, {backend_id}) must declare brand_color as '#RRGGBB' "
            f"(the provider brand hue, identical across the provider's backends)"
        )


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


def backend_name(agent_type: str, backend_id: str) -> str:
    """Canonical public name for one registered provider/backend pair."""

    return f"{agent_type}_{backend_id}"


def registered_backend_names() -> List[str]:
    return sorted(backend_name(agent_type, backend_id) for agent_type, backend_id in _REGISTRY)


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
    import importlib

    for name in _BUILTIN_BACKENDS:
        module = importlib.import_module(f".{name}", __name__)
        register(module.build())


_BUILTIN_BACKENDS = (
    "claude_cli",
    "claude_sdk",
    "codex_cli",
    "codex_sdk",
    "antigravity_cli",
    "antigravity_sdk",
    "xai_cli",
    "xai_sdk",
)


_register_builtins()
