"""The Codex ``sdk`` backend (``openai-codex``), lazy + first-class.

The real ``openai_codex`` module is imported **lazily** — only inside the probe's
``find_spec`` check and the default item-stream factory — never at import time,
so importing this module (which the registry does at startup) costs nothing and a
missing wheel degrades to an *unavailable* backend rather than an import crash.

**API mapping** targets the Codex Python SDK (Python 3.10+), which drives the
local Codex app-server over JSON-RPC. A turn surfaces as a stream of *thread
items*; this backend maps the item types it can recover and degrades to
message-only for anything else (it does **not** fake `codex exec --json` parity):

- an agent-message item (``.text``) -> ``codex`` message,
- a command-execution item (``.command``) -> ``tool`` ``command``,
- a file-change / patch item (``.changes``) -> ``tool`` ``file_change``,
- a reasoning item -> a verbose status,
- an error item -> ``error``.

The thread id (``.thread_id``) is captured as the provider session id
(``kind="thread"``). agent-collab never manages credentials: auth
(``OPENAI_API_KEY`` or Codex's local sign-in) comes from the passed-through
environment. The mapper is exercised by fake-module tests
(``tests/test_backend_codex_sdk.py``) — no live call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

from ..config import AgentConfig
from ..events import Event, compact_json
from ..runners import AgentRunner
from .base import BackendCapabilities, BackendHealth, BackendUnavailable
from .health import codex_api_key_credentials, probe_sdk_backend
from .sdk_common import package_version, provider_session_event, sdk_error_event, stringify

MODULE_NAME = "openai_codex"
PACKAGE_NAME = "openai-codex"
INSTALL_HINT = "install the Codex SDK: pip install openai-codex"

# `codex_options.sandbox` string -> the Codex SDK `Sandbox` enum member name.
_SANDBOX_MEMBERS = {
    "read-only": "read_only",
    "workspace-write": "workspace_write",
    "danger-full-access": "full_access",
}

# A factory opens the SDK item stream for one turn. Injectable so tests drive the
# runner with a fake item iterator without installing the SDK or calling a model.
ItemStreamFactory = Callable[[AgentConfig, Dict[str, Any], Path, str], AsyncIterator[Any]]


class CodexSdkBackend:
    """Registered as ``(codex, "sdk")``. Capabilities are all false."""

    id = "sdk"
    agent_type = "codex"

    def __init__(self, item_stream: Optional[ItemStreamFactory] = None) -> None:
        self.capabilities = BackendCapabilities()
        self.checks_credentials = True
        self.block_on_unavailable = True
        self._item_stream = item_stream

    def probe(self) -> BackendHealth:
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=codex_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Dict[str, Any]) -> AgentRunner:
        factory = self._item_stream or _default_item_stream
        return CodexSdkRunner(agent, verbose, dict(options or {}), item_stream=factory)

    def settings_summary(self, agent: AgentConfig, options: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"backend": "sdk", "package": PACKAGE_NAME}
        version = package_version(PACKAGE_NAME)
        if version:
            summary["version"] = version
        mapped = _map_sdk_options(options)
        if mapped:
            summary["options"] = mapped
        return summary


class CodexSdkRunner(AgentRunner):
    def __init__(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Dict[str, Any],
        item_stream: ItemStreamFactory,
    ) -> None:
        self.name = agent.id
        self.agent = agent
        self.verbose = verbose
        self.options = options
        self._item_stream = item_stream

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        if self.verbose:
            yield Event.create("codex", "status", f"codex sdk starting in {workdir}")
        try:
            stream = self._item_stream(self.agent, self.options, workdir, prompt)
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        thread_id: Optional[str] = None
        try:
            async for item in stream:
                tid = _item_thread_id(item)
                if tid and tid != thread_id:
                    thread_id = tid
                    yield provider_session_event("codex", self.name, tid, "thread")
                for event in iter_codex_events(item, self.verbose):
                    yield event
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        except Exception as exc:  # surface SDK errors as transcript errors
            yield sdk_error_event("codex", exc)
            return
        if self.verbose:
            yield Event.create("codex", "status", "codex sdk turn complete")


def iter_codex_events(item: Any, verbose: bool) -> Iterator[Event]:
    """Map one Codex thread item onto the standard Event stream."""

    item_type = str(getattr(item, "type", "") or "").lower()

    # Classify by item type FIRST. `is_error` is only the fallback trigger for an
    # otherwise-unclassified item, so a `command_execution`/`apply_patch` item that
    # merely *failed* (carries is_error) still surfaces as its real command/file
    # event with the command string intact, not a bare "codex sdk error".
    if item_type == "error":
        text = stringify(getattr(item, "message", None)) or stringify(getattr(item, "text", None))
        yield Event.create("error", "error", text or "codex sdk error", _item_raw(item))
        return

    if item_type in {"command_execution", "command", "exec_command", "local_shell_call"}:
        command = getattr(item, "command", None)
        text = command if isinstance(command, str) else compact_json(command)
        yield Event.create("tool", "command", text, {"command": command, "type": item_type})
        return

    if item_type in {"file_change", "patch", "apply_patch", "file_update"}:
        changes = getattr(item, "changes", None)
        path = getattr(item, "path", None)
        text = str(path) if isinstance(path, str) and path else compact_json(changes)
        yield Event.create("tool", "file_change", text, {"changes": changes, "path": path, "type": item_type})
        return

    if item_type in {"reasoning", "agent_reasoning", "thinking"}:
        if verbose:
            reasoning = stringify(getattr(item, "text", None))
            if reasoning:
                yield Event.create("codex", "status", reasoning, {"reasoning": True})
        return

    # agent_message / assistant_message / message, or an unclassified item that
    # still carries prose: degrade to a codex message (message-only fidelity).
    text = stringify(getattr(item, "text", None))
    if text:
        yield Event.create("codex", "message", text, {"text": text})
        return

    # An unclassified item flagged as an error is the last resort -> error event.
    if getattr(item, "is_error", False):
        yield Event.create("error", "error", "codex sdk error", _item_raw(item))


def _item_thread_id(item: Any) -> Optional[str]:
    tid = getattr(item, "thread_id", None)
    if isinstance(tid, str) and tid:
        return tid
    return None


def _item_raw(item: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    item_type = getattr(item, "type", None)
    if item_type is not None:
        raw["type"] = item_type
    return raw


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    # Explicit mapping, no blind pass-through. Only options with a confirmed SDK
    # equivalent map (model -> thread model, sandbox -> Sandbox enum); the rest
    # (profile, approval_policy, thinking_level/reasoning_effort, search) are
    # cli-only and rejected at start validation for the sdk backend.
    mapped: Dict[str, Any] = {}
    if "model" in options:
        mapped["model"] = options["model"]
    if "sandbox" in options:
        mapped["sandbox"] = options["sandbox"]
    return mapped


def sandbox_member_name(value: Any) -> Optional[str]:
    """Map a ``codex_options.sandbox`` value to the SDK ``Sandbox`` member name."""

    return _SANDBOX_MEMBERS.get(str(value))


def _default_item_stream(
    agent: AgentConfig, options: Dict[str, Any], workdir: Path, prompt: str
) -> AsyncIterator[Any]:
    """Lazily import the real SDK and open the async item stream for one turn.

    Best-effort against a young SDK: any import/attribute drift becomes a
    BackendUnavailable the runner surfaces as an actionable error event rather
    than crashing. Confirm and pin the exact call shape with a live smoke.
    """

    try:
        import openai_codex  # type: ignore
    except ImportError as exc:
        raise BackendUnavailable("codex", "sdk", f"{MODULE_NAME} is not importable", INSTALL_HINT) from exc
    try:
        codex = openai_codex.Codex()
        start_kwargs: Dict[str, Any] = {}
        if "model" in options:
            start_kwargs["model"] = options["model"]
        member = sandbox_member_name(options.get("sandbox")) if "sandbox" in options else None
        if member is not None:
            start_kwargs["sandbox"] = getattr(openai_codex.Sandbox, member)
        thread = codex.start_thread(working_directory=str(workdir), **start_kwargs)
        return thread.run_streamed(prompt)
    except Exception as exc:  # pragma: no cover - live-only path
        raise BackendUnavailable(
            "codex", "sdk", f"could not start a Codex SDK thread: {exc}", INSTALL_HINT
        ) from exc


def build_codex_sdk_backends() -> List[CodexSdkBackend]:
    return [CodexSdkBackend()]
