"""Helpers shared by first-class provider backends (claude/codex/antigravity/xai).

Every provider backend maps a provider-specific API onto the one ``Event``
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

Nothing here imports a real SDK; it is standard-library only, and the uniform
provider-session event helper is intentionally shared with CLI parsers.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from ...events import Event

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


def sdk_settings_summary(package_name: str, mapped_options: Mapping[str, Any]) -> Dict[str, Any]:
    """The base of every SDK ``settings_summary``.

    Package identity, the installed version when known, and the backend's
    mapped provider options when any. Backends append their specifics.
    """

    summary: Dict[str, Any] = {"backend": "sdk", "package": package_name}
    version = package_version(package_name)
    if version:
        summary["version"] = version
    if mapped_options:
        summary["options"] = dict(mapped_options)
    return summary


def backend_unavailable_event(exc: Exception) -> Event:
    """The one error event every SDK runner yields for ``BackendUnavailable``.

    The exception text is the user-facing remediation (it names the missing
    package or credential), so it is surfaced verbatim rather than wrapped in
    the generic sdk-error shape.
    """

    return Event.create("error", "error", str(exc), {"error": str(exc)})


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


async def close_async_stream(stream: Any) -> None:
    """Best-effort close without masking a turn error or cancellation.

    ``CancelledError`` remains a ``BaseException`` and is deliberately not
    swallowed if a new cancellation interrupts the close itself.
    """

    close = getattr(stream, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except Exception:
        return


def provider_session_event(
    source: str,
    agent_id: str,
    provider_session_id: str,
    provider_session_kind: str,
    *,
    raw: Optional[Mapping[str, Any]] = None,
) -> Event:
    """Build the status event that carries a provider session id to session state.

    ``raw`` uses the uniform persisted keys (``provider_session_id`` +
    ``provider_session_kind``) plus the workflow ``agent_id`` the daemon keys the
    per-agent ``agent_sessions`` map on. Emitted regardless of verbosity so the
    capture is reliable; it never claims the session is resumable.
    """

    event_raw = dict(raw or {})
    event_raw.update(
        {
            "provider_session_id": provider_session_id,
            "provider_session_kind": provider_session_kind,
            "agent_id": agent_id,
        }
    )
    return Event.create(
        source,
        "status",
        f"{source} {provider_session_kind}_id={provider_session_id}",
        event_raw,
    ).mark_provider_session(
        agent_id=agent_id,
        session_id=provider_session_id,
        kind=provider_session_kind,
    )
