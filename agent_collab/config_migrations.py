"""Centralized config shape migrations.

All compatibility handling for old config shapes lives here. ``load_config``
migrates each parsed TOML file to ``CURRENT_CONFIG_SCHEMA`` before merging and
validating, so the rest of the runtime only ever consumes the latest shape.

Migrations are lazy and in-memory: they normalize known old shapes and emit
warnings, but never rewrite files. Ambiguous or unknown data is left in place
for the latest-schema validator to reject.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set

CURRENT_CONFIG_SCHEMA = 6

_logger = logging.getLogger("agent_collab.config")


class ConfigMigrationError(ValueError):
    """Raised when config data cannot be migrated to the current schema."""


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
        result = MIGRATIONS[version](result, label)
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


def _migrate_v1_to_v2(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """v1 is the pre-``schema_version`` config era.

    v2 declares its version explicitly; no field shapes changed, so this only
    stamps the version. Future renames of clearly-old-but-valid fields belong
    in migrations like this one, with a warning for anything they change.
    """

    if "schema_version" not in data:
        _logger.warning("%s: config has no schema_version, assuming version 1", source)
    return data


def _migrate_v2_to_v3(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """v3 binds each agent's option policy to its configured backend."""

    return data


def _migrate_v3_to_v4(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """v4 adds optional daemon-user backend enablement policy."""

    return data


def _migrate_v4_to_v5(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """v5 adds optional user-config [sessions] retention settings."""

    return data


def _migrate_v5_to_v6(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """v6 adds optional user-config [workdir] confinement settings."""

    return data


MIGRATIONS: Dict[int, Callable[[Dict[str, Any], str], Dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
}
