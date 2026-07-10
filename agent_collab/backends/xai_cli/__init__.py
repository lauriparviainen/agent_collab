"""xAI Grok Build CLI backend package."""

from .backend import XaiCliBackend
from .parser import XaiStreamingParser, parse_xai_line


def build() -> XaiCliBackend:
    return XaiCliBackend()


__all__ = ["XaiCliBackend", "XaiStreamingParser", "build", "parse_xai_line"]
