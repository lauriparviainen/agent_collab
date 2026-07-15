from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .config import (
    AgentConfig,
    CollaborationConfig,
    WorkflowConfig,
    load_config,
    validate_workflow,
    workflow_member_state,
    workflow_members,
)
from .backend_contract import BackendOptionError, OptionSpec


@dataclass(frozen=True)
class NormalizedStartOptions:
    """Backend-qualified buckets plus exact backend-normalized agent values."""

    backend_options: Dict[str, Dict[str, Any]]
    agent_options: Dict[str, Dict[str, Any]]


class StartOptionsError(ValueError):
    code = "invalid_start_options"

    def __init__(self, details: Sequence[Mapping[str, Any]]):
        self.details = [dict(detail) for detail in details]
        super().__init__(format_validation_error(self.details))

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "details": deepcopy(self.details)}


def format_validation_error(details: Sequence[Mapping[str, Any]]) -> str:
    lines = [StartOptionsError.code]
    for detail in details:
        path = detail.get("path", "")
        message = detail.get("message", "")
        lines.append(f"{path}: {message}" if path else message)
    return "\n".join(lines)


def resolve_workflow_members(
    config: CollaborationConfig,
    workflow_id: str,
    members: Any,
) -> Optional[WorkflowConfig]:
    """Validate a start-time member selection and return the effective workflow.

    ``members`` maps a workflow slot (named by the configured member id, see
    ``workflow_member_slots``) to the globally enabled agent that fills it.
    Selection is a caller-side start choice only — project config can neither
    supply nor influence it — and every substituted group keeps the rules of
    ``validate_workflow`` (members enabled, parallel groups duplicate-free with
    an unchanged width). Returns ``None`` when nothing was substituted so an
    absent or empty field stays byte-for-byte today's behavior.
    """

    from .config import workflow_member_slots

    errors: List[Dict[str, str]] = []
    selection = _expect_mapping(members, "members", errors)
    if errors:
        raise StartOptionsError(errors)
    if not selection:
        return None
    workflow = config.workflows.get(workflow_id)
    if workflow is None:
        raise StartOptionsError(
            [{"path": "workflow", "message": f"unknown workflow {workflow_id!r}"}]
        )
    slots = workflow_member_slots(workflow)
    mapping: Dict[str, str] = {}
    for key in selection:
        if not isinstance(key, str) or not key:
            errors.append({"path": "members", "message": "slot names must be non-empty strings"})
            continue
        if key not in slots:
            errors.append(
                {
                    "path": f"members.{key}",
                    "message": (
                        f"unknown slot for workflow {workflow_id!r}; "
                        "expected one of: " + ", ".join(slots)
                    ),
                }
            )
            continue
        value = selection[key]
        if not isinstance(value, str) or not value:
            errors.append({"path": f"members.{key}", "message": "must be an agent id string"})
            continue
        state = workflow_member_state(config, value)
        if state == "unknown":
            errors.append(
                {"path": f"members.{key}", "message": f"references unknown agent {value!r}"}
            )
            continue
        if state == "disabled":
            errors.append(
                {
                    "path": f"members.{key}",
                    "message": (
                        f"references agent {value!r} of a disabled backend; enable it in "
                        "the user config or select an enabled agent"
                    ),
                }
            )
            continue
        mapping[key] = value
    if errors:
        raise StartOptionsError(_dedupe_details(errors))
    if all(mapping[slot] == slot for slot in mapping):
        return None
    if workflow.parallel is not None:
        substituted = [mapping.get(member, member) for member in workflow.parallel]
        seen: Dict[str, str] = {}
        for slot, agent_id in zip(workflow.parallel, substituted):
            if agent_id in seen:
                errors.append(
                    {
                        "path": f"members.{slot}",
                        "message": (
                            f"parallel workflow {workflow_id!r} members must be distinct; "
                            f"{agent_id!r} already fills slot {seen[agent_id]!r}"
                        ),
                    }
                )
                continue
            seen[agent_id] = slot
        if errors:
            raise StartOptionsError(errors)
        return WorkflowConfig(id=workflow.id, sequence=[], parallel=substituted)
    return WorkflowConfig(
        id=workflow.id,
        sequence=[mapping.get(member, member) for member in workflow.sequence],
        parallel=None,
    )


def validate_start_options(
    config: CollaborationConfig,
    workflow_id: str,
    backend_options: Any = None,
    *,
    agent_backends: Optional[Mapping[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Validate and return normalized backend-qualified option buckets."""

    return normalize_start_options(
        config,
        workflow_id,
        backend_options,
        agent_backends=agent_backends,
    ).backend_options


def normalize_start_options(
    config: CollaborationConfig,
    workflow_id: str,
    backend_options: Any = None,
    *,
    agent_backends: Optional[Mapping[str, str]] = None,
) -> NormalizedStartOptions:
    """Validate backend-qualified buckets through backend-owned contracts."""

    from . import backends as backend_registry

    errors: List[Dict[str, str]] = []
    raw_payloads = _expect_mapping(backend_options, "backend_options", errors)
    option_payloads: Dict[str, Mapping[str, Any]] = {}
    for name, value in raw_payloads.items():
        if not isinstance(name, str) or not name:
            errors.append(
                {"path": "backend_options", "message": "backend names must be non-empty strings"}
            )
            continue
        option_payloads[name] = _expect_mapping(value, f"backend_options.{name}", errors)
    if errors:
        raise StartOptionsError(errors)

    validate_workflow(config, workflow_id)
    workflow = config.workflows[workflow_id]
    members = workflow_members(workflow)
    resolved = dict(agent_backends or {})
    selected_names = {
        backend_registry.backend_name(
            config.agents[agent_id].type,
            resolved.get(agent_id) or backend_registry.resolve_backend_id(config.agents[agent_id]),
        )
        for agent_id in members
        if config.agents[agent_id].type != "mock"
    }
    for name, payload in option_payloads.items():
        if name not in backend_registry.registered_backend_names():
            errors.append(
                {
                    "path": f"backend_options.{name}",
                    "message": (
                        "unknown backend; expected one of: "
                        + ", ".join(backend_registry.registered_backend_names())
                    ),
                }
            )
        elif name not in selected_names:
            errors.append(
                {
                    "path": f"backend_options.{name}",
                    "message": (
                        f"does not apply to workflow {workflow_id!r}; selected backends: "
                        + ", ".join(sorted(selected_names))
                    ),
                }
            )
    if errors:
        raise StartOptionsError(errors)

    agent_options: Dict[str, Dict[str, Any]] = {}
    seen_agents: Set[str] = set()
    for agent_id in members:
        if agent_id in seen_agents:
            continue
        seen_agents.add(agent_id)
        agent = config.agents[agent_id]
        if agent.type == "mock":
            continue
        backend_id = resolved.get(agent_id) or backend_registry.resolve_backend_id(agent)
        if not backend_registry.is_registered(agent.type, backend_id):
            errors.append(
                {
                    "path": "backend",
                    "message": f"backend {backend_id!r} is not registered for agent {agent_id!r}",
                }
            )
            continue
        backend = backend_registry.get_backend(agent.type, backend_id)
        name = backend_registry.backend_name(agent.type, backend_id)
        path = f"backend_options.{name}"
        schema = _effective_backend_schema(backend, agent, path, errors)
        requested = dict(option_payloads.get(name) or {})
        before = len(errors)
        _validate_backend_values(
            requested,
            schema,
            path,
            errors,
            agent_id=agent_id,
            backend_id=backend_id,
            explicit=True,
        )
        if len(errors) != before:
            continue
        try:
            normalized = dict(backend.normalize_options(agent, requested))
        except BackendOptionError as exc:
            errors.append(
                {"path": f"{path}.{exc.field}" if exc.field else path, "message": exc.message}
            )
            continue
        except Exception as exc:
            errors.append(
                {
                    "path": path,
                    "message": f"backend {backend_id!r} for agent {agent_id!r} could not normalize options: {exc}",
                }
            )
            continue
        undeclared = sorted(set(normalized) - set(schema))
        for key in undeclared:
            errors.append(
                {
                    "path": f"{path}.{key}",
                    "message": (
                        f"backend {backend_id!r} for agent {agent_id!r} returned an undeclared option"
                    ),
                }
            )
        _validate_backend_values(
            normalized,
            schema,
            path,
            errors,
            agent_id=agent_id,
            backend_id=backend_id,
            explicit=False,
        )
        agent_options[agent_id] = normalized

    if errors:
        raise StartOptionsError(_dedupe_details(errors))

    normalized_backend_options: Dict[str, Dict[str, Any]] = {}
    for name in selected_names:
        values = [
            agent_options[agent_id]
            for agent_id in seen_agents
            if agent_id in agent_options
            and backend_registry.backend_name(
                config.agents[agent_id].type,
                resolved.get(agent_id)
                or backend_registry.resolve_backend_id(config.agents[agent_id]),
            )
            == name
        ]
        normalized_backend_options[name] = _common_options(values)
    return NormalizedStartOptions(
        backend_options=normalized_backend_options, agent_options=agent_options
    )


def validate_start_backends(
    config: CollaborationConfig,
    workflow_id: str,
    request_backend: Optional[str] = None,
    backend_options: Optional[Mapping[str, Any]] = None,
    *,
    health: Any = None,
) -> "BackendSelection":
    """Resolve and validate the effective backend for each workflow agent.

    Resolution is *most specific wins* (request > agent config > default ``cli``)
    and is computed exactly once here so execution uses the same selection the
    start response advertises. A session-level ``request_backend`` applies
    uniformly to every non-``mock`` selected agent; if any such agent's type does
    not register it, the whole start is rejected before any session state exists.
    ``mock`` agents ignore backend selection and are excluded from the map.

    ``health`` (a ``callable(agent_type, backend_id) -> BackendHealth``) enables
    fresh availability gating; it is wired in a later step and defaults to off.
    """

    from . import backends as backend_registry

    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    resolved: Dict[str, str] = {}
    validate_workflow(config, workflow_id)
    workflow = config.workflows[workflow_id]
    members = workflow_members(workflow)

    for agent_id in members:
        if agent_id in resolved:
            continue
        agent = config.agents[agent_id]
        if agent.type == "mock":
            continue
        backend_id = backend_registry.resolve_backend_id(agent, request_backend)
        if not backend_registry.is_registered(agent.type, backend_id):
            available = backend_registry.registered_backends(agent.type)
            errors.append(
                {
                    "path": "backend",
                    "message": (
                        f"backend {backend_id!r} is not available for agent {agent_id!r} "
                        f"(type {agent.type!r}); available: {', '.join(available) or '(none)'}"
                    ),
                }
            )
            continue
        resolved[agent_id] = backend_id
    if errors:
        raise StartOptionsError(errors)

    from .config import backend_policy

    for agent_id, backend_id in resolved.items():
        canonical = backend_registry.backend_name(config.agents[agent_id].type, backend_id)
        policy = backend_policy(config, canonical)
        if not policy.enabled:
            errors.append(
                {
                    "path": "backend",
                    "code": "backend_disabled",
                    "agent_id": agent_id,
                    "canonical_backend": canonical,
                    "message": (
                        f"backend {canonical!r} for agent {agent_id!r} is disabled by user config; "
                        "enable it in $AGENT_COLLAB_HOME/config.toml or select an enabled configured backend"
                    ),
                    "remediation": [
                        {
                            "code": "enable_backend_in_user_config",
                            "message": f"Set [backends.{canonical}] enabled = true in the user config.",
                        }
                    ],
                }
            )
    if errors:
        raise StartOptionsError(errors)

    normalize_start_options(
        config,
        workflow_id,
        backend_options=backend_options,
        agent_backends=resolved,
    )

    if health is not None:
        _gate_backend_health(config, resolved, health, errors, warnings)
        if errors:
            raise StartOptionsError(errors)

    return BackendSelection(agent_backends=resolved, warnings=warnings)


class BackendSelection:
    """Resolved per-agent backend map plus any non-fatal start warnings."""

    def __init__(
        self, agent_backends: Dict[str, str], warnings: Optional[List[Dict[str, str]]] = None
    ):
        self.agent_backends = dict(agent_backends)
        self.warnings = list(warnings or [])


def _effective_backend_schema(
    backend: Any,
    agent: AgentConfig,
    path: str,
    errors: List[Dict[str, str]],
) -> Dict[str, OptionSpec]:
    try:
        declared = backend.option_schema(agent)
    except Exception as exc:
        errors.append({"path": path, "message": f"backend option schema failed: {exc}"})
        return {}
    if not isinstance(declared, Mapping):
        errors.append({"path": path, "message": "backend option schema must be an object"})
        return {}

    result: Dict[str, OptionSpec] = {}
    for key, spec in declared.items():
        if not isinstance(key, str) or not key or not isinstance(spec, OptionSpec):
            errors.append(
                {
                    "path": path,
                    "message": "backend option schema entries must be non-empty strings mapped to OptionSpec",
                }
            )
            continue
        result[key] = spec
    return result


def _validate_backend_values(
    values: Mapping[str, Any],
    schema: Mapping[str, OptionSpec],
    path: str,
    errors: List[Dict[str, str]],
    *,
    agent_id: str,
    backend_id: str,
    explicit: bool,
) -> None:
    for key in sorted(values):
        field_path = f"{path}.{key}"
        spec = schema.get(key)
        if spec is None:
            expected = ", ".join(sorted(schema)) or "(none)"
            if explicit:
                message = (
                    f"is not supported on backend {backend_id!r} for agent {agent_id!r}; "
                    f"expected one of: {expected}"
                )
            else:
                message = (
                    f"backend {backend_id!r} returned an undeclared option for agent {agent_id!r}"
                )
            errors.append({"path": field_path, "message": message})
            continue
        _validate_field_value(values[key], field_path, spec.to_dict(), errors)


def _common_options(values: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not values:
        return {}
    first = values[0]
    return {
        key: deepcopy(value)
        for key, value in first.items()
        if all(key in other and other[key] == value for other in values[1:])
    }


def _dedupe_details(details: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    seen: Set[Tuple[str, str]] = set()
    result: List[Dict[str, str]] = []
    for detail in details:
        item = (detail.get("path", ""), detail.get("message", ""))
        if item in seen:
            continue
        seen.add(item)
        result.append(dict(detail))
    return result


def _gate_backend_health(
    config: CollaborationConfig,
    resolved: Mapping[str, str],
    health: Any,
    errors: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
) -> None:
    from . import backends as backend_registry
    from .backends.base import (
        CREDENTIALS_MISSING,
        CREDENTIALS_UNKNOWN,
        HEALTH_UNAVAILABLE,
        HEALTH_UNKNOWN,
    )

    checked: Dict[tuple, Any] = {}
    for agent_id, backend_id in resolved.items():
        agent_type = config.agents[agent_id].type
        backend = backend_registry.get_backend(agent_type, backend_id)
        block = backend.block_on_unavailable
        checks_credentials = backend.checks_credentials
        # Default providers (claude/codex on cli) keep their legacy per-turn-error
        # contract: nothing to gate or warn about, so never probe them on start.
        if not block and not checks_credentials:
            continue
        key = (agent_type, backend_id)
        status = checked.get(key)
        if status is None:
            status = health(agent_type, backend_id)
            checked[key] = status
        if status.status == HEALTH_UNAVAILABLE:
            if block:
                errors.append(_health_reject_detail(agent_id, agent_type, backend_id, status))
            continue
        if status.status == HEALTH_UNKNOWN:
            # Block only on certainty: an indeterminate probe warns, never blocks.
            if block:
                warnings.append(
                    {
                        "path": "backend",
                        "message": (
                            f"backend {backend_id!r} for agent {agent_id!r} availability is unknown"
                            + (f": {status.reason}" if status.reason else "")
                            + "; the first turn's real error remains the authority"
                        ),
                    }
                )
            continue
        if status.credentials == CREDENTIALS_MISSING and block:
            canonical = backend_registry.backend_name(agent_type, backend_id)
            errors.append(
                {
                    "path": "backend",
                    "code": "credentials_missing",
                    "agent_id": agent_id,
                    "canonical_backend": canonical,
                    "checked_at": getattr(status, "checked_at", None),
                    "message": (
                        f"backend {backend_id!r} for agent {agent_id!r} has missing credentials"
                        + (f": {status.reason}" if status.reason else "")
                        + "; run the provider CLI and sign in"
                    ),
                    "remediation": [
                        {
                            "code": "provider_sign_in",
                            "message": "Use the provider's supported sign-in flow, then retry.",
                        }
                    ],
                }
            )
        elif status.credentials == CREDENTIALS_UNKNOWN and checks_credentials:
            warnings.append(
                {
                    "path": "backend",
                    "message": (
                        f"backend {backend_id!r} for agent {agent_id!r} could not verify credentials; "
                        "the first turn's real error remains the authority"
                    ),
                }
            )


def _health_reject_message(agent_id: str, backend_id: str, status: Any) -> str:
    detail = f": {status.reason}" if getattr(status, "reason", None) else ""
    return f"backend {backend_id!r} for agent {agent_id!r} is unavailable{detail}"


def _health_reject_detail(
    agent_id: str,
    agent_type: str,
    backend_id: str,
    status: Any,
) -> Dict[str, Any]:
    from .backends import backend_name

    reason_codes = list(getattr(status, "reason_codes", ()) or ())
    return {
        "path": "backend",
        "code": reason_codes[0] if reason_codes else "backend_unavailable",
        "agent_id": agent_id,
        "canonical_backend": backend_name(agent_type, backend_id),
        "checked_at": getattr(status, "checked_at", None),
        "message": _health_reject_message(agent_id, backend_id, status),
        "remediation": [dict(item) for item in (getattr(status, "remediation", ()) or ())],
    }


def describe_options(
    config: CollaborationConfig,
    workdir: Optional[Path] = None,
    *,
    health: Any = None,
    health_refresh: str = "cached",
) -> Dict[str, Any]:
    if health_refresh not in {"cached", "fresh"}:
        raise ValueError("health_refresh must be 'cached' or 'fresh'")
    from .events import utc_timestamp

    resolved_workdir = str(workdir.expanduser().resolve()) if workdir else "."
    backends = _describe_backends(config, health, health_refresh)
    agents = [
        _describe_agent(config, agent, backends)
        for agent in sorted(config.agents.values(), key=lambda item: item.id)
    ]
    workflows = []
    workflow_agent_types: Dict[str, List[str]] = {}
    for workflow_id, workflow in sorted(config.workflows.items()):
        types = sorted(_workflow_agent_types(config, workflow_members(workflow)))
        workflow_agent_types[workflow_id] = types
        workflows.append(_describe_workflow(config, workflow_id, types, backends))

    generated_at = utc_timestamp()

    # Invariant contract semantics (probe meaning, start revalidation, first-turn
    # error authority) live in the guidance document, not in every response.
    payload: Dict[str, Any] = {
        "discovery": {
            "protocol_version": 2,
            "workdir": resolved_workdir,
            "generated_at": generated_at,
            "health_request": health_refresh,
        },
        "workdir": resolved_workdir if workdir else None,
        "backends": backends,
        "agents": agents,
        "workflows": workflows,
        "workflow_agent_types": workflow_agent_types,
        "recommendations": {
            "agents": {
                agent["id"]: _agent_recommendation(agent, backends)
                for agent in agents
                if agent.get("canonical_backend")
            },
            "workflows": {
                workflow["id"]: _workflow_recommendation(workflow) for workflow in workflows
            },
        },
    }
    if config.warnings:
        payload["warnings"] = [dict(warning) for warning in config.warnings]
    return payload


def describe_options_for_workdir(
    workdir: Path, *, health_refresh: str = "cached"
) -> Dict[str, Any]:
    from .config import resolve_existing_workdir

    root = resolve_existing_workdir(workdir)
    return describe_options(load_config(root), root, health_refresh=health_refresh)


def _describe_backends(
    config: CollaborationConfig,
    health: Any,
    health_refresh: str,
) -> Dict[str, Any]:
    from . import backends as backend_registry
    from .config import backend_policy

    result: Dict[str, Any] = {}
    for agent_type in backend_registry.registered_agent_types():
        for backend_id in backend_registry.registered_backends(agent_type):
            canonical = backend_registry.backend_name(agent_type, backend_id)
            backend = backend_registry.get_backend(agent_type, backend_id)
            user_policy = backend_policy(config, canonical)
            agent = _representative_agent(config, agent_type, backend_id)
            option_schema = _option_object_schema(
                _effective_backend_schema(backend, agent, f"backend_options.{canonical}", [])
            )
            # Option defaults ship in the built-in config, not the backend
            # manifests; overlay them so discovery keeps showing defaults.
            section = config.backends.get(canonical)
            if section is not None:
                for key, value in section.default_options.items():
                    spec = option_schema["properties"].get(key)
                    if spec is not None:
                        spec["default"] = value
            config_schema = _backend_configuration_schema(backend)
            probe = _probe_description(
                backend,
                health,
                fresh=health_refresh == "fresh",
                run=user_policy.enabled or health_refresh == "fresh",
            )
            policy = {
                "enabled": user_policy.enabled,
                "enabled_source": user_policy.source,
                "selection_eligible": user_policy.enabled,
                "block_on_unavailable": backend.block_on_unavailable,
                "checks_credentials": backend.checks_credentials,
                "start_probe_policy": (
                    "fresh"
                    if backend.block_on_unavailable or backend.checks_credentials
                    else "not_probed"
                ),
            }
            assessment = assess_backend(canonical, probe, policy)
            result[canonical] = {
                "identity": {
                    "provider_type": agent_type,
                    "backend_id": backend_id,
                    "canonical_backend": canonical,
                    "registered": True,
                    "registry_default": backend_id == backend_registry.DEFAULT_BACKEND,
                },
                "static": {
                    "capabilities": backend.capabilities.to_dict(),
                    "event_fidelity": backend.event_fidelity,
                    "provider_session_id_kind": backend.provider_session_id_kind,
                    "option_schema": option_schema,
                    "configuration_schema": config_schema,
                },
                "probe": probe,
                "policy": policy,
                "assessment": assessment,
            }
    return result


def _probe_description(backend: Any, health: Any, *, fresh: bool, run: bool) -> Dict[str, Any]:
    if not run:
        return {
            "status": "not_run",
            "reason": "disabled_by_user_config",
            "health": {},
            "checked_at": None,
            "age_seconds": None,
            "cache_hit": False,
            "stale": False,
            "cache_ttl_seconds": 60,
        }
    if health is not None:
        status = health(backend)
        cache_hit, age, ttl = False, 0.0, 60.0
    else:
        from . import backends as backend_registry

        observation = backend_registry.HEALTH.observe(backend, fresh=fresh)
        status = observation.health
        cache_hit, age, ttl = (
            observation.cache_hit,
            observation.age_seconds,
            observation.ttl_seconds,
        )
    return {
        "status": "completed",
        "source": "side_effect_free_probe",
        "health": status.to_dict(),
        "checked_at": status.checked_at,
        "age_seconds": round(age, 3),
        "cache_hit": cache_hit,
        "stale": age >= ttl,
        "cache_ttl_seconds": ttl,
    }


def _backend_configuration_schema(backend: Any) -> Dict[str, Any]:
    builder = getattr(backend, "configuration_schema", None)
    if not callable(builder):
        return _option_object_schema({})
    try:
        return _option_object_schema(builder())
    except Exception:
        return _option_object_schema({})


def assess_backend(
    canonical: str, probe: Mapping[str, Any], policy: Mapping[str, Any]
) -> Dict[str, Any]:
    if not policy["enabled"]:
        return {
            "state": "unknown",
            "discovery_gate": "disabled_by_user_config",
            "reason_codes": ["backend_disabled"],
            "uncertainties": ["probe_not_run", "no_model_call_was_made"],
            "remediation": [
                {
                    "code": "enable_backend_in_user_config",
                    "message": f"Set [backends.{canonical}] enabled = true in the user config.",
                }
            ],
        }
    health = probe.get("health") or {}
    status = health.get("status", "unknown")
    credentials = health.get("credentials", "unknown")
    reason_codes = list(health.get("reason_codes") or [])
    remediation = [dict(item) for item in (health.get("remediation") or [])]
    uncertainties = [
        "no_model_call_was_made",
        "authentication_entitlement_model_and_service_state_not_proven",
    ]
    if probe.get("stale"):
        state = "unknown"
        reason_codes.append("probe_stale")
    elif status == "unavailable" or credentials == "missing":
        state = "unavailable"
        if credentials == "missing" and "credentials_missing" not in reason_codes:
            reason_codes.append("credentials_missing")
            remediation.append(
                {
                    "code": "provider_sign_in",
                    "message": "Use the provider's supported sign-in flow, then retry discovery.",
                }
            )
    elif status == "ok":
        state = "usable"
    else:
        state = "unknown"
        reason_codes.append("probe_indeterminate")
    if credentials == "unknown":
        uncertainties.append("credentials_not_verified")
    if state == "unavailable" and policy["block_on_unavailable"]:
        gate = "block_if_unchanged_at_start"
    elif state == "unknown" and (policy["block_on_unavailable"] or policy["checks_credentials"]):
        gate = "warn_if_unchanged_at_start"
    else:
        gate = "allow_if_unchanged_at_start"
    return {
        "state": state,
        "discovery_gate": gate,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "uncertainties": uncertainties,
        "remediation": remediation,
    }


def _describe_agent(
    config: CollaborationConfig,
    agent: AgentConfig,
    backends: Mapping[str, Any],
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "id": agent.id,
        "type": agent.type,
        "provider_type": agent.type,
        "enabled": agent.enabled,
        "name": agent.name,
        "configured_backend": agent.backend,
    }
    if agent.type == "mock":
        entry.update(
            {"effective_backend": None, "canonical_backend": None, "selection_source": "mock"}
        )
        return entry
    from . import backends as backend_registry

    backend_id = backend_registry.resolve_backend_id(agent)
    canonical = backend_registry.backend_name(agent.type, backend_id)
    backend = backend_registry.get_backend(agent.type, backend_id)
    entry.update(
        {
            "effective_backend": backend_id,
            "canonical_backend": canonical,
            "selection_source": "agent_config" if agent.backend else "registry_default",
            "configured_session_defaults": _configured_defaults(backend, agent),
            "static_configuration": _safe_static_configuration(backend, agent),
            "selection_eligible": bool(backends[canonical]["policy"]["enabled"]),
        }
    )
    return entry


def _configured_defaults(backend: Any, agent: AgentConfig) -> Dict[str, Any]:
    try:
        return dict(backend.normalize_options(agent, {}))
    except Exception as exc:
        return {"validation": "invalid", "reason": str(exc)}


def _safe_static_configuration(backend: Any, agent: AgentConfig) -> Dict[str, Any]:
    formatter = getattr(backend, "safe_configuration_summary", None)
    if callable(formatter):
        try:
            return dict(formatter(agent))
        except Exception as exc:
            return {"validation": "invalid", "reason": str(exc)}
    return {
        "validation": "valid",
        "fields": {name: "configured" for name in sorted(agent.backend_config)},
    }


def _describe_workflow(
    config: CollaborationConfig,
    workflow_id: str,
    types: List[str],
    backends: Mapping[str, Any],
) -> Dict[str, Any]:
    from . import backends as backend_registry

    workflow = config.workflows[workflow_id]
    members = workflow_members(workflow)
    effective_agents: List[Dict[str, Any]] = []
    selected: List[str] = []
    ineligible: List[str] = []
    recommendation_blockers: List[str] = []
    provider_types: Set[str] = set()
    for agent_id in members:
        agent = config.agents.get(agent_id)
        if agent is None:
            # The member references a disabled backend: the workflow stays
            # visible but cannot start until that backend is enabled.
            effective_agents.append(
                {
                    "agent_id": agent_id,
                    "canonical_backend": agent_id.partition(".")[0],
                    "selection_source": "backend_disabled",
                }
            )
            selected.append(agent_id.partition(".")[0])
            ineligible.append("backend_disabled")
            recommendation_blockers.append("backend_disabled")
            continue
        if agent.type == "mock":
            effective_agents.append({"agent_id": agent_id, "canonical_backend": None})
            continue
        backend_id = backend_registry.resolve_backend_id(agent)
        canonical = backend_registry.backend_name(agent.type, backend_id)
        effective_agents.append(
            {
                "agent_id": agent_id,
                "canonical_backend": canonical,
                "selection_source": "agent_config" if agent.backend else "registry_default",
            }
        )
        selected.append(canonical)
        provider_types.add(agent.type)
        catalog = backends[canonical]
        if not catalog["policy"]["enabled"]:
            ineligible.append("backend_disabled")
            recommendation_blockers.append("backend_disabled")
        elif catalog["assessment"]["discovery_gate"] == "block_if_unchanged_at_start":
            ineligible.extend(catalog["assessment"]["reason_codes"] or ["backend_unavailable"])
        if catalog["assessment"]["state"] == "unavailable":
            recommendation_blockers.extend(
                catalog["assessment"]["reason_codes"] or ["backend_unavailable"]
            )
    uniform = []
    # Uniform overrides are meaningless while any member's backend is disabled
    # (that member has no derived agent and the workflow is start-ineligible).
    if provider_types and all(agent_id in config.agents for agent_id in members):
        candidates = set.intersection(
            *(
                set(backend_registry.registered_backends(agent_type))
                for agent_type in provider_types
            )
        )
        for backend_id in sorted(candidates):
            names = [
                backend_registry.backend_name(agent_type, backend_id)
                for agent_type in provider_types
            ]
            if all(
                backends[name]["policy"]["enabled"]
                and backends[name]["assessment"]["state"] != "unavailable"
                for name in names
            ):
                resolved = {
                    agent_id: backend_id
                    for agent_id in dict.fromkeys(members)
                    if config.agents[agent_id].type != "mock"
                }
                try:
                    normalize_start_options(config, workflow_id, agent_backends=resolved)
                except (StartOptionsError, ValueError):
                    continue
                uniform.append(backend_id)
    return {
        "id": workflow_id,
        "sequence": members,
        "parallel": None if workflow.parallel is None else list(workflow.parallel),
        "agent_types": types,
        "member_selection": _describe_member_selection(config, workflow),
        "effective_agents": effective_agents,
        "selected_backends": sorted(set(selected)),
        "start_eligible": not ineligible,
        "ineligible_reasons": list(dict.fromkeys(ineligible)),
        "recommendation_blockers": list(dict.fromkeys(recommendation_blockers)),
        "uniform_backend_overrides": uniform,
    }


def _describe_member_selection(
    config: CollaborationConfig, workflow: WorkflowConfig
) -> Dict[str, Any]:
    """Describe the workflow's start-time member slots for discovery.

    Advertises the additive ``members`` start field: one entry per slot (see
    ``workflow_member_slots``) with its configured default and the globally
    enabled agents eligible to fill it. ``default_eligible`` is false when the
    configured member's backend is disabled — the slot then *requires* a
    substitution before the workflow can start.
    """

    from .config import workflow_member_slots

    eligible = sorted(agent.id for agent in config.agents.values() if agent.enabled)
    distinct = workflow.parallel is not None
    return {
        "start_field": "members",
        "distinct_members": distinct,
        "slots": [
            {
                "slot": slot,
                "default": slot,
                "default_eligible": slot in eligible,
                "eligible_members": eligible,
            }
            for slot in workflow_member_slots(workflow)
        ],
    }


def _agent_recommendation(agent: Mapping[str, Any], catalog: Mapping[str, Any]) -> Dict[str, Any]:
    selected = agent["canonical_backend"]
    backend = catalog[selected]
    blocked = not backend["policy"]["enabled"] or backend["assessment"]["state"] == "unavailable"
    return {
        "selected": selected,
        "recommended": None if blocked else selected,
        "action": "remediate" if blocked else "keep",
        "reason_codes": backend["assessment"]["reason_codes"]
        if blocked
        else ["configured_selection_has_no_definite_blocker"],
        "reasons": ["Keep the configured backend unless a definite blocker is present."]
        if not blocked
        else [
            "The configured backend has a definite blocker; no automatic fallback is recommended."
        ],
        "evidence_checked_at": backend["probe"].get("checked_at"),
        "uncertainties": backend["assessment"]["uncertainties"],
        "remediation": backend["assessment"]["remediation"],
        "expressible_by_start_api": not blocked,
    }


def _workflow_recommendation(workflow: Mapping[str, Any]) -> Dict[str, Any]:
    blocked = bool(workflow.get("recommendation_blockers"))
    return {
        "selected": list(workflow["selected_backends"]),
        "recommended": list(workflow["selected_backends"]) if not blocked else None,
        "action": "remediate" if blocked else "keep",
        "reason_codes": list(
            workflow.get("recommendation_blockers")
            or ["configured_selection_has_no_definite_blocker"]
        ),
        "uniform_backend_overrides": list(workflow["uniform_backend_overrides"]),
        "expressible_by_start_api": not blocked,
    }


def build_session_settings(
    config: CollaborationConfig,
    workflow_id: str,
    normalized_options: Mapping[str, Mapping[str, Any]],
    *,
    agent_backends: Optional[Mapping[str, str]] = None,
    agent_options: Optional[Mapping[str, Mapping[str, Any]]] = None,
    warnings: Optional[Sequence[Mapping[str, str]]] = None,
    interactive: bool = False,
    interactive_idle_timeout: float = 600.0,
    workdir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the effective session settings confirmation for start responses.

    Reflects effective config plus validated start options; fields that are
    unavailable for an agent are omitted rather than invented. Records the
    effective backend and its capability flags per agent. Command previews apply
    to the ``cli`` backend only and never include the task prompt (runners append
    it separately); a non-``cli`` backend contributes an equivalent summary.
    """

    from . import backends as backend_registry

    resolved = dict(agent_backends or {})
    workflow = config.workflows[workflow_id]
    members = workflow_members(workflow)
    agents: Dict[str, Dict[str, Any]] = {}
    for agent_id in members:
        if agent_id in agents:
            continue
        agent = config.agents[agent_id]
        entry: Dict[str, Any] = {"type": agent.type}
        backend_id = (
            None
            if agent.type == "mock"
            else (resolved.get(agent_id) or backend_registry.resolve_backend_id(agent))
        )
        options: Dict[str, Any] = {}
        if backend_id is not None:
            backend = backend_registry.get_backend(agent.type, backend_id)
            if agent_options is not None and agent_id in agent_options:
                options = dict(agent_options[agent_id])
            else:
                name = backend_registry.backend_name(agent.type, backend_id)
                requested = dict(normalized_options.get(name) or {})
                options = dict(backend.normalize_options(agent, requested))
            entry.update(options)
        if backend_id is not None:
            entry["backend"] = backend_id
            entry["capabilities"] = backend_registry.capabilities_for(
                agent.type, backend_id
            ).to_dict()
            # Provider brand hue, a backend-declared static fact; the TUI
            # colors agent labels from this and falls back to its own accent
            # when absent (mock agents, unknown providers).
            brand_color = getattr(backend, "brand_color", None)
            if brand_color:
                entry["brand_color"] = brand_color
            summary = _backend_settings_summary(agent, backend_id, options)
            if summary is not None:
                entry["backend_summary"] = summary
            preview = backend.command_preview(agent, options, workdir)
            if preview is not None:
                entry["command_preview"] = preview
        agents[agent_id] = entry
    settings: Dict[str, Any] = {
        "workflow": {
            "name": workflow_id,
            "sequence": members,
            "parallel": None if workflow.parallel is None else list(workflow.parallel),
        },
        "agents": agents,
        "interactive": bool(interactive),
        "interactive_idle_timeout": float(interactive_idle_timeout),
    }
    if warnings:
        settings["warnings"] = [dict(warning) for warning in warnings]
    return settings


def _backend_settings_summary(
    agent: AgentConfig, backend_id: str, options: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """Ask the selected backend to summarize the exact normalized options."""

    from . import backends as backend_registry

    if not backend_registry.is_registered(agent.type, backend_id):
        return None
    backend = backend_registry.get_backend(agent.type, backend_id)
    return dict(backend.settings_summary(agent, dict(options)))


def _expect_mapping(value: Any, path: str, errors: List[Dict[str, str]]) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.append({"path": path, "message": "must be an object"})
        return {}
    return value


def _validate_field_value(
    value: Any, path: str, schema: Mapping[str, Any], errors: List[Dict[str, str]]
) -> None:
    expected_type = schema.get("type")
    if expected_type == "string" and not isinstance(value, str):
        errors.append({"path": path, "message": "must be a string"})
        return
    if expected_type == "boolean" and not isinstance(value, bool):
        errors.append({"path": path, "message": "must be a boolean"})
        return
    if expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        errors.append({"path": path, "message": "must be an integer"})
        return

    allowed = schema.get("allowed")
    if allowed is not None and value not in allowed:
        errors.append(
            {
                "path": path,
                "message": f"unsupported value {value!r}; expected one of: {_join_values(allowed)}",
            }
        )
        return

    minimum = schema.get("min")
    if minimum is not None and value < minimum:
        errors.append({"path": path, "message": f"must be >= {minimum}"})
    maximum = schema.get("max")
    if maximum is not None and value > maximum:
        errors.append({"path": path, "message": f"must be <= {maximum}"})


def _representative_agent(
    config: CollaborationConfig,
    agent_type: str,
    backend_id: str,
) -> AgentConfig:
    agents = sorted(
        (agent for agent in config.agents.values() if agent.type == agent_type),
        key=lambda agent: agent.id,
    )
    explicitly_matching = [agent for agent in agents if (agent.backend or "cli") == backend_id]
    if explicitly_matching:
        return explicitly_matching[0]
    if agents:
        source = agents[0]
        return AgentConfig(
            id=source.id,
            type=source.type,
            command=source.command,
            args=list(source.args),
            enabled=source.enabled,
            name=source.name,
            env=dict(source.env),
            cwd=source.cwd,
            timeout=source.timeout,
            backend_config={},
            options={},
            backend=backend_id,
        )
    return AgentConfig(id=agent_type, type=agent_type, backend=backend_id)


def _option_object_schema(schema: Mapping[str, OptionSpec]) -> Dict[str, Any]:
    result = {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: spec.to_dict() for key, spec in sorted(schema.items())},
    }
    required = sorted(key for key, spec in schema.items() if spec.required)
    if required:
        result["required"] = required
    return result


def _workflow_agent_types(config: CollaborationConfig, sequence: Iterable[str]) -> Set[str]:
    from .config import split_canonical_backend

    types: Set[str] = set()
    for agent_id in sequence:
        agent = config.agents.get(agent_id)
        if agent is not None:
            types.add(agent.type)
            continue
        # A member of a disabled backend has no derived agent; its provider
        # type still comes from the canonical reference.
        types.add(split_canonical_backend(agent_id.partition(".")[0])[0])
    return types


def _join_values(values: Sequence[Any]) -> str:
    return ", ".join(
        str(value).lower() if isinstance(value, bool) else str(value) for value in values
    )
