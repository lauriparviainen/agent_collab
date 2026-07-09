"""Helpers shared by the first-class SDK backends (claude/codex/antigravity).

Every SDK backend maps a young, provider-specific API onto the one ``Event``
contract and the one persisted session schema. The provider-specific *calls*
live in each module; only the cross-provider glue lives here:

- :func:`classify_tool_kind` — one tool-name classifier so a file edit, a shell
  command, and a generic tool call read the same across providers.
- :func:`package_version` — best-effort installed version for backend summaries.
- :func:`provider_session_event` — the one status event that carries a provider
  session id into central session state under a uniform schema
  (``provider_session_id`` + a sibling ``provider_session_kind``), with the
  workflow ``agent_id`` so the daemon can attribute it. Nothing resumes it this
  stage; capabilities stay honest.

Nothing here imports a real SDK; it is standard-library only.
"""

from __future__ import annotations

from typing import Any, Optional

from ..events import Event

# Tool-name tokens that mean a file was written vs a shell command ran; anything
# else is a generic tool call. Matched case-insensitively as substrings so both
# Claude tool names (Edit/Write/Bash) and Antigravity BuiltinTools enum names
# (EDIT_FILE/RUN_COMMAND) and Codex item types (apply_patch/command) classify.
_FILE_CHANGE_TOKENS = ("edit", "write", "patch", "create_file")
_COMMAND_TOKENS = ("command", "bash", "exec", "shell")


def classify_tool_kind(name: Any) -> str:
    """Classify a tool/item name into ``file_change`` / ``command`` / ``tool_call``."""

    low = str(name).lower()
    if any(token in low for token in _FILE_CHANGE_TOKENS):
        return "file_change"
    if any(token in low for token in _COMMAND_TOKENS):
        return "command"
    return "tool_call"


def package_version(package_name: str) -> Optional[str]:
    """Best-effort installed distribution version; ``None`` when absent."""

    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - py<3.8 only
        return None
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def stringify(value: Any) -> str:
    """A stripped string, or ``""`` for a non-string — the value coercion every
    SDK mapper needs before deciding whether a field carries prose."""

    return value.strip() if isinstance(value, str) else ""


def sdk_error_event(source: str, exc: Exception) -> Event:
    """The one error-event shape every SDK runner uses for an unexpected SDK error.

    Kept here so the load-bearing "how an SDK failure reaches the transcript"
    contract (source ``error``, machine-readable ``error``/``exception`` raw) lives
    in one place instead of drifting across three runners.
    """

    return Event.create(
        "error",
        "error",
        f"{source} sdk error: {exc}",
        {"error": str(exc), "exception": exc.__class__.__name__},
    )


def provider_session_event(
    source: str,
    agent_id: str,
    provider_session_id: str,
    provider_session_kind: str,
) -> Event:
    """Build the status event that carries a provider session id to session state.

    ``raw`` uses the uniform persisted keys (``provider_session_id`` +
    ``provider_session_kind``) plus the workflow ``agent_id`` the daemon keys the
    per-agent ``agent_sessions`` map on. Emitted regardless of verbosity so the
    capture is reliable; it never claims the session is resumable.
    """

    return Event.create(
        source,
        "status",
        f"{source} {provider_session_kind}_id={provider_session_id}",
        {
            "provider_session_id": provider_session_id,
            "provider_session_kind": provider_session_kind,
            "agent_id": agent_id,
        },
    )
