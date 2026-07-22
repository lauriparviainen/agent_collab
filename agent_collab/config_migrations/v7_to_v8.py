"""v7 -> v8 migration step: the backend-first schema fold.

``[agents.*]`` sections collapse into ``[backends.*]``: each old agent maps to
its effective canonical backend, one agent per backend becomes the backend's
default and additional enabled agents become options-only nested personae.
Owns the tables describing what the v7 built-in agents ran with, so an omitted
key never hides a real difference during the fold, plus the project-scope
reference remap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

from .base import ConfigMigrationError, _logger

# The v7 built-in agents and their effective canonical backends, used to
# rewrite workflow references to agents the user file never redefined.
_V7_BUILTIN_AGENTS = {
    "claude": ("claude", "cli"),
    "codex": ("codex", "cli"),
    "antigravity": ("antigravity", "cli"),
    "xai": ("xai", "cli"),
}
_V7_EXECUTION_KEYS = ("command", "args", "env", "cwd", "timeout")
# What a v7 built-in agent effectively ran with when its user-file section
# omitted the key; used so persona folding never hides a real difference.
_V7_BUILTIN_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "claude": {
        "command": "claude",
        "args": ["-p", "--output-format", "stream-json", "--verbose"],
    },
    "codex": {"command": "codex", "args": ["exec", "--json"]},
    "antigravity": {"command": "agy", "args": ["--mode", "accept-edits", "-p"]},
    "xai": {
        "command": "grok",
        "args": ["--no-auto-update", "--output-format", "streaming-json", "-p"],
    },
}


def _migrate_v7_to_v8(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v8 is the backend-first schema: `[agents.*]` folds into `[backends.*]`.

    Each old agent maps to its effective canonical backend. One agent per
    backend becomes the backend section (the default agent); additional
    enabled agents become options-only nested personae. Anything that cannot
    be expressed that way is a fatal migration error — the old shape is not
    supported at runtime.
    """

    if scope == "project":
        # Project config never carries execution settings; the trust filters
        # decide what survives. Only remap built-in agent references so old
        # project files keep addressing the derived v8 agents.
        return _remap_v7_project_references(data)

    agents = data.get("agents")
    if agents is None:
        agents = {}
    if not isinstance(agents, Mapping):
        raise ConfigMigrationError(f"{source}: [agents] must be a table")

    backends: Dict[str, Dict[str, Any]] = {}
    raw_backends = data.get("backends")
    if isinstance(raw_backends, Mapping):
        for name, values in raw_backends.items():
            if not isinstance(values, Mapping):
                raise ConfigMigrationError(f"{source}: [backends.{name}] must be a table")
            backends[str(name)] = dict(values)

    reference_map: Dict[str, str] = {
        agent_id: f"{spec[0]}_{spec[1]}" for agent_id, spec in _V7_BUILTIN_AGENTS.items()
    }
    default_agent: Dict[str, str] = {}
    name_only_agents: Dict[str, Any] = {}

    # First pass: classify and group by canonical backend, preserving file
    # order. Folding happens per group so the default-agent choice is
    # deterministic (canonical id, else the first *enabled* agent), never a
    # side effect of a disabled agent appearing first in the file.
    grouped: Dict[str, List[Tuple[str, bool, Dict[str, Any]]]] = {}
    group_order: List[str] = []
    for raw_agent_id, raw_values in agents.items():
        agent_id = str(raw_agent_id)
        if not isinstance(raw_values, Mapping):
            raise ConfigMigrationError(f"{source}: [agents.{agent_id}] must be a table")
        values = dict(raw_values)
        if set(values) <= {"name"}:
            # Display-name-only overrides stay agent-level in v8 (the shape
            # project config uses to rename derived agents).
            new_id = reference_map.get(agent_id, agent_id)
            name_only_agents[new_id] = dict(values)
            continue
        agent_type = values.pop("type", None) or _V7_BUILTIN_AGENTS.get(agent_id, (None,))[0]
        if not isinstance(agent_type, str) or not agent_type:
            raise ConfigMigrationError(
                f"{source}: cannot migrate agents.{agent_id}: its provider type is unknown; "
                "move the settings into [backends.<canonical>] manually"
            )
        backend_kind = (
            values.pop("backend", None) or (_V7_BUILTIN_AGENTS.get(agent_id, (None, "cli"))[1])
        )
        canonical = "mock" if agent_type == "mock" else f"{agent_type}_{backend_kind}"
        enabled = values.pop("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigMigrationError(f"{source}: agents.{agent_id}.enabled must be a boolean")
        if canonical not in grouped:
            grouped[canonical] = []
            group_order.append(canonical)
        grouped[canonical].append((agent_id, enabled, values))

    for canonical in group_order:
        entries = grouped[canonical]
        default_index = 0
        for index, (agent_id, enabled, _values) in enumerate(entries):
            if agent_id == canonical:
                default_index = index
                break
        else:
            for index, (_agent_id, enabled, _values) in enumerate(entries):
                if enabled:
                    default_index = index
                    break
        default_id, default_enabled, default_values = entries[default_index]
        section = backends.setdefault(canonical, {})
        default_agent[canonical] = default_id
        reference_map[default_id] = canonical
        for key, value in default_values.items():
            section.setdefault(key, value)
        policy_enabled = section.get("enabled", True)
        section["enabled"] = bool(policy_enabled) and default_enabled

        for index, (agent_id, enabled, values) in enumerate(entries):
            if index == default_index:
                continue
            # Every other agent on the backend must be expressible as an
            # options-only persona of the folded default.
            if not enabled:
                _logger.warning(
                    "%s: dropping disabled agent %r; backend %s uses agents.%s as its default",
                    source,
                    agent_id,
                    canonical,
                    default_id,
                )
                continue
            # Compare what this agent effectively ran with in v7 (its file
            # values over the built-in defaults for built-in ids) against the
            # folded section, so an omitted key never hides a real difference.
            effective = dict(_V7_BUILTIN_DEFAULTS.get(agent_id, {}))
            effective.update(values)
            conflicting = [
                key
                for key in _V7_EXECUTION_KEYS
                if key in effective and effective[key] != section.get(key)
            ]
            unknown_keys = sorted(set(values) - set(_V7_EXECUTION_KEYS) - {"name", "options"})
            if conflicting or unknown_keys:
                detail = ", ".join(conflicting + unknown_keys)
                raise ConfigMigrationError(
                    f"{source}: cannot migrate agents.{agent_id} automatically: it shares "
                    f"backend {canonical} with agents.{default_id} but differs beyond options "
                    f"({detail}); merge them into one [backends.{canonical}] section with "
                    "nested options-only personae and re-run install"
                )
            persona: Dict[str, Any] = {}
            if "name" in values:
                persona["name"] = values["name"]
            if "options" in values:
                persona["options"] = values["options"]
            section.setdefault("agents", {})[agent_id] = persona
            reference_map[agent_id] = f"{canonical}.{agent_id}"

    # Old policy-only sections: `enabled = true` was the permissive default
    # with no agent attached — in v8 it would activate the backend, so drop it.
    for name in list(backends):
        if name in default_agent:
            continue
        section = backends[name]
        if set(section) <= {"enabled"} and section.get("enabled", True):
            backends.pop(name)

    workflows = data.get("workflows")
    if isinstance(workflows, Mapping):
        rewritten: Dict[str, Any] = {}
        for workflow_id, values in workflows.items():
            if not isinstance(values, Mapping):
                raise ConfigMigrationError(f"{source}: [workflows.{workflow_id}] must be a table")
            updated = dict(values)
            for key in ("sequence", "parallel"):
                members = updated.get(key)
                if isinstance(members, list):
                    updated[key] = [
                        reference_map.get(member, member) if isinstance(member, str) else member
                        for member in members
                    ]
            rewritten[str(workflow_id)] = updated
        data["workflows"] = rewritten

    data.pop("agents", None)
    if name_only_agents:
        data["agents"] = name_only_agents
    if backends:
        data["backends"] = backends
    else:
        data.pop("backends", None)
    return data


def _remap_v7_project_references(data: Dict[str, Any]) -> Dict[str, Any]:
    remap = {agent_id: f"{spec[0]}_{spec[1]}" for agent_id, spec in _V7_BUILTIN_AGENTS.items()}
    agents = data.get("agents")
    if isinstance(agents, Mapping):
        data["agents"] = {remap.get(str(k), str(k)): v for k, v in agents.items()}
    workflows = data.get("workflows")
    if isinstance(workflows, Mapping):
        for values in workflows.values():
            if not isinstance(values, dict):
                continue
            for key in ("sequence", "parallel"):
                members = values.get(key)
                if isinstance(members, list):
                    values[key] = [
                        remap.get(member, member) if isinstance(member, str) else member
                        for member in members
                    ]
    return data
