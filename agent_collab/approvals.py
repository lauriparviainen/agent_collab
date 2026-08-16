"""Session-scoped tool-approval registry and park-payload helpers.

In-memory only: entries never persist callbacks, worker sessions, or raw SDK
objects. The daemon mints ``approval_request`` / ``approval_resolved`` events
and parks ``wait_result`` while this registry holds unresolved requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import json
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .events import compact_json
from .sandbox.worker_codec import sanitize_error_text


# Middle-elision budget for one approval summary (head and tail kept).
MAX_APPROVAL_SUMMARY_CHARS = 160
# Bound for public request_id / agent_id fields in the park payload.
MAX_APPROVAL_ID_CHARS = 200
# Total JSON size of ``pending_approvals`` blocks in a park payload. First
# blocks that fit are listed with their summaries; remaining requests are
# counted in ``pending_approvals_omitted`` rather than dropped silently.
MAX_PENDING_APPROVALS_PAYLOAD_BYTES = 2048
# Bound for delivering one approval_decision frame to a worker.
WORKER_DECISION_TIMEOUT_SECONDS = 1.0
DECISION_OPTIONS = ("approve", "deny")
AUTHORIZED_OUTCOMES = {"approved": "approve", "denied": "deny"}
RESOLUTION_OUTCOMES = frozenset({"approved", "denied", "auto_denied", "abandoned"})
# Fail-closed tool-approval deadline (start setting). Expiry auto-denies.
DEFAULT_APPROVAL_DEADLINE_SECONDS = 120.0
MIN_APPROVAL_DEADLINE_SECONDS = 0.05
MAX_APPROVAL_DEADLINE_SECONDS = 3600.0
# Per-turn park-exclusion cap is twice the configured approval deadline.
APPROVAL_PARK_EXCLUSION_FACTOR = 2.0


class ApprovalDecisionError(ValueError):
    """Structured resolve failure: ``not_found``, ``conflict``, or ``stale``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sanitize_tool_name(name: Any) -> str:
    text = sanitize_error_text(str(name or "tool"), limit=MAX_APPROVAL_ID_CHARS)
    return text or "tool"


def sanitize_request_id(value: Any) -> str:
    """Strip and bound a public approval id. Empty stays empty."""

    text = str(value or "").strip()
    if not text:
        return ""
    return sanitize_error_text(text, limit=MAX_APPROVAL_ID_CHARS)


def sanitize_tool_value(value: Any) -> Any:
    """Redact secrets in tool input at the source (not codec error-text only)."""

    if isinstance(value, str):
        return sanitize_error_text(value, limit=10_000)
    if isinstance(value, Mapping):
        return {str(key): sanitize_tool_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_tool_value(item) for item in value]
    return value


def middle_elide(text: str, limit: int = MAX_APPROVAL_SUMMARY_CHARS) -> Tuple[str, bool]:
    """Keep head and tail of ``text``; mark the elided span. Total length <= limit."""

    if limit < 8:
        limit = 8
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    marker = f"…[+{omitted} chars]…"
    if len(marker) >= limit:
        marker = "…"
    keep = limit - len(marker)
    head = keep // 2
    tail = keep - head
    if tail <= 0:
        return text[:limit], True
    return text[:head] + marker + text[-tail:], True


def build_approval_summary(
    *,
    summary: Any = None,
    tool_input: Any = None,
    already_truncated: bool = False,
) -> Tuple[str, bool]:
    """Sanitize then middle-elide a summary. Never returns raw tool input."""

    if summary is not None and str(summary).strip():
        text = sanitize_error_text(str(summary), limit=10_000)
        elided, truncated = middle_elide(text)
        return elided, truncated or bool(already_truncated)
    if tool_input is not None:
        cleaned = sanitize_tool_value(tool_input)
        source = cleaned if isinstance(cleaned, str) else compact_json(cleaned, limit=4000)
        elided, truncated = middle_elide(str(source))
        return elided, truncated or bool(already_truncated)
    text = sanitize_error_text(str(summary or ""), limit=10_000)
    elided, truncated = middle_elide(text)
    return elided, truncated or bool(already_truncated)


@dataclass
class ApprovalEntry:
    request_id: str
    agent_id: str
    tool_name: str
    summary: str
    summary_truncated: bool
    turn_id: str = ""
    worker_instance: Optional[str] = None
    approval_id: Optional[str] = None
    run_id: Optional[str] = None
    send_decision: Optional[Callable[[str], Any]] = field(default=None, repr=False, compare=False)
    send_in_flight: bool = field(default=False, repr=False, compare=False)
    deadline_task: Optional[asyncio.Task[Any]] = field(default=None, repr=False, compare=False)
    decision_options: Tuple[str, ...] = DECISION_OPTIONS
    seq: int = 0
    outcome: Optional[str] = None
    reason: Optional[str] = None

    def to_block(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "summary": self.summary,
            "summary_truncated": self.summary_truncated,
            "decision_options": list(self.decision_options),
        }

    def request_raw(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "summary": self.summary,
            "summary_truncated": self.summary_truncated,
            "decision_options": list(self.decision_options),
        }

    def resolved_raw(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "outcome": self.outcome,
            "reason": self.reason,
        }


def cancel_approval_deadline(entry: ApprovalEntry) -> None:
    """Cancel a pending per-request deadline task. Safe if none or already done."""

    task = entry.deadline_task
    entry.deadline_task = None
    if task is not None and not task.done():
        task.cancel()


def normalize_approval_deadline(value: Any, *, label: str = "approval_deadline") -> float:
    """Validate the bounded start setting. Default 120 s; expiry auto-denies."""

    try:
        deadline = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if deadline < MIN_APPROVAL_DEADLINE_SECONDS or deadline > MAX_APPROVAL_DEADLINE_SECONDS:
        raise ValueError(
            f"{label} must be >= {MIN_APPROVAL_DEADLINE_SECONDS} and "
            f"<= {MAX_APPROVAL_DEADLINE_SECONDS}"
        )
    return deadline


class ApprovalRegistry:
    """Session-scoped map of unresolved and recently resolved approval requests."""

    def __init__(self) -> None:
        self._pending: Dict[str, ApprovalEntry] = {}
        self._resolved: Dict[str, ApprovalEntry] = {}
        self._finished_turns: set[str] = set()
        self._seq = 0
        self.auto_denied_count = 0

    def unresolved_count(self) -> int:
        return len(self._pending)

    def pending_in_event_order(self) -> List[ApprovalEntry]:
        return sorted(self._pending.values(), key=lambda item: item.seq)

    def add(self, entry: ApprovalEntry) -> ApprovalEntry:
        self._seq += 1
        entry.seq = self._seq
        self._pending[entry.request_id] = entry
        return entry

    def get_pending(self, request_id: str) -> Optional[ApprovalEntry]:
        return self._pending.get(request_id)

    def get_resolved(self, request_id: str) -> Optional[ApprovalEntry]:
        return self._resolved.get(request_id)

    def turn_finished(self, turn_id: str) -> bool:
        return bool(turn_id) and turn_id in self._finished_turns

    def finish_turn(self, turn_id: str) -> None:
        if turn_id:
            self._finished_turns.add(turn_id)

    def take_pending(self, request_id: str) -> Optional[ApprovalEntry]:
        return self._pending.pop(request_id, None)

    def take_turn(self, turn_id: str) -> List[ApprovalEntry]:
        self.finish_turn(turn_id)
        taken = [entry for entry in self._pending.values() if entry.turn_id == turn_id]
        for entry in taken:
            self._pending.pop(entry.request_id, None)
        return taken

    def take_all(self) -> List[ApprovalEntry]:
        taken = self.pending_in_event_order()
        self._pending.clear()
        for entry in taken:
            if entry.turn_id:
                self._finished_turns.add(entry.turn_id)
        return taken

    def complete(self, entry: ApprovalEntry, outcome: str, reason: str) -> ApprovalEntry:
        entry.outcome = outcome
        entry.reason = reason
        self._pending.pop(entry.request_id, None)
        self._resolved[entry.request_id] = entry
        if outcome == "auto_denied":
            self.auto_denied_count += 1
        return entry


def park_payload(
    entries: Sequence[ApprovalEntry],
    *,
    budget: int = MAX_PENDING_APPROVALS_PAYLOAD_BYTES,
) -> Tuple[List[Dict[str, Any]], int]:
    """First blocks that fit the budget, plus a count of overflow (never silent)."""

    ordered = list(entries)
    if not ordered:
        return [], 0
    included: List[Dict[str, Any]] = []
    for index, entry in enumerate(ordered):
        block = entry.to_block()
        candidate = included + [block]
        encoded = _payload_bytes(candidate)
        if encoded <= budget:
            included = candidate
            continue
        if included:
            return included, len(ordered) - index
        fitted = _fit_block(block, budget)
        if _payload_bytes([fitted]) > budget:
            return [], len(ordered)
        return [fitted], len(ordered) - 1
    return included, 0


def _fit_block(block: Dict[str, Any], budget: int) -> Dict[str, Any]:
    """Shrink the first block's summary until it fits, or drop the summary."""

    fitted = dict(block)
    summary = str(fitted.get("summary") or "")
    while _payload_bytes([fitted]) > budget and summary:
        next_limit = max(8, len(summary) // 2)
        summary, _ = middle_elide(summary, next_limit)
        fitted["summary"] = summary
        fitted["summary_truncated"] = True
        if len(summary) <= 8:
            break
    if _payload_bytes([fitted]) > budget:
        fitted["summary"] = ""
        fitted["summary_truncated"] = True
    return fitted


def _payload_bytes(blocks: List[Dict[str, Any]]) -> int:
    try:
        return len(json.dumps(blocks, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return sum(len(str(block)) for block in blocks)


def dispatch_worker_approval(runner: Any, frame: Mapping[str, Any]) -> Any:
    """Map a worker ``approval_request`` frame onto the session registry callback.

    Envelope ``request_id`` is not the approval id. The worker field is
    ``approval_id``; the session-level transcript id is ``request_id``.
    """

    callback = getattr(runner, "_approval_callback", None)
    if callback is None:
        return None
    session = getattr(runner, "_worker_session", None)
    approval_id = frame.get("approval_id")
    request_id = approval_id if isinstance(approval_id, str) and approval_id else ""
    run_id = frame.get("run_id")
    if not isinstance(run_id, str):
        run_id = getattr(session, "_active_run", None)

    async def _send(decision: str) -> bool:
        sender = getattr(session, "send_approval_decision", None)
        if not callable(sender) or not isinstance(approval_id, str) or not approval_id:
            return False
        result = sender(approval_id=approval_id, decision=decision, run_id=run_id)
        if asyncio.iscoroutine(result):
            result = await result
        return result is True

    payload = {
        "request_id": request_id,
        "approval_id": approval_id if isinstance(approval_id, str) else None,
        "agent_id": getattr(runner, "_bound_agent_id", None) or getattr(runner, "name", ""),
        "turn_id": getattr(runner, "_bound_turn_id", None) or "",
        "tool_name": frame.get("tool_name") or "tool",
        "summary": frame.get("summary"),
        "summary_truncated": bool(frame.get("summary_truncated")),
        "tool_input": frame.get("input") if "input" in frame else frame.get("tool_input"),
        "worker_instance": getattr(session, "instance", None),
        "run_id": run_id if isinstance(run_id, str) else None,
        "send_decision": _send,
    }
    return callback(payload)


def worker_on_approval(runner: Any) -> Optional[Callable[[Mapping[str, Any]], Any]]:
    if getattr(runner, "_approval_callback", None) is None:
        return None
    return lambda frame: dispatch_worker_approval(runner, frame)


def worker_session_run_kwargs(runner: Any, *, emit: Any) -> Dict[str, Any]:
    """Keyword args for ``SupervisedWorkerSession.run``.

    ``on_approval`` is included only when the runner has a registry callback, so
    test doubles that accept ``emit`` but not ``on_approval`` keep working.
    """

    kwargs: Dict[str, Any] = {"emit": emit}
    on_approval = worker_on_approval(runner)
    if on_approval is not None:
        kwargs["on_approval"] = on_approval
    return kwargs
