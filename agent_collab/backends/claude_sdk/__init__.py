"""Claude SDK backend package."""

from .backend import ClaudeSdkBackend


def build() -> ClaudeSdkBackend:
    return ClaudeSdkBackend()


__all__ = ["ClaudeSdkBackend", "build"]
