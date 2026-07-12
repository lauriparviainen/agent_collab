"""Persistent session index for the global daemon.

One JSON file under the global data root holds the latest SessionState
record per session, so list/status survive daemon restarts. The daemon is
the single writer; writes are atomic replaces of the whole file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable

INDEX_VERSION = 1


class SessionIndex:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Return session_id -> state dict; missing or corrupt files yield {}."""

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, dict):
            return {}
        return {
            str(session_id): dict(state)
            for session_id, state in sessions.items()
            if isinstance(state, dict)
        }

    def upsert(self, state: Dict[str, Any]) -> None:
        session_id = str(state.get("session_id", ""))
        if not session_id:
            raise ValueError("session state must include session_id")
        sessions = self.load()
        sessions[session_id] = dict(state)
        self._write(sessions)

    def remove_many(self, session_ids: Iterable[Any]) -> None:
        """Remove the given ids in one atomic rewrite; unknown ids are no-ops."""

        ids = {str(session_id) for session_id in session_ids}
        if not ids:
            return
        sessions = self.load()
        remaining = {
            session_id: state for session_id, state in sessions.items() if session_id not in ids
        }
        if len(remaining) == len(sessions):
            return
        self._write(remaining)

    def _write(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        payload = {"version": INDEX_VERSION, "sessions": sessions}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)
