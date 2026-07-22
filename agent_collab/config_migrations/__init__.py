"""Centralized config shape migrations.

All compatibility handling for old config shapes lives in this package.
``load_config`` migrates each parsed TOML file to ``CURRENT_CONFIG_SCHEMA``
before merging and validating, so the rest of the runtime only ever consumes
the latest shape.

Migrations are lazy and in-memory: they normalize known old shapes and emit
warnings without touching files, so old configs keep loading even when nobody
re-runs install. ``migrate_user_config_file`` is the one deliberate exception:
install calls it to write the user config forward (with a backup) so the file
on disk stays current. Ambiguous or unknown data is left in place for the
latest-schema validator to reject.

Layout: one file per migration step (``v1_to_v2`` … ``v9_to_v10``), each owning
its step's docstring, tables, and helpers. Shared machinery lives in named
modules — :mod:`base` (schema constant + exceptions), :mod:`scope` (the
project-scope filters), :mod:`writeback` (the install-time write-back path).
This module keeps :func:`migrate_config_data` and the :data:`MIGRATIONS`
registry, and re-exports the public surface unchanged so
``agent_collab.config_migrations`` stays the import path every caller uses.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .base import CURRENT_CONFIG_SCHEMA, ConfigError, ConfigMigrationError
from .scope import (
    _filter_project_agents,
    _filter_project_workflows,
    _strip_project_only_sections,
)
from .v1_to_v2 import _migrate_v1_to_v2
from .v2_to_v3 import _migrate_v2_to_v3
from .v3_to_v4 import _migrate_v3_to_v4
from .v4_to_v5 import _migrate_v4_to_v5
from .v5_to_v6 import _migrate_v5_to_v6
from .v6_to_v7 import _migrate_v6_to_v7
from .v7_to_v8 import _migrate_v7_to_v8
from .v8_to_v9 import _migrate_v8_to_v9
from .v9_to_v10 import _migrate_v9_to_v10
from .writeback import UserConfigWriteBack, migrate_user_config_file

__all__ = [
    "CURRENT_CONFIG_SCHEMA",
    "ConfigError",
    "ConfigMigrationError",
    "MIGRATIONS",
    "UserConfigWriteBack",
    "migrate_config_data",
    "migrate_user_config_file",
]


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


# Built from explicit imports of the step modules (no dynamic discovery): each
# entry migrates a config from version N to N+1, keyed by N.
MIGRATIONS: Dict[int, Callable[[Dict[str, Any], str, str], Dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
    7: _migrate_v7_to_v8,
    8: _migrate_v8_to_v9,
    9: _migrate_v9_to_v10,
}
