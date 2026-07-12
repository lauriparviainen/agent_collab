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
from typing import Any, Callable, Dict, Mapping

CURRENT_CONFIG_SCHEMA = 5

_logger = logging.getLogger("agent_collab.config")


class ConfigMigrationError(ValueError):
    """Raised when config data cannot be migrated to the current schema."""


def migrate_config_data(
    data: Mapping[str, Any],
    source: str = "",
    *,
    scope: str = "generic",
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
    if scope == "project" and "backends" in result:
        _logger.warning(
            "%s: ignoring project [backends.*] policy; backend enablement is allowed only in the user config",
            label,
        )
        result.pop("backends", None)
    if scope == "project" and "daemon" in result:
        _logger.warning(
            "%s: ignoring project [daemon] section; the daemon token is allowed only in the user config",
            label,
        )
        result.pop("daemon", None)
    if scope == "project" and "sessions" in result:
        _logger.warning(
            "%s: ignoring project [sessions] section; session retention is allowed only in the user config",
            label,
        )
        result.pop("sessions", None)
    return result


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


MIGRATIONS: Dict[int, Callable[[Dict[str, Any], str], Dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
}
