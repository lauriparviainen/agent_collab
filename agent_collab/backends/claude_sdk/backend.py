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
  tool_call/command/file_change), ``ToolResultBlock(.tool_use_id/.content)`` ->
  a correlated tool result/error, and ``ThinkingBlock(.thinking)`` -> a verbose
  status (reasoning text only — the block's ``signature`` is never read/emitted).
- ``ResultMessage``/``SystemMessage`` carry ``session_id`` -> captured as the
  provider session id (``kind="session"``); ``is_error`` -> an ``error`` event;
  successful result usage/cost -> a verbose status.

agent-collab never manages credentials: auth (``ANTHROPIC_API_KEY`` or Claude
Code's local sign-in) comes from the passed-through environment, and the first
turn's real error is the authority. The mapper is exercised by fake-module tests
(``tests/backends/claude_sdk/test_backend.py``) built to these shapes — no live call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Mapping, Optional

from ...config import AgentConfig
from ...events import Event, compact_json
from ...runners import AgentRunner
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
    classify_tool_kind,
    package_version,
    provider_session_event,
    sdk_error_event,
    stringify,
)
from ..common.options import configured_choices, resolve_claude_thinking

MODULE_NAME = "claude_agent_sdk"
PACKAGE_NAME = "claude-agent-sdk"
INSTALL_HINT = "install the Claude Agent SDK: pip install claude-agent-sdk"

CLAUDE_SDK_OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))

# A factory opens the SDK message stream for one turn. Injectable so tests drive
# the runner with a fake message iterator without installing the SDK or calling a
# model. It may raise BackendUnavailable synchronously (a missing import).
MessageStreamFactory = Callable[[AgentConfig, Dict[str, Any], Path, str], AsyncIterator[Any]]


class ClaudeSdkBackend:
    """Registered as ``(claude, "sdk")``. Capabilities are all false."""

    id = "sdk"
    agent_type = "claude"
    brand_color = "#D97757"
    event_fidelity = "typed"
    provider_session_id_kind = "session"

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

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(CLAUDE_SDK_OPTION_SCHEMA)

    def normalize_options(
        self,
        agent: AgentConfig,
        requested: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        configured = agent.options_for(self.id)
        normalized = normalize_declared_options(
            requested, self.option_schema(agent), configured=configured
        )
        return resolve_claude_thinking(normalized, configured_choices(configured, requested))

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]) -> AgentRunner:
        factory = self._message_stream or _default_message_stream
        return ClaudeSdkRunner(agent, verbose, dict(options or {}), message_stream=factory)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
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
        summary["system_prompt"] = "claude_code"
        summary["tools"] = "claude_code"
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
        except Exception as exc:
            # Constructor/API drift is surfaced instead of silently retrying
            # with the cwd or settings-isolation options removed.
            yield sdk_error_event("claude", exc)
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
        yield Event.create("error", "error", text, _result_raw(message))
        return
    if verbose:
        subtype = getattr(message, "subtype", None)
        if isinstance(subtype, str) and subtype:
            yield Event.create("claude", "status", _result_status_text(message), _result_raw(message))


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


def build_claude_agent_options(options_cls: Any, options: Dict[str, Any], workdir: Path) -> Any:
    """Construct the verified ``ClaudeAgentOptions`` coding-agent configuration.

    The coding prompt/tool presets and empty filesystem setting sources are
    intentional runtime semantics. If an installed SDK does not accept them,
    its constructor error must remain visible rather than weakening the run.
    """

    mapped = _map_sdk_options(options)
    return options_cls(
        **mapped,
        cwd=str(workdir),
        setting_sources=[],
        system_prompt={"type": "preset", "preset": "claude_code"},
        tools={"type": "preset", "preset": "claude_code"},
    )


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
