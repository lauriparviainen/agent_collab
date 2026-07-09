"""The Claude ``sdk`` backend (``claude-agent-sdk``), lazy + first-class.

The real ``claude_agent_sdk`` module is imported **lazily** — only inside the
probe's ``find_spec`` check and the default message-stream factory — never at
import time. So importing this module (which the registry does at startup) costs
nothing and needs no dependency; a missing wheel degrades to an *unavailable*
backend (a fast, hinted start rejection) rather than an import crash.

**API mapping** targets ``claude-agent-sdk`` (the renamed ``claude-code-sdk``,
Python 3.10+), whose ``query(prompt=..., options=ClaudeAgentOptions(...))`` is an
async iterator of typed messages:

- ``AssistantMessage.content`` is a list of blocks: ``TextBlock(.text)`` ->
  ``claude`` message, ``ToolUseBlock(.name/.input)`` -> ``tool`` (classified into
  tool_call/command/file_change), ``ThinkingBlock(.thinking)`` -> a verbose
  status (reasoning text only — the block's ``signature`` is never read/emitted).
- ``ResultMessage``/``SystemMessage`` carry ``session_id`` -> captured as the
  provider session id (``kind="session"``); ``is_error`` -> an ``error`` event.

agent-collab never manages credentials: auth (``ANTHROPIC_API_KEY`` or Claude
Code's local sign-in) comes from the passed-through environment, and the first
turn's real error is the authority. The mapper is exercised by fake-module tests
(``tests/test_backend_claude_sdk.py``) built to these shapes — no live call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

from ..config import AgentConfig
from ..events import Event, compact_json
from ..runners import AgentRunner
from .base import BackendCapabilities, BackendHealth, BackendUnavailable
from .health import anthropic_api_key_credentials, probe_sdk_backend
from .sdk_common import (
    classify_tool_kind,
    package_version,
    provider_session_event,
    sdk_error_event,
    stringify,
)

MODULE_NAME = "claude_agent_sdk"
PACKAGE_NAME = "claude-agent-sdk"
INSTALL_HINT = "install the Claude Agent SDK: pip install claude-agent-sdk"

# A factory opens the SDK message stream for one turn. Injectable so tests drive
# the runner with a fake message iterator without installing the SDK or calling a
# model. It may raise BackendUnavailable synchronously (a missing import).
MessageStreamFactory = Callable[[AgentConfig, Dict[str, Any], Path, str], AsyncIterator[Any]]


class ClaudeSdkBackend:
    """Registered as ``(claude, "sdk")``. Capabilities are all false."""

    id = "sdk"
    agent_type = "claude"

    def __init__(self, message_stream: Optional[MessageStreamFactory] = None) -> None:
        self.capabilities = BackendCapabilities()
        self.checks_credentials = True
        # First-class but opt-in: a missing wheel / import failure fails the start
        # fast with an install hint instead of burning the first turn.
        self.block_on_unavailable = True
        self._message_stream = message_stream

    def probe(self) -> BackendHealth:
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=anthropic_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Dict[str, Any]) -> AgentRunner:
        factory = self._message_stream or _default_message_stream
        return ClaudeSdkRunner(agent, verbose, dict(options or {}), message_stream=factory)

    def settings_summary(self, agent: AgentConfig, options: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"backend": "sdk", "package": PACKAGE_NAME}
        version = package_version(PACKAGE_NAME)
        if version:
            summary["version"] = version
        mapped = _map_sdk_options(options)
        if mapped:
            summary["options"] = mapped
        # Be explicit that runs do not implicitly load user/project filesystem
        # settings, so behaviour is predictable regardless of the host's config.
        summary["setting_sources"] = "none"
        return summary


class ClaudeSdkRunner(AgentRunner):
    def __init__(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Dict[str, Any],
        message_stream: MessageStreamFactory,
    ) -> None:
        self.name = agent.id
        self.agent = agent
        self.verbose = verbose
        self.options = options
        self._message_stream = message_stream

    async def run(self, prompt: str, workdir: Path) -> AsyncIterator[Event]:
        if self.verbose:
            yield Event.create("claude", "status", f"claude sdk starting in {workdir}")
        try:
            stream = self._message_stream(self.agent, self.options, workdir, prompt)
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        session_id: Optional[str] = None
        try:
            async for message in stream:
                sid = _message_session_id(message)
                if sid and sid != session_id:
                    session_id = sid
                    # Uniform provider-session capture (kind="session"). Nothing
                    # resumes it this stage; capabilities stay false.
                    yield provider_session_event("claude", self.name, sid, "session")
                for event in iter_claude_events(message, self.verbose):
                    yield event
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        except Exception as exc:  # surface SDK errors as transcript errors
            yield sdk_error_event("claude", exc)
            return
        if self.verbose:
            yield Event.create("claude", "status", "claude sdk turn complete")


def iter_claude_events(message: Any, verbose: bool) -> Iterator[Event]:
    """Map one SDK message onto the standard Event stream.

    Content blocks -> text/tool/thinking events; a terminal ``ResultMessage`` ->
    an ``error`` event (``is_error``) or a verbose status; everything else with no
    prose degrades to nothing, the same honest fidelity as the cli parser.
    """

    content = getattr(message, "content", None)
    if isinstance(content, list):
        yield from _map_claude_content(content, verbose)
        return
    if getattr(message, "is_error", False):
        text = stringify(getattr(message, "result", None)) or "claude sdk error"
        yield Event.create("error", "error", text, _result_raw(message))
        return
    if verbose:
        subtype = getattr(message, "subtype", None)
        if isinstance(subtype, str) and subtype:
            yield Event.create("claude", "status", subtype, _result_raw(message))


def _map_claude_content(content: List[Any], verbose: bool) -> Iterator[Event]:
    text_parts: List[str] = []
    tool_events: List[Event] = []
    thinking_parts: List[str] = []
    for block in content:
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
    for field in ("subtype", "num_turns", "total_cost_usd", "is_error"):
        value = getattr(message, field, None)
        if value is not None:
            raw[field] = value
    return raw


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    # Explicit mapping, no blind pass-through. Only options with a confirmed
    # ClaudeAgentOptions equivalent map to the sdk; cli-only options
    # (thinking_level, thinking_budget_tokens) are rejected at start validation.
    mapped: Dict[str, Any] = {}
    for key in ("model", "permission_mode"):
        if key in options:
            mapped[key] = options[key]
    return mapped


def build_claude_agent_options(options_cls: Any, options: Dict[str, Any], workdir: Path) -> Any:
    """Construct a ``ClaudeAgentOptions`` from mapped options + a predictable cwd.

    Tries the richest kwargs first (``cwd`` + explicit empty ``setting_sources`` so
    filesystem settings are not implicitly loaded) and degrades if the installed
    SDK version does not accept them, so a minor API drift never crashes a run.
    """

    mapped = _map_sdk_options(options)
    for extra in (
        {"cwd": str(workdir), "setting_sources": []},
        {"cwd": str(workdir)},
        {},
    ):
        try:
            return options_cls(**{**mapped, **extra})
        except TypeError:
            continue
    return options_cls(**mapped)


def _default_message_stream(
    agent: AgentConfig, options: Dict[str, Any], workdir: Path, prompt: str
) -> AsyncIterator[Any]:
    """Lazily import the real SDK and open the async message stream for one turn."""

    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
    except ImportError as exc:
        raise BackendUnavailable("claude", "sdk", f"{MODULE_NAME} is not importable", INSTALL_HINT) from exc
    sdk_options = build_claude_agent_options(ClaudeAgentOptions, options, workdir)
    return query(prompt=prompt, options=sdk_options)


def build_claude_sdk_backends() -> List[ClaudeSdkBackend]:
    return [ClaudeSdkBackend()]
