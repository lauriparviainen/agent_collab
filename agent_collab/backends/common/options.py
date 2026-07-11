"""Pure cross-field rules shared by backends that expose equivalent options."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from ...backend_contract import BackendOptionError


def highest_precedence_choices(
    fields: tuple[str, ...], *layers: Mapping[str, Any]
) -> Dict[str, Any]:
    """Return relevant values from the highest-precedence layer that selects one."""

    for layer in reversed(layers):
        selected = {field: layer[field] for field in fields if field in layer}
        if selected:
            return selected
    return {}


def configured_choices(
    configured: Mapping[str, Any], requested: Mapping[str, Any]
) -> Dict[str, Any]:
    """Fields deliberately selected by config defaults or the current request."""

    result: Dict[str, Any] = {}
    result.update(configured)
    result.update(requested)
    return result


def resolve_claude_thinking(
    options: Mapping[str, Any], requested: Mapping[str, Any]
) -> Dict[str, Any]:
    result = dict(options)
    explicit = set(requested)
    if {"thinking_level", "thinking_budget_tokens"}.issubset(explicit):
        raise BackendOptionError(
            "thinking_level",
            "conflicts with thinking_budget_tokens; use thinking_level or a raw token budget, not both",
        )
    if "thinking_budget_tokens" in explicit:
        result.pop("thinking_level", None)
    elif "thinking_level" in explicit:
        result.pop("thinking_budget_tokens", None)
    return result


def resolve_codex_effort(
    options: Mapping[str, Any], requested: Mapping[str, Any]
) -> Dict[str, Any]:
    result = dict(options)
    explicit = set(requested)
    if {"thinking_level", "reasoning_effort"}.issubset(explicit):
        if result.get("thinking_level") != result.get("reasoning_effort"):
            raise BackendOptionError(
                "thinking_level",
                "conflicts with reasoning_effort; use one thinking level field or provide matching values",
            )
    if "thinking_level" in explicit:
        result["reasoning_effort"] = result["thinking_level"]
    elif "reasoning_effort" in explicit:
        result["thinking_level"] = result["reasoning_effort"]
    elif "reasoning_effort" in result:
        result["thinking_level"] = result["reasoning_effort"]
    elif "thinking_level" in result:
        result["reasoning_effort"] = result["thinking_level"]
    return result
