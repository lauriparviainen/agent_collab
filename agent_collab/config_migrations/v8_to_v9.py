"""v8 -> v9 migration step."""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v8_to_v9(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v9 adds global system and usage-window alignment policy."""

    return data
