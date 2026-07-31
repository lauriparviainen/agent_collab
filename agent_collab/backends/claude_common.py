"""Shared Claude Code filesystem facts used by CLI and SDK adapters."""

from __future__ import annotations

import os
from pathlib import Path

from ..sandbox.specs import SandboxContext


def claude_accounting_peer_root(context: SandboxContext) -> Path:
    """Return Claude Code's host tool-result temp tree."""

    inherited = context.inherited_environment
    raw_temp = inherited.get("CLAUDE_CODE_TMPDIR")
    if not raw_temp:
        raw_temp = (
            inherited.get("TMPDIR") or inherited.get("TMP") or inherited.get("TEMP") or "/tmp"
        )
    temp_path = Path(raw_temp)
    if not temp_path.is_absolute():
        temp_path = context.cwd / temp_path
    return Path(os.path.abspath(temp_path)) / f"claude-{os.getuid()}"
