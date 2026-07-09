from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set

from .config import AgentConfig, CollaborationConfig, load_config, validate_workflow


CODEX_THINKING_LEVELS = ["minimal", "low", "medium", "high", "xhigh"]
CLAUDE_THINKING_LEVELS = ["low", "medium", "high", "xhigh", "max"]

CODEX_OPTION_FIELDS = {
    "model": {"type": "string"},
    "profile": {"type": "string"},
    "thinking_level": {"type": "string", "allowed": CODEX_THINKING_LEVELS},
    "reasoning_effort": {"type": "string", "allowed": CODEX_THINKING_LEVELS},
    "sandbox": {"type": "string", "allowed": ["read-only", "workspace-write", "danger-full-access"]},
    "approval_policy": {"type": "string", "allowed": ["untrusted", "on-failure", "on-request", "never"]},
    "search": {"type": "boolean", "allowed": [True, False]},
}

CLAUDE_OPTION_FIELDS = {
    "model": {"type": "string"},
    "permission_mode": {"type": "string", "allowed": ["default", "acceptEdits", "bypassPermissions"]},
    "thinking_level": {"type": "string", "allowed": CLAUDE_THINKING_LEVELS},
    "thinking_budget_tokens": {"type": "integer", "min": 0},
}

# `agy --mode` accepts these; `default` is the interactive request-review posture.
ANTIGRAVITY_MODES = ["default", "accept-edits", "plan"]
ANTIGRAVITY_OPTION_FIELDS = {
    "model": {"type": "string"},
    "mode": {"type": "string", "allowed": ANTIGRAVITY_MODES},
}

OPTION_FIELDS = {
    "codex": CODEX_OPTION_FIELDS,
    "claude": CLAUDE_OPTION_FIELDS,
    "antigravity": ANTIGRAVITY_OPTION_FIELDS,
}

# Which typed options each backend actually honours, per provider. An option the
# caller *explicitly* requests that is outside its resolved backend's set is
# rejected at start with a field path — so a cli-only option (no SDK mapping)
# fails on `sdk`, and any sdk-only option fails on `cli`. Values inferred from an
# agent's cli args/defaults are never checked here (only explicit request keys),
# so selecting `sdk` on the built-in antigravity agent (whose args carry
# `--mode`) is not blocked. Keep this in lockstep with each backend's option
# mapping (`_map_sdk_options` / `_apply_*_options`).
BACKEND_OPTION_SUPPORT: Dict[str, Dict[str, Set[str]]] = {
    "claude": {
        "cli": {"model", "permission_mode", "thinking_level", "thinking_budget_tokens"},
        "sdk": {"model", "permission_mode", "thinking_level", "thinking_budget_tokens"},
    },
    "codex": {
        "cli": {"model", "profile", "thinking_level", "reasoning_effort", "sandbox", "approval_policy", "search"},
        "sdk": {"model", "thinking_level", "reasoning_effort", "sandbox"},
    },
    "antigravity": {
        "cli": {"model", "mode"},
        "sdk": {"model"},
    },
}

class StartOptionsError(ValueError):
    code = "invalid_start_options"

    def __init__(self, details: Sequence[Mapping[str, str]]):
        self.details = [dict(detail) for detail in details]
        super().__init__(format_validation_error(self.details))

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "details": deepcopy(self.details)}


def format_validation_error(details: Sequence[Mapping[str, str]]) -> str:
    lines = [StartOptionsError.code]
    for detail in details:
        path = detail.get("path", "")
        message = detail.get("message", "")
        lines.append(f"{path}: {message}" if path else message)
    return "\n".join(lines)


def validate_start_options(
    config: CollaborationConfig,
    workflow_id: str,
    codex_options: Any = None,
    claude_options: Any = None,
    antigravity_options: Any = None,
) -> Dict[str, Dict[str, Any]]:
    errors: List[Dict[str, str]] = []
    normalized: Dict[str, Dict[str, Any]] = {}
    option_payloads = {
        "codex": _expect_mapping(codex_options, "codex_options", errors),
        "claude": _expect_mapping(claude_options, "claude_options", errors),
        "antigravity": _expect_mapping(antigravity_options, "antigravity_options", errors),
    }
    if errors:
        raise StartOptionsError(errors)

    validate_workflow(config, workflow_id)
    workflow = config.workflows[workflow_id]
    workflow_types = _workflow_agent_types(config, workflow.sequence)

    for agent_type, payload in option_payloads.items():
        path = f"{agent_type}_options"
        if payload and agent_type not in workflow_types:
            errors.append(
                {
                    "path": path,
                    "message": (
                        f"does not apply to workflow {workflow_id!r}; "
                        f"workflow uses: {', '.join(sorted(workflow_types))}"
                    ),
                }
            )
            continue
        if agent_type in workflow_types:
            agent_ids = [agent_id for agent_id in workflow.sequence if config.agents[agent_id].type == agent_type]
            merged_payload = _default_options_for_agent_type(config, agent_type, agent_ids)
            explicit_keys = set(payload)
            merged_payload.update(payload)
            _resolve_thinking_level(agent_type, merged_payload, explicit_keys, path, errors)
            _validate_type_options(config, agent_type, agent_ids, merged_payload, path, errors)
            normalized[agent_type] = merged_payload
        elif payload:
            merged_payload = dict(payload)
            _resolve_thinking_level(agent_type, merged_payload, set(payload), path, errors)
            _validate_type_options(config, agent_type, [], merged_payload, path, errors)
            normalized[agent_type] = merged_payload
        else:
            normalized[agent_type] = {}

    if errors:
        raise StartOptionsError(errors)
    return {f"{agent_type}_options": dict(normalized.get(agent_type, {})) for agent_type in option_payloads}


def validate_start_backends(
    config: CollaborationConfig,
    workflow_id: str,
    request_backend: Optional[str] = None,
    antigravity_options: Optional[Mapping[str, Any]] = None,
    claude_options: Optional[Mapping[str, Any]] = None,
    codex_options: Optional[Mapping[str, Any]] = None,
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

    for agent_id in workflow.sequence:
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

    requested_by_type = {
        "antigravity": antigravity_options,
        "claude": claude_options,
        "codex": codex_options,
    }
    _reject_backend_unsupported_options(config, resolved, requested_by_type, errors)
    if errors:
        raise StartOptionsError(errors)

    if health is not None:
        _gate_backend_health(config, resolved, health, errors, warnings)
        if errors:
            raise StartOptionsError(errors)

    return BackendSelection(agent_backends=resolved, warnings=warnings)


class BackendSelection:
    """Resolved per-agent backend map plus any non-fatal start warnings."""

    def __init__(self, agent_backends: Dict[str, str], warnings: Optional[List[Dict[str, str]]] = None):
        self.agent_backends = dict(agent_backends)
        self.warnings = list(warnings or [])


def _reject_backend_unsupported_options(
    config: CollaborationConfig,
    resolved: Mapping[str, str],
    requested_by_type: Mapping[str, Optional[Mapping[str, Any]]],
    errors: List[Dict[str, str]],
    support: Optional[Mapping[str, Mapping[str, Set[str]]]] = None,
) -> None:
    """Reject explicitly-requested options a resolved backend does not support.

    Symmetric in both directions: a cli-only option (e.g. antigravity ``mode``,
    claude ``thinking_level``, codex ``profile``) requested with the ``sdk``
    backend is rejected, and any sdk-only option requested with a ``cli`` backend
    is rejected — each with a ``<type>_options.<key>`` field path. Only keys the
    caller *explicitly* passed are checked (never defaults inferred from cli args),
    and a backend with no support entry (a custom backend) is left unchecked.
    """

    support = support if support is not None else BACKEND_OPTION_SUPPORT
    seen: Set[str] = set()
    for agent_id, backend_id in resolved.items():
        agent_type = config.agents[agent_id].type
        requested = requested_by_type.get(agent_type)
        if not requested:
            continue
        supported = support.get(agent_type, {}).get(backend_id)
        if supported is None:
            continue
        for key in sorted(requested):
            if key in supported:
                continue
            path = f"{agent_type}_options.{key}"
            if path in seen:
                continue
            seen.add(path)
            errors.append({"path": path, "message": f"is not supported on the {backend_id!r} backend"})


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
        block = getattr(backend, "block_on_unavailable", False)
        checks_credentials = getattr(backend, "checks_credentials", False)
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
                errors.append({"path": "backend", "message": _health_reject_message(agent_id, backend_id, status)})
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
            errors.append(
                {
                    "path": "backend",
                    "message": (
                        f"backend {backend_id!r} for agent {agent_id!r} has missing credentials"
                        + (f": {status.reason}" if status.reason else "")
                        + "; run the provider CLI and sign in"
                    ),
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


def describe_options(
    config: CollaborationConfig,
    workdir: Optional[Path] = None,
    *,
    health: Any = None,
) -> Dict[str, Any]:
    resolved_workdir = str(workdir.expanduser().resolve()) if workdir else "."
    agents = [
        {
            "id": agent.id,
            "type": agent.type,
            "enabled": agent.enabled,
            "name": agent.name,
            "backend": agent.backend,
        }
        for agent in sorted(config.agents.values(), key=lambda item: item.id)
    ]
    workflows = []
    workflow_agent_types: Dict[str, List[str]] = {}
    for workflow_id, workflow in sorted(config.workflows.items()):
        types = sorted(_workflow_agent_types(config, workflow.sequence))
        workflow_agent_types[workflow_id] = types
        workflows.append({"id": workflow_id, "sequence": list(workflow.sequence), "agent_types": types})

    return {
        "workdir": resolved_workdir if workdir else None,
        "agents": agents,
        "workflows": workflows,
        "workflow_agent_types": workflow_agent_types,
        "backends": _describe_backends(config, health),
        "codex_options": _schema_for_agent_type(config, "codex"),
        "claude_options": _schema_for_agent_type(config, "claude"),
        "antigravity_options": _schema_for_agent_type(config, "antigravity"),
        "examples": [
            {
                "task": "Review this repository",
                "workdir": resolved_workdir,
                "workflow": "compare",
                "codex_options": {"thinking_level": "medium", "sandbox": "workspace-write"},
                "claude_options": {"model": "opus", "thinking_level": "high"},
            },
            {
                "task": "Run a mock smoke test",
                "workdir": resolved_workdir,
                "mock": True,
                "max_turns": 1,
            },
        ],
    }


def describe_options_for_workdir(workdir: Path) -> Dict[str, Any]:
    root = workdir.expanduser().resolve()
    return describe_options(load_config(root), root)


def _describe_backends(config: CollaborationConfig, health: Any = None) -> Dict[str, Any]:
    """Per agent type: registered backend ids, the default, availability,
    capability flags, and health/reason. Health is the discoverability surface
    ("install the CLI / sign in, then start"); it uses the short-TTL cache by
    default so it does not hammer the filesystem.
    """

    from . import backends as backend_registry

    if health is None:
        health = lambda backend: backend_registry.health(backend, fresh=False)

    config_types = {agent.type for agent in config.agents.values() if agent.type != "mock"}
    types = sorted(config_types | set(backend_registry.registered_agent_types()))
    result: Dict[str, Any] = {}
    for agent_type in types:
        ids = backend_registry.registered_backends(agent_type)
        if not ids:
            continue
        entries: Dict[str, Any] = {}
        for backend_id in ids:
            backend = backend_registry.get_backend(agent_type, backend_id)
            status = health(backend)
            entries[backend_id] = {
                "available": status.available,
                "capabilities": backend.capabilities.to_dict(),
                "health": status.to_dict(),
            }
        result[agent_type] = {
            "default": backend_registry.DEFAULT_BACKEND,
            "backends": ids,
            "entries": entries,
        }
    return result


def _effective_options_for_agent(agent: AgentConfig, options: Mapping[str, Any]) -> Dict[str, Any]:
    effective_options = _default_options_for_agent(agent)
    if agent.type == "codex" and "thinking_level" in options and "reasoning_effort" not in options:
        effective_options.pop("reasoning_effort", None)
    if agent.type == "claude" and "thinking_budget_tokens" in options and "thinking_level" not in options:
        effective_options.pop("thinking_level", None)
    effective_options.update(options)
    return effective_options


SETTINGS_DISPLAY_FIELDS = {
    "claude": ("model", "thinking_level", "thinking_budget_tokens", "permission_mode"),
    "codex": ("model", "profile", "thinking_level", "sandbox", "approval_policy", "search"),
    "antigravity": ("model", "mode"),
}


def build_session_settings(
    config: CollaborationConfig,
    workflow_id: str,
    normalized_options: Mapping[str, Mapping[str, Any]],
    *,
    agent_backends: Optional[Mapping[str, str]] = None,
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
    agents: Dict[str, Dict[str, Any]] = {}
    for agent_id in workflow.sequence:
        if agent_id in agents:
            continue
        agent = config.agents[agent_id]
        entry: Dict[str, Any] = {"type": agent.type}
        backend_id = None if agent.type == "mock" else (
            resolved.get(agent_id) or backend_registry.resolve_backend_id(agent)
        )
        options: Dict[str, Any] = {}
        if agent.type in OPTION_FIELDS:
            options = dict(normalized_options.get(f"{agent.type}_options") or {})
            effective = _effective_options_for_agent(agent, options)
            # Only advertise options the resolved backend actually honours, so
            # settings never claim a cli-only option (e.g. an inferred `mode` or a
            # default `thinking_level`) applied on an `sdk` backend that ignores
            # it. A custom backend with no support entry advertises everything.
            supported = BACKEND_OPTION_SUPPORT.get(agent.type, {}).get(backend_id) if backend_id else None
            for field in SETTINGS_DISPLAY_FIELDS.get(agent.type, ()):
                if supported is not None and field not in supported:
                    continue
                if field in effective:
                    entry[field] = effective[field]
            if (
                (supported is None or "thinking_level" in supported)
                and "thinking_level" not in entry
                and "reasoning_effort" in effective
            ):
                entry["thinking_level"] = effective["reasoning_effort"]
        if backend_id is not None:
            entry["backend"] = backend_id
            entry["capabilities"] = backend_registry.capabilities_for(agent.type, backend_id).to_dict()
            if backend_id == "cli":
                if agent.command:
                    entry["command_preview"] = build_cli_command(agent, options, workdir=workdir)
            else:
                summary = _backend_settings_summary(agent, backend_id, options)
                if summary is not None:
                    entry["backend_summary"] = summary
        agents[agent_id] = entry
    settings: Dict[str, Any] = {
        "workflow": {"name": workflow_id, "sequence": list(workflow.sequence)},
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
    """Non-``cli`` backends describe themselves in place of a command preview."""

    from . import backends as backend_registry

    if not backend_registry.is_registered(agent.type, backend_id):
        return None
    backend = backend_registry.get_backend(agent.type, backend_id)
    describe = getattr(backend, "settings_summary", None)
    if callable(describe):
        return describe(agent, dict(options))
    return None


def apply_agent_options(command: List[str], agent: AgentConfig, options: Mapping[str, Any]) -> List[str]:
    effective_options = _effective_options_for_agent(agent, options)
    if not effective_options:
        return list(command)
    if agent.type == "codex":
        return _apply_codex_options(command, effective_options)
    if agent.type == "claude":
        return _apply_claude_options(command, effective_options)
    if agent.type == "antigravity":
        return _apply_antigravity_options(command, effective_options)
    return list(command)


def build_cli_command(
    agent: AgentConfig,
    options: Mapping[str, Any],
    *,
    workdir: Optional[Path] = None,
) -> List[str]:
    command = apply_agent_options([agent.command or agent.id] + list(agent.args), agent, options)
    run_dir = resolve_agent_run_dir(workdir, agent.cwd) if workdir is not None else None
    return apply_runtime_workdir_args(command, agent, run_dir)


def resolve_agent_run_dir(workdir: Path, cwd: Optional[str]) -> Path:
    base = workdir.expanduser().resolve()
    if not cwd:
        return base
    cwd_path = Path(cwd).expanduser()
    if cwd_path.is_absolute():
        return cwd_path
    return (base / cwd_path).resolve()


def apply_runtime_workdir_args(
    command: List[str],
    agent: AgentConfig,
    workdir: Optional[Path],
) -> List[str]:
    if agent.type != "antigravity" or workdir is None or _has_flag(command, "--add-dir"):
        return list(command)
    return _insert_before_print_prompt(command, ["--add-dir", str(workdir.expanduser().resolve())])


def _apply_antigravity_options(command: List[str], options: Mapping[str, Any]) -> List[str]:
    result = list(command)
    if "model" in options:
        result = _set_flag_value_before_print_prompt(result, "--model", str(options["model"]))
    if "mode" in options:
        result = _set_flag_value_before_print_prompt(result, "--mode", str(options["mode"]))
    return result


def _apply_codex_options(command: List[str], options: Mapping[str, Any]) -> List[str]:
    result = list(command)
    reasoning_effort = options.get("reasoning_effort", options.get("thinking_level"))
    if reasoning_effort is not None:
        result = _remove_flag(result, "--reasoning-effort", has_value=True)
        result = _set_config_value(result, "model_reasoning_effort", str(reasoning_effort))
    for key, flag in (
        ("model", "--model"),
        ("profile", "--profile"),
        ("sandbox", "--sandbox"),
        ("approval_policy", "--approval-policy"),
    ):
        if key in options:
            result = _set_flag_value(result, flag, str(options[key]))
    if "search" in options:
        result = _remove_flag(result, "--search", has_value=False)
        if options["search"]:
            result.append("--search")
    return result


def _apply_claude_options(command: List[str], options: Mapping[str, Any]) -> List[str]:
    result = list(command)
    if "model" in options:
        result = _set_flag_value(result, "--model", str(options["model"]))
    if "permission_mode" in options:
        result = _set_flag_value(result, "--permission-mode", str(options["permission_mode"]))
    if "thinking_level" in options:
        result = _set_flag_value(result, "--effort", str(options["thinking_level"]))
    if "thinking_budget_tokens" in options:
        result = _set_flag_value(result, "--thinking-budget-tokens", str(options["thinking_budget_tokens"]))
    return result


def _set_flag_value(command: List[str], flag: str, value: str) -> List[str]:
    result = _remove_flag(command, flag, has_value=True)
    result.extend([flag, value])
    return result


def _set_flag_value_before_print_prompt(command: List[str], flag: str, value: str) -> List[str]:
    result = _remove_flag(command, flag, has_value=True)
    return _insert_before_print_prompt(result, [flag, value])


def _insert_before_print_prompt(command: List[str], items: Sequence[str]) -> List[str]:
    result = list(command)
    for index, item in enumerate(result):
        if item in {"-p", "--print", "--prompt"}:
            return result[:index] + list(items) + result[index:]
    result.extend(items)
    return result


def _has_flag(command: Sequence[str], flag: str) -> bool:
    prefix = f"{flag}="
    return any(item == flag or item.startswith(prefix) for item in command)


def _set_config_value(command: List[str], key: str, value: str) -> List[str]:
    result = _remove_config_value(command, key)
    result.extend(["-c", f'{key}="{value}"'])
    return result


def _remove_flag(command: List[str], flag: str, *, has_value: bool) -> List[str]:
    result: List[str] = []
    skip_next = False
    prefix = f"{flag}="
    for item in command:
        if skip_next:
            skip_next = False
            continue
        if item == flag:
            skip_next = has_value
            continue
        if item.startswith(prefix):
            continue
        result.append(item)
    return result


def _remove_config_value(command: List[str], key: str) -> List[str]:
    result: List[str] = []
    skip_next = False
    for index, item in enumerate(command):
        if skip_next:
            skip_next = False
            continue
        if item in {"-c", "--config"} and index + 1 < len(command):
            if _config_item_key(command[index + 1]) == key:
                skip_next = True
                continue
        if item.startswith("--config=") and _config_item_key(item[len("--config=") :]) == key:
            continue
        result.append(item)
    return result


def _expect_mapping(value: Any, path: str, errors: List[Dict[str, str]]) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.append({"path": path, "message": "must be an object"})
        return {}
    return value


def _validate_type_options(
    config: CollaborationConfig,
    agent_type: str,
    agent_ids: Iterable[str],
    payload: Mapping[str, Any],
    path: str,
    errors: List[Dict[str, str]],
) -> None:
    known_fields = OPTION_FIELDS[agent_type]
    for key in sorted(payload):
        field_path = f"{path}.{key}"
        if key not in known_fields:
            errors.append({"path": field_path, "message": f"unknown option; expected one of: {', '.join(sorted(known_fields))}"})
            continue
        _validate_field_value(payload[key], field_path, _effective_field_schema(config, agent_type, agent_ids, key), errors)


def _resolve_thinking_level(
    agent_type: str,
    payload: Dict[str, Any],
    explicit_keys: Set[str],
    path: str,
    errors: List[Dict[str, str]],
) -> None:
    if agent_type == "codex":
        if "thinking_level" not in payload:
            return
        if "thinking_level" in explicit_keys and "reasoning_effort" in explicit_keys:
            if payload.get("thinking_level") != payload.get("reasoning_effort"):
                errors.append(
                    {
                        "path": f"{path}.thinking_level",
                        "message": "conflicts with reasoning_effort; use one thinking level field or provide matching values",
                    }
                )
            return
        if "reasoning_effort" not in explicit_keys:
            payload["reasoning_effort"] = payload["thinking_level"]
        return

    if agent_type == "claude":
        if "thinking_level" in explicit_keys and "thinking_budget_tokens" in explicit_keys:
            errors.append(
                {
                    "path": f"{path}.thinking_level",
                    "message": "conflicts with thinking_budget_tokens; use thinking_level or a raw token budget, not both",
                }
            )
            return
        if "thinking_budget_tokens" in explicit_keys:
            payload.pop("thinking_level", None)


def _validate_field_value(value: Any, path: str, schema: Mapping[str, Any], errors: List[Dict[str, str]]) -> None:
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
        errors.append({"path": path, "message": f"unsupported value {value!r}; expected one of: {_join_values(allowed)}"})
        return

    minimum = schema.get("min")
    if minimum is not None and value < minimum:
        errors.append({"path": path, "message": f"must be >= {minimum}"})
    maximum = schema.get("max")
    if maximum is not None and value > maximum:
        errors.append({"path": path, "message": f"must be <= {maximum}"})


def _effective_field_schema(
    config: CollaborationConfig,
    agent_type: str,
    agent_ids: Iterable[str],
    field: str,
) -> Dict[str, Any]:
    schema = dict(OPTION_FIELDS[agent_type][field])
    agents = [config.agents[agent_id] for agent_id in agent_ids]
    if not agents:
        agents = [agent for agent in config.agents.values() if agent.type == agent_type]
    for agent in agents:
        _merge_field_schema(schema, agent.options.get(field, {}))
        default = _infer_default(agent, field)
        if default is not None and "default" not in schema:
            schema["default"] = default
    return schema


def _default_options_for_agent_type(
    config: CollaborationConfig,
    agent_type: str,
    agent_ids: Iterable[str],
) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    for field in sorted(OPTION_FIELDS[agent_type]):
        schema = _effective_field_schema(config, agent_type, agent_ids, field)
        if "default" in schema:
            defaults[field] = deepcopy(schema["default"])
    return defaults


def _default_options_for_agent(agent: AgentConfig) -> Dict[str, Any]:
    if agent.type not in OPTION_FIELDS:
        return {}
    defaults: Dict[str, Any] = {}
    for field in sorted(OPTION_FIELDS[agent.type]):
        schema = dict(OPTION_FIELDS[agent.type][field])
        _merge_field_schema(schema, agent.options.get(field, {}))
        if "default" not in schema:
            inferred = _infer_default(agent, field)
            if inferred is not None:
                schema["default"] = inferred
        if "default" in schema:
            defaults[field] = deepcopy(schema["default"])
    return defaults


def _schema_for_agent_type(config: CollaborationConfig, agent_type: str) -> Dict[str, Any]:
    properties: Dict[str, Dict[str, Any]] = {}
    for field in sorted(OPTION_FIELDS[agent_type]):
        properties[field] = _effective_field_schema(config, agent_type, [], field)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }


def _merge_field_schema(schema: MutableMapping[str, Any], configured: Any) -> None:
    if not isinstance(configured, Mapping):
        return
    for key in ("allowed", "min", "max", "default"):
        if key in configured:
            schema[key] = deepcopy(configured[key])


def _infer_default(agent: AgentConfig, field: str) -> Optional[Any]:
    args = list(agent.args)
    if field == "model":
        return _flag_value(args, "--model")
    if field == "profile":
        return _flag_value(args, "--profile")
    if field == "sandbox":
        return _flag_value(args, "--sandbox")
    if field == "approval_policy":
        return _flag_value(args, "--approval-policy")
    if field == "thinking_level":
        if agent.type == "codex":
            return _config_value(args, "model_reasoning_effort") or _flag_value(args, "--reasoning-effort")
        if agent.type == "claude":
            return _flag_value(args, "--effort")
    if field == "reasoning_effort":
        return _config_value(args, "model_reasoning_effort") or _flag_value(args, "--reasoning-effort")
    if field == "permission_mode":
        return _flag_value(args, "--permission-mode")
    if field == "mode":
        return _flag_value(args, "--mode")
    if field == "thinking_budget_tokens":
        value = _flag_value(args, "--thinking-budget-tokens")
        return int(value) if value is not None and value.isdigit() else value
    if field == "search":
        return True if "--search" in args else None
    return None


def _config_value(args: Sequence[str], key: str) -> Optional[str]:
    for index, item in enumerate(args):
        value: Optional[str] = None
        if item in {"-c", "--config"} and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith("--config="):
            value = item[len("--config=") :]
        if value is not None and _config_item_key(value) == key:
            raw_value = value.split("=", 1)[1]
            return raw_value.strip("\"'")
    return None


def _config_item_key(item: str) -> Optional[str]:
    if "=" not in item:
        return None
    return item.split("=", 1)[0].strip()


def _flag_value(args: Sequence[str], flag: str) -> Optional[str]:
    prefix = f"{flag}="
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def _workflow_agent_types(config: CollaborationConfig, sequence: Iterable[str]) -> Set[str]:
    return {config.agents[agent_id].type for agent_id in sequence}


def _join_values(values: Sequence[Any]) -> str:
    return ", ".join(str(value).lower() if isinstance(value, bool) else str(value) for value in values)
