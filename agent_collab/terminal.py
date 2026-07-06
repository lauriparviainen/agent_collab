from __future__ import annotations

import sys

from .events import Event


COLORS = {
    "human": "\033[36m",
    "referee": "\033[35m",
    "claude": "\033[34m",
    "codex": "\033[32m",
    "tool": "\033[33m",
    "error": "\033[31m",
}
RESET = "\033[0m"


def print_event(event: Event, color: bool = True) -> None:
    label = event.source.upper()
    prefix = f"{label:<7}"
    if color and sys.stdout.isatty():
        prefix = f"{COLORS.get(event.source, '')}{prefix}{RESET}"
    for index, line in enumerate((event.text or "").splitlines() or [""]):
        if index == 0:
            print(f"{prefix} {line}", flush=True)
        else:
            print(f"{'':<7} {line}", flush=True)
