"""Centralized config shape migrations.

All compatibility handling for old config shapes lives here. ``load_config``
migrates each parsed TOML file to ``CURRENT_CONFIG_SCHEMA`` before merging and
validating, so the rest of the runtime only ever consumes the latest shape.

Migrations are lazy and in-memory: they normalize known old shapes and emit
warnings without touching files, so old configs keep loading even when nobody
re-runs install. ``migrate_user_config_file`` is the one deliberate exception:
install calls it to write the user config forward (with a backup) so the file
on disk stays current. Ambiguous or unknown data is left in place for the
latest-schema validator to reject.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

CURRENT_CONFIG_SCHEMA = 8

_logger = logging.getLogger("agent_collab.config")


class ConfigError(ValueError):
    """Raised when agent-collab configuration is invalid.

    Defined here so ``ConfigMigrationError`` can subclass it without a
    circular import; ``agent_collab.config`` re-exports it as the public name.
    """


class ConfigMigrationError(ConfigError):
    """Raised when config data cannot be migrated to the current schema.

    Subclassing ``ConfigError`` keeps every ``except ConfigError`` fail-safe
    (daemon retention, sanitized session-start errors) working for migration
    failures too.
    """


def migrate_config_data(
    data: Mapping[str, Any],
    source: str = "",
    *,
    scope: str = "generic",
    global_agent_ids: Iterable[str] = (),
    enabled_global_agent_ids: Iterable[str] = (),
    warnings: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Return a new dict migrated to ``CURRENT_CONFIG_SCHEMA``.

    Never mutates ``data``. Missing ``schema_version`` means version 1.
    """

    label = source or "config"
    result: Dict[str, Any] = copy.deepcopy(dict(data))
    version = result.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigMigrationError(f"{label}: schema_version must be an integer")
    if version < 1:
        raise ConfigMigrationError(f"{label}: schema_version must be >= 1")
    if version > CURRENT_CONFIG_SCHEMA:
        raise ConfigMigrationError(
            f"{label}: schema_version {version} is newer than supported schema {CURRENT_CONFIG_SCHEMA}"
        )

    while version < CURRENT_CONFIG_SCHEMA:
        result = MIGRATIONS[version](result, label, scope)
        version += 1
        result["schema_version"] = version
    result["schema_version"] = CURRENT_CONFIG_SCHEMA
    if scope == "project":
        _strip_project_only_sections(result, label, warnings)
        _filter_project_agents(result, label, set(global_agent_ids), warnings)
        _filter_project_workflows(
            result,
            label,
            set(enabled_global_agent_ids),
            warnings,
        )
    return result


def _warning(
    label: str,
    path: str,
    message: str,
    warnings: Optional[List[Dict[str, str]]],
) -> None:
    rendered = f"{label}: {message}"
    _logger.warning("%s", rendered)
    if warnings is not None:
        warnings.append(
            {
                "code": "ignored_project_config",
                "path": path,
                "message": message,
            }
        )


def _strip_project_only_sections(
    result: Dict[str, Any],
    label: str,
    warnings: Optional[List[Dict[str, str]]],
) -> None:
    sections = {
        "backends": "backend enablement is allowed only in the user config",
        "daemon": "the daemon token is allowed only in the user config",
        "sessions": "session retention is allowed only in the user config",
        "workdir": "workdir policy is allowed only in the user config",
    }
    for section, reason in sections.items():
        if section not in result:
            continue
        display = "[backends.*] policy" if section == "backends" else f"[{section}] section"
        _warning(
            label,
            section,
            f"ignoring project {display}; {reason}",
            warnings,
        )
        result.pop(section, None)


def _filter_project_agents(
    result: Dict[str, Any],
    label: str,
    global_agent_ids: Set[str],
    warnings: Optional[List[Dict[str, str]]],
) -> None:
    agents = result.get("agents")
    if not isinstance(agents, Mapping):
        return
    filtered: Dict[str, Any] = {}
    for raw_agent_id, values in agents.items():
        agent_id = str(raw_agent_id)
        path = f"agents.{agent_id}"
        if agent_id not in global_agent_ids:
            _warning(
                label,
                path,
                f"ignoring project-only agent {agent_id!r}; define agents in the user config",
                warnings,
            )
            continue
        if not isinstance(values, Mapping):
            _warning(
                label,
                path,
                f"ignoring malformed project agent {agent_id!r}",
                warnings,
            )
            continue
        ignored = {str(key) for key in values if str(key) != "name"}
        if ignored:
            known_fields = {
                "type",
                "command",
                "args",
                "enabled",
                "env",
                "cwd",
                "timeout",
                "options",
                "backend",
            }
            categories = sorted(ignored & known_fields)
            if ignored - known_fields:
                categories.append("backend-specific fields")
            _warning(
                label,
                path,
                (
                    f"ignoring execution-relevant fields for project agent {agent_id!r}: "
                    + ", ".join(categories)
                    + "; define them in the user config"
                ),
                warnings,
            )
        if "name" in values:
            filtered[agent_id] = {"name": values["name"]}
    if filtered:
        result["agents"] = filtered
    else:
        result.pop("agents", None)


def _filter_project_workflows(
    result: Dict[str, Any],
    label: str,
    enabled_global_agent_ids: Set[str],
    warnings: Optional[List[Dict[str, str]]],
) -> None:
    workflows = result.get("workflows")
    if not isinstance(workflows, Mapping):
        return
    filtered: Dict[str, Any] = {}
    for raw_workflow_id, values in workflows.items():
        workflow_id = str(raw_workflow_id)
        path = f"workflows.{workflow_id}"
        if not isinstance(values, Mapping):
            _warning(
                label,
                path,
                f"ignoring malformed project workflow {workflow_id!r}",
                warnings,
            )
            continue
        sequence = values.get("sequence")
        if (
            set(values) != {"sequence"}
            or not isinstance(sequence, list)
            or not sequence
            or not all(isinstance(item, str) for item in sequence)
        ):
            _warning(
                label,
                path,
                f"ignoring malformed project workflow {workflow_id!r}",
                warnings,
            )
            continue
        unavailable = sorted(set(sequence) - enabled_global_agent_ids)
        if unavailable:
            _warning(
                label,
                path,
                (
                    f"ignoring project workflow {workflow_id!r}; it references agents not "
                    "enabled by built-in or user config: " + ", ".join(unavailable)
                ),
                warnings,
            )
            continue
        filtered[workflow_id] = values
    if filtered:
        result["workflows"] = filtered
    else:
        result.pop("workflows", None)


def _migrate_v1_to_v2(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v1 is the pre-``schema_version`` config era.

    v2 declares its version explicitly; no field shapes changed, so this only
    stamps the version. Future renames of clearly-old-but-valid fields belong
    in migrations like this one, with a warning for anything they change.
    """

    if "schema_version" not in data:
        _logger.warning("%s: config has no schema_version, assuming version 1", source)
    return data


def _migrate_v2_to_v3(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v3 binds each agent's option policy to its configured backend."""

    return data


def _migrate_v3_to_v4(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v4 adds optional daemon-user backend enablement policy."""

    return data


def _migrate_v4_to_v5(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v5 adds optional user-config [sessions] retention settings."""

    return data


def _migrate_v5_to_v6(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v6 adds optional user-config [workdir] confinement settings."""

    return data


def _migrate_v6_to_v7(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v7 adds flat parallel workflows."""

    return data


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


MIGRATIONS: Dict[int, Callable[[Dict[str, Any], str, str], Dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
    7: _migrate_v7_to_v8,
}


@dataclass(frozen=True)
class UserConfigWriteBack:
    """Result of ``migrate_user_config_file``."""

    status: str  # "absent" | "current" | "migrated"
    path: Path
    backup_path: Optional[Path] = None
    previous_version: Optional[int] = None
    permissions_fixed: bool = False


def migrate_user_config_file(path: Path) -> UserConfigWriteBack:
    """Migrate the user config file on disk to ``CURRENT_CONFIG_SCHEMA``.

    Write-back is the install-time convenience on top of the lazy in-memory
    layer, never a replacement for it. The original file is backed up to
    ``<name>.bak`` first, and user comments and formatting are preserved: an
    existing ``schema_version`` value is updated through tomlkit, a missing
    one is prepended as text. Every migration so far only stamps the version;
    a future shape-changing migration must implement a comment-preserving
    counterpart here before it ships.
    """

    path = path.expanduser()
    if not path.exists():
        return UserConfigWriteBack(status="absent", path=path)
    # Operate on the symlink target so a dotfile-managed config keeps its
    # link: os.replace on the symlink path itself would sever it.
    path = path.resolve()
    from .config import load_toml_file
    from .paths import atomic_write_private_text

    text = path.read_text(encoding="utf-8")
    data = load_toml_file(path)
    migrated = migrate_config_data(data, source=str(path), scope="user")
    permissions_fixed = _tighten_private_permissions(path)
    raw_version = data.get("schema_version", 1)
    if raw_version == CURRENT_CONFIG_SCHEMA:
        return UserConfigWriteBack(status="current", path=path, permissions_fixed=permissions_fixed)

    backup_path = path.with_name(path.name + ".bak")
    atomic_write_private_text(backup_path, text)
    if int(raw_version) < 8:
        new_text = _rewrite_backend_first(text, migrated, path)
    elif "schema_version" in data:
        new_text = _stamp_schema_version(text, path)
    else:
        new_text = f"schema_version = {CURRENT_CONFIG_SCHEMA}\n\n{text}"
    atomic_write_private_text(path, new_text)
    return UserConfigWriteBack(
        status="migrated",
        path=path,
        backup_path=backup_path,
        previous_version=int(raw_version),
        permissions_fixed=permissions_fixed,
    )


def _tighten_private_permissions(path: Path) -> bool:
    """Chmod a group/world-readable user config to 0600.

    The file can hold the daemon bearer token; a restored backup or copy made
    with a loose umask must not stay world-readable just because its schema
    is already current.
    """

    import os
    import stat

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            return True
    except OSError:
        pass
    return False


def _rewrite_backend_first(text: str, migrated: Mapping[str, Any], path: Path) -> str:
    """Rewrite a pre-v8 user config to the backend-first shape.

    Sections the migration does not touch (daemon, sessions, workdir) keep
    their comments and formatting; the agents/backends/workflows sections are
    regenerated from the migrated data. This structural rewrite requires
    tomlkit — install fails with a clear error rather than guessing.
    """

    try:
        import tomlkit
    except ImportError:
        raise ConfigMigrationError(
            f"{path}: migrating to the backend-first schema (v{CURRENT_CONFIG_SCHEMA}) requires "
            "tomlkit; install it with: pip install tomlkit"
        ) from None
    document = tomlkit.parse(text)
    for key in ("agents", "backends", "workflows"):
        if key in document:
            del document[key]
    had_version = "schema_version" in document
    if had_version:
        document["schema_version"] = CURRENT_CONFIG_SCHEMA
    for key in ("agents", "backends", "workflows"):
        value = migrated.get(key)
        if value:
            document[key] = tomlkit.item(value)
    rendered = tomlkit.dumps(document)
    if not had_version:
        # Appending a top-level scalar after tables would parse inside the
        # last table; a missing version is prepended as text instead.
        rendered = f"schema_version = {CURRENT_CONFIG_SCHEMA}\n\n{rendered}"
    return rendered


def _stamp_schema_version(text: str, path: Path) -> str:
    """Update an existing ``schema_version`` value, preserving everything else.

    tomlkit is the primary, fully style-preserving writer. The regex fallback
    is exactly equivalent while every migration only stamps the version — it
    lets a bootstrap Python without tomlkit (fresh machine, dotfile-carried
    old config) still complete install. The first shape-changing migration
    must drop the fallback and require tomlkit here.
    """

    try:
        import tomlkit
    except ImportError:
        import re

        from .config import load_toml_text

        new_text, count = re.subn(
            r"(?m)^(\s*schema_version\s*=\s*)\d+",
            lambda match: f"{match.group(1)}{CURRENT_CONFIG_SCHEMA}",
            text,
            count=1,
        )
        # The single replacement may have hit a lookalike line inside a
        # multi-line string instead of the real top-level key; reparsing
        # proves the stamp landed (one replacement cannot do both).
        if (
            count != 1
            or load_toml_text(new_text, source=str(path)).get("schema_version")
            != CURRENT_CONFIG_SCHEMA
        ):
            raise ConfigMigrationError(
                f"{path}: could not safely update schema_version without tomlkit; "
                "install it with: pip install tomlkit"
            )
        return new_text
    document = tomlkit.parse(text)
    document["schema_version"] = CURRENT_CONFIG_SCHEMA
    return tomlkit.dumps(document)
