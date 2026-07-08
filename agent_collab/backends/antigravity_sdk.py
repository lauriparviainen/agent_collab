"""The Antigravity ``sdk`` backend (``google-antigravity``), lazy + extras-gated.

The base install and default ``cli`` backend stay standard-library only: the
real ``google.antigravity`` module is imported **lazily** — only inside the
probe's ``find_spec`` check and the default agent factory — never at import time.
So importing this module (which the registry does at startup) costs nothing and
requires no dependency.

**The SDK object shapes are a hypothesis, not a live capture.** The SDK could
not be installed in the spike environment (Python 3.9 < the SDK's required 3.10;
no installable distribution), so the real ``ChatResponse`` / ``ToolCall`` /
``Step`` attributes and async surface were never observed — see
``tests/fixtures/antigravity/README.md``. Per project practice we do not guess
shapes into a hard dependency: the runner reads a small, documented hypothesised
surface through an **injectable agent factory** so tests drive it with a fake
module, it **degrades to message-only** when typed tool events are absent, and it
captures **no** conversation id (none was confirmed). When a Python>=3.10 host
with the SDK is available, re-run the spike, replace the hypothesis fixture with
a real capture, and reconcile the mapping below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

from ..config import AgentConfig
from ..events import Event, _classify_claude_tool_block, compact_json
from ..runners import AgentRunner
from .base import (
    BackendCapabilities,
    BackendHealth,
    BackendUnavailable,
)
from .health import antigravity_credentials, probe_sdk_backend

MODULE_NAME = "google.antigravity"
PACKAGE_NAME = "google-antigravity"
EXTRA_HINT = f"requires the antigravity-sdk extra: pip install agent-collab[antigravity-sdk]"

# A factory builds the SDK agent context manager for one turn. Injectable so
# tests drive the runner with a fake without installing the SDK.
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
                for event in map_sdk_response(response, self.verbose):
                    yield event
        except BackendUnavailable as exc:
            yield Event.create("error", "error", str(exc), {"error": str(exc)})
            return
        except Exception as exc:  # surface SDK errors as transcript errors
            yield Event.create(
                "error", "error", f"antigravity sdk error: {exc}", {"error": str(exc), "exception": exc.__class__.__name__}
            )
            return
        if self.verbose:
            yield Event.create("antigravity", "status", "antigravity sdk turn complete")


def map_sdk_response(response: Any, verbose: bool) -> Iterator[Event]:
    """Map one hypothesised ``ChatResponse`` onto the standard Event stream.

    Hypothesised surface (see module docstring — confirm against a real capture):
    ``response.thoughts`` (reasoning text), ``response.tool_calls`` (typed
    ``ToolCall`` with ``.name`` / ``.input``), and ``response.text`` (final text).
    Reasoning is hidden unless ``verbose`` and never carries an opaque signature.
    If ``tool_calls`` is absent/empty the runner honestly degrades to
    message-only — the same fidelity as the ``cli`` path.
    """

    thoughts = getattr(response, "thoughts", None)
    if verbose and isinstance(thoughts, str) and thoughts.strip():
        yield Event.create("antigravity", "status", thoughts.strip(), {"reasoning": True})

    for tool_call in getattr(response, "tool_calls", None) or []:
        yield _map_tool_call(tool_call)

    text = _response_text(response)
    if text:
        yield Event.create("antigravity", "message", text, {"text": text})


def _map_tool_call(tool_call: Any) -> Event:
    name = getattr(tool_call, "name", None)
    tool_input = getattr(tool_call, "input", None)
    kind = _classify_claude_tool_block({"name": name or ""})
    if isinstance(name, str) and name:
        text = f"{name} {compact_json(tool_input)}" if tool_input else name
    else:
        text = compact_json({"input": tool_input})
    return Event.create("tool", kind, text, {"name": name, "input": tool_input})


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text.strip()
    return ""


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    # Explicit mapping, no blind pass-through. `mode` is cli-only and is rejected
    # at start validation for the sdk backend, so only `model` maps here.
    mapped: Dict[str, Any] = {}
    if "model" in options:
        mapped["model"] = options["model"]
    return mapped


def _default_agent_factory(agent: AgentConfig, options: Dict[str, Any], workdir: Path) -> Any:
    """Lazily import the real SDK and build its agent context manager.

    HYPOTHESIS (unverified — see module docstring). The names here follow the
    plan's hypothesised v0.1.x API. Reconcile against a real capture before
    relying on this in production.
    """

    try:
        from google.antigravity import Agent, LocalAgentConfig  # type: ignore
    except ImportError as exc:
        raise BackendUnavailable("antigravity", "sdk", f"{MODULE_NAME} is not importable", EXTRA_HINT) from exc
    config_kwargs: Dict[str, Any] = {"working_directory": str(workdir)}
    if "model" in options:
        config_kwargs["model"] = options["model"]
    return Agent(LocalAgentConfig(**config_kwargs))


def build_sdk_backends() -> List[AntigravitySdkBackend]:
    return [AntigravitySdkBackend()]
