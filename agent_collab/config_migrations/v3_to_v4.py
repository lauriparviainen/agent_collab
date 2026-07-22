"""v3 -> v4 migration step."""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v3_to_v4(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v4 adds optional daemon-user backend enablement policy."""

    return data
