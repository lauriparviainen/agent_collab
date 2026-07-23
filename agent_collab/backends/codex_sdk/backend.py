"""The Codex ``sdk`` backend (``openai-codex``), lazy + first-class.

The installed ``openai-codex==0.1.0b3`` surface keeps one ``AsyncCodex`` client
and ``AsyncThread`` open across collected ``thread.run(...)`` calls. A captured
thread id reconnects through ``AsyncCodex.thread_resume(...)`` after an abnormal
turn resets the live client. The conversation adapter serializes run/reset/close
because SDK cancellation stops only the asyncio waiter while its blocking worker
continues until the provider turn or client transport settles.

``run`` returns one collected ``TurnResult``. Its ``final_response`` is the
stable, message-first surface; its ``items`` are ``ThreadItem`` root models.
Only installed public item roots are mapped. No SDK import, client construction,
or model call happens at module import time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Protocol

from ...config import AgentConfig
from ...events import Event, compact_json
from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from ...runners import AgentRunner, AsyncEventSink
from ..base import (
    BackendCapabilities,
    BackendHealth,
    BackendUnavailable,
    OptionSpec,
    load_option_schema,
    normalize_declared_options,
)
from ..common.health import codex_api_key_credentials, probe_sdk_backend
from ..common.sdk import (
    SDK_CLOSE_GRACE_SECONDS,
    agent_environment,
    backend_unavailable_event,
    package_version,
    sdk_settings_summary,
    provider_session_event,
    sdk_error_event,
    stringify,
)
from ..common.options import configured_choices, resolve_codex_effort

MODULE_NAME = "openai_codex"
PACKAGE_NAME = "openai-codex"
INSTALL_HINT = (
    "install the Codex SDK: pip install openai-codex, or re-run ./agent_collab.sh install"
)

CODEX_SDK_OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))

# ``codex_sdk.sandbox`` -> the verified Codex SDK ``Sandbox`` member.
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


class CodexConversation(Protocol):
    """One runner-owned provider conversation; fakeable without the real SDK."""

    def active(self) -> bool: ...

    async def run(self, prompt: str) -> CodexTurnOutcome: ...

    def note_session_id(self, thread_id: str) -> None: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


ConversationFactory = Callable[
    [AgentConfig, Dict[str, Any], Path],
    CodexConversation,
]


class CodexSdkBackend:
    """Registered as ``(codex, "sdk")`` with live-session continuity."""

    id = "sdk"
    agent_type = "codex"
    brand_color = "#10A37F"
    event_fidelity = "message_first"
    provider_session_id_kind = "thread"

    def __init__(self, conversation_factory: Optional[ConversationFactory] = None) -> None:
        self.capabilities = BackendCapabilities(continuity=True)
        self.checks_credentials = True
        self.block_on_unavailable = True
        self._conversation_factory = conversation_factory

    def probe(self) -> BackendHealth:
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=codex_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(CODEX_SDK_OPTION_SCHEMA)

    def normalize_options(
        self,
        agent: AgentConfig,
        requested: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        configured = agent.options_for(self.id)
        normalized = normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=configured,
            configured_defaults=agent.default_options_for(self.id),
        )
        return resolve_codex_effort(normalized, configured_choices(configured, requested))

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        factory = self._conversation_factory or _default_conversation
        return CodexSdkRunner(agent, verbose, dict(options or {}), conversation_factory=factory)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        summary = sdk_settings_summary(PACKAGE_NAME, _map_sdk_options(options))
        summary["conversation"] = "persistent"
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
        conversation_factory: ConversationFactory,
    ) -> None:
        self.name = agent.id
        self.agent = agent
        self.verbose = verbose
        self.options = options
        self._conversation_factory = conversation_factory
        self._conversation: Optional[CodexConversation] = None
        self._workdir: Optional[Path] = None

    def conversation_active(self) -> bool:
        return self._conversation is not None and self._conversation.active()

    async def close(self) -> None:
        if self._conversation is not None:
            await self._conversation.close()

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        if self.verbose:
            await emit(Event.create("codex", "status", f"codex sdk starting in {workdir}"))

        conversation: Optional[CodexConversation] = None
        thread_id: Optional[str] = None
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        try:
            conversation = self._conversation_for(workdir)
            outcome = await conversation.run(prompt)
            if outcome.thread_id:
                thread_id = outcome.thread_id
                conversation.note_session_id(thread_id)
                await emit(provider_session_event("codex", self.name, thread_id, "thread"))
            status = _enum_value(getattr(outcome.result, "status", None))
            if status == "completed":
                evidence.add(TerminalEvidence("completed"))
            elif status == "interrupted":
                evidence.add(
                    TerminalEvidence(
                        "cancelled",
                        "provider_turn_cancelled",
                        provider_stop_reason="interrupted",
                    )
                )
            elif status == "failed":
                evidence.add(TerminalEvidence("failed", "provider_terminal_failure"))
            else:
                exception_code = "provider_output_invalid"
            for event in iter_codex_turn_events(outcome.result, self.verbose):
                await emit(event)
        except asyncio.CancelledError:
            if conversation is not None:
                await _reset_conversation_bounded(conversation)
            raise
        except BackendUnavailable as exc:
            await emit(backend_unavailable_event(exc))
            exception_code = "provider_transport_failed"
        except Exception as exc:  # startup, auth, and turn errors reach the transcript
            await emit(sdk_error_event("codex", exc))
            exception_code = "provider_transport_failed"

        result = evidence.resolve(exception_code=exception_code)
        if result.outcome != "completed" and conversation is not None:
            await _reset_conversation_bounded(conversation)
        if self.verbose:
            await emit(Event.create("codex", "status", "codex sdk turn complete"))
        return result

    def _conversation_for(self, workdir: Path) -> CodexConversation:
        resolved = workdir.resolve()
        if self._conversation is None:
            self._conversation = self._conversation_factory(
                self.agent,
                self.options,
                resolved,
            )
            self._workdir = resolved
        elif self._workdir != resolved:
            raise RuntimeError("codex sdk conversation workdir changed between turns")
        return self._conversation


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
            {
                "text": final_response,
                "turn_id": turn_id or None,
                "status": status,
                "final": True,
            },
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
        yield Event.create("error", "error", text, {**raw, "fatal": True})
    elif status == "failed":
        yield Event.create(
            "error",
            "error",
            "codex sdk turn failed",
            {"turn_id": turn_id or None, "status": status, "fatal": True},
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
    """Map a ``codex_sdk.sandbox`` value to the SDK enum member name."""

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


def _default_conversation(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
) -> CodexConversation:
    """Build one lazy-imported persistent conversation for a runner."""

    try:
        import openai_codex  # type: ignore
    except ImportError as exc:
        raise _backend_unavailable(f"{MODULE_NAME} is not importable") from exc

    async_codex = getattr(openai_codex, "AsyncCodex", None)
    if (
        async_codex is None
        or not hasattr(async_codex, "thread_start")
        or not hasattr(async_codex, "thread_resume")
        or not hasattr(async_codex, "close")
    ):
        raise _backend_unavailable(
            "openai_codex has no compatible AsyncCodex thread_start/thread_resume/close API"
        )

    client_config = None
    config_kwargs: Dict[str, Any] = {}
    codex_bin = _configured_codex_bin(agent)
    if codex_bin:
        config_kwargs["codex_bin"] = codex_bin
    env = agent_environment(agent)
    if env:
        config_kwargs["env"] = env
    if config_kwargs:
        config_cls = getattr(openai_codex, "CodexConfig", None)
        if config_cls is None:
            raise _backend_unavailable("openai_codex has no compatible CodexConfig API")
        try:
            client_config = config_cls(**config_kwargs)
        except Exception as exc:
            raise _backend_unavailable(f"could not configure Codex SDK client: {exc}") from exc

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

    return _PersistentCodexConversation(
        async_codex,
        client_config,
        start_kwargs,
        run_kwargs,
    )


class _PersistentCodexConversation:
    """Serialize one live SDK client/thread and its reconnect identity."""

    def __init__(
        self,
        client_factory: Any,
        client_config: Any,
        thread_kwargs: Dict[str, Any],
        run_kwargs: Dict[str, Any],
    ) -> None:
        self._client_factory = client_factory
        self._client_config = client_config
        self._thread_kwargs = dict(thread_kwargs)
        self._run_kwargs = dict(run_kwargs)
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._thread: Any = None
        self._thread_id: Optional[str] = None
        self._pending_prompt: Optional[str] = None
        self._closed = False

    def active(self) -> bool:
        # A reset drops only the live transport. The retained id still names
        # provider-side context that the next run will resume, so the referee
        # must keep sending delta prompts rather than replaying the full task.
        return not self._closed and (
            self._thread_id is not None or self._pending_prompt is not None
        )

    def note_session_id(self, thread_id: str) -> None:
        if self._thread_id is not None and self._thread_id != thread_id:
            raise RuntimeError("Codex resumed a different provider thread")
        self._thread_id = thread_id

    async def run(self, prompt: str) -> CodexTurnOutcome:
        # Referee watermarks advance when a prompt is built, before transport
        # delivery. Queue it before waiting for the lifecycle lock so a failed
        # connect/resume—or cancellation behind a slow reset—cannot orphan that
        # delta. Once handed to thread.run(), delivery is uncertain and replay
        # would risk duplication, so clear it at that boundary.
        self._pending_prompt = _join_pending_prompt(self._pending_prompt, prompt)
        async with self._lock:
            if self._closed:
                raise RuntimeError("codex sdk conversation is closed")
            if self._thread is None:
                await self._connect_locked()
            thread_id = stringify(getattr(self._thread, "id", None))
            run = getattr(self._thread, "run", None)
            if not thread_id or not callable(run):
                raise _backend_unavailable("openai_codex returned an incompatible AsyncThread")
            effective_prompt = self._pending_prompt
            if effective_prompt is None:
                raise RuntimeError("codex sdk pending prompt was lost")
            self._pending_prompt = None
            result = await _await_provider_run(run(effective_prompt, **self._run_kwargs))
            if not hasattr(result, "final_response") or not hasattr(result, "items"):
                raise _backend_unavailable("openai_codex returned an incompatible TurnResult")
            return CodexTurnOutcome(thread_id=thread_id, result=result)

    async def reset(self) -> None:
        async with self._lock:
            client = self._drop_live_locked(keep_thread_id=True)
            if client is not None:
                await client.close()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._drop_live_locked(keep_thread_id=False)
            if client is not None:
                await client.close()

    async def _connect_locked(self) -> None:
        client = (
            self._client_factory(self._client_config)
            if self._client_config is not None
            else self._client_factory()
        )
        try:
            await client.__aenter__()
            resume_id = self._thread_id
            if resume_id is None:
                thread = await client.thread_start(**self._thread_kwargs)
            else:
                thread = await client.thread_resume(resume_id, **self._thread_kwargs)
            thread_id = stringify(getattr(thread, "id", None))
            if not thread_id or not callable(getattr(thread, "run", None)):
                raise _backend_unavailable("openai_codex returned an incompatible AsyncThread")
            if resume_id is not None and thread_id != resume_id:
                raise _backend_unavailable("openai_codex resumed a different provider thread")
        except BaseException:
            await client.close()
            raise
        self._client = client
        self._thread = thread
        self._thread_id = thread_id

    def _drop_live_locked(self, *, keep_thread_id: bool) -> Any:
        client = self._client
        self._client = None
        self._thread = None
        if not keep_thread_id:
            self._thread_id = None
            self._pending_prompt = None
        return client


def _join_pending_prompt(pending: Optional[str], prompt: str) -> str:
    if not pending:
        return prompt
    return f"{pending}\n\n{prompt}"


async def _await_provider_run(awaitable: Any) -> Any:
    """Keep SDK worker ownership until a cancellation-insensitive run settles."""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise


async def _reset_conversation_bounded(conversation: CodexConversation) -> bool:
    """Reset once; a slow SDK close continues as a background reaper."""

    task = asyncio.create_task(conversation.reset())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=SDK_CLOSE_GRACE_SECONDS)
        return True
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_background_result)
        return False
    except asyncio.CancelledError:
        task.add_done_callback(_consume_background_result)
        raise
    except Exception:
        return False


def _consume_background_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except BaseException:
        pass
