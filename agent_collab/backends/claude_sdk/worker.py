"""Claude SDK conversation owned entirely by the sandboxed worker process."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Tuple

from ...events import Event
from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from ...sandbox.worker_codec import sanitize_error_text
from ..common.sdk import close_async_stream
from .backend import (
    _default_conversation,
    _is_result_message,
    _message_session_id,
    _reset_conversation_bounded,
    iter_claude_events,
)

EventEmit = Callable[[Any], Awaitable[None]]


class ClaudeSdkWorkerBackend:
    """Worker-side backend for open/run/reset/close of a Claude SDK conversation."""

    def __init__(self) -> None:
        self._conversation: Any = None
        self._verbose = False
        self._workspace: Optional[Path] = None
        self._agent_id = "claude_sdk"

    async def open(self, payload: Mapping[str, Any]) -> None:
        workspace = Path(str(payload["workspace"])).resolve()
        raw_cwd = payload.get("cwd")
        cwd = Path(str(raw_cwd)).resolve() if raw_cwd else workspace
        options = dict(payload.get("options") or {})
        verbose = bool(payload.get("verbose"))
        agent_env = dict(payload.get("agent_env") or {})
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            self._agent_id = agent_id

        agent = _WorkerAgent(agent_id=self._agent_id, env=agent_env)
        # Outer read-only worker path suppresses ambient project/user MCP.
        conversation = _default_conversation(
            agent,
            options,
            cwd,
            suppress_ambient_mcp=True,
        )
        self._conversation = conversation
        self._verbose = verbose
        self._workspace = workspace

    async def run(
        self,
        prompt: str,
        *,
        run_id: str,
        emit: Optional[EventEmit] = None,
    ) -> Tuple[List[Any], TurnOutcome]:
        del run_id
        if self._conversation is None:
            raise RuntimeError("claude sdk worker is not open")
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        # Residual list for callers that do not stream via emit.
        events: List[Any] = []
        stream = None
        clean_close = True
        session_id: Optional[str] = None
        try:
            stream = self._conversation.run(prompt)
            async for message in stream:
                sid = _message_session_id(message)
                # Match the in-process runner: emit provider_session once per id.
                if sid and sid != session_id:
                    session_id = sid
                    self._conversation.note_session_id(sid)
                    from ..common.sdk import provider_session_event

                    await _deliver(
                        emit,
                        events,
                        provider_session_event("claude", self._agent_id, sid, "session"),
                    )
                if _is_result_message(message):
                    if getattr(message, "is_error", False):
                        evidence.add(TerminalEvidence("failed", "provider_terminal_failure"))
                    else:
                        evidence.add(TerminalEvidence("completed"))
                for event in iter_claude_events(message, self._verbose):
                    await _deliver(emit, events, event)
        except Exception as exc:
            await _deliver(
                emit,
                events,
                Event.create(
                    "error",
                    "error",
                    sanitize_error_text(f"claude sdk error: {type(exc).__name__}"),
                    {
                        "error": sanitize_error_text(type(exc).__name__),
                        "exception": type(exc).__name__,
                        "fatal": True,
                    },
                ),
            )
            exception_code = "provider_transport_failed"
        finally:
            if stream is not None:
                clean_close = await close_async_stream(stream)
        if not clean_close and exception_code is None:
            exception_code = "provider_transport_failed"
        result = evidence.resolve(exception_code=exception_code)
        if result.outcome != "completed" and self._conversation is not None:
            await _reset_conversation_bounded(self._conversation)
        # When emit is provided, events already crossed the framed transport.
        return ([] if emit is not None else events), result

    async def reset(self) -> None:
        if self._conversation is not None:
            await self._conversation.reset()

    async def close(self) -> None:
        if self._conversation is not None:
            await self._conversation.close()
            self._conversation = None


async def _deliver(
    emit: Optional[EventEmit],
    events: List[Any],
    event: Any,
) -> None:
    if emit is not None:
        await emit(event)
    else:
        events.append(event)


class _WorkerAgent:
    """Duck-typed AgentConfig subset for the existing conversation factory."""

    def __init__(self, *, agent_id: str, env: Mapping[str, str]) -> None:
        self.id = agent_id
        self.env = dict(env)
        self.command = None

    def options_for(self, backend_id: str) -> Mapping[str, Any]:
        del backend_id
        return {}
