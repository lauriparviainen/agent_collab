"""v9 -> v10 migration step: retire the Antigravity display-name namespace.

Maps every Antigravity display-name model value any earlier release shipped to
the canonical id ``agy models`` emits. ``_apply_antigravity_model_renames`` is
shared: the in-memory migration here calls it, and the comment-preserving
write-back imports the same function so the two can never drift.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .base import _logger

# v10 retires the Antigravity display-name model namespace. The shipped
# manifests and defaults now use the canonical ids that ``agy models`` emits;
# this table maps every display name any earlier release ever shipped (the
# full historical set — verified against the manifest git history) to its
# canonical id. Values outside this table pass through unchanged: the model
# option is a free-form string and an unknown value is the user's own choice,
# never something a migration may guess about.
_ANTIGRAVITY_MODEL_RENAMES: Dict[str, str] = {
    "Gemini 3.6 Flash (Medium)": "gemini-3.6-flash-medium",
    "Gemini 3.6 Flash (High)": "gemini-3.6-flash-high",
    "Gemini 3.6 Flash (Low)": "gemini-3.6-flash-low",
    "Gemini 3.5 Flash (Medium)": "gemini-3.5-flash-medium",
    "Gemini 3.5 Flash (High)": "gemini-3.5-flash-high",
    "Gemini 3.5 Flash (Low)": "gemini-3.5-flash-low",
    "Gemini 3.1 Pro (Low)": "gemini-3.1-pro-low",
    "Gemini 3.1 Pro (High)": "gemini-3.1-pro-high",
    # The Antigravity UI labels Sonnet 4.6 "(Thinking)" but its catalog id
    # carries no suffix; Opus 4.6 keeps the "-thinking" suffix in the catalog.
    "Claude Sonnet 4.6 (Thinking)": "claude-sonnet-4-6",
    "Claude Opus 4.6 (Thinking)": "claude-opus-4-6-thinking",
    "GPT-OSS 120B (Medium)": "gpt-oss-120b-medium",
}
_ANTIGRAVITY_BACKENDS = ("antigravity_cli", "antigravity_sdk")


def _apply_antigravity_model_renames(root: Any) -> List[str]:
    """Rewrite known display-name model values to canonical ids in place.

    Works on plain dicts (in-memory migration) and tomlkit documents (the
    comment-preserving write-back) alike — both are mutable mappings. Returns
    a human-readable description per rename; an empty list means the config
    carried no display-name model values.
    """

    renamed: List[str] = []

    def rename(container: Any, key: str, location: str) -> None:
        value = container.get(key)
        if isinstance(value, str) and value in _ANTIGRAVITY_MODEL_RENAMES:
            replacement = _ANTIGRAVITY_MODEL_RENAMES[value]
            container[key] = replacement
            renamed.append(f"{location}: {value!r} -> {replacement!r}")

    backends = root.get("backends") if isinstance(root, Mapping) else None
    if isinstance(backends, Mapping):
        for canonical in _ANTIGRAVITY_BACKENDS:
            section = backends.get(canonical)
            if not isinstance(section, Mapping):
                continue
            options = section.get("options")
            if isinstance(options, Mapping):
                rename(options, "model", f"backends.{canonical}.options.model")
            personae = section.get("agents")
            if isinstance(personae, Mapping):
                for persona_id, persona in personae.items():
                    if not isinstance(persona, Mapping):
                        continue
                    persona_options = persona.get("options")
                    if isinstance(persona_options, Mapping):
                        rename(
                            persona_options,
                            "model",
                            f"backends.{canonical}.agents.{persona_id}.options.model",
                        )
    usage_windows = root.get("usage_windows") if isinstance(root, Mapping) else None
    if isinstance(usage_windows, Mapping):
        targets = usage_windows.get("targets")
        if isinstance(targets, Mapping):
            for target_id, target in targets.items():
                if not isinstance(target, Mapping):
                    continue
                if target.get("backend") in _ANTIGRAVITY_BACKENDS:
                    rename(target, "model", f"usage_windows.targets.{target_id}.model")
    return renamed


def _migrate_v9_to_v10(data: Dict[str, Any], source: str, scope: str = "generic") -> Dict[str, Any]:
    """v10 retires Antigravity display-name model values for canonical ids."""

    for description in _apply_antigravity_model_renames(data):
        _logger.warning("%s: migrated antigravity model name %s", source, description)
    return data
