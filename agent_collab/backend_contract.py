"""Pure backend option-contract data with no registry side effects."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


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


def load_option_schema(path: Path) -> Dict[str, OptionSpec]:
    """Load and validate a backend-owned ``options.toml`` contract."""

    # Lazy import keeps this module a registry-independent leaf.
    from .config import ConfigError, load_toml_file

    data = load_toml_file(path)
    unknown = set(data) - {"schema_version", "options"}
    if unknown:
        raise ConfigError(f"{path}: unknown option manifest field {sorted(unknown)[0]!r}")
    if data.get("schema_version") != 1:
        raise ConfigError(f"{path}: schema_version must be 1")
    options = data.get("options")
    if not isinstance(options, Mapping):
        raise ConfigError(f"{path}: [options] must be a table")

    result: Dict[str, OptionSpec] = {}
    valid_types = {"string", "integer", "boolean"}
    for name, raw in options.items():
        label = f"{path}: options.{name}"
        if not isinstance(name, str) or not name or not isinstance(raw, Mapping):
            raise ConfigError(f"{label} must be a table")
        extra = set(raw) - {"type", "allowed", "min", "max", "default", "inferred"}
        if extra:
            raise ConfigError(f"{label}: unknown field {sorted(extra)[0]!r}")
        option_type = raw.get("type")
        if option_type not in valid_types:
            raise ConfigError(f"{label}.type must be one of {sorted(valid_types)}")
        allowed = raw.get("allowed")
        if allowed is not None and not isinstance(allowed, list):
            raise ConfigError(f"{label}.allowed must be an array")
        minimum = raw.get("min")
        maximum = raw.get("max")
        for key, value in (("min", minimum), ("max", maximum)):
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise ConfigError(f"{label}.{key} must be a number")
        if (minimum is not None or maximum is not None) and option_type != "integer":
            raise ConfigError(f"{label}: min/max are supported only for integer options")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ConfigError(f"{label}: min must be <= max")
        inferred = raw.get("inferred", False)
        if not isinstance(inferred, bool):
            raise ConfigError(f"{label}.inferred must be a boolean")
        spec = OptionSpec(
            option_type,
            allowed=tuple(allowed) if allowed is not None else None,
            minimum=minimum,
            maximum=maximum,
            default=deepcopy(raw["default"]) if "default" in raw else OPTION_UNSET,
            inferred=inferred,
        )
        _validate_manifest_value(spec.default, spec, f"{label}.default", ConfigError)
        if spec.allowed is not None:
            for index, value in enumerate(spec.allowed):
                _validate_manifest_value(value, spec, f"{label}.allowed[{index}]", ConfigError)
        if spec.default is not OPTION_UNSET:
            if spec.allowed is not None and spec.default not in spec.allowed:
                raise ConfigError(f"{label}.default must be one of allowed")
            if spec.minimum is not None and spec.default < spec.minimum:
                raise ConfigError(f"{label}.default must be >= min")
            if spec.maximum is not None and spec.default > spec.maximum:
                raise ConfigError(f"{label}.default must be <= max")
        result[name] = spec
    return result


def _validate_manifest_value(value: Any, spec: OptionSpec, label: str, error_type: type) -> None:
    if value is OPTION_UNSET:
        return
    valid = (
        (spec.type == "string" and isinstance(value, str))
        or (spec.type == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (spec.type == "boolean" and isinstance(value, bool))
    )
    if not valid:
        raise error_type(f"{label} must match declared type {spec.type!r}")


def normalize_declared_options(
    requested: Mapping[str, Any],
    schema: Mapping[str, OptionSpec],
    *,
    configured: Optional[Mapping[str, Any]] = None,
    inferred: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply backend inference, declared/configured defaults, then request data."""

    result = {key: deepcopy(value) for key, value in (inferred or {}).items() if key in schema}
    configured_options = configured or {}
    _validate_values(configured_options, schema)
    _validate_values(requested, schema)
    for key, spec in schema.items():
        if spec.default is not OPTION_UNSET:
            result[key] = deepcopy(spec.default)
        if key in configured_options:
            result[key] = deepcopy(configured_options[key])
    for key, value in requested.items():
        if key in schema:
            result[key] = deepcopy(value)
    return result


def _validate_values(values: Mapping[str, Any], schema: Mapping[str, OptionSpec]) -> None:
    unknown = sorted(set(values) - set(schema))
    if unknown:
        expected = ", ".join(sorted(schema)) or "(none)"
        raise BackendOptionError(
            unknown[0], f"is not declared by this backend; expected one of: {expected}"
        )
    for field, value in values.items():
        spec = schema[field]
        valid_type = (
            (spec.type == "string" and isinstance(value, str))
            or (spec.type == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (spec.type == "boolean" and isinstance(value, bool))
        )
        if not valid_type:
            raise BackendOptionError(field, f"must be a {spec.type}")
        if spec.allowed is not None and value not in spec.allowed:
            raise BackendOptionError(
                field, f"unsupported value {value!r}; expected one of: {', '.join(map(str, spec.allowed))}"
            )
        if spec.minimum is not None and value < spec.minimum:
            raise BackendOptionError(field, f"must be >= {spec.minimum:g}")
        if spec.maximum is not None and value > spec.maximum:
            raise BackendOptionError(field, f"must be <= {spec.maximum:g}")
