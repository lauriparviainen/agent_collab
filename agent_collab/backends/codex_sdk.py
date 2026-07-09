"""The Codex ``sdk`` backend (``openai-codex``), lazy + first-class.

The real ``openai_codex`` module is imported lazily by the production turn
factory.  The verified ``openai-codex==0.1.0b3`` async surface is:

``async with AsyncCodex() -> await thread_start(...) -> await thread.run(...)``.

``run`` returns one collected ``TurnResult``.  Its ``final_response`` is the
stable, message-first surface; its ``items`` are ``ThreadItem`` root models.
Only the installed public item roots are mapped here: agent messages, reasoning,
command execution, and file changes.  Unknown roots remain verbose status data
instead of being treated like guessed ``codex exec --json`` events.

The async generator used for the production path deliberately yields the
collected result *inside* the ``AsyncCodex`` context.  Consequently the SDK
client and app-server connection stay alive until the runner has mapped every
event.  No SDK import, client construction, or model call happens at module
import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
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

# ``codex_options.sandbox`` -> the verified Codex SDK ``Sandbox`` member.
_SANDBOX_MEMBERS = {
    "read-only": "read_only",
    "workspace-write": "workspace_write",
    "danger-full-access": "full_access",
}


@dataclass(frozen=True)
class CodexTurnOutcome:
    """One collected SDK turn and the public id of its owning thread."""

    thread_id: str
    result: Any


# A factory owns the SDK resources for one turn and yields its collected result
# while those resources are still open.  It is injectable so unit tests use
# real-shape fakes without importing the SDK or making a live model call.
ItemStreamFactory = Callable[
    [AgentConfig, Dict[str, Any], Path, str], AsyncIterator[CodexTurnOutcome]
]


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
        codex_bin = _configured_codex_bin(agent)
        summary["runtime"] = "configured_cli" if codex_bin else "sdk_pinned"
        if codex_bin:
            summary["codex_bin"] = codex_bin
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

        stream: Optional[AsyncIterator[CodexTurnOutcome]] = None
        thread_id: Optional[str] = None
        try:
            stream = self._item_stream(self.agent, self.options, workdir, prompt)
            async for outcome in stream:
                if outcome.thread_id and outcome.thread_id != thread_id:
                    thread_id = outcome.thread_id
                    yield provider_session_event("codex", self.name, thread_id, "thread")
                for event in iter_codex_turn_events(outcome.result, self.verbose):
                    yield event
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        except Exception as exc:  # startup, auth, and turn errors reach the transcript
            yield sdk_error_event("codex", exc)
            return
        finally:
            # Explicitly close an injected/production async generator if the
            # consumer cancels before exhausting the transcript.  This unwinds
            # the production AsyncCodex context promptly.
            close = getattr(stream, "aclose", None)
            if callable(close):
                await close()

        if self.verbose:
            yield Event.create("codex", "status", "codex sdk turn complete")


def iter_codex_turn_events(result: Any, verbose: bool) -> Iterator[Event]:
    """Map one verified ``TurnResult`` onto standard transcript events.

    The final response is emitted first because it is the SDK's stable collected
    message surface.  The corresponding final ``AgentMessageThreadItem`` is
    suppressed to avoid duplicating that response; other captured item roots are
    still mapped afterward.
    """

    turn_id = stringify(getattr(result, "id", None))
    status = _enum_value(getattr(result, "status", None))
    final_response = stringify(getattr(result, "final_response", None))

    if final_response:
        yield Event.create(
            "codex",
            "message",
            final_response,
            {"text": final_response, "turn_id": turn_id or None, "status": status},
        )

    items = getattr(result, "items", None)
    if isinstance(items, list):
        for wrapped_item in items:
            item = _item_root(wrapped_item)
            if _is_collected_final_message(item, final_response):
                continue
            yield from iter_codex_events(item, verbose)

    error = getattr(result, "error", None)
    if error is not None:
        text = stringify(getattr(error, "message", None)) or "codex sdk turn failed"
        raw: Dict[str, Any] = {"turn_id": turn_id or None, "status": status}
        details = stringify(getattr(error, "additional_details", None))
        if details:
            raw["additional_details"] = details
        yield Event.create("error", "error", text, raw)
    elif status == "failed":
        yield Event.create(
            "error",
            "error",
            "codex sdk turn failed",
            {"turn_id": turn_id or None, "status": status},
        )


def iter_codex_events(item: Any, verbose: bool) -> Iterator[Event]:
    """Map one installed-SDK ``ThreadItem`` root onto standard events."""

    item = _item_root(item)
    item_type = stringify(getattr(item, "type", None))
    item_id = stringify(getattr(item, "id", None))

    if item_type == "agentMessage":
        text = stringify(getattr(item, "text", None))
        if text:
            yield Event.create(
                "codex",
                "message",
                text,
                {
                    "text": text,
                    "item_id": item_id or None,
                    "phase": _enum_value(getattr(item, "phase", None)),
                },
            )
        return

    if item_type == "reasoning":
        if verbose:
            summary = _string_list(getattr(item, "summary", None))
            content = _string_list(getattr(item, "content", None))
            reasoning = summary or content
            if reasoning:
                yield Event.create(
                    "codex",
                    "status",
                    "\n".join(reasoning),
                    {
                        "reasoning": True,
                        "item_id": item_id or None,
                        "summary": summary,
                        "content": content,
                    },
                )
        return

    if item_type == "commandExecution":
        command = stringify(getattr(item, "command", None))
        if command:
            yield Event.create(
                "tool",
                "command",
                command,
                {
                    "item_id": item_id or None,
                    "command": command,
                    "cwd": _scalar_value(getattr(item, "cwd", None)),
                    "status": _enum_value(getattr(item, "status", None)),
                    "exit_code": getattr(item, "exit_code", None),
                    "aggregated_output": getattr(item, "aggregated_output", None),
                    "duration_ms": getattr(item, "duration_ms", None),
                },
            )
        return

    if item_type == "fileChange":
        changes = _file_changes(getattr(item, "changes", None))
        paths = [change["path"] for change in changes if change.get("path")]
        text = ", ".join(paths) or compact_json(changes)
        yield Event.create(
            "tool",
            "file_change",
            text,
            {
                "item_id": item_id or None,
                "changes": changes,
                "status": _enum_value(getattr(item, "status", None)),
            },
        )
        return

    if verbose and item_type:
        yield Event.create(
            "codex",
            "status",
            f"codex sdk item {item_type}",
            {"item_type": item_type, "item_id": item_id or None},
        )


def _item_root(item: Any) -> Any:
    """Unwrap the SDK's ``ThreadItem(RootModel[...])`` object."""

    root = getattr(item, "root", None)
    return root if root is not None else item


def _is_collected_final_message(item: Any, final_response: str) -> bool:
    if not final_response or stringify(getattr(item, "type", None)) != "agentMessage":
        return False
    if stringify(getattr(item, "text", None)) != final_response:
        return False
    phase = _enum_value(getattr(item, "phase", None))
    # TurnResult derives final_response from final_answer, falling back to an
    # agent message whose phase is absent.
    return phase in (None, "final_answer")


def _enum_value(value: Any) -> Optional[str]:
    # Most generated statuses are Enum values. PatchChangeKind is instead a
    # RootModel whose root has a literal ``type`` discriminator.
    root = getattr(value, "root", value)
    raw = getattr(root, "value", root)
    if not isinstance(raw, str):
        raw = getattr(root, "type", None)
    return raw if isinstance(raw, str) and raw else None


def _scalar_value(value: Any) -> Any:
    root = getattr(value, "root", value)
    enum_value = getattr(root, "value", root)
    if enum_value is None or isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return str(enum_value)


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [part.strip() for part in value if isinstance(part, str) and part.strip()]


def _file_changes(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    changes: List[Dict[str, Any]] = []
    for change in value:
        path = stringify(getattr(change, "path", None))
        diff = stringify(getattr(change, "diff", None))
        entry: Dict[str, Any] = {
            "path": path or None,
            "kind": _enum_value(getattr(change, "kind", None)),
            "diff": diff or None,
        }
        changes.append(entry)
    return changes


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only options with a verified SDK equivalent."""

    mapped: Dict[str, Any] = {}
    if "model" in options:
        mapped["model"] = options["model"]
    if "sandbox" in options:
        mapped["sandbox"] = options["sandbox"]
    effort = options.get("reasoning_effort", options.get("thinking_level"))
    if effort is not None:
        mapped["reasoning_effort"] = effort
    return mapped


def sandbox_member_name(value: Any) -> Optional[str]:
    """Map a ``codex_options.sandbox`` value to the SDK enum member name."""

    return _SANDBOX_MEMBERS.get(str(value))


def _backend_unavailable(reason: str) -> BackendUnavailable:
    return BackendUnavailable("codex", "sdk", reason, INSTALL_HINT)


def _configured_codex_bin(agent: AgentConfig) -> Optional[str]:
    """Resolve the agent's configured Codex CLI for an intentional SDK override.

    The latest Python beta can lag newly selected Codex models because it pins a
    CLI runtime. Reusing the explicitly configured local executable keeps the
    SDK transport/API while honoring the project's normal Codex runtime. The
    SDK-pinned runtime remains the fallback when no executable is configured or
    resolvable.
    """

    command = agent.command
    if not isinstance(command, str) or not command.strip():
        return None
    return shutil.which(command.strip())


async def _default_item_stream(
    agent: AgentConfig, options: Dict[str, Any], workdir: Path, prompt: str
) -> AsyncIterator[CodexTurnOutcome]:
    """Run one turn through the verified async SDK while owning its resources."""

    try:
        import openai_codex  # type: ignore
    except ImportError as exc:
        raise _backend_unavailable(f"{MODULE_NAME} is not importable") from exc

    async_codex = getattr(openai_codex, "AsyncCodex", None)
    if async_codex is None or not hasattr(async_codex, "thread_start"):
        raise _backend_unavailable("openai_codex has no compatible AsyncCodex.thread_start API")

    client_config = None
    codex_bin = _configured_codex_bin(agent)
    if codex_bin:
        config_cls = getattr(openai_codex, "CodexConfig", None)
        if config_cls is None:
            raise _backend_unavailable("openai_codex has no compatible CodexConfig API")
        try:
            client_config = config_cls(codex_bin=codex_bin)
        except Exception as exc:
            raise _backend_unavailable(f"could not configure Codex executable {codex_bin!r}: {exc}") from exc

    mapped = _map_sdk_options(options)
    start_kwargs: Dict[str, Any] = {"cwd": str(workdir)}
    if "model" in mapped:
        start_kwargs["model"] = mapped["model"]

    if "sandbox" in mapped:
        member = sandbox_member_name(mapped["sandbox"])
        sandbox = getattr(openai_codex, "Sandbox", None)
        if member is None or sandbox is None or not hasattr(sandbox, member):
            raise _backend_unavailable(
                f"openai_codex has no compatible Sandbox value for {mapped['sandbox']!r}"
            )
        start_kwargs["sandbox"] = getattr(sandbox, member)

    run_kwargs: Dict[str, Any] = {}
    if "reasoning_effort" in mapped:
        generated = getattr(openai_codex, "generated", None)
        v2_all = getattr(generated, "v2_all", None)
        effort_cls = getattr(v2_all, "ReasoningEffort", None)
        effort_name = str(mapped["reasoning_effort"])
        if effort_cls is None or not hasattr(effort_cls, effort_name):
            raise _backend_unavailable(
                f"openai_codex has no compatible ReasoningEffort value for {effort_name!r}"
            )
        run_kwargs["effort"] = getattr(effort_cls, effort_name)

    # Yield from inside the context: the runner maps the provider session and
    # every TurnResult item before asking this generator for its next value,
    # which is when __aexit__ finally closes the SDK client.
    client = async_codex(client_config) if client_config is not None else async_codex()
    async with client as codex:
        thread = await codex.thread_start(**start_kwargs)
        thread_id = stringify(getattr(thread, "id", None))
        run = getattr(thread, "run", None)
        if not thread_id or not callable(run):
            raise _backend_unavailable("openai_codex returned an incompatible AsyncThread")
        result = await run(prompt, **run_kwargs)
        if not hasattr(result, "final_response") or not hasattr(result, "items"):
            raise _backend_unavailable("openai_codex returned an incompatible TurnResult")
        yield CodexTurnOutcome(thread_id=thread_id, result=result)


def build_codex_sdk_backends() -> List[CodexSdkBackend]:
    return [CodexSdkBackend()]
