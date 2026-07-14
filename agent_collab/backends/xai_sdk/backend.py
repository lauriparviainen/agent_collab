"""Message-only remote chat backend for the official ``xai-sdk`` package.

The verified 1.17.0 surface is imported only inside the production async turn
stream: ``async with AsyncClient()``, ``client.chat.create(...)``,
``chat.append(user(prompt))``, and ``await chat.sample()``. The response's
public ``content`` and ``id`` fields are the only mapped fields in this stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, Mapping, Optional

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
from ..base import BackendCapabilities, BackendHealth, BackendUnavailable
from ..common.health import probe_sdk_backend, xai_api_key_credentials
from ..common.options import canonical_reasoning
from ..common.sdk import (
    backend_unavailable_event,
    close_async_stream,
    package_version,
    sdk_settings_summary,
    provider_session_event,
    sdk_error_event,
    stringify,
)

MODULE_NAME = "xai_sdk"
PACKAGE_NAME = "xai-sdk"
INSTALL_HINT = "install the xAI SDK: pip install xai-sdk, or re-run ./agent_collab.sh install"

OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))
TurnStreamFactory = Callable[[AgentConfig, Dict[str, Any], Path, str], AsyncIterator[Any]]


def _map_sdk_options(options: Mapping[str, Any]) -> Dict[str, Any]:
    mapped: Dict[str, Any] = {}
    if options.get("model"):
        mapped["model"] = options["model"]
    if options.get("thinking_level"):
        mapped["reasoning_effort"] = options["thinking_level"]
    return mapped


class XaiSdkBackend:
    id = "sdk"
    agent_type = "xai"
    # xAI's brand is monochrome rather than a single signature hue. A mid-light
    # neutral remains legible on both dark and light terminal backgrounds.
    brand_color = "#A0A0A0"
    event_fidelity = "message_only"
    provider_session_id_kind = "response"
    capabilities = BackendCapabilities()
    checks_credentials = True
    block_on_unavailable = True

    def __init__(self, turn_stream: Optional[TurnStreamFactory] = None) -> None:
        self._turn_stream = turn_stream

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
        return summary

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        return XaiSdkRunner(
            agent,
            verbose,
            dict(options or {}),
            turn_stream=self._turn_stream or _default_turn_stream,
        )


class XaiSdkRunner(AgentRunner):
    def __init__(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Dict[str, Any],
        turn_stream: TurnStreamFactory,
    ) -> None:
        self.name = agent.id
        self.agent = agent
        self.verbose = verbose
        self.options = options
        self._turn_stream = turn_stream

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        if self.verbose:
            await emit(Event.create("xai", "status", f"xai sdk starting in {workdir}"))
        stream: Optional[AsyncIterator[Any]] = None
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        clean_close = True
        try:
            stream = self._turn_stream(self.agent, self.options, workdir, prompt)
            async for response in stream:
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
                response_id = stringify(getattr(response, "id", None))
                if response_id:
                    await emit(provider_session_event("xai", self.name, response_id, "response"))
        except BackendUnavailable as exc:
            await emit(backend_unavailable_event(exc))
            exception_code = "provider_transport_failed"
        except Exception as exc:
            await emit(sdk_error_event("xai", exc))
            exception_code = "provider_transport_failed"
        finally:
            clean_close = await close_async_stream(stream)
        if not clean_close and exception_code is None:
            exception_code = "provider_transport_failed"
        if self.verbose:
            await emit(Event.create("xai", "status", "xai sdk turn complete"))
        return evidence.resolve(exception_code=exception_code)


def iter_xai_response_events(response: Any) -> Iterator[Event]:
    content = stringify(getattr(response, "content", None))
    if content:
        yield Event.create("xai", "message", content, {"text": content})


def _finish_reason(response: Any) -> Optional[str]:
    value = getattr(response, "finish_reason", None)
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raw = getattr(value, "name", None)
    return raw if isinstance(raw, str) and raw else None


async def _default_turn_stream(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
    prompt: str,
) -> AsyncIterator[Any]:
    del agent, workdir
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
    async with AsyncClient() as client:
        chat = client.chat.create(**mapped)
        chat.append(user(prompt))
        yield await chat.sample()
