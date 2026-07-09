"""Claude CLI backend package."""

from .backend import ClaudeCliBackend
from .parser import parse_claude_line


def build() -> ClaudeCliBackend:
    return ClaudeCliBackend()


__all__ = ["ClaudeCliBackend", "build", "parse_claude_line"]
