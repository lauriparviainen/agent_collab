"""v10 -> v11 migration: add the outer-sandbox configuration schema.

The new fields have complete built-in defaults and are intentionally not
written into user configuration.  This migration is therefore a stamp-only
shape migration: omitted values inherit the built-in ``sandbox_default``
(currently ``"read-only"``).
"""

from __future__ import annotations

from typing import Any, Dict


def _migrate_v10_to_v11(
    data: Dict[str, Any], source: str, scope: str = "generic"
) -> Dict[str, Any]:
    del source, scope
    return data
