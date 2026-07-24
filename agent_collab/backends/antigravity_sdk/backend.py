"""The Antigravity ``sdk`` backend (``google-antigravity``), lazy + first-class.

The real ``google.antigravity`` module is imported **lazily** — only inside the
probe's ``find_spec`` check and the default agent factory — never at import time.
So importing this module (which the registry does at startup) costs nothing, and
a missing wheel degrades to an *unavailable* backend (a fast, hinted start
rejection) rather than an import crash.

**API shapes CONFIRMED** against the installed ``google-antigravity`` 0.1.8
(Python 3.14.4)
— see ``tests/fixtures/antigravity/sdk-introspection.json``:

- ``from google.antigravity import Agent, LocalAgentConfig``; ``Agent`` is an
  async context manager reused for sequential ``chat()`` calls;
  ``response = await agent.chat(prompt)`` returns a ``types.ChatResponse``.
- ``await response.resolve()`` drains the response once into a typed list of
  ``Text``, ``Thought``, ``ToolCall``, and ``ToolResult`` values. ``text()`` is
  also async, while ``thoughts`` and ``tool_calls`` are independent async cursor
  properties and therefore must not be passed to synchronous iteration.
- ``Text(step_index, text)`` and ``Thought(step_index, text, signature)`` carry
  response deltas. Signatures are opaque and never enter the event stream.
- ``ToolCall(name, args, id, canonical_path)`` and
  ``ToolResult(name, id, result, error, exception)`` carry correlated tool data.
- ``response.usage_metadata`` exposes optional per-turn token counts after the
  response is resolved.
- ``LocalAgentConfig`` takes ``workspaces=[...]`` (the working dirs), ``model``,
  and the Vertex shorthands ``vertex``, ``project``, and ``location``; the
  working directory is a workspace, not a ``working_directory`` kwarg.
- ``Agent.conversation_id`` is absent before start and becomes available after
  message exchange. ``LocalAgentConfig(conversation_id=...,
  session_continuation_mode=SessionContinuationMode.RESUME)`` is the strict
  reopen API; its ``save_dir`` trajectory storage must remain stable across
  Agent objects. ``CREATE_OR_RESUME`` is intentionally never used.
- The bundled Linux ``localharness`` has ``GLIBC_2.26`` as its newest versioned
  libc symbol.

Live calls use either a Gemini API key (``GEMINI_API_KEY``) or Vertex AI with
Google Application Default Credentials. Agent-collab never stores credential
material. The event mapper is exercised by fake-module tests built to the
confirmed shapes (``tests/backends/antigravity_sdk/test_backend.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import enum
import inspect
import platform
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Protocol

from ...config import AgentConfig
from ...events import Event, compact_json
from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from ...runners import AgentRunner, AsyncEventSink
from ..base import (
    BackendCapabilities,
    BackendHealth,
    BackendOptionError,
    BackendUnavailable,
    OptionSpec,
    load_option_schema,
    normalize_declared_options,
)
from ..common.health import gemini_api_key_credentials, probe_sdk_backend
from ..common.sdk import (
    SDK_CLOSE_GRACE_SECONDS,
    backend_unavailable_event,
    classify_tool_kind,
    close_async_stream,
    package_version,
    sdk_settings_summary,
    provider_session_event,
    sdk_error_event,
)

MODULE_NAME = "google.antigravity"
PACKAGE_NAME = "google-antigravity"
INSTALL_HINT = "install the Antigravity SDK: pip install google-antigravity, or re-run ./agent_collab.sh install"
# google-antigravity 0.1.8's bundled localharness has GLIBC_2.26 as its
# newest versioned libc symbol (verified with objdump/readelf on the wheel).
REQUIRED_GLIBC = "2.26"
# The 0.1.8 wheel's generated localharness_pb2.py declares gencode 7.35.0.
REQUIRED_PROTOBUF = "7.35"

ANTIGRAVITY_SDK_OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))
ANTIGRAVITY_SDK_CONFIG_SCHEMA = load_option_schema(Path(__file__).with_name("config.toml"))


@dataclass(frozen=True)
class AntigravityTurn:
    """One resolved SDK turn while the runner-owned Agent stays connected."""

    chunks: List[Any]
    usage_metadata: Any
    conversation_id: Optional[str]
    response_clean_close: bool


class AntigravityConversation(Protocol):
    """One runner-owned provider conversation; fakeable without the real SDK."""

    def active(self) -> bool: ...

    async def run(self, prompt: str) -> AntigravityTurn: ...

    def note_session_id(self, conversation_id: str) -> None: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


ConversationFactory = Callable[
    [AgentConfig, Dict[str, Any], Path],
    AntigravityConversation,
]


def assess_native_runtime(
    host_libc: tuple[str, str],
    *,
    required: str = REQUIRED_GLIBC,
) -> Dict[str, str]:
    """Compare injectable host libc facts without launching the native runtime."""

    family, observed = host_libc
    normalized = (family or "").lower()
    if normalized and normalized not in {"glibc", "gnu libc"}:
        return {
            "status": "not_applicable",
            "required": f"glibc >= {required} on glibc Linux hosts",
            "observed": f"{family} {observed}".strip(),
        }
    if not normalized or not _version_tuple(observed):
        return {
            "status": "indeterminate",
            "required": f"glibc >= {required}",
            "observed": f"{family} {observed}".strip() or "unknown",
        }
    status = (
        "compatible" if _version_tuple(observed) >= _version_tuple(required) else "incompatible"
    )
    return {
        "status": status,
        "required": f"glibc >= {required}",
        "observed": f"glibc {observed}",
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for part in (value or "").split("."):
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


class AntigravitySdkBackend:
    """Registered as ``(antigravity, "sdk")`` with in-session continuity."""

    id = "sdk"
    agent_type = "antigravity"
    brand_color = "#4285F4"
    event_fidelity = "typed"
    provider_session_id_kind = "conversation"

    def __init__(
        self,
        conversation_factory: Optional[ConversationFactory] = None,
        *,
        dependency_probe: Optional[Callable[[], BackendHealth]] = None,
        libc_ver: Optional[Callable[[], tuple[str, str]]] = None,
        protobuf_version: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.capabilities = BackendCapabilities(continuity=True)
        self.checks_credentials = True
        # Opt-in backend: a missing extra / sign-out fails the start fast.
        self.block_on_unavailable = True
        self._conversation_factory = conversation_factory
        self._dependency_probe = dependency_probe
        self._libc_ver = libc_ver or platform.libc_ver
        self._protobuf_version = protobuf_version or (lambda: package_version("protobuf"))

    def probe(self) -> BackendHealth:
        # The SDK authenticates with a Gemini API key, not the ~/.gemini OAuth the
        # agy CLI uses; check the right thing (absence -> unknown, never missing).
        health = (
            self._dependency_probe()
            if self._dependency_probe is not None
            else probe_sdk_backend(
                MODULE_NAME,
                package_version=lambda: package_version(PACKAGE_NAME),
                credentials=gemini_api_key_credentials,
                extra_hint=INSTALL_HINT,
            )
        )
        if health.status != "ok":
            return health

        checks = dict(health.checks)
        # Both compatibility floors are facts about the verified package line.
        # If distribution metadata is missing, do not green-light an import that
        # may fail before the lazy factory can produce a structured error.
        if not health.version:
            checks["protobuf_runtime"] = {
                "status": "indeterminate",
                "reason": "package version metadata is unavailable",
                "observed": self._protobuf_version() or "missing",
            }
            checks["native_runtime"] = {
                "status": "indeterminate",
                "reason": "package version metadata is unavailable",
            }
            return BackendHealth(
                status="unavailable",
                reason=(
                    "google-antigravity distribution version metadata is unavailable; "
                    "protobuf and native runtime compatibility cannot be verified"
                ),
                credentials=health.credentials,
                version=health.version,
                checked_at=health.checked_at,
                checks=checks,
                reason_codes=("dependency_version_unknown",),
                remediation=(
                    {
                        "code": "reinstall_sdk_dependency",
                        "message": (
                            "Reinstall the verified Antigravity extra so "
                            "google-antigravity distribution metadata is present."
                        ),
                    },
                ),
            )

        protobuf_version = self._protobuf_version()
        protobuf_status = (
            "compatible"
            if protobuf_version
            and _version_tuple(protobuf_version) >= _version_tuple(REQUIRED_PROTOBUF)
            else "incompatible"
        )
        checks["protobuf_runtime"] = {
            "status": protobuf_status,
            "required": f"protobuf >= {REQUIRED_PROTOBUF}",
            "observed": protobuf_version or "missing",
        }
        if protobuf_status == "incompatible":
            reason = (
                "google-antigravity 0.1.8 generated protobuf code requires "
                f"protobuf >= {REQUIRED_PROTOBUF}; observed "
                f"{protobuf_version or 'missing'}"
            )
            return BackendHealth(
                status="unavailable",
                reason=reason,
                credentials=health.credentials,
                version=health.version,
                checked_at=health.checked_at,
                checks=checks,
                reason_codes=("protobuf_runtime_incompatible",),
                remediation=(
                    {
                        "code": "use_compatible_protobuf_runtime",
                        "message": (
                            f"Use an isolated Antigravity environment with protobuf "
                            f">= {REQUIRED_PROTOBUF},<8. xai-sdk 1.17 requires "
                            "protobuf <7, so the two SDKs cannot currently share "
                            "one dependency environment."
                        ),
                    },
                ),
            )

        native = assess_native_runtime(self._libc_ver(), required=REQUIRED_GLIBC)
        checks["native_runtime"] = native
        if native["status"] != "incompatible":
            return BackendHealth(
                status=health.status,
                reason=health.reason,
                credentials=health.credentials,
                version=health.version,
                checked_at=health.checked_at,
                checks=checks,
                reason_codes=health.reason_codes,
                remediation=health.remediation,
            )
        observed = native.get("observed", "unknown")
        reason = f"bundled native runtime requires glibc >= {REQUIRED_GLIBC}; observed {observed}"
        return BackendHealth(
            status="unavailable",
            reason=reason,
            credentials=health.credentials,
            version=health.version,
            checked_at=health.checked_at,
            checks=checks,
            reason_codes=("native_runtime_incompatible",),
            remediation=(
                {
                    "code": "use_compatible_native_runtime",
                    "message": (
                        f"Use a glibc {REQUIRED_GLIBC}+ host/container or a compatible provider binary. "
                        "Do not replace the host system glibc manually."
                    ),
                },
            ),
        )

    def configuration_schema(self) -> Mapping[str, OptionSpec]:
        return dict(ANTIGRAVITY_SDK_CONFIG_SCHEMA)

    def safe_configuration_summary(self, agent: AgentConfig) -> Mapping[str, Any]:
        self.normalize_config(agent)
        return {
            "validation": "valid",
            "fields": {
                name: "configured" if name in agent.backend_config else "default_or_unset"
                for name in sorted(ANTIGRAVITY_SDK_CONFIG_SCHEMA)
            },
        }

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(ANTIGRAVITY_SDK_OPTION_SCHEMA)

    def normalize_options(
        self,
        agent: AgentConfig,
        requested: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.normalize_config(agent)
        return normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=agent.options_for(self.id),
            configured_defaults=agent.default_options_for(self.id),
        )

    def normalize_config(self, agent: AgentConfig) -> Mapping[str, Any]:
        return _normalize_sdk_config(agent)

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        factory = self._conversation_factory or _default_conversation
        return AntigravitySdkRunner(
            agent,
            verbose,
            dict(options or {}),
            conversation_factory=factory,
        )

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        # The sdk backend has no command_preview; it summarises itself instead.
        summary = sdk_settings_summary(PACKAGE_NAME, _map_sdk_options(options))
        config = self.normalize_config(agent)
        if config:
            summary["config"] = config
        summary["conversation"] = "persistent"
        return summary


class AntigravitySdkRunner(AgentRunner):
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
        self._conversation: Optional[AntigravityConversation] = None
        self._workdir: Optional[Path] = None

    def conversation_active(self) -> bool:
        return self._conversation is not None and self._conversation.active()

    async def close(self) -> None:
        if self._conversation is not None:
            await self._conversation.close()

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        if self.verbose:
            await emit(
                Event.create("antigravity", "status", f"antigravity sdk starting in {workdir}")
            )
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        clean_close = True
        conversation: Optional[AntigravityConversation] = None
        try:
            conversation = self._conversation_for(workdir)
            turn = await conversation.run(prompt)
            clean_close = turn.response_clean_close
            mapped = list(map_antigravity_turn(turn.chunks, self.verbose, turn.usage_metadata))
            for event in mapped:
                await emit(event)
            if any(
                event.type == "message" and event.source == "antigravity" and event.text.strip()
                for event in mapped
            ):
                evidence.add(TerminalEvidence("completed"))
            else:
                exception_code = "provider_empty_response"
            if turn.conversation_id:
                conversation.note_session_id(turn.conversation_id)
                await emit(
                    provider_session_event(
                        "antigravity",
                        self.name,
                        turn.conversation_id,
                        "conversation",
                    )
                )
        except asyncio.CancelledError:
            if conversation is not None:
                await _reset_conversation_bounded(conversation)
            raise
        except BackendUnavailable as exc:
            await emit(backend_unavailable_event(exc))
            exception_code = "provider_transport_failed"
        except Exception as exc:  # surface SDK errors as transcript errors
            await emit(sdk_error_event("antigravity", exc))
            exception_code = "provider_transport_failed"
        if not clean_close and exception_code is None:
            exception_code = "provider_transport_failed"
        result = evidence.resolve(exception_code=exception_code)
        if result.outcome != "completed" and conversation is not None:
            await _reset_conversation_bounded(conversation)
        if self.verbose:
            await emit(Event.create("antigravity", "status", "antigravity sdk turn complete"))
        return result

    def _conversation_for(self, workdir: Path) -> AntigravityConversation:
        resolved = workdir.resolve()
        if self._conversation is None:
            self._conversation = self._conversation_factory(
                self.agent,
                self.options,
                resolved,
            )
            self._workdir = resolved
        elif self._workdir != resolved:
            raise RuntimeError("antigravity sdk conversation workdir changed between turns")
        return self._conversation


def map_antigravity_turn(
    chunks: Iterable[Any],
    verbose: bool,
    usage_metadata: Any = None,
) -> Iterator[Event]:
    """Map one resolved, typed ``ChatResponse`` buffer onto standard events.

    Text deltas become one assistant message; ToolCall values become
    tool_call/command/file_change events; ToolResult values become correlated
    tool statuses or error events; Thought deltas become one verbose reasoning
    status. ``Thought.signature`` is deliberately never read into raw data.
    """

    text_parts: List[str] = []
    thought_parts: List[str] = []
    tool_events: List[Event] = []
    for chunk in chunks:
        kind = _chunk_kind(chunk)
        if kind == "Text":
            text = getattr(chunk, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        elif kind == "Thought":
            thought = getattr(chunk, "text", None)
            if isinstance(thought, str):
                thought_parts.append(thought)
        elif kind == "ToolCall":
            tool_events.append(_map_tool_call(chunk))
        elif kind == "ToolResult":
            tool_events.append(_map_tool_result(chunk))

    thoughts = "".join(thought_parts).strip()
    if verbose and thoughts:
        yield Event.create("antigravity", "status", thoughts, {"reasoning": True})

    yield from tool_events

    text = "".join(text_parts).strip()
    if text:
        yield Event.create("antigravity", "message", text, {"text": text})

    usage = _usage_raw(usage_metadata)
    if verbose and usage:
        yield Event.create(
            "antigravity",
            "status",
            f"antigravity sdk usage {compact_json(usage)}",
            {"usage": usage},
        )


async def _resolve_chunks(response: Any) -> List[Any]:
    """Drain ``ChatResponse`` through its confirmed async ``resolve`` method."""

    resolve = getattr(response, "resolve", None)
    if not callable(resolve):
        raise TypeError("google-antigravity ChatResponse.resolve is unavailable")
    result = resolve()
    if not inspect.isawaitable(result):
        raise TypeError("google-antigravity ChatResponse.resolve must be async")
    chunks = await result
    if not isinstance(chunks, list):
        raise TypeError("google-antigravity ChatResponse.resolve did not return a list")
    return chunks


def _chunk_kind(chunk: Any) -> str:
    """Recognize installed SDK types without importing the optional wheel."""

    class_name = type(chunk).__name__
    if class_name in {"Text", "Thought", "ToolCall", "ToolResult"}:
        return class_name
    # Structural fallbacks keep dependency-free fakes useful while matching only
    # the four verified public shapes.
    if hasattr(chunk, "args") and hasattr(chunk, "canonical_path"):
        return "ToolCall"
    if hasattr(chunk, "result") and hasattr(chunk, "error"):
        return "ToolResult"
    if hasattr(chunk, "step_index") and isinstance(getattr(chunk, "text", None), str):
        return "Thought" if hasattr(chunk, "signature") else "Text"
    return ""


def _map_tool_call(tool_call: Any) -> Event:
    name = getattr(tool_call, "name", None)
    name_str = name.name if isinstance(name, enum.Enum) else (str(name) if name is not None else "")
    args = getattr(tool_call, "args", None)
    canonical_path = getattr(tool_call, "canonical_path", None)
    kind = classify_tool_kind(name_str)
    if name_str and args:
        text = f"{name_str} {compact_json(args)}"
    elif name_str:
        text = name_str
    else:
        text = compact_json({"args": args})
    return Event.create(
        "tool",
        kind,
        text,
        {
            "name": name_str,
            "args": args,
            "id": getattr(tool_call, "id", None),
            "canonical_path": canonical_path,
        },
    )


def _map_tool_result(tool_result: Any) -> Event:
    name = getattr(tool_result, "name", None)
    name_str = name.name if isinstance(name, enum.Enum) else (str(name) if name is not None else "")
    result = getattr(tool_result, "result", None)
    error = getattr(tool_result, "error", None)
    exception = getattr(tool_result, "exception", None)
    error_text = error.strip() if isinstance(error, str) else ""
    if not error_text and isinstance(exception, BaseException):
        error_text = str(exception).strip() or exception.__class__.__name__

    raw: Dict[str, Any] = {
        "name": name_str,
        "id": getattr(tool_result, "id", None),
        "result": result,
    }
    if error_text:
        raw["error"] = error_text
    if isinstance(exception, BaseException):
        raw["exception"] = exception.__class__.__name__

    label = name_str or "tool"
    if error_text:
        return Event.create("error", "error", f"{label} failed: {error_text}", raw)
    text = f"{label} result"
    if result is not None:
        text += f" {compact_json(result)}"
    return Event.create("tool", "status", text, raw)


def _usage_raw(usage_metadata: Any) -> Dict[str, int]:
    raw: Dict[str, int] = {}
    for field in (
        "prompt_token_count",
        "cached_content_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
    ):
        value = getattr(usage_metadata, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            raw[field] = value
    return raw


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    # Explicit mapping, no blind pass-through. `mode` is cli-only (the SDK has no
    # `--mode` equivalent; it uses CapabilitiesConfig/policies) and is rejected at
    # start validation for the sdk backend.
    mapped: Dict[str, Any] = {}
    if "model" in options:
        mapped["model"] = options["model"]
    return mapped


def _normalize_sdk_config(agent: AgentConfig) -> Dict[str, Any]:
    config = normalize_declared_options(
        {}, ANTIGRAVITY_SDK_CONFIG_SCHEMA, configured=agent.backend_config
    )
    vertex = config.get("vertex")
    project = config.get("project")
    location = config.get("location")
    if vertex is True:
        if not isinstance(project, str) or not project.strip():
            raise BackendOptionError(
                "project", "is required and must be non-empty when vertex is true"
            )
        if not isinstance(location, str) or not location.strip():
            raise BackendOptionError(
                "location", "is required and must be non-empty when vertex is true"
            )
    elif project is not None or location is not None:
        field = "project" if project is not None else "location"
        raise BackendOptionError(field, "requires vertex=true")
    return config


def _map_sdk_config(agent: AgentConfig) -> Dict[str, Any]:
    return _normalize_sdk_config(agent)


def _default_conversation(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
) -> AntigravityConversation:
    """Build one lazy-imported persistent Agent conversation for a runner."""

    save_directory = tempfile.TemporaryDirectory(prefix="agent-collab-antigravity-")
    return _PersistentAntigravityConversation(
        lambda conversation_id: _default_agent_factory(
            agent,
            options,
            workdir,
            conversation_id=conversation_id,
            save_dir=save_directory.name,
        ),
        close_cleanup=save_directory.cleanup,
    )


def _default_agent_factory(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
    *,
    conversation_id: Optional[str] = None,
    save_dir: Optional[str] = None,
) -> Any:
    """Lazily import the verified 0.1.8 SDK and build one Agent context.

    A captured id always uses explicit ``SessionContinuationMode.RESUME``.
    ``CREATE_OR_RESUME`` is deliberately never used because an expired or
    rejected id must not become a fresh provider conversation.
    """

    try:
        from google.antigravity import Agent, LocalAgentConfig  # type: ignore
        from google.antigravity.types import SessionContinuationMode  # type: ignore
    except ImportError as exc:
        raise BackendUnavailable(
            "antigravity", "sdk", f"{MODULE_NAME} is not importable", INSTALL_HINT
        ) from exc

    config_kwargs: Dict[str, Any] = {"workspaces": [str(workdir)]}
    config_kwargs.update(_map_sdk_config(agent))
    config_kwargs.update(_map_sdk_options(options))
    fields = getattr(LocalAgentConfig, "model_fields", {})
    if save_dir is not None:
        if "save_dir" not in fields:
            raise BackendUnavailable(
                "antigravity",
                "sdk",
                "google.antigravity has no compatible persistent trajectory storage API",
                INSTALL_HINT,
            )
        config_kwargs["save_dir"] = save_dir
    if conversation_id is not None:
        resume = getattr(SessionContinuationMode, "RESUME", None)
        if (
            "conversation_id" not in fields
            or "session_continuation_mode" not in fields
            or resume is None
        ):
            raise BackendUnavailable(
                "antigravity",
                "sdk",
                "google.antigravity has no compatible strict conversation resume API",
                INSTALL_HINT,
            )
        config_kwargs["conversation_id"] = conversation_id
        config_kwargs["session_continuation_mode"] = resume
    return Agent(LocalAgentConfig(**config_kwargs))


class _PersistentAntigravityConversation:
    """Serialize one live SDK Agent, response, and reconnect identity."""

    def __init__(
        self,
        agent_factory: Callable[[Optional[str]], Any],
        *,
        close_cleanup: Optional[Callable[[], None]] = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._close_cleanup = close_cleanup
        self._lock = asyncio.Lock()
        self._agent_cm: Any = None
        self._sdk_agent: Any = None
        self._conversation_id: Optional[str] = None
        self._pending_prompts: list[tuple[object, str]] = []
        self._turn_handed_off = False
        self._resume_missing_id = False
        self._closed = False

    def active(self) -> bool:
        # A reset drops only the live Agent. A retained id still names
        # provider-side context, so the referee must keep sending delta prompts.
        # If a handed-off turn produced no id, strict native resume is
        # impossible for the next continuation: do not let a later undelivered
        # prompt make that fail-closed state look active to the referee.
        return (
            not self._closed
            and not self._resume_missing_id
            and (
                self._sdk_agent is not None
                or self._conversation_id is not None
                or bool(self._pending_prompts)
            )
        )

    def note_session_id(self, conversation_id: str) -> None:
        if self._conversation_id is not None and self._conversation_id != conversation_id:
            raise RuntimeError("Antigravity resumed a different provider conversation")
        self._conversation_id = conversation_id

    async def run(self, prompt: str) -> AntigravityTurn:
        # Preserve a prompt whose connect failed before provider hand-off. Once
        # chat() is called delivery is uncertain, so replay would risk a duplicate
        # provider turn and the pending copy is cleared. Queue before the lock so
        # cancellation while a slow reset/close holds it cannot orphan a referee
        # delta whose watermark has already advanced.
        if self._resume_missing_id:
            async with self._lock:
                if self._closed:
                    raise RuntimeError("antigravity sdk conversation is closed")
                if self._resume_missing_id:
                    # Fail the required next continuation structurally, but do
                    # not brick the runner forever. Since active() was false,
                    # a later user turn receives a full stateless prompt and may
                    # explicitly start a new native conversation.
                    self._resume_missing_id = False
                    raise RuntimeError(
                        "antigravity sdk cannot continue: the abnormal turn produced "
                        "no conversation id for strict native resume"
                    )
        prompt_token = object()
        self._pending_prompts.append((prompt_token, prompt))
        async with self._lock:
            if self._closed:
                self._pending_prompts.clear()
                raise RuntimeError("antigravity sdk conversation is closed")
            if self._sdk_agent is None and self._resume_missing_id:
                # A concurrent reset may have made resume terminal after the
                # optimistic pre-lock check. Do not retain an undeliverable prompt.
                self._take_pending_prompts_through(prompt_token)
                self._resume_missing_id = False
                raise RuntimeError(
                    "antigravity sdk cannot continue: the abnormal turn produced "
                    "no conversation id for strict native resume"
                )
            if self._sdk_agent is None:
                await self._connect_locked()
            effective_prompt = self._take_pending_prompts_through(prompt_token)
            chat = getattr(self._sdk_agent, "chat", None)
            if not callable(chat):
                raise BackendUnavailable(
                    "antigravity",
                    "sdk",
                    "google.antigravity returned an incompatible Agent",
                    INSTALL_HINT,
                )
            self._turn_handed_off = True
            response = None
            clean_close = True
            try:
                response = await chat(effective_prompt)
                self._capture_agent_id_locked()
                chunks = await _resolve_chunks(response)
                self._capture_agent_id_locked()
                usage_metadata = getattr(response, "usage_metadata", None)
            except BaseException:
                self._capture_agent_id_locked()
                if response is not None:
                    await _cancel_response_bounded(response)
                raise
            finally:
                clean_close = await close_async_stream(response)
            return AntigravityTurn(
                chunks=chunks,
                usage_metadata=usage_metadata,
                conversation_id=self._conversation_id,
                response_clean_close=clean_close,
            )

    async def reset(self) -> None:
        async with self._lock:
            agent_cm = self._drop_live_locked(keep_conversation_id=True)
            if self._turn_handed_off and self._conversation_id is None:
                # A prompt may have reached the provider, but no native identity
                # exists to reopen it. Never substitute a fresh Agent.
                self._resume_missing_id = True
            self._turn_handed_off = False
            if agent_cm is not None:
                await _exit_agent_context(agent_cm)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            agent_cm = self._drop_live_locked(keep_conversation_id=False)
            self._pending_prompts.clear()
            self._turn_handed_off = False
            self._resume_missing_id = False
            cleanup = self._close_cleanup
            self._close_cleanup = None
            try:
                if agent_cm is not None:
                    await _exit_agent_context(agent_cm)
            finally:
                if cleanup is not None:
                    cleanup()

    async def _connect_locked(self) -> None:
        resume_id = self._conversation_id
        if resume_id is None and self._resume_missing_id:
            raise RuntimeError(
                "antigravity sdk cannot continue: the abnormal turn produced "
                "no conversation id for strict native resume"
            )
        agent_cm = self._agent_factory(resume_id)
        enter = getattr(agent_cm, "__aenter__", None)
        exit_ = getattr(agent_cm, "__aexit__", None)
        if not callable(enter) or not callable(exit_):
            raise BackendUnavailable(
                "antigravity",
                "sdk",
                "google.antigravity Agent is not an async context manager",
                INSTALL_HINT,
            )
        try:
            sdk_agent = await enter()
            observed = _conversation_id(sdk_agent)
            if resume_id is not None and observed is not None and observed != resume_id:
                raise RuntimeError("Antigravity resumed a different provider conversation")
        except BaseException:
            try:
                await exit_(None, None, None)
            except BaseException:
                pass
            raise
        self._agent_cm = agent_cm
        self._sdk_agent = sdk_agent

    def _capture_agent_id_locked(self) -> None:
        conversation_id = _conversation_id(self._sdk_agent)
        if conversation_id is not None:
            self.note_session_id(conversation_id)

    def _take_pending_prompts_through(self, prompt_token: object) -> str:
        for index, (token, _prompt) in enumerate(self._pending_prompts):
            if token is prompt_token:
                claimed = self._pending_prompts[: index + 1]
                del self._pending_prompts[: index + 1]
                return "\n\n".join(prompt for _token, prompt in claimed)
        raise RuntimeError("antigravity sdk pending prompt was lost")

    def _drop_live_locked(self, *, keep_conversation_id: bool) -> Any:
        agent_cm = self._agent_cm
        self._agent_cm = None
        self._sdk_agent = None
        if not keep_conversation_id:
            self._conversation_id = None
        return agent_cm


def _conversation_id(sdk_agent: Any) -> Optional[str]:
    value = getattr(sdk_agent, "conversation_id", None)
    return value if isinstance(value, str) and value else None


async def _cancel_response_bounded(response: Any) -> bool:
    """Best-effort abnormal-turn cleanup; not an advertised interrupt path."""

    cancel = getattr(response, "cancel", None)
    if not callable(cancel):
        return True
    task = asyncio.ensure_future(cancel())
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=SDK_CLOSE_GRACE_SECONDS,
        )
        return True
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_background_result)
        return False
    except BaseException:
        task.add_done_callback(_consume_background_result)
        return False


async def _exit_agent_context(agent_cm: Any) -> None:
    await agent_cm.__aexit__(None, None, None)


async def _reset_conversation_bounded(
    conversation: AntigravityConversation,
) -> bool:
    """Reset once; a slow Agent close continues as a background reaper."""

    task = asyncio.create_task(conversation.reset())
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=SDK_CLOSE_GRACE_SECONDS,
        )
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
