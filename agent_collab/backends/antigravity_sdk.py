"""The Antigravity ``sdk`` backend (``google-antigravity``), lazy + first-class.

The real ``google.antigravity`` module is imported **lazily** — only inside the
probe's ``find_spec`` check and the default agent factory — never at import time.
So importing this module (which the registry does at startup) costs nothing, and
a missing wheel degrades to an *unavailable* backend (a fast, hinted start
rejection) rather than an import crash.

**API shapes CONFIRMED live** against ``google-antigravity`` 0.1.5 (Python 3.12)
— see ``tests/fixtures/antigravity/sdk-introspection.json``:

- ``from google.antigravity import Agent, LocalAgentConfig``; ``Agent`` is an
  async context manager; ``response = await agent.chat(prompt)`` returns a
  ``types.ChatResponse``.
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
- ``LocalAgentConfig`` takes ``workspaces=[...]`` (the working dirs) and ``model``;
  the working directory is a workspace, not a ``working_directory`` kwarg.
- ``Agent.conversation_id`` exposes a stable, resume-capable id.

Only the live *call* is blocked here: the SDK requires a Gemini API key
(``GEMINI_API_KEY`` env or ``LocalAgentConfig(api_key=...)``); agent-collab never
manages credentials, so it passes the environment through and the first turn's
real error is the authority. The event mapper is exercised by fake-module tests
built to the confirmed shapes (``tests/test_backend_sdk.py``).
"""

from __future__ import annotations

import enum
import inspect
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Iterator, List, Mapping, Optional

from ..config import AgentConfig
from ..events import Event, compact_json
from ..runners import AgentRunner
from .base import (
    BackendCapabilities,
    BackendHealth,
    BackendUnavailable,
    OptionSpec,
    normalize_declared_options,
)
from .health import gemini_api_key_credentials, probe_sdk_backend
from .sdk_common import classify_tool_kind, package_version, provider_session_event, sdk_error_event

MODULE_NAME = "google.antigravity"
PACKAGE_NAME = "google-antigravity"
INSTALL_HINT = "install the Antigravity SDK: pip install google-antigravity"

ANTIGRAVITY_SDK_OPTION_SCHEMA = {
    "model": OptionSpec("string"),
}

# A factory builds the SDK agent context manager for one turn. Injectable so
# tests drive the runner with a fake without installing the SDK or calling a model.
AgentFactory = Callable[[AgentConfig, Dict[str, Any], Path], Any]


class AntigravitySdkBackend:
    """Registered as ``(antigravity, "sdk")``. Capabilities are all false."""

    id = "sdk"
    agent_type = "antigravity"

    def __init__(self, agent_factory: Optional[AgentFactory] = None) -> None:
        self.capabilities = BackendCapabilities()
        self.checks_credentials = True
        # Opt-in backend: a missing extra / sign-out fails the start fast.
        self.block_on_unavailable = True
        self._agent_factory = agent_factory

    def probe(self) -> BackendHealth:
        # The SDK authenticates with a Gemini API key, not the ~/.gemini OAuth the
        # agy CLI uses; check the right thing (absence -> unknown, never missing).
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=gemini_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(ANTIGRAVITY_SDK_OPTION_SCHEMA)

    def normalize_options(
        self,
        agent: AgentConfig,
        requested: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return normalize_declared_options(agent, requested, self.option_schema(agent))

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]) -> AgentRunner:
        factory = self._agent_factory or _default_agent_factory
        return AntigravitySdkRunner(agent, verbose, dict(options or {}), agent_factory=factory)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        # The sdk backend has no command_preview; it summarises itself instead.
        summary: Dict[str, Any] = {"backend": "sdk", "package": PACKAGE_NAME}
        version = package_version(PACKAGE_NAME)
        if version:
            summary["version"] = version
        mapped = _map_sdk_options(options)
        if mapped:
            summary["options"] = mapped
        return summary


class AntigravitySdkRunner(AgentRunner):
    def __init__(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Dict[str, Any],
        agent_factory: AgentFactory,
    ) -> None:
        self.name = agent.id
        self.agent = agent
        self.verbose = verbose
        self.options = options
        self._agent_factory = agent_factory

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        if self.verbose:
            yield Event.create("antigravity", "status", f"antigravity sdk starting in {workdir}")
        try:
            agent_cm = self._agent_factory(self.agent, self.options, workdir)
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        try:
            async with agent_cm as sdk_agent:
                response = await sdk_agent.chat(prompt)
                # Resolve the SDK's shared response stream exactly once. Its
                # thoughts/tool_calls properties are async cursors, not lists.
                chunks = await _resolve_chunks(response)
                usage_metadata = getattr(response, "usage_metadata", None)
                for event in map_antigravity_turn(chunks, self.verbose, usage_metadata):
                    yield event
                conversation_id = getattr(sdk_agent, "conversation_id", None)
                if conversation_id:
                    # Uniform provider-session capture (kind="conversation"). The
                    # daemon records it into central session state; nothing resumes
                    # it this stage (capabilities stay false).
                    yield provider_session_event(
                        "antigravity", self.name, str(conversation_id), "conversation"
                    )
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        except Exception as exc:  # surface SDK errors as transcript errors
            yield sdk_error_event("antigravity", exc)
            return
        if self.verbose:
            yield Event.create("antigravity", "status", "antigravity sdk turn complete")


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
    # start validation for the sdk backend, so only `model` maps here.
    mapped: Dict[str, Any] = {}
    if "model" in options:
        mapped["model"] = options["model"]
    return mapped


def _default_agent_factory(agent: AgentConfig, options: Dict[str, Any], workdir: Path) -> Any:
    """Lazily import the real SDK and build its agent context manager.

    Names/kwargs confirmed against google-antigravity 0.1.5. The working dir is a
    workspace; auth (GEMINI_API_KEY) comes from the passed-through environment,
    never managed here.
    """

    try:
        from google.antigravity import Agent, LocalAgentConfig  # type: ignore
    except ImportError as exc:
        raise BackendUnavailable("antigravity", "sdk", f"{MODULE_NAME} is not importable", INSTALL_HINT) from exc
    config_kwargs: Dict[str, Any] = {"workspaces": [str(workdir)]}
    if "model" in options:
        config_kwargs["model"] = options["model"]
    return Agent(LocalAgentConfig(**config_kwargs))


def build_antigravity_sdk_backends() -> List[AntigravitySdkBackend]:
    return [AntigravitySdkBackend()]
