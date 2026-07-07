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

OPTION_FIELDS = {
    "codex": CODEX_OPTION_FIELDS,
    "claude": CLAUDE_OPTION_FIELDS,
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
) -> Dict[str, Dict[str, Any]]:
    errors: List[Dict[str, str]] = []
    normalized: Dict[str, Dict[str, Any]] = {}
    option_payloads = {
        "codex": _expect_mapping(codex_options, "codex_options", errors),
        "claude": _expect_mapping(claude_options, "claude_options", errors),
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


def describe_options(config: CollaborationConfig, workdir: Optional[Path] = None) -> Dict[str, Any]:
    agents = [
        {
            "id": agent.id,
            "type": agent.type,
            "enabled": agent.enabled,
            "name": agent.name,
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
        "workdir": str(workdir.expanduser().resolve()) if workdir else None,
        "agents": agents,
        "workflows": workflows,
        "workflow_agent_types": workflow_agent_types,
        "codex_options": _schema_for_agent_type(config, "codex"),
        "claude_options": _schema_for_agent_type(config, "claude"),
        "examples": [
            {
                "task": "Review this repository",
                "workflow": "compare",
                "codex_options": {"thinking_level": "medium", "sandbox": "workspace-write"},
                "claude_options": {"model": "opus", "thinking_level": "high"},
            },
            {
                "task": "Run a mock smoke test",
                "mock": True,
                "max_turns": 1,
            },
        ],
    }


def describe_options_for_workdir(workdir: Path) -> Dict[str, Any]:
    root = workdir.expanduser().resolve()
    return describe_options(load_config(root), root)


def apply_agent_options(command: List[str], agent: AgentConfig, options: Mapping[str, Any]) -> List[str]:
    effective_options = _default_options_for_agent(agent)
    if agent.type == "codex" and "thinking_level" in options and "reasoning_effort" not in options:
        effective_options.pop("reasoning_effort", None)
    if agent.type == "claude" and "thinking_budget_tokens" in options and "thinking_level" not in options:
        effective_options.pop("thinking_level", None)
    effective_options.update(options)
    if not effective_options:
        return list(command)
    if agent.type == "codex":
        return _apply_codex_options(command, effective_options)
    if agent.type == "claude":
        return _apply_claude_options(command, effective_options)
    return list(command)


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
