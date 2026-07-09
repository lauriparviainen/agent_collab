"""Codex CLI backend package."""

from .backend import CodexCliBackend
from .parser import parse_codex_line


def build() -> CodexCliBackend:
    return CodexCliBackend()


__all__ = ["CodexCliBackend", "build", "parse_codex_line"]
