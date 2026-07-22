"""v1 -> v2 migration step."""

from __future__ import annotations

from typing import Any, Dict

from .base import _logger


def _migrate_v1_to_v2(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v1 is the pre-``schema_version`` config era.

    v2 declares its version explicitly; no field shapes changed, so this only
    stamps the version. Future renames of clearly-old-but-valid fields belong
    in migrations like this one, with a warning for anything they change.
    """

    if "schema_version" not in data:
        _logger.warning("%s: config has no schema_version, assuming version 1", source)
    return data
