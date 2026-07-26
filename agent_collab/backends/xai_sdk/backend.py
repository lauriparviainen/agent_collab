"""Message-only remote chat backend for the official ``xai-sdk`` package.

The verified 1.17.0 surface is imported only inside the production conversation
factory. One runner retains an ``AsyncClient`` and chains stored completions
through ``store_messages=True`` plus ``previous_response_id``. Every turn builds
a fresh ``Chat`` containing only that turn's user prompt; xAI prepends prior
history on the server. Response ids are captured as the changing continuation
identity and stored completions are deleted best-effort on final close.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Protocol

from ...backend_contract import (
    BackendOptionError,
    OptionSpec,
    load_option_schema,
    normalize_declared_options,
)
from ...config import AgentConfig
from ...events import Event
from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from ...runners import AgentRunner, AsyncEventSink
from ...sandbox.specs import UnsupportedSandboxAdapter
from ..base import BackendCapabilities, BackendHealth, BackendUnavailable
from ..common.health import probe_sdk_backend, xai_api_key_credentials
from ..common.options import canonical_reasoning
from ..common.sdk import (
    SDK_CLOSE_GRACE_SECONDS,
    agent_environment,
    backend_unavailable_event,
    package_version,
    provider_session_event,
    sdk_error_event,
    sdk_settings_summary,
    stringify,
)

MODULE_NAME = "xai_sdk"
PACKAGE_NAME = "xai-sdk"
INSTALL_HINT = "install the xAI SDK: pip install xai-sdk, or re-run ./agent_collab.sh install"

OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))


class XaiConversation(Protocol):
    """One runner-owned provider conversation; fakeable without the real SDK."""

    def active(self) -> bool: ...

    async def run(self, prompt: str) -> Any: ...

    def note_session_id(self, response_id: str) -> None: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


ConversationFactory = Callable[
    [AgentConfig, Dict[str, Any], Path],
    XaiConversation,
]


def _map_sdk_options(options: Mapping[str, Any]) -> Dict[str, Any]:
    mapped: Dict[str, Any] = {}
    if options.get("model"):
        mapped["model"] = options["model"]
    if options.get("thinking_level"):
        mapped["reasoning_effort"] = options["thinking_level"]
    return mapped


class XaiSdkBackend:
    sandbox_adapter = UnsupportedSandboxAdapter()
    id = "sdk"
    agent_type = "xai"
    # xAI's brand is monochrome rather than a single signature hue. A mid-light
    # neutral remains legible on both dark and light terminal backgrounds.
    brand_color = "#A0A0A0"
    event_fidelity = "message_only"
    provider_session_id_kind = "response"
    capabilities = BackendCapabilities(continuity=True)
    checks_credentials = True
    block_on_unavailable = True

    def __init__(self, conversation_factory: Optional[ConversationFactory] = None) -> None:
        self._conversation_factory = conversation_factory

    def probe(self) -> BackendHealth:
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=xai_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(OPTION_SCHEMA)

    def normalize_options(
        self, agent: AgentConfig, requested: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        normalized = normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=agent.options_for(self.id),
            configured_defaults=agent.default_options_for(self.id),
        )
        model = normalized.get("model")
        if not isinstance(model, str) or not model.strip():
            raise BackendOptionError("model", "must be a non-empty string")
        return canonical_reasoning(normalized)

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        summary = sdk_settings_summary(PACKAGE_NAME, _map_sdk_options(options))
        summary["conversation"] = "persistent"
        return summary

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        factory = self._conversation_factory or _default_conversation
        return XaiSdkRunner(
            agent,
            verbose,
            dict(options or {}),
            conversation_factory=factory,
        )


class XaiSdkRunner(AgentRunner):
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
        self._conversation: Optional[XaiConversation] = None
        self._workdir: Optional[Path] = None

    def conversation_active(self) -> bool:
        return self._conversation is not None and self._conversation.active()

    async def close(self) -> None:
        if self._conversation is not None:
            await self._conversation.close()

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        if self.verbose:
            await emit(Event.create("xai", "status", f"xai sdk starting in {workdir}"))
        conversation: Optional[XaiConversation] = None
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        try:
            conversation = self._conversation_for(workdir)
            response = await conversation.run(prompt)
            response_id = stringify(getattr(response, "id", None))
            if response_id:
                conversation.note_session_id(response_id)
                await emit(provider_session_event("xai", self.name, response_id, "response"))
            else:
                exception_code = "provider_output_invalid"

            content = stringify(getattr(response, "content", None))
            finish_reason = _finish_reason(response)
            tool_calls = getattr(response, "tool_calls", None)
            if tool_calls:
                evidence.add(TerminalEvidence("failed", "provider_output_invalid"))
            elif finish_reason == "STOP":
                evidence.add(
                    TerminalEvidence(
                        "completed" if content else "failed",
                        None if content else "provider_empty_response",
                        provider_stop_reason="STOP",
                    )
                )
            elif finish_reason in {"MAX_TOKENS", "LENGTH"}:
                evidence.add(
                    TerminalEvidence(
                        "failed",
                        "provider_output_incomplete",
                        provider_stop_reason=finish_reason,
                    )
                )
            elif finish_reason is None:
                exception_code = "provider_output_incomplete"
            else:
                evidence.add(
                    TerminalEvidence(
                        "failed",
                        "provider_terminal_failure",
                        provider_stop_reason=finish_reason,
                    )
                )
            for event in iter_xai_response_events(response):
                await emit(event)
        except asyncio.CancelledError:
            if conversation is not None:
                await _reset_conversation_bounded(conversation)
            raise
        except BackendUnavailable as exc:
            await emit(backend_unavailable_event(exc))
            exception_code = "provider_transport_failed"
        except Exception as exc:
            await emit(sdk_error_event("xai", exc))
            exception_code = "provider_transport_failed"
        result = evidence.resolve(exception_code=exception_code)
        if result.outcome != "completed" and conversation is not None:
            await _reset_conversation_bounded(conversation)
        if self.verbose:
            await emit(Event.create("xai", "status", "xai sdk turn complete"))
        return result

    def _conversation_for(self, workdir: Path) -> XaiConversation:
        resolved = workdir.resolve()
        if self._conversation is None:
            self._conversation = self._conversation_factory(
                self.agent,
                self.options,
                resolved,
            )
            self._workdir = resolved
        elif self._workdir != resolved:
            raise RuntimeError("xai sdk conversation workdir changed between turns")
        return self._conversation


def iter_xai_response_events(response: Any) -> Iterator[Event]:
    content = stringify(getattr(response, "content", None))
    if content:
        yield Event.create("xai", "message", content, {"text": content})


def _finish_reason(response: Any) -> Optional[str]:
    value = getattr(response, "finish_reason", None)
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raw = getattr(value, "name", None)
    if not isinstance(raw, str) or not raw:
        return None
    return {
        "REASON_STOP": "STOP",
        "REASON_MAX_LEN": "LENGTH",
        "REASON_MAX_CONTEXT": "LENGTH",
    }.get(raw, raw)


def _default_conversation(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
) -> XaiConversation:
    del workdir
    try:
        from xai_sdk import AsyncClient  # type: ignore
        from xai_sdk.chat import user  # type: ignore
    except ImportError as exc:
        raise BackendUnavailable(
            "xai", "sdk", f"{MODULE_NAME} is not importable", INSTALL_HINT
        ) from exc

    mapped = _map_sdk_options(options)
    if "model" not in mapped:
        raise BackendUnavailable(
            "xai",
            "sdk",
            "an xAI SDK model is required",
            "pass backend_options.xai_sdk.model",
        )
    client_kwargs: Dict[str, Any] = {}
    api_key = agent_environment(agent).get("XAI_API_KEY")
    if api_key:
        client_kwargs["api_key"] = api_key
    return _PersistentXaiConversation(
        AsyncClient,
        user,
        mapped,
        client_kwargs,
    )


class _PersistentXaiConversation:
    """Serialize one client, stored-response chain, and lifecycle."""

    def __init__(
        self,
        client_factory: Any,
        user_factory: Callable[[str], Any],
        chat_kwargs: Dict[str, Any],
        client_kwargs: Dict[str, Any],
    ) -> None:
        self._client_factory = client_factory
        self._user_factory = user_factory
        self._chat_kwargs = dict(chat_kwargs)
        self._client_kwargs = dict(client_kwargs)
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._response_id: Optional[str] = None
        self._stored_response_ids: list[str] = []
        self._pending_prompt: Optional[str] = None
        self._closed = False

    def active(self) -> bool:
        # A reset drops only the live transport. The retained response id still
        # names provider-held context, so the referee must keep sending deltas.
        return not self._closed and (
            self._response_id is not None or self._pending_prompt is not None
        )

    def note_session_id(self, response_id: str) -> None:
        # Unlike thread/session ids, xAI response ids advance on every stored
        # turn. The newest id is the strict continuation point.
        self._capture_response_id(response_id)

    async def run(self, prompt: str) -> Any:
        # Preserve a prompt that has not reached sample(). Once handed to the
        # RPC, delivery is uncertain and replay could duplicate a paid turn.
        self._pending_prompt = _join_pending_prompt(self._pending_prompt, prompt)
        async with self._lock:
            if self._closed:
                raise RuntimeError("xai sdk conversation is closed")
            if self._client is None:
                self._client = self._client_factory(**self._client_kwargs)

            effective_prompt = self._pending_prompt
            if effective_prompt is None:
                raise RuntimeError("xai sdk pending prompt was lost")
            chat_kwargs = {
                **self._chat_kwargs,
                "store_messages": True,
            }
            if self._response_id is not None:
                chat_kwargs["previous_response_id"] = self._response_id
            chat = self._client.chat.create(**chat_kwargs)
            chat.append(self._user_factory(effective_prompt))
            self._pending_prompt = None

            sample_task = asyncio.ensure_future(chat.sample())
            try:
                response = await asyncio.shield(sample_task)
            except asyncio.CancelledError:
                # Own the in-flight RPC until it settles so close/reset cannot
                # race the gRPC channel. Capture a response id if the request
                # completed despite local cancellation, then re-raise.
                try:
                    response = await asyncio.shield(sample_task)
                    self._capture_response_id(stringify(getattr(response, "id", None)))
                except BaseException:
                    pass
                raise
            self._capture_response_id(stringify(getattr(response, "id", None)))
            return response

    async def reset(self) -> None:
        async with self._lock:
            client = self._client
            self._client = None
            if client is not None:
                await client.close()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
            response_ids = list(reversed(self._stored_response_ids))
            self._response_id = None
            self._stored_response_ids.clear()
            self._pending_prompt = None

            if client is None and response_ids:
                client = self._client_factory(**self._client_kwargs)

            first_error: Optional[BaseException] = None
            if client is not None:
                delete = getattr(getattr(client, "chat", None), "delete_stored_completion", None)
                if callable(delete):
                    for response_id in response_ids:
                        try:
                            await delete(response_id)
                        except BaseException as exc:
                            if first_error is None:
                                first_error = exc
                try:
                    await client.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

    def _capture_response_id(self, response_id: str) -> None:
        if not response_id:
            return
        self._response_id = response_id
        if response_id not in self._stored_response_ids:
            self._stored_response_ids.append(response_id)


def _join_pending_prompt(pending: Optional[str], prompt: str) -> str:
    if not pending:
        return prompt
    return f"{pending}\n\n{prompt}"


async def _reset_conversation_bounded(conversation: XaiConversation) -> bool:
    """Reset once; a slow gRPC close continues as a background reaper."""

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
