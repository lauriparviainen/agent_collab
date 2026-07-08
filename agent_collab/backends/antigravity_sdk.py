"""The Antigravity ``sdk`` backend (``google-antigravity``), lazy + extras-gated.

The base install and default ``cli`` backend stay standard-library only: the
real ``google.antigravity`` module is imported **lazily** — only inside the
probe's ``find_spec`` check and the default agent factory — never at import time.
So importing this module (which the registry does at startup) costs nothing and
requires no dependency.

**API shapes CONFIRMED live** against ``google-antigravity`` 0.1.5 (Python 3.12)
during the stage 4.9 spike — see ``tests/fixtures/antigravity/sdk-introspection.json``:

- ``from google.antigravity import Agent, LocalAgentConfig``; ``Agent`` is an
  async context manager; ``response = await agent.chat(prompt)`` returns a
  ``types.ChatResponse``.
- ``await response.text()`` (async) for the final text; ``response.thoughts`` and
  ``response.tool_calls`` are sync properties; each ``types.ToolCall`` has
  ``.name`` (a ``BuiltinTools`` enum or ``str``), ``.args`` (dict),
  ``.canonical_path``, ``.id``.
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
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Iterator, List, Optional

from ..config import AgentConfig
from ..events import Event, compact_json
from ..runners import AgentRunner
from .base import BackendCapabilities, BackendHealth, BackendUnavailable
from .health import antigravity_credentials, probe_sdk_backend

MODULE_NAME = "google.antigravity"
PACKAGE_NAME = "google-antigravity"
EXTRA_HINT = "requires the antigravity-sdk extra: pip install agent-collab[antigravity-sdk]"

# Built-in tool names (google.antigravity.types.BuiltinTools) that mean a file
# was written vs a shell command ran; everything else is a generic tool call.
_FILE_CHANGE_TOOLS = {"CREATE_FILE", "EDIT_FILE"}
_COMMAND_TOOLS = {"RUN_COMMAND"}

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
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=self._package_version,
            credentials=antigravity_credentials,
            extra_hint=EXTRA_HINT,
        )

    @staticmethod
    def _package_version() -> Optional[str]:
        try:
            from importlib.metadata import PackageNotFoundError, version
        except ImportError:  # pragma: no cover - py<3.8 only
            return None
        try:
            return version(PACKAGE_NAME)
        except PackageNotFoundError:
            return None

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Dict[str, Any]) -> AgentRunner:
        factory = self._agent_factory or _default_agent_factory
        return AntigravitySdkRunner(agent, verbose, dict(options or {}), agent_factory=factory)

    def settings_summary(self, agent: AgentConfig, options: Dict[str, Any]) -> Dict[str, Any]:
        # The sdk backend has no command_preview; it summarises itself instead.
        summary: Dict[str, Any] = {"backend": "sdk", "package": PACKAGE_NAME}
        version = self._package_version()
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
                text = await _resolve_text(response)
                thoughts = getattr(response, "thoughts", None)
                tool_calls = getattr(response, "tool_calls", None) or []
                for event in map_antigravity_turn(text, thoughts, tool_calls, self.verbose):
                    yield event
                conversation_id = getattr(sdk_agent, "conversation_id", None)
                if self.verbose and conversation_id:
                    # Capture the confirmed provider conversation id in the
                    # transcript. Nothing resumes it this stage (capabilities stay
                    # false); structured agent_sessions persistence is future work.
                    yield Event.create(
                        "antigravity",
                        "status",
                        f"antigravity conversation_id={conversation_id}",
                        {"conversation_id": conversation_id},
                    )
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        except Exception as exc:  # surface SDK errors as transcript errors
            yield Event.create(
                "error",
                "error",
                f"antigravity sdk error: {exc}",
                {"error": str(exc), "exception": exc.__class__.__name__},
            )
            return
        if self.verbose:
            yield Event.create("antigravity", "status", "antigravity sdk turn complete")


def map_antigravity_turn(
    text: Optional[str],
    thoughts: Optional[str],
    tool_calls: Iterable[Any],
    verbose: bool,
) -> Iterator[Event]:
    """Map one resolved ``ChatResponse`` onto the standard Event stream.

    text -> ``antigravity`` message; each ``ToolCall`` -> tool_call/command/
    file_change (classified from its ``BuiltinTools`` name); thoughts -> a
    ``verbose`` status (reasoning text only, never an opaque signature). An empty
    ``tool_calls`` degrades honestly to message-only — the same fidelity as cli.
    """

    if verbose and isinstance(thoughts, str) and thoughts.strip():
        yield Event.create("antigravity", "status", thoughts.strip(), {"reasoning": True})

    for tool_call in tool_calls or []:
        yield _map_tool_call(tool_call)

    if isinstance(text, str) and text.strip():
        yield Event.create("antigravity", "message", text.strip(), {"text": text.strip()})


async def _resolve_text(response: Any) -> str:
    """``ChatResponse.text`` is an async method; tolerate a str/sync attr too."""

    text_attr = getattr(response, "text", None)
    if callable(text_attr):
        result = text_attr()
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, str) else ""
    return text_attr if isinstance(text_attr, str) else ""


def _map_tool_call(tool_call: Any) -> Event:
    name = getattr(tool_call, "name", None)
    name_str = name.name if isinstance(name, enum.Enum) else (str(name) if name is not None else "")
    args = getattr(tool_call, "args", None)
    canonical_path = getattr(tool_call, "canonical_path", None)
    kind = _classify_antigravity_tool(name_str)
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
        {"name": name_str, "args": args, "canonical_path": canonical_path},
    )


def _classify_antigravity_tool(name: str) -> str:
    up = str(name).upper()
    if up in _FILE_CHANGE_TOOLS or any(token in up for token in ("EDIT", "WRITE", "PATCH", "CREATE_FILE")):
        return "file_change"
    if up in _COMMAND_TOOLS or any(token in up for token in ("COMMAND", "BASH", "EXEC", "SHELL")):
        return "command"
    return "tool_call"


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
        raise BackendUnavailable("antigravity", "sdk", f"{MODULE_NAME} is not importable", EXTRA_HINT) from exc
    config_kwargs: Dict[str, Any] = {"workspaces": [str(workdir)]}
    if "model" in options:
        config_kwargs["model"] = options["model"]
    return Agent(LocalAgentConfig(**config_kwargs))


def build_sdk_backends() -> List[AntigravitySdkBackend]:
    return [AntigravitySdkBackend()]
