"""v5 -> v6 migration step."""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v5_to_v6(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v6 adds optional user-config [workdir] confinement settings."""

    return data
