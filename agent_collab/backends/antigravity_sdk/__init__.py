"""Antigravity SDK backend package."""

from .backend import AntigravitySdkBackend


def build() -> AntigravitySdkBackend:
    return AntigravitySdkBackend()


__all__ = ["AntigravitySdkBackend", "build"]
