from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Callable, Iterable, Optional, Union

from .events import Event, compact_json
from .paths import default_session_log_dirs
from .terminal import print_event


DEFAULT_POLL_INTERVAL = 0.25


def _looks_like_path(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_absolute() or len(path.parts) > 1 or path.suffix == ".jsonl" or path.exists()


def _session_jsonl_name(session_id: str) -> str:
    return session_id if session_id.endswith(".jsonl") else f"{session_id}.jsonl"


def resolve_jsonl_path(
    session_or_path: Optional[Union[str, Path]] = None,
    *,
    workdir: Optional[Path] = None,
    session_id: Optional[str] = None,
    log_dir: Optional[Path] = None,
) -> Path:
    if session_or_path is not None and session_id is not None:
        raise ValueError("provide either SESSION_OR_PATH or --session-id, not both")

    if session_id is None and session_or_path is not None:
        value = str(session_or_path)
        if _looks_like_path(value):
            return Path(value).expanduser().resolve()
        session_id = value

    if log_dir is not None:
        base_dir = log_dir.expanduser().resolve()
        if session_id is None:
            return latest_jsonl_path(base_dir)
        return base_dir / _session_jsonl_name(session_id)

    roots = default_session_log_dirs(workdir or Path("."))
    if session_id is None:
        return latest_jsonl_path_multi(roots)
    name = _session_jsonl_name(session_id)
    for base_dir in roots:
        candidate = base_dir / name
        if candidate.exists():
            return candidate
    return roots[0] / name


def latest_jsonl_path(log_dir: Path) -> Path:
    base_dir = log_dir.expanduser().resolve()
    candidates = [path for path in base_dir.glob("*.jsonl") if path.is_file()]
    if not candidates:
        raise ValueError(f"no session JSONL logs found in {base_dir}")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def latest_jsonl_path_multi(log_dirs: Iterable[Path]) -> Path:
    errors = []
    for log_dir in log_dirs:
        try:
            return latest_jsonl_path(log_dir)
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError("; ".join(errors) if errors else "no session JSONL logs found")


def event_from_jsonl_line(line: str, line_number: int) -> Optional[Event]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return Event.create(
            "error",
            "error",
            f"malformed JSONL line {line_number}: {stripped[:180]}",
            {"line": line},
        )

    if not isinstance(payload, dict):
        return Event.create(
            "error",
            "error",
            f"malformed JSONL line {line_number}: expected object, got {compact_json(payload)}",
            payload,
        )

    required = ("timestamp", "source", "type", "text")
    if any(key not in payload for key in required):
        return Event.create(
            "error",
            "error",
            f"malformed JSONL line {line_number}: {compact_json(payload)}",
            payload,
        )

    return Event(
        timestamp=str(payload["timestamp"]),
        source=str(payload["source"]),
        type=str(payload["type"]),
        text=str(payload["text"]),
        raw=payload.get("raw"),
    )


def iter_jsonl_events(path: Path, start_cursor: int = 0) -> Iterable[Event]:
    cursor = max(0, start_cursor)
    with path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            if line_number <= cursor:
                continue
            event = event_from_jsonl_line(line, line_number)
            if event is not None:
                yield event


def watch_jsonl(
    path: Path,
    follow: bool = True,
    start_cursor: int = 0,
    *,
    color: bool = True,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    printer: Optional[Callable[[Event], None]] = None,
) -> None:
    emit = printer or (lambda event: print_event(event, color=color))
    cursor = max(0, start_cursor)
    line_number = 0

    with path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            if line_number <= cursor:
                continue
            event = event_from_jsonl_line(line, line_number)
            if event is not None:
                emit(event)

        if not follow:
            return

        while True:
            position = file_obj.tell()
            line = file_obj.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            if not line.endswith("\n"):
                file_obj.seek(position)
                time.sleep(poll_interval)
                continue
            line_number += 1
            event = event_from_jsonl_line(line, line_number)
            if event is not None:
                emit(event)
