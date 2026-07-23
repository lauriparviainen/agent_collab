"""The Claude ``sdk`` backend (``claude-agent-sdk``), lazy + first-class.

The real ``claude_agent_sdk`` module is imported **lazily** — only inside the
probe's ``find_spec`` check and the default conversation factory — never at
import time. So importing this module (which the registry does at startup) costs
nothing and needs no dependency; a missing wheel degrades to an *unavailable*
backend (a fast, hinted start rejection) rather than an import crash.

**Conversation lifecycle** (verified on ``claude-agent-sdk`` 0.2.126): one
persistent ``ClaudeSDKClient`` per runner/session accepts sequential
``query()`` / ``receive_response()`` turns on one live provider session whose
id stays stable across turns. After an abnormal turn the conversation adapter
resets the live client but keeps the captured session id; the next turn
reconnects through ``ClaudeAgentOptions(resume=<sid>, fork_session=False)``,
which the CLI either continues under the same id or rejects with a process
error — it never silently starts a fresh session. Local asyncio cancellation
stops only the consumer; the SDK's detached reader and CLI subprocess unwind
via ``disconnect()`` (bounded terminate/kill escalation inside the SDK).

**API mapping** targets ``claude-agent-sdk`` (the renamed ``claude-code-sdk``,
Python 3.10+), whose typed messages are:

- ``AssistantMessage.content`` is a list of blocks: ``TextBlock(.text)`` ->
  ``claude`` message, ``ToolUseBlock(.name/.input)`` -> ``tool`` (classified into
  tool_call/command/file_change), ``ToolResultBlock(.tool_use_id/.content)`` ->
  a correlated tool result/error, and ``ThinkingBlock(.thinking)`` -> a verbose
  status (reasoning text only — the block's ``signature`` is never read/emitted).
- ``ResultMessage``/``SystemMessage`` carry ``session_id`` -> captured as the
  provider session id (``kind="session"``) and fed back to the adapter for
  reconnect; ``is_error`` -> an ``error`` event; successful result usage/cost ->
  a verbose status.

agent-collab never manages credentials: auth (``ANTHROPIC_API_KEY`` or Claude
Code's local sign-in) comes from the passed-through environment, and the first
turn's real error is the authority. The mapper and lifecycle are exercised by
fake-module tests (``tests/backends/claude_sdk/test_backend.py``) built to these
shapes — no live call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Mapping, Optional, Protocol

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
from ..common.health import anthropic_api_key_credentials, probe_sdk_backend
from ..common.sdk import (
    SDK_CLOSE_GRACE_SECONDS,
    backend_unavailable_event,
    classify_tool_kind,
    close_async_stream,
    package_version,
    sdk_settings_summary,
    provider_session_event,
    sdk_error_event,
    stringify,
)
from ..common.options import configured_choices, resolve_claude_thinking

MODULE_NAME = "claude_agent_sdk"
PACKAGE_NAME = "claude-agent-sdk"
INSTALL_HINT = "install the Claude Agent SDK: pip install claude-agent-sdk, or re-run ./agent_collab.sh install"

CLAUDE_SDK_OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))


class ClaudeConversation(Protocol):
    """One runner-owned provider conversation; fakeable without the real SDK."""

    def active(self) -> bool: ...

    def run(self, prompt: str) -> AsyncIterator[Any]: ...

    def note_session_id(self, session_id: str) -> None: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


ConversationFactory = Callable[
    [AgentConfig, Dict[str, Any], Path],
    ClaudeConversation,
]


class ClaudeSdkBackend:
    """Registered as ``(claude, "sdk")`` with live-session continuity."""

    id = "sdk"
    agent_type = "claude"
    brand_color = "#D97757"
    event_fidelity = "typed"
    provider_session_id_kind = "session"

    def __init__(self, conversation_factory: Optional[ConversationFactory] = None) -> None:
        self.capabilities = BackendCapabilities(continuity=True)
        self.checks_credentials = True
        # First-class but opt-in: a missing wheel / import failure fails the start
        # fast with an install hint instead of burning the first turn.
        self.block_on_unavailable = True
        self._conversation_factory = conversation_factory

    def probe(self) -> BackendHealth:
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=anthropic_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(CLAUDE_SDK_OPTION_SCHEMA)

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
        return resolve_claude_thinking(normalized, configured_choices(configured, requested))

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        factory = self._conversation_factory or _default_conversation
        return ClaudeSdkRunner(agent, verbose, dict(options or {}), conversation_factory=factory)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        summary = sdk_settings_summary(PACKAGE_NAME, _map_sdk_options(options))
        # Be explicit that runs do not implicitly load user/project filesystem
        # settings, so behaviour is predictable regardless of the host's config.
        summary["setting_sources"] = "none"
        summary["system_prompt"] = "claude_code"
        summary["tools"] = "claude_code"
        summary["conversation"] = "persistent"
        return summary


class ClaudeSdkRunner(AgentRunner):
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
        self._conversation: Optional[ClaudeConversation] = None
        self._workdir: Optional[Path] = None

    def conversation_active(self) -> bool:
        return self._conversation is not None and self._conversation.active()

    async def close(self) -> None:
        if self._conversation is not None:
            await self._conversation.close()

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        if self.verbose:
            await emit(Event.create("claude", "status", f"claude sdk starting in {workdir}"))
        conversation: Optional[ClaudeConversation] = None
        stream: Optional[AsyncIterator[Any]] = None
        session_id: Optional[str] = None
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        clean_close = True
        try:
            try:
                conversation = self._conversation_for(workdir)
                stream = conversation.run(prompt)
                async for message in stream:
                    sid = _message_session_id(message)
                    if sid and sid != session_id:
                        session_id = sid
                        # Uniform provider-session capture (kind="session"),
                        # fed back to the adapter for reconnect-by-resume.
                        conversation.note_session_id(sid)
                        await emit(provider_session_event("claude", self.name, sid, "session"))
                    if _is_result_message(message):
                        if getattr(message, "is_error", False):
                            evidence.add(TerminalEvidence("failed", "provider_terminal_failure"))
                        else:
                            evidence.add(TerminalEvidence("completed"))
                    for event in iter_claude_events(message, self.verbose):
                        await emit(event)
            finally:
                # Close the response stream first: it releases the adapter's
                # internal lock so a subsequent reset does not have to wait out
                # its grace period behind a still-open generator.
                if stream is not None:
                    clean_close = await close_async_stream(stream)
        except asyncio.CancelledError:
            if conversation is not None:
                await _reset_conversation_bounded(conversation)
            raise
        except BackendUnavailable as exc:
            await emit(backend_unavailable_event(exc))
            exception_code = "provider_transport_failed"
        except Exception as exc:  # startup, auth, resume, and turn errors reach the transcript
            await emit(sdk_error_event("claude", exc))
            exception_code = "provider_transport_failed"
        if not clean_close and exception_code is None:
            exception_code = "provider_transport_failed"
        result = evidence.resolve(exception_code=exception_code)
        if result.outcome != "completed" and conversation is not None:
            await _reset_conversation_bounded(conversation)
        if self.verbose:
            await emit(Event.create("claude", "status", "claude sdk turn complete"))
        return result

    def _conversation_for(self, workdir: Path) -> ClaudeConversation:
        resolved = workdir.resolve()
        if self._conversation is None:
            self._conversation = self._conversation_factory(
                self.agent,
                self.options,
                resolved,
            )
            self._workdir = resolved
        elif self._workdir != resolved:
            raise RuntimeError("claude sdk conversation workdir changed between turns")
        return self._conversation


def _is_result_message(message: Any) -> bool:
    return not isinstance(getattr(message, "content", None), list) and hasattr(message, "is_error")


def iter_claude_events(message: Any, verbose: bool) -> Iterator[Event]:
    """Map one SDK message onto the standard Event stream.

    Content blocks -> text/tool/thinking events; a terminal ``ResultMessage`` ->
    an ``error`` event (``is_error``) or a verbose status; everything else with no
    prose degrades to nothing, the same honest fidelity as the cli parser.
    """

    content = getattr(message, "content", None)
    if isinstance(content, list):
        yield from _map_claude_content(content, verbose)
        assistant_error = stringify(getattr(message, "error", None))
        if assistant_error:
            raw: Dict[str, Any] = {"error": assistant_error}
            for field in ("model", "usage"):
                value = getattr(message, field, None)
                if value is not None:
                    raw[field] = value
            yield Event.create("error", "error", f"claude sdk error: {assistant_error}", raw)
        return
    if getattr(message, "is_error", False):
        text = _result_error_text(message)
        yield Event.create("error", "error", text, {**_result_raw(message), "fatal": True})
        return
    if verbose:
        subtype = getattr(message, "subtype", None)
        if isinstance(subtype, str) and subtype:
            yield Event.create(
                "claude", "status", _result_status_text(message), _result_raw(message)
            )


def _map_claude_content(content: List[Any], verbose: bool) -> Iterator[Event]:
    text_parts: List[str] = []
    tool_events: List[Event] = []
    thinking_parts: List[str] = []
    for block in content:
        tool_use_id = getattr(block, "tool_use_id", None)
        if isinstance(tool_use_id, str) and tool_use_id:  # ToolResultBlock
            tool_events.append(_map_claude_tool_result(block))
            continue
        name = getattr(block, "name", None)
        tool_input = getattr(block, "input", None)
        if name is not None and tool_input is not None:  # ToolUseBlock
            tool_events.append(_map_claude_tool_use(name, tool_input, block))
            continue
        thinking = getattr(block, "thinking", None)  # ThinkingBlock (never .signature)
        if isinstance(thinking, str) and thinking.strip():
            thinking_parts.append(thinking.strip())
            continue
        text = getattr(block, "text", None)  # TextBlock
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())

    if verbose and thinking_parts:
        yield Event.create("claude", "status", "\n".join(thinking_parts), {"reasoning": True})
    for event in tool_events:
        yield event
    if text_parts:
        text = "\n".join(text_parts)
        yield Event.create("claude", "message", text, {"text": text})


def _map_claude_tool_use(name: Any, tool_input: Any, block: Any) -> Event:
    name_str = str(name)
    kind = classify_tool_kind(name_str)
    text = f"{name_str} {compact_json(tool_input)}" if tool_input else name_str
    return Event.create(
        "tool",
        kind,
        text,
        {"name": name_str, "input": tool_input, "id": getattr(block, "id", None)},
    )


def _map_claude_tool_result(block: Any) -> Event:
    tool_use_id = str(getattr(block, "tool_use_id"))
    content = getattr(block, "content", None)
    detail = stringify(content)
    if not detail and content is not None:
        detail = compact_json(content)
    raw = {
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": getattr(block, "is_error", None),
    }
    if getattr(block, "is_error", False):
        text = detail or f"claude tool result {tool_use_id} failed"
        return Event.create("error", "error", text, raw)
    text = f"tool result {tool_use_id}"
    if detail:
        text = f"{text}: {detail}"
    return Event.create("tool", "tool_call", text, raw)


def _message_session_id(message: Any) -> Optional[str]:
    sid = getattr(message, "session_id", None)
    if isinstance(sid, str) and sid:
        return sid
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    return None


def _result_raw(message: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    # Deliberately enumerate the public result fields. In particular, never
    # serialize the full message/block object: thinking signatures are opaque
    # verification data and must not reach transcripts.
    for field in (
        "subtype",
        "duration_ms",
        "duration_api_ms",
        "is_error",
        "num_turns",
        "session_id",
        "stop_reason",
        "total_cost_usd",
        "usage",
        "model_usage",
        "errors",
        "api_error_status",
    ):
        value = getattr(message, field, None)
        if value is not None:
            raw[field] = value
    return raw


def _result_error_text(message: Any) -> str:
    result = stringify(getattr(message, "result", None))
    if result:
        return result
    errors = getattr(message, "errors", None)
    if isinstance(errors, list):
        text = "; ".join(item.strip() for item in errors if isinstance(item, str) and item.strip())
        if text:
            return text
    status = getattr(message, "api_error_status", None)
    return f"claude sdk error (HTTP {status})" if status is not None else "claude sdk error"


def _result_status_text(message: Any) -> str:
    subtype = stringify(getattr(message, "subtype", None)) or "result"
    parts = [subtype]
    cost = getattr(message, "total_cost_usd", None)
    if cost is not None:
        parts.append(f"cost_usd={cost}")
    usage = getattr(message, "usage", None)
    if usage is not None:
        parts.append(f"usage={compact_json(usage)}")
    return "; ".join(parts)


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    # Explicit mapping, no blind pass-through. Only options with a confirmed
    # ClaudeAgentOptions equivalent map to the sdk. The public request names
    # stay provider-neutral while the installed SDK uses effort/token names.
    mapped: Dict[str, Any] = {}
    for key in ("model", "permission_mode"):
        if key in options:
            mapped[key] = options[key]
    if "thinking_level" in options:
        mapped["effort"] = options["thinking_level"]
    if "thinking_budget_tokens" in options:
        mapped["max_thinking_tokens"] = options["thinking_budget_tokens"]
    return mapped


def build_claude_agent_options(
    options_cls: Any,
    options: Dict[str, Any],
    workdir: Path,
    resume_session_id: Optional[str] = None,
) -> Any:
    """Construct the verified ``ClaudeAgentOptions`` coding-agent configuration.

    The coding prompt/tool presets and empty filesystem setting sources are
    intentional runtime semantics. If an installed SDK does not accept them,
    its constructor error must remain visible rather than weakening the run.
    ``resume_session_id`` is set only when reconnecting a captured provider
    session (with ``fork_session=False`` so the id is continued, not forked);
    a fresh first connection must omit both fields.
    """

    mapped = _map_sdk_options(options)
    if resume_session_id is not None:
        mapped["resume"] = resume_session_id
        mapped["fork_session"] = False
    return options_cls(
        **mapped,
        cwd=str(workdir),
        setting_sources=[],
        system_prompt={"type": "preset", "preset": "claude_code"},
        tools={"type": "preset", "preset": "claude_code"},
    )


def _backend_unavailable(reason: str) -> BackendUnavailable:
    return BackendUnavailable("claude", "sdk", reason, INSTALL_HINT)


def _default_conversation(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
) -> ClaudeConversation:
    """Build one lazy-imported persistent conversation for a runner."""

    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore
    except ImportError as exc:
        raise _backend_unavailable(f"{MODULE_NAME} is not importable") from exc
    for attr in ("connect", "query", "receive_response", "disconnect"):
        if not hasattr(ClaudeSDKClient, attr):
            raise _backend_unavailable(
                "claude_agent_sdk has no compatible ClaudeSDKClient "
                "connect/query/receive_response/disconnect API"
            )
    return _PersistentClaudeConversation(ClaudeSDKClient, ClaudeAgentOptions, options, workdir)


class _PersistentClaudeConversation:
    """Serialize one live SDK client and its reconnect identity."""

    def __init__(
        self,
        client_cls: Any,
        options_cls: Any,
        options: Dict[str, Any],
        workdir: Path,
    ) -> None:
        self._client_cls = client_cls
        self._options_cls = options_cls
        self._options = dict(options)
        self._workdir = workdir
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._session_id: Optional[str] = None
        self._pending_prompt: Optional[str] = None
        self._closed = False

    def active(self) -> bool:
        # A reset drops only the live client. The retained id still names
        # provider-side context that the next run will resume, so the referee
        # must keep sending delta prompts rather than replaying the full task.
        return not self._closed and (
            self._session_id is not None or self._pending_prompt is not None
        )

    def note_session_id(self, session_id: str) -> None:
        if self._session_id is not None and self._session_id != session_id:
            raise RuntimeError("Claude resumed a different provider session")
        self._session_id = session_id

    async def run(self, prompt: str) -> AsyncIterator[Any]:
        # Referee watermarks advance when a prompt is built, before transport
        # delivery. Queue it before waiting for the lifecycle lock so a failed
        # connect/resume—or cancellation behind a slow reset—cannot orphan that
        # delta. Once handed to the client's query() call, delivery is
        # uncertain (a cancel or write error may land after the CLI accepted
        # the message) and replay would risk a duplicate provider turn, so
        # clear it at that hand-off boundary.
        self._pending_prompt = _join_pending_prompt(self._pending_prompt, prompt)
        async with self._lock:
            if self._closed:
                raise RuntimeError("claude sdk conversation is closed")
            if self._client is None:
                await self._connect_locked()
            effective_prompt = self._pending_prompt
            if effective_prompt is None:
                raise RuntimeError("claude sdk pending prompt was lost")
            self._pending_prompt = None
            await self._client.query(effective_prompt)
            async for message in self._client.receive_response():
                yield message

    async def reset(self) -> None:
        async with self._lock:
            client = self._client
            self._client = None
            if client is not None:
                await client.disconnect()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
            self._session_id = None
            self._pending_prompt = None
            if client is not None:
                await client.disconnect()

    async def _connect_locked(self) -> None:
        # A captured id always reconnects through the provider's native resume
        # (continuing, not forking, the session). A rejected/expired resume
        # surfaces as the CLI's connect error — never a silent fresh session.
        resume_id = self._session_id
        client = self._client_cls(
            build_claude_agent_options(
                self._options_cls,
                self._options,
                self._workdir,
                resume_session_id=resume_id,
            )
        )
        try:
            await client.connect()
        except BaseException:
            # The SDK's connect() unwinds its own partial state on failure; a
            # second disconnect is idempotent and covers API drift.
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self._client = client


def _join_pending_prompt(pending: Optional[str], prompt: str) -> str:
    if not pending:
        return prompt
    return f"{pending}\n\n{prompt}"


async def _reset_conversation_bounded(conversation: ClaudeConversation) -> bool:
    """Reset once; a slow SDK disconnect continues as a background reaper."""

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
