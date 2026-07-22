"""v2 -> v3 migration step."""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v2_to_v3(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v3 binds each agent's option policy to its configured backend."""

    return data
