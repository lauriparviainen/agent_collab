"""v6 -> v7 migration step."""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v6_to_v7(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v7 adds flat parallel workflows."""

    return data
