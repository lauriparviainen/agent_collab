"""Codex SDK conversation owned entirely by the sandboxed worker process."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Tuple

from ...outcomes import TerminalEvidenceAccumulator, TurnOutcome
from .backend import (
    CodexTurnOutcome,
    _collected_turn_evidence,
    _default_conversation,
    _reset_conversation_bounded,
    _should_reset_after_outcome,
    iter_codex_turn_events,
)
from .permissions import make_sync_approval_handler, park_codex_tool_approval

EventEmit = Callable[[Any], Awaitable[None]]


class CodexSdkWorkerBackend:
    """Worker-side backend for open/run/reset/close of a Codex SDK conversation."""

    def __init__(self) -> None:
        self._conversation: Any = None
        self._verbose = False
        self._workspace: Optional[Path] = None
        self._agent_id = "codex_sdk"
        self._request_approval: Optional[Callable[..., Awaitable[Mapping[str, Any]]]] = None
        self._approval_loop: Optional[asyncio.AbstractEventLoop] = None
        self._sync_approval_handler: Optional[Callable[..., Mapping[str, Any]]] = None

    async def open(self, payload: Mapping[str, Any]) -> None:
        workspace = Path(str(payload["workspace"])).resolve()
        raw_cwd = payload.get("cwd")
        cwd = Path(str(raw_cwd)).resolve() if raw_cwd else workspace
        options = dict(payload.get("options") or {})
        verbose = bool(payload.get("verbose"))
        agent_env = dict(payload.get("agent_env") or {})
        codex_bin = payload.get("codex_bin")
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            self._agent_id = agent_id

        # Minimal agent stand-in so the existing conversation factory can reuse
        # env/command mapping without importing daemon session state.
        agent = _WorkerAgent(
            agent_id=self._agent_id,
            env=agent_env,
            command=codex_bin if isinstance(codex_bin, str) else None,
        )
        # Codex thread cwd is the effective agent cwd, not only the session root.
        # Worker serve always binds approvals; install the host handler so the
        # SDK default accept cannot shadow the gate. Capture the serve loop
        # here, not inside the reader-thread handler.
        self._approval_loop = asyncio.get_running_loop()
        self._sync_approval_handler = make_sync_approval_handler(
            loop=self._approval_loop,
            park_async=self._park_tool_approval,
        )
        from ...resume import require_resume_session_id

        resume_id = require_resume_session_id(payload)
        conversation = _default_conversation(
            agent,
            options,
            cwd,
            approval_handler=self._sync_approval_handler,
        )
        if resume_id is not None:
            conversation.note_session_id(resume_id)
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
            raise RuntimeError("codex sdk worker is not open")
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        events: List[Any] = []
        try:
            # Codex settles handle.run before events are available, so residual
            # return-list delivery remains the primary path; emit is optional.
            outcome: CodexTurnOutcome = await self._conversation.run(prompt)
            if outcome.thread_id:
                self._conversation.note_session_id(outcome.thread_id)
                from ..common.sdk import provider_session_event

                events.append(
                    provider_session_event(
                        "codex",
                        self._agent_id,
                        outcome.thread_id,
                        "thread",
                    )
                )
            mapped = _collected_turn_evidence(outcome.result)
            if mapped is None:
                exception_code = "provider_output_invalid"
            else:
                evidence.add(mapped)
            events.extend(list(iter_codex_turn_events(outcome.result, self._verbose)))
        except Exception as exc:
            from ...sandbox.worker_codec import sanitize_error_text
            from ...events import Event

            # Sanitized, non-secret text only; never ship raw exception strings.
            events.append(
                Event.create(
                    "error",
                    "error",
                    sanitize_error_text(f"codex sdk error: {type(exc).__name__}"),
                    {
                        "error": sanitize_error_text(type(exc).__name__),
                        "exception": type(exc).__name__,
                        "fatal": True,
                    },
                )
            )
            exception_code = "provider_transport_failed"
        result = evidence.resolve(exception_code=exception_code)
        if _should_reset_after_outcome(result.outcome) and self._conversation is not None:
            await _reset_conversation_bounded(self._conversation)
        if emit is not None:
            for event in events:
                await emit(event)
            return [], result
        return events, result

    async def interrupt(self, run_id: str) -> None:
        del run_id
        conversation = self._conversation
        if conversation is None:
            return
        method = getattr(conversation, "interrupt", None)
        if not callable(method):
            return
        await method()

    async def _park_tool_approval(self, method: str, params: Any) -> Mapping[str, Any]:
        """Worker ``approval_handler`` park: enqueue via the serve loop."""

        return await park_codex_tool_approval(
            request_approval=self._request_approval,
            method=method,
            params=params,
        )

    def bind_approvals(self, request_approval: Callable[..., Awaitable[Mapping[str, Any]]]) -> None:
        self._request_approval = request_approval
        if self._approval_loop is None:
            try:
                self._approval_loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        if self._sync_approval_handler is None:
            self._sync_approval_handler = make_sync_approval_handler(
                loop=self._approval_loop,
                park_async=self._park_tool_approval,
            )

    async def reset(self) -> None:
        if self._conversation is not None:
            await self._conversation.reset()

    async def close(self) -> None:
        if self._conversation is not None:
            await self._conversation.close()
            self._conversation = None


class _WorkerAgent:
    """Duck-typed AgentConfig subset for the existing conversation factory."""

    def __init__(
        self,
        *,
        agent_id: str,
        env: Mapping[str, str],
        command: Optional[str],
    ) -> None:
        self.id = agent_id
        self.env = dict(env)
        self.command = command

    def options_for(self, backend_id: str) -> Mapping[str, Any]:
        del backend_id
        return {}
