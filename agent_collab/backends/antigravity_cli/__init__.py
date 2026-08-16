"""Antigravity CLI backend package."""

from .backend import AntigravityCliBackend
from .parser import AntigravityStreamingParser, parse_antigravity_line


def build() -> AntigravityCliBackend:
    return AntigravityCliBackend()


__all__ = [
    "AntigravityCliBackend",
    "AntigravityStreamingParser",
    "build",
    "parse_antigravity_line",
]
