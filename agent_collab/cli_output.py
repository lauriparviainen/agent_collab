"""User-facing CLI output markers and formatting.

The marker vocabulary is fixed by ``.claude/skills/cli-scripting/SKILL.md``:
``▶`` progress, ``ⓘ Info:`` neutral information, ``✓`` success,
``! Warning:`` non-fatal warnings, ``✗`` non-fatal failed checks, and a
grep-friendly ``Error:`` prefix for fatal errors that exit non-zero.
"""

from __future__ import annotations

import sys
from typing import Any, Sequence, Tuple


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
