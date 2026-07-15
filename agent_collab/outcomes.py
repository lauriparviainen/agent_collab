"""Backend-neutral terminal outcomes for one supervised agent turn.

Provider detail remains private until it has been reduced to the small,
allowlisted values in this module.  In particular, exception text and provider
payloads must never be copied into :class:`TurnOutcome` or persisted records.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, Iterable, Literal, Optional


TurnOutcomeKind = Literal["completed", "cancelled", "interrupted", "timed_out", "refused", "failed"]

OUTCOME_KINDS = frozenset(
    {"completed", "cancelled", "interrupted", "timed_out", "refused", "failed"}
)

CANONICAL_MESSAGES: Dict[str, str] = {
    "provider_turn_cancelled": "The provider cancelled the turn",
    "provider_turn_refused": "The provider refused the turn",
    "provider_terminal_failure": "The provider reported a terminal failure",
    "provider_protocol_conflict": "The provider reported conflicting terminal states",
    "provider_authentication_failed": "Provider authentication failed",
    "provider_entitlement_failed": "Provider access is not entitled",
    "provider_model_unavailable": "The requested provider model is unavailable",
    "provider_transport_failed": "The provider transport failed",
    "provider_output_invalid": "The provider output was invalid",
    "provider_output_incomplete": "The provider output ended before terminal completion",
    "provider_empty_response": "The provider returned no usable response",
    "local_turn_timed_out": "The turn exceeded its local deadline",
    "local_turn_interrupted": "The turn was interrupted by an explicit session stop",
    "referee_turn_cancelled": "The referee cancelled the turn",
    "referee_cancelled_unexpected": "The turn supervisor was cancelled unexpectedly",
    "subprocess_exit_nonzero": "The provider subprocess exited unsuccessfully",
    "parallel_stage_no_accepted_member": "No parallel reviewer produced an accepted review",
}

# These are provider enum tokens observed in repository fixtures or installed
# interfaces.  Keep the list deliberately closed; prose is never accepted.
PROVIDER_STOP_REASONS = frozenset(
    {
        "EndTurn",
        "Cancelled",
        "STOP",
        "MAX_TOKENS",
        "LENGTH",
        "completed",
        "failed",
        "interrupted",
        "success",
        "error",
    }
)
CANONICAL_BACKENDS = frozenset(
    {
        "claude_cli",
        "codex_cli",
        "antigravity_cli",
        "xai_cli",
        "claude_sdk",
        "codex_sdk",
        "antigravity_sdk",
        "xai_sdk",
        "mock",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def canonical_message(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    try:
        return CANONICAL_MESSAGES[code]
    except KeyError as exc:
        raise ValueError(f"unknown turn outcome code {code!r}") from exc


def _safe_stop_reason(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if value not in PROVIDER_STOP_REASONS or len(value) > 32:
        return None
    return value


def _safe_exit_code(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _safe_retry_after(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds <= 0 or seconds > 7 * 24 * 60 * 60:
        return None
    return seconds


@dataclass(frozen=True)
class TurnOutcome:
    outcome: TurnOutcomeKind
    code: Optional[str] = None
    message: Optional[str] = None
    provider_stop_reason: Optional[str] = None
    process_exit_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOME_KINDS:
            raise ValueError(f"unknown turn outcome {self.outcome!r}")
        expected = canonical_message(self.code)
        if self.message not in (None, expected):
            raise ValueError("turn outcome message must be canonical for its code")
        object.__setattr__(self, "message", expected)
        object.__setattr__(
            self, "provider_stop_reason", _safe_stop_reason(self.provider_stop_reason)
        )
        object.__setattr__(self, "process_exit_code", _safe_exit_code(self.process_exit_code))
        object.__setattr__(self, "retry_after_seconds", _safe_retry_after(self.retry_after_seconds))
        if self.outcome == "completed" and self.code is not None:
            raise ValueError("completed outcomes cannot carry a failure code")
        if self.outcome != "completed" and self.code is None:
            raise ValueError("non-completed outcomes require a stable failure code")

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class TurnOutcomeRecord:
    turn_id: str
    stage_index: int
    agent_id: str
    backend: str
    outcome: TurnOutcomeKind
    code: Optional[str] = None
    message: Optional[str] = None
    provider_stop_reason: Optional[str] = None
    process_exit_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None

    @classmethod
    def from_outcome(
        cls,
        *,
        turn_id: str,
        stage_index: int,
        agent_id: str,
        backend: str,
        outcome: TurnOutcome,
    ) -> "TurnOutcomeRecord":
        return cls(
            turn_id=turn_id,
            stage_index=stage_index,
            agent_id=agent_id,
            backend=backend,
            outcome=outcome.outcome,
            code=outcome.code,
            message=outcome.message,
            provider_stop_reason=outcome.provider_stop_reason,
            process_exit_code=outcome.process_exit_code,
            retry_after_seconds=outcome.retry_after_seconds,
        )

    def __post_init__(self) -> None:
        if not self.turn_id.startswith("turn-") or not self.turn_id[5:].isdigit():
            raise ValueError("turn_id must be a workflow-owned turn-N identifier")
        if isinstance(self.stage_index, bool) or self.stage_index < 1:
            raise ValueError("stage_index must be a positive integer")
        if not isinstance(self.agent_id, str) or _IDENTIFIER.fullmatch(self.agent_id) is None:
            raise ValueError("agent_id must be a bounded workflow-owned identifier")
        if self.backend not in CANONICAL_BACKENDS:
            raise ValueError("backend must be a canonical registered backend name")
        normalized = TurnOutcome(
            self.outcome,
            self.code,
            self.message,
            self.provider_stop_reason,
            self.process_exit_code,
            self.retry_after_seconds,
        )
        object.__setattr__(self, "message", normalized.message)
        object.__setattr__(self, "provider_stop_reason", normalized.provider_stop_reason)
        object.__setattr__(self, "process_exit_code", normalized.process_exit_code)
        object.__setattr__(self, "retry_after_seconds", normalized.retry_after_seconds)

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TurnOutcomeRecord":
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: data.get(key) for key in allowed})


@dataclass(frozen=True)
class SessionFailure:
    code: str
    message: Optional[str] = None
    stage_index: Optional[int] = None
    turn_id: Optional[str] = None
    agent_id: Optional[str] = None
    backend: Optional[str] = None
    outcome: Optional[TurnOutcomeKind] = None
    provider_stop_reason: Optional[str] = None
    process_exit_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None

    @classmethod
    def from_record(cls, record: TurnOutcomeRecord) -> "SessionFailure":
        if record.code is None:
            raise ValueError("a decisive failure record requires a stable code")
        return cls(
            code=record.code,
            stage_index=record.stage_index,
            turn_id=record.turn_id,
            agent_id=record.agent_id,
            backend=record.backend,
            outcome=record.outcome,
            provider_stop_reason=record.provider_stop_reason,
            process_exit_code=record.process_exit_code,
            retry_after_seconds=record.retry_after_seconds,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in CANONICAL_MESSAGES:
            raise ValueError("session failure requires a stable canonical code")
        expected = canonical_message(self.code)
        if self.message not in (None, expected):
            raise ValueError("session failure message must be canonical for its code")
        object.__setattr__(self, "message", expected)
        object.__setattr__(
            self, "provider_stop_reason", _safe_stop_reason(self.provider_stop_reason)
        )
        object.__setattr__(self, "process_exit_code", _safe_exit_code(self.process_exit_code))
        object.__setattr__(self, "retry_after_seconds", _safe_retry_after(self.retry_after_seconds))
        if self.outcome is not None and self.outcome not in OUTCOME_KINDS:
            raise ValueError(f"unknown turn outcome {self.outcome!r}")
        if self.stage_index is not None and (
            isinstance(self.stage_index, bool)
            or not isinstance(self.stage_index, int)
            or self.stage_index < 1
        ):
            raise ValueError("stage_index must be a positive integer or null")
        if self.turn_id is not None and (
            not self.turn_id.startswith("turn-") or not self.turn_id[5:].isdigit()
        ):
            raise ValueError("turn_id must be a workflow-owned turn-N identifier or null")
        if self.agent_id is not None and (
            not isinstance(self.agent_id, str) or _IDENTIFIER.fullmatch(self.agent_id) is None
        ):
            raise ValueError("agent_id must be a workflow-owned identifier or null")
        if self.backend is not None and self.backend not in CANONICAL_BACKENDS:
            raise ValueError("backend must be a canonical backend name or null")

    def to_dict(self) -> Dict[str, Any]:
        # Failure fields remain explicit/null so stage-level failures have a
        # stable shape on REST and MCP.
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionFailure":
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: data.get(key) for key in allowed})


@dataclass(frozen=True)
class TerminalEvidence:
    """Private provider/parser evidence; never persisted directly."""

    outcome: TurnOutcomeKind
    code: Optional[str] = None
    provider_stop_reason: Optional[str] = None
    retry_after_seconds: Optional[float] = None

    def to_outcome(self, process_exit_code: Optional[int] = None) -> TurnOutcome:
        return TurnOutcome(
            self.outcome,
            self.code,
            provider_stop_reason=self.provider_stop_reason,
            process_exit_code=process_exit_code,
            retry_after_seconds=self.retry_after_seconds,
        )


class TerminalEvidenceAccumulator:
    """Resolve provider evidence once process/transport teardown is known."""

    def __init__(self) -> None:
        self._evidence: list[TerminalEvidence] = []

    def add(self, evidence: TerminalEvidence) -> None:
        if evidence not in self._evidence:
            self._evidence.append(evidence)

    def extend(self, evidence: Iterable[TerminalEvidence]) -> None:
        for item in evidence:
            self.add(item)

    def resolve(
        self,
        *,
        process_exit_code: Optional[int] = None,
        exception_code: Optional[str] = None,
        clean_eof_fallback: bool = False,
        produced_message: bool = False,
    ) -> TurnOutcome:
        if len(self._evidence) > 1:
            return TurnOutcome(
                "failed",
                "provider_protocol_conflict",
                process_exit_code=process_exit_code,
            )
        if self._evidence and self._evidence[0].outcome != "completed":
            return self._evidence[0].to_outcome(process_exit_code)
        if exception_code is not None:
            return TurnOutcome("failed", exception_code, process_exit_code=process_exit_code)
        if process_exit_code not in (None, 0):
            return TurnOutcome(
                "failed", "subprocess_exit_nonzero", process_exit_code=process_exit_code
            )
        if self._evidence:
            return self._evidence[0].to_outcome(process_exit_code)
        if clean_eof_fallback:
            if produced_message:
                return TurnOutcome("completed", process_exit_code=process_exit_code)
            return TurnOutcome(
                "failed", "provider_empty_response", process_exit_code=process_exit_code
            )
        return TurnOutcome(
            "failed", "provider_output_incomplete", process_exit_code=process_exit_code
        )


def outcome_from_exception(code: str = "provider_transport_failed") -> TurnOutcome:
    """Reduce any exception to a canonical safe outcome without retaining text."""

    return TurnOutcome("failed", code)
