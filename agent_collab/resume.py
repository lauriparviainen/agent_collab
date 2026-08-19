"""Restart-safe resume: descriptors, fingerprints, eligibility, and errors.

Increment 4 owns the persisted explicit-resume contract. Capture of a provider
id is not readiness; a session is resumable only when every *started* non-mock
agent (one that already has an ``agent_sessions`` row) holds a fully eligible
descriptor. Unstarted members do ordinary first-turn establishment after
resume. No credentials or raw SDK objects are stored.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Dict, FrozenSet, Mapping, Optional, Set

from .retention import DONE, FAILED, INTERRUPTED, LIVE_WAIT_STATUSES, STOPPED


RESTART_ELIGIBLE_STATUSES = frozenset({"completed"})
RESUME_FAILURE_STATUSES = frozenset({"resume_rejected", "resume_uncertain"})
INELIGIBLE_TURN_STATUSES = frozenset(
    {
        "in_flight",
        "interrupted",
        "timed_out",
        "failed",
        "missing",
        "resume_rejected",
        "resume_uncertain",
    }
)
HANDOFF_PRESERVED_FIELDS = (
    "prompt_event_cursor",
    "last_turn_status",
    "resume_fingerprint",
    "backend_version",
    "interrupt_acknowledged",
    "quarantined",
)
FINGERPRINT_REQUIRED_KEYS = ("provider_type", "backend", "workdir")
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|credential|authorization|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_METADATA_OPTION_KEYS = frozenset(
    {
        "type",
        "backend",
        "capabilities",
        "brand_color",
        "backend_summary",
        "command_preview",
        "outer_sandbox",
    }
)
_BINARY_IDENTITY = {
    "claude_cli": "claude",
    "codex_cli": "codex",
    "xai_cli": "grok",
    "antigravity_cli": "agy",
    "claude_sdk": "claude-agent-sdk",
    "codex_sdk": "openai-codex",
    "antigravity_sdk": "google-antigravity",
    "xai_sdk": "xai-sdk",
}
_STATE_ROOT_KIND = {
    "claude_cli": "CLAUDE_CONFIG_DIR",
    "claude_sdk": "CLAUDE_CONFIG_DIR",
    "codex_cli": "CODEX_HOME",
    "codex_sdk": "CODEX_HOME",
    "xai_cli": "GROK_HOME",
    "xai_sdk": "none",
    "antigravity_cli": "GEMINI_STATE",
    "antigravity_sdk": "ANTIGRAVITY_SAVE_DIR",
}
# Re-verified floor only. Do not invent Claude/Codex/Grok floors.
_VERSION_FLOORS = {
    "antigravity_cli": "1.1.8",
}


class ResumeError(ValueError):
    """Structured resume failure.

    Codes: ``conflict``, ``live``, ``ineligible``, ``incompatible``,
    ``quarantined``, ``not_found``. HTTP maps ``conflict``/``live`` to 409,
    ``not_found`` to 404, and the rest to 400 — same shape as
    ``ApprovalDecisionError``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InterruptError(ValueError):
    """Structured turn-interrupt failure.

    Codes: ``conflict``, ``not_found``, ``unsupported``. HTTP maps
    ``conflict`` to 409, ``not_found`` to 404, and ``unsupported`` to 400 —
    same shape as ``ResumeError``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def last_turn_status_from_record(record: Any) -> str:
    """Map a committed ``TurnOutcomeRecord`` onto the persisted turn status."""

    code = getattr(record, "code", None)
    if code in RESUME_FAILURE_STATUSES:
        return str(code)
    outcome = getattr(record, "outcome", None)
    if outcome == "completed":
        return "completed"
    if outcome == "interrupted":
        return "interrupted"
    if outcome == "timed_out":
        return "timed_out"
    return "failed"


def resume_failure_status_for_exception(exc: BaseException) -> str:
    """Classify a resume-open failure without persisting exception text."""

    code = getattr(exc, "code", None)
    if code == "outer_sandbox_backend_incompatible":
        return "resume_rejected"
    if isinstance(exc, RuntimeError):
        text = str(exc).lower()
        if any(
            marker in text
            for marker in (
                "resume block",
                "cannot resume",
                "save_dir",
                "provider_session_id",
                "conversation",
                "thread",
            )
        ):
            return "resume_rejected"
    return "resume_uncertain"


def normalize_workflow_phase(value: Any) -> Optional[Dict[str, Any]]:
    """Return a validated session-level phase record, or None if unusable."""

    if not isinstance(value, dict):
        return None
    completed = value.get("completed_stages")
    parked = value.get("parked_in_input_loop")
    if not isinstance(completed, int) or isinstance(completed, bool) or completed < 0:
        return None
    if not isinstance(parked, bool):
        return None
    return {"completed_stages": completed, "parked_in_input_loop": parked}


def default_workflow_phase() -> Dict[str, Any]:
    return {"completed_stages": 0, "parked_in_input_loop": False}


def planned_stage_count(state: Any) -> Optional[int]:
    """Planned referee stages from persisted settings, capped by max_turns."""

    settings = getattr(state, "settings", None) or {}
    if not isinstance(settings, dict):
        return None
    workflow = settings.get("workflow") or {}
    if not isinstance(workflow, dict):
        return None
    sequence = workflow.get("sequence")
    if not isinstance(sequence, list) or not sequence:
        return None
    if workflow.get("parallel"):
        count = 1
    else:
        count = len(sequence)
    try:
        max_turns = int(getattr(state, "max_turns", count))
    except (TypeError, ValueError):
        max_turns = count
    return min(count, max(0, max_turns))


def fingerprint_is_well_formed(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    for key in FINGERPRINT_REQUIRED_KEYS:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            return False
    return True


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                continue
            cleaned[str(key)] = _strip_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonicalize_fingerprint(fingerprint: Mapping[str, Any]) -> str:
    return json.dumps(
        _strip_secrets(dict(fingerprint)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def fingerprints_match(stored: Any, current: Any) -> bool:
    if not fingerprint_is_well_formed(stored) or not fingerprint_is_well_formed(current):
        return False
    return canonicalize_fingerprint(stored) == canonicalize_fingerprint(current)


def infer_execution_path(backend_id: str, sandbox_effective: str) -> str:
    is_cli = backend_id == "cli" or backend_id.endswith("_cli")
    if is_cli:
        return "cli-direct" if sandbox_effective in {"none", "", None} else "cli-outer"
    return "sdk-inprocess" if sandbox_effective in {"none", "", None} else "sdk-worker"


def canonical_backend_name(agent_type: str, backend_id: str) -> str:
    if backend_id in {agent_type, f"{agent_type}_{backend_id}"}:
        return backend_id
    if "_" in backend_id and backend_id.startswith(agent_type):
        return backend_id
    return f"{agent_type}_{backend_id}"


def compute_resume_fingerprint(
    *,
    agent_type: str,
    backend_id: str,
    workdir: str,
    model: str = "",
    backend_version: str = "",
    sandbox_policy: str = "none",
    execution_path: str = "",
    permission_mode: str = "",
    static_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the structured fingerprint persisted on a descriptor.

    Covers provider type, canonical backend, binary/SDK identity and version,
    model, workdir, permission/sandbox posture and execution path, provider
    state-root *kind* (not a host path), and normalized backend-owned static
    config. No secrets. Q7 version floors are included only when re-verified.
    """

    backend = canonical_backend_name(agent_type, backend_id)
    fingerprint: Dict[str, Any] = {
        "provider_type": agent_type,
        "backend": backend,
        "binary": _BINARY_IDENTITY.get(backend, backend),
        "backend_version": backend_version or "",
        "model": model or "",
        "workdir": workdir,
        "sandbox_policy": sandbox_policy or "none",
        "execution_path": execution_path
        or infer_execution_path(backend_id, sandbox_policy or "none"),
        "permission_mode": permission_mode or "",
        "state_root": _STATE_ROOT_KIND.get(backend, "none"),
        "static_config": _strip_secrets(dict(static_config or {})),
    }
    floor = _VERSION_FLOORS.get(backend)
    if floor:
        fingerprint["version_floor"] = floor
    return fingerprint


def fingerprint_from_session(state: Any, agent_id: str) -> Optional[Dict[str, Any]]:
    """Recompute a fingerprint from persisted session settings."""

    settings = getattr(state, "settings", None) or {}
    if not isinstance(settings, dict):
        return None
    agents = settings.get("agents") or {}
    if not isinstance(agents, dict):
        return None
    entry = agents.get(agent_id)
    if not isinstance(entry, dict):
        return None
    agent_type = entry.get("type")
    backend_id = entry.get("backend")
    if not isinstance(agent_type, str) or not agent_type:
        return None
    if not isinstance(backend_id, str) or not backend_id:
        return None
    sandbox = settings.get("sandbox") or {}
    effective = "none"
    if isinstance(sandbox, dict):
        raw = sandbox.get("effective") or sandbox.get("requested") or "none"
        if isinstance(raw, str) and raw:
            effective = raw
    static_config = {
        key: value
        for key, value in entry.items()
        if key not in _METADATA_OPTION_KEYS and not _SECRET_KEY_RE.search(str(key))
    }
    permission = entry.get("permission_mode")
    if not isinstance(permission, str):
        permission = ""
    model = entry.get("model")
    if not isinstance(model, str):
        model = ""
    return compute_resume_fingerprint(
        agent_type=agent_type,
        backend_id=backend_id,
        workdir=str(getattr(state, "workdir", "") or ""),
        model=model,
        sandbox_policy=effective,
        permission_mode=permission,
        static_config=static_config,
    )


def descriptor_is_quarantined(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("quarantined") is True:
        return True
    return entry.get("last_turn_status") in RESUME_FAILURE_STATUSES


def descriptor_is_eligible(
    entry: Any,
    *,
    transcript_len: Optional[int] = None,
    expected_fingerprint: Any = None,
) -> bool:
    """True when one agent descriptor is restart-safe under the initial contract.

    Initial eligible set is ``completed`` only. ``interrupt_acknowledged`` is
    persisted for increment 5 and does not make ``interrupted`` eligible.
    """

    if not isinstance(entry, dict) or descriptor_is_quarantined(entry):
        return False
    session_id = entry.get("provider_session_id")
    if not isinstance(session_id, str) or not session_id:
        return False
    if entry.get("last_turn_status") not in RESTART_ELIGIBLE_STATUSES:
        return False
    cursor = entry.get("prompt_event_cursor")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        return False
    if transcript_len is not None and cursor > transcript_len:
        return False
    fingerprint = entry.get("resume_fingerprint")
    if not fingerprint_is_well_formed(fingerprint):
        return False
    if expected_fingerprint is not None and not fingerprints_match(
        fingerprint, expected_fingerprint
    ):
        return False
    return True


def any_agent_quarantined(agent_sessions: Any) -> bool:
    if not isinstance(agent_sessions, dict):
        return False
    return any(descriptor_is_quarantined(entry) for entry in agent_sessions.values())


def session_phase_blocks_resume(state: Any) -> Optional[str]:
    """Return a reason if the session-level phase is not resumable."""

    if getattr(state, "mock", False) or getattr(state, "dry_run", False):
        return "mock and dry-run sessions are not resumable"
    phase = normalize_workflow_phase(getattr(state, "workflow_phase", None))
    if phase is None:
        return "workflow phase is missing or invalid"
    if phase["parked_in_input_loop"] and not getattr(state, "interactive", False):
        return "parked input-loop phase is invalid on a non-interactive session"
    planned = planned_stage_count(state)
    if planned is not None and phase["completed_stages"] > planned:
        return "completed stage count is past the planned workflow"
    if (
        not getattr(state, "interactive", False)
        and not phase["parked_in_input_loop"]
        and planned is not None
        and phase["completed_stages"] >= planned
    ):
        return "non-interactive session already completed every planned stage"
    return None


def eligible_resume_agent_ids(
    state: Any,
    *,
    transcript_len: Optional[int] = None,
    compare_fingerprints: bool = False,
) -> FrozenSet[str]:
    """Agent ids that currently hold a fully eligible resume descriptor.

    Capture alone is not readiness. One quarantined agent makes the whole
    session's eligible set empty. Mock agents are excluded.
    """

    if getattr(state, "mock", False) or getattr(state, "dry_run", False):
        return frozenset()
    if session_phase_blocks_resume(state) is not None:
        return frozenset()
    sessions = getattr(state, "agent_sessions", None) or {}
    if not isinstance(sessions, dict) or any_agent_quarantined(sessions):
        return frozenset()
    agents = (getattr(state, "settings", None) or {}).get("agents") or {}
    if not isinstance(agents, dict):
        agents = {}
    captured: Set[str] = set()
    for agent_id, entry in sessions.items():
        if not isinstance(agent_id, str) or not agent_id:
            continue
        agent_entry = agents.get(agent_id)
        if isinstance(agent_entry, dict) and agent_entry.get("type") == "mock":
            continue
        expected = fingerprint_from_session(state, agent_id) if compare_fingerprints else None
        if descriptor_is_eligible(
            entry, transcript_len=transcript_len, expected_fingerprint=expected
        ):
            captured.add(agent_id)
    return frozenset(captured)


def selected_non_mock_agent_ids(state: Any) -> FrozenSet[str]:
    """Selected non-mock agents from persisted settings."""

    agents = (getattr(state, "settings", None) or {}).get("agents") or {}
    if not isinstance(agents, dict):
        return frozenset()
    selected: Set[str] = set()
    for agent_id, entry in agents.items():
        if not isinstance(agent_id, str) or not agent_id or not isinstance(entry, dict):
            continue
        if entry.get("type") == "mock":
            continue
        if not isinstance(entry.get("type"), str) or not entry.get("type"):
            continue
        if not isinstance(entry.get("backend"), str) or not entry.get("backend"):
            continue
        selected.add(agent_id)
    return frozenset(selected)


def required_resume_agent_ids(state: Any) -> FrozenSet[str]:
    """Non-mock agents that already have an ``agent_sessions`` row.

    Those started members must each hold a fully eligible descriptor.
    Unstarted members (no row) are not required to hold one; they do
    ordinary first-turn establishment after resume. An ineligible or
    quarantined row is still required, so it blocks resume.
    """

    sessions = getattr(state, "agent_sessions", None) or {}
    if not isinstance(sessions, dict):
        return frozenset()
    return frozenset(
        agent_id
        for agent_id in selected_non_mock_agent_ids(state)
        if isinstance(sessions.get(agent_id), dict)
    )


def unstarted_resume_agent_ids(state: Any) -> FrozenSet[str]:
    """Selected non-mock agents with no ``agent_sessions`` row."""

    return selected_non_mock_agent_ids(state) - required_resume_agent_ids(state)


def projection_captured_resume_agent_ids(
    state: Any,
    *,
    transcript_len: Optional[int] = None,
    compare_fingerprints: bool = False,
) -> FrozenSet[str]:
    """Captured set passed into the session-capability reducer.

    The reducer contract is unchanged: ``resumable`` is true only when every
    selected agent is in this set *and* every selected backend advertises
    ``resume``. Unstarted members (no row) are treated as captured so they do
    not fail that membership check. Agents with an ineligible row are not
    unstarted. When nothing is eligible the set stays empty so a never-invoked
    or quarantined session does not project ``resumable``.
    """

    eligible = eligible_resume_agent_ids(
        state, transcript_len=transcript_len, compare_fingerprints=compare_fingerprints
    )
    if not eligible:
        return eligible
    return eligible | unstarted_resume_agent_ids(state)


def session_is_live_for_resume(state: Any, *, task_running: bool) -> bool:
    if task_running:
        return True
    return getattr(state, "status", "") in LIVE_WAIT_STATUSES


def session_status_allows_resume(status: str) -> bool:
    return status in {INTERRUPTED, STOPPED}


def validate_session_resume(
    state: Any,
    *,
    transcript_len: Optional[int] = None,
    compare_fingerprints: bool = False,
) -> Dict[str, Any]:
    """Validate descriptors + phase for an explicit resume. Raises ``ResumeError``."""

    status = getattr(state, "status", "")
    if status in {DONE, FAILED}:
        raise ResumeError("ineligible", f"session status {status!r} is not resumable")
    if not session_status_allows_resume(str(status)):
        raise ResumeError("ineligible", f"session status {status!r} is not resumable")
    phase_reason = session_phase_blocks_resume(state)
    if phase_reason is not None:
        raise ResumeError("ineligible", phase_reason)
    sessions = getattr(state, "agent_sessions", None) or {}
    if any_agent_quarantined(sessions):
        raise ResumeError(
            "quarantined",
            "a quarantined agent makes session resume permanently unavailable",
        )
    required = required_resume_agent_ids(state)
    if not required:
        raise ResumeError("ineligible", "session has no resumable agents")
    eligible = eligible_resume_agent_ids(
        state, transcript_len=transcript_len, compare_fingerprints=compare_fingerprints
    )
    missing = required - eligible
    if missing:
        raise ResumeError(
            "ineligible",
            "every required agent must hold a fully eligible resume descriptor",
        )
    phase = normalize_workflow_phase(getattr(state, "workflow_phase", None))
    assert phase is not None
    return {
        "phase": phase,
        "eligible_agent_ids": eligible,
        "descriptors": {
            agent_id: dict(sessions[agent_id])
            for agent_id in required
            if isinstance(sessions.get(agent_id), dict)
        },
    }


def watermarks_from_descriptors(descriptors: Mapping[str, Mapping[str, Any]]) -> Dict[str, int]:
    watermarks: Dict[str, int] = {}
    for agent_id, entry in descriptors.items():
        cursor = entry.get("prompt_event_cursor")
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            watermarks[agent_id] = cursor
    return watermarks


def attach_resume_block(
    payload: Dict[str, Any],
    resume: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Add a typed resume block to a worker open payload, or leave it unchanged."""

    if resume is None:
        return payload
    session_id = resume.get("provider_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("resume block missing provider_session_id")
    block: Dict[str, str] = {"provider_session_id": session_id}
    kind = resume.get("provider_session_kind")
    if isinstance(kind, str) and kind:
        block["provider_session_kind"] = kind
    payload["resume"] = block
    return payload


def require_resume_session_id(payload: Mapping[str, Any]) -> Optional[str]:
    """If a resume block is present, return its id or fail structurally.

    Absence means ordinary establishment. A present-but-invalid block must
    never fall through to a fresh start.
    """

    if "resume" not in payload:
        return None
    resume = payload.get("resume")
    if not isinstance(resume, Mapping):
        raise RuntimeError("resume block is malformed")
    session_id = resume.get("provider_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("resume block missing provider_session_id")
    return session_id


def backend_options_from_settings(
    settings: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    options: Dict[str, Dict[str, Any]] = {}
    if not isinstance(settings, Mapping):
        return options
    agents = settings.get("agents") or {}
    if not isinstance(agents, Mapping):
        return options
    for entry in agents.values():
        if not isinstance(entry, Mapping):
            continue
        agent_type = entry.get("type")
        backend_id = entry.get("backend")
        if not isinstance(agent_type, str) or not isinstance(backend_id, str):
            continue
        name = canonical_backend_name(agent_type, backend_id)
        opts = {
            key: value
            for key, value in entry.items()
            if key not in _METADATA_OPTION_KEYS and not _SECRET_KEY_RE.search(str(key))
        }
        if opts:
            options[name] = opts
    return options


def members_from_settings(
    settings: Optional[Mapping[str, Any]],
    workflow: Any,
) -> Optional[Dict[str, str]]:
    """Rebuild a start-time members map when the persisted sequence differs."""

    if not isinstance(settings, Mapping) or workflow is None:
        return None
    recorded = (settings.get("workflow") or {}).get("sequence")
    if not isinstance(recorded, list) or not recorded:
        return None
    from .config import workflow_member_slots, workflow_members

    default = list(workflow_members(workflow))
    persisted = [item for item in recorded if isinstance(item, str) and item]
    if persisted == default:
        return None
    slots = list(workflow_member_slots(workflow))
    if len(slots) != len(persisted):
        return None
    return dict(zip(slots, persisted))


@dataclass(frozen=True)
class ResumeClaim:
    """Snapshot of a validated resume used to seed the referee."""

    phase: Dict[str, Any]
    descriptors: Dict[str, Dict[str, Any]]
    watermarks: Dict[str, int]
