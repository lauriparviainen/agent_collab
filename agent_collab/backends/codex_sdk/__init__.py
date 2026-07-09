"""Codex SDK backend package."""

from .backend import CodexSdkBackend


def build() -> CodexSdkBackend:
    return CodexSdkBackend()


__all__ = ["CodexSdkBackend", "build"]
