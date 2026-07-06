from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

from .events import Event, utc_timestamp


def _safe_slug(text: str, max_len: int = 42) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "session")[:max_len]


class SessionLogger:
    def __init__(self, base_dir: Path, task: str, session_id: Optional[str] = None):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or self._new_session_id(task)
        self.jsonl_path = self.base_dir / f"{self.session_id}.jsonl"
        self.markdown_path = self.base_dir / f"{self.session_id}.md"
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        self._markdown = self.markdown_path.open("a", encoding="utf-8")
        if self.markdown_path.stat().st_size == 0:
            self._markdown.write(f"# agent-collab session {self.session_id}\n\n")
            self._markdown.flush()

    def _new_session_id(self, task: str) -> str:
        stamp = utc_timestamp().replace(":", "").replace("+00:00", "Z")
        return f"{stamp}-{_safe_slug(task)}"

    def write(self, event: Event) -> None:
        self._jsonl.write(event.to_json() + "\n")
        self._jsonl.flush()
        label = event.source.upper()
        self._markdown.write(f"## {label} `{event.type}`\n\n{event.text}\n\n")
        self._markdown.flush()

    def close(self) -> None:
        self._jsonl.close()
        self._markdown.close()

    def __enter__(self) -> "SessionLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
