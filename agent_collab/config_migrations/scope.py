"""Project-scope filters applied after the version-migration loop.

Project config is untrusted relative to the user config: it may only rename
built-in agents and define workflows over already-enabled agents. These filters
strip the sections a project file is never allowed to carry and drop project
agents/workflows that reach beyond what the user config enabled, emitting a
warning for each thing removed. They run once, after ``migrate_config_data``
has brought the data to the current schema.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

from .base import _logger


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
        "system": "system settings are allowed only in the global user config",
        "usage_windows": ("scheduled provider calls are allowed only in the global user config"),
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
