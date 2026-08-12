"""v11 -> v12 migration: rename packaged xAI Event Window target ids.

Grok 4.6 replaces 4.5 as the shipped xAI default. Packaged Event Window
targets are named for the model they call, so ``xai_cli_grok_4_5`` /
``xai_sdk_grok_4_5`` become ``xai_cli_grok_4_6`` / ``xai_sdk_grok_4_6``.

An enable-only leftover table for the old id would otherwise fail config
load: merge creates a new target when the packaged id is gone, and
validation then requires ``backend`` and ``model``. This step remaps the
old ids so those inherit tables keep working against the new packaged
defaults. An explicit ``model`` on the old table is left unchanged — that
is user ownership, not a packaged default.

``_apply_xai_event_window_target_renames`` is shared: the in-memory
migration here calls it, and the comment-preserving write-back imports the
same function so the two cannot drift.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .base import ConfigMigrationError, _logger

_XAI_EVENT_WINDOW_TARGET_RENAMES = {
    "xai_cli_grok_4_5": "xai_cli_grok_4_6",
    "xai_sdk_grok_4_5": "xai_sdk_grok_4_6",
}


def _apply_xai_event_window_target_renames(root: Any, source: str = "") -> List[str]:
    """Rename packaged xAI Event Window target ids in place.

    Works on plain dicts (in-memory migration) and tomlkit documents (the
    comment-preserving write-back) alike — both are mutable mappings.
    Returns a human-readable description per rename; an empty list means the
    config carried none of the retired ids. Both old and new ids together is
    a conflict: fail closed rather than guess which table to keep.
    """

    usage_windows = root.get("usage_windows") if isinstance(root, Mapping) else None
    if not isinstance(usage_windows, Mapping):
        return []
    targets = usage_windows.get("targets")
    if not isinstance(targets, Mapping):
        return []

    label = source or "config"
    for old_id, new_id in _XAI_EVENT_WINDOW_TARGET_RENAMES.items():
        if old_id in targets and new_id in targets:
            raise ConfigMigrationError(
                f"{label}: cannot migrate usage_windows.targets.{old_id} to "
                f"{new_id} because both tables exist; keep one and delete the other"
            )

    renamed: List[str] = []
    for old_id, new_id in _XAI_EVENT_WINDOW_TARGET_RENAMES.items():
        if old_id not in targets:
            continue
        targets[new_id] = targets[old_id]
        del targets[old_id]
        renamed.append(f"usage_windows.targets.{old_id} -> {new_id}")
    return renamed


def _migrate_v11_to_v12(
    data: Dict[str, Any], source: str, scope: str = "generic"
) -> Dict[str, Any]:
    """v12 remaps packaged xAI Event Window target ids for Grok 4.6."""

    del scope
    label = source or "config"
    for description in _apply_xai_event_window_target_renames(data, source):
        _logger.warning("%s: migrated event-window target %s", label, description)
    return data
