"""Antigravity CLI backend package."""

from .backend import AntigravityCliBackend
from .parser import parse_antigravity_line


def build() -> AntigravityCliBackend:
    return AntigravityCliBackend()


__all__ = ["AntigravityCliBackend", "build", "parse_antigravity_line"]
