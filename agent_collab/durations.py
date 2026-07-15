"""Shared whole-number duration parsing primitives."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping


def parse_whole_duration(
    text: Any,
    *,
    units: Mapping[str, timedelta],
    allow_zero: bool,
    example: str,
    units_description: str | None = None,
    string_description: str | None = None,
) -> timedelta:
    """Parse ``<whole number><unit>`` with a caller-owned grammar."""

    if not isinstance(text, str):
        raise ValueError(f"duration must be a string like {string_description or example}")
    value = text.strip()
    number, unit = value[:-1], value[-1:]
    unit_names = units_description or ", ".join(units)
    if unit not in units or not number.isascii() or not number.isdigit():
        raise ValueError(
            f"invalid duration {text!r}; use a whole number with {unit_names} (e.g. {example})"
        )
    count = int(number)
    minimum = 0 if allow_zero else 1
    if count < minimum:
        raise ValueError(f"invalid duration {text!r}; the value must be at least {minimum}")
    return count * units[unit]
