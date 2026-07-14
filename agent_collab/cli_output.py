"""User-facing CLI output markers and formatting.

The marker vocabulary is fixed by ``.claude/skills/cli-scripting/SKILL.md``:
``▶`` progress, ``ⓘ Info:`` neutral information, ``✓`` success,
``! Warning:`` non-fatal warnings, ``✗`` non-fatal failed checks, and a
grep-friendly ``Error:`` prefix for fatal errors that exit non-zero.
"""

from __future__ import annotations

import sys
from typing import Any, Optional, Sequence, Tuple


def step(message: str) -> None:
    print(f"▶ {message}", flush=True)


def ok(message: str) -> None:
    print(f"✓ {message}", flush=True)


def info(message: str) -> None:
    print(f"ⓘ Info: {message}", flush=True)


def warn(message: str) -> None:
    print(f"! Warning: {message}", flush=True)


def fail(message: str) -> None:
    print(f"✗ {message}", flush=True)


def error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr, flush=True)


def print_kv(pairs: Sequence[Tuple[str, Any]], indent: str = "  ") -> None:
    """Print an aligned key/value block: keys padded to a common width."""

    rendered = [(key, str(value)) for key, value in pairs if value is not None]
    if not rendered:
        return
    width = max(len(key) for key, _ in rendered)
    for key, value in rendered:
        print(f"{indent}{key.ljust(width)}  {value}", flush=True)


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    indent: str = "  ",
    max_widths: Optional[Sequence[int]] = None,
) -> Tuple[str, ...]:
    """Return a compact, dependency-free plain-text table."""

    if not headers:
        return ()
    column_count = len(headers)
    if max_widths is not None and len(max_widths) != column_count:
        raise ValueError("max_widths must have one entry per table column")
    rendered_rows = [_table_row(row, column_count) for row in rows]
    rendered_headers = _table_row(headers, column_count)
    widths = []
    for index in range(column_count):
        natural = max([len(rendered_headers[index]), *(len(row[index]) for row in rendered_rows)])
        widths.append(min(natural, max_widths[index]) if max_widths is not None else natural)

    def line(values: Sequence[str]) -> str:
        cells = [
            _truncate_table_cell(value, widths[index]).ljust(widths[index])
            for index, value in enumerate(values)
        ]
        return indent + "  ".join(cells).rstrip()

    separator = indent + "  ".join("-" * width for width in widths)
    return (line(rendered_headers), separator, *(line(row) for row in rendered_rows))


def print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    indent: str = "  ",
    max_widths: Optional[Sequence[int]] = None,
) -> None:
    """Print :func:`format_table` with the standard CLI flushing behavior."""

    for line in format_table(headers, rows, indent=indent, max_widths=max_widths):
        print(line, flush=True)


def _table_row(row: Sequence[Any], column_count: int) -> Tuple[str, ...]:
    if len(row) != column_count:
        raise ValueError("every table row must have the same number of columns as headers")
    return tuple(" ".join(str(value).splitlines()).strip() for value in row)


def _truncate_table_cell(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    return value[: width - 1] + "…"
