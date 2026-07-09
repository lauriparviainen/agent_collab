"""Pure backend option-contract data with no registry side effects."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from .config import AgentConfig


OPTION_UNSET = object()


@dataclass(frozen=True)
class OptionSpec:
    """Declarative schema for one option accepted by a backend."""

    type: str
    allowed: Optional[Tuple[Any, ...]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    default: Any = OPTION_UNSET
    inferred: bool = False

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"type": self.type}
        if self.allowed is not None:
            result["allowed"] = list(self.allowed)
        if self.minimum is not None:
            result["min"] = self.minimum
        if self.maximum is not None:
            result["max"] = self.maximum
        if self.default is not OPTION_UNSET:
            result["default"] = self.default
        if self.inferred:
            result["inferred"] = True
        return result


class BackendOptionError(ValueError):
    """A backend rejected a cross-field option combination while normalizing."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}" if field else message)


def normalize_declared_options(
    agent: "AgentConfig",
    requested: Mapping[str, Any],
    schema: Mapping[str, OptionSpec],
    *,
    inferred: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply backend inference, declared/configured defaults, then request data."""

    result = {key: deepcopy(value) for key, value in (inferred or {}).items() if key in schema}
    configured_options = getattr(agent, "options", {})
    for key, spec in schema.items():
        if spec.default is not OPTION_UNSET:
            result[key] = deepcopy(spec.default)
        configured = configured_options.get(key, {}) if isinstance(configured_options, Mapping) else {}
        if isinstance(configured, Mapping) and "default" in configured:
            result[key] = deepcopy(configured["default"])
    for key, value in requested.items():
        if key in schema:
            result[key] = deepcopy(value)
    return result
