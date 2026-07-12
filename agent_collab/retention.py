"""Pure session retention selection, duration parsing, and path ownership.

Everything here is side-effect-free so boundary tests inject the clock and
never depend on wall time. `SessionManager.prune_sessions` consumes the plans
this module produces; no filesystem mutation lives here. This module is also
the canonical home of the session status constants — `daemon.py` imports them
from here so the two can never drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
import stat as stat_module
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Tuple

RUNNING = "running"
AWAITING_INPUT = "awaiting_input"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
INTERRUPTED = "interrupted"
TERMINAL_STATUSES = {DONE, FAILED, STOPPED, INTERRUPTED}
LIVE_WAIT_STATUSES = {RUNNING, AWAITING_INPUT}

_DURATION_UNITS = {
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


def parse_duration(text: Any) -> timedelta:
    """Parse whole-number durations like ``12h``, ``7d``, or ``2w``.

    Rejects zero, negative, fractional, unitless, and unknown-unit values.
    Rejecting zero is deliberate: "prune everything terminal right now" should
    not be one typo away.
    """

    if not isinstance(text, str):
        raise ValueError("duration must be a string like 7d (whole-number h, d, or w)")
    value = text.strip()
    number, unit = value[:-1], value[-1:]
    if unit not in _DURATION_UNITS or not number.isascii() or not number.isdigit():
        raise ValueError(f"invalid duration {text!r}; use a whole number with h, d, or w (e.g. 7d)")
    count = int(number)
    if count < 1:
        raise ValueError(f"invalid duration {text!r}; the value must be at least 1")
    return count * _DURATION_UNITS[unit]


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp; None when missing or unusable.

    The daemon always writes aware UTC ISO strings (`events.utc_timestamp`).
    A naive value is defensively treated as UTC instead of raising on
    aware/naive comparison.
    """

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def effective_timestamp(record: Mapping[str, Any]) -> Optional[datetime]:
    """The retention timestamp: ``ended_at``, else legacy ``updated_at``.

    ``created_at`` and file mtimes are deliberately never used; guessing could
    delete data prematurely.
    """

    return parse_timestamp(record.get("ended_at")) or parse_timestamp(record.get("updated_at"))


@dataclass(frozen=True)
class RetentionCandidate:
    session_id: str
    status: str
    effective_at: datetime


@dataclass
class RetentionSelection:
    """Terminal sessions partitioned for one prune evaluation.

    ``expired`` is eligible for pruning; ``kept`` would be expired but is
    protected by the keep count; ``skipped_no_timestamp`` is terminal but has
    no usable timestamp and is always preserved. Live sessions never appear.
    """

    expired: List[RetentionCandidate] = field(default_factory=list)
    kept: List[RetentionCandidate] = field(default_factory=list)
    skipped_no_timestamp: List[str] = field(default_factory=list)


def select_expired_sessions(
    records: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    retention: timedelta,
    keep: int = 0,
) -> RetentionSelection:
    """Pure selection over session-state mappings with an injected clock.

    A session is expired when its effective timestamp is at or before
    ``now - retention``. ``keep`` protects the newest ``keep`` terminal
    sessions overall (ordered by effective timestamp, then session ID,
    newest first), so at least that many terminal sessions always survive
    regardless of age; only protected sessions that would otherwise be
    pruned are reported as ``kept``.
    """

    if keep < 0:
        raise ValueError("keep must be >= 0")
    cutoff = now - retention
    selection = RetentionSelection()
    terminal: List[RetentionCandidate] = []
    for record in records:
        status = str(record.get("status") or "")
        if status not in TERMINAL_STATUSES:
            continue
        effective_at = effective_timestamp(record)
        if effective_at is None:
            selection.skipped_no_timestamp.append(str(record.get("session_id") or ""))
            continue
        terminal.append(
            RetentionCandidate(
                session_id=str(record.get("session_id") or ""),
                status=status,
                effective_at=effective_at,
            )
        )
    terminal.sort(key=lambda item: (item.effective_at, item.session_id), reverse=True)
    protected = {item.session_id for item in terminal[:keep]}
    for candidate in terminal:
        if candidate.effective_at > cutoff:
            continue
        if candidate.session_id in protected:
            selection.kept.append(candidate)
        else:
            selection.expired.append(candidate)
    return selection


def is_valid_session_id(session_id: str) -> bool:
    """Mirror the manager's session-id rule for path construction safety."""

    return (
        bool(session_id)
        and "/" not in session_id
        and "\\" not in session_id
        and session_id not in {".", ".."}
    )


@dataclass
class TranscriptPlan:
    """Which of a record's transcript paths pruning may unlink.

    ``preserved`` holds (path, reason) pairs for anything outside the managed
    boundary; those files are never touched even though the index record is
    still removed.
    """

    deletable: List[Path] = field(default_factory=list)
    preserved: List[Tuple[str, str]] = field(default_factory=list)


def classify_transcript_paths(record: Mapping[str, Any], session_dir: Path) -> TranscriptPlan:
    """Apply the filesystem ownership boundary to one session record.

    A recorded path is deletable only when it is exactly the expected managed
    path ``session_dir/<session-id>.jsonl`` or ``.md`` for a valid session ID.
    ``session_dir`` must already be the resolved managed directory; exact
    equality against paths built from it is what rules out traversal and
    custom log directories. Symlink and file-type checks happen at unlink
    time via `transcript_unlink_blocker`.
    """

    plan = TranscriptPlan()
    session_id = str(record.get("session_id") or "")
    valid_id = is_valid_session_id(session_id)
    for key, suffix in (("jsonl_path", ".jsonl"), ("markdown_path", ".md")):
        recorded = record.get(key)
        if not isinstance(recorded, str) or not recorded:
            continue
        if not valid_id:
            plan.preserved.append((recorded, "invalid session id"))
            continue
        expected = session_dir / f"{session_id}{suffix}"
        if Path(recorded) == expected:
            plan.deletable.append(expected)
        else:
            plan.preserved.append((recorded, "outside the managed session directory"))
    return plan


def transcript_unlink_blocker(path: Path) -> Optional[str]:
    """Why ``path`` must not be unlinked, or None when it may be.

    None means the target is a regular file or already absent. The check uses
    ``lstat`` so a symlink is seen as itself, never followed.
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable: {exc}"
    if stat_module.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat_module.S_ISREG(info.st_mode):
        return "not a regular file"
    return None
