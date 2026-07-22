"""v4 -> v5 migration step."""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v4_to_v5(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v5 adds optional user-config [sessions] retention settings."""

    return data
