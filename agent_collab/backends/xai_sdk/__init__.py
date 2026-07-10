"""xAI Python SDK backend package."""

from .backend import XaiSdkBackend


def build() -> XaiSdkBackend:
    return XaiSdkBackend()


__all__ = ["XaiSdkBackend", "build"]
