"""Antigravity SDK conversation owned entirely by the sandboxed worker process."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Tuple

from ...events import Event
from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from ...sandbox.worker_codec import sanitize_error_text
from ..common.sdk import provider_session_event
from .backend import (
    _default_conversation,
    _reset_conversation_bounded,
    map_antigravity_turn,
)

EventEmit = Callable[[Any], Awaitable[None]]


class AntigravitySdkWorkerBackend:
    """Worker-side backend for open/run/reset/close of an Antigravity conversation."""

    def __init__(self) -> None:
        self._conversation: Any = None
        self._verbose = False
        self._workspace: Optional[Path] = None
        self._agent_id = "antigravity_sdk"

    async def open(self, payload: Mapping[str, Any]) -> None:
        workspace = Path(str(payload["workspace"])).resolve()
        raw_cwd = payload.get("cwd")
        cwd = Path(str(raw_cwd)).resolve() if raw_cwd else workspace
        options = dict(payload.get("options") or {})
        backend_config = dict(payload.get("backend_config") or {})
        verbose = bool(payload.get("verbose"))
        agent_env = dict(payload.get("agent_env") or {})
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            self._agent_id = agent_id
        save_dir = payload.get("save_dir")
        app_data_dir = payload.get("app_data_dir")
        if not isinstance(save_dir, str) or not save_dir:
            raise RuntimeError("antigravity sdk worker open requires save_dir")
        if not isinstance(app_data_dir, str) or not app_data_dir:
            raise RuntimeError("antigravity sdk worker open requires app_data_dir")
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        Path(app_data_dir).mkdir(parents=True, exist_ok=True)

        agent = _WorkerAgent(
            agent_id=self._agent_id,
            env=agent_env,
            backend_config=backend_config,
        )
        # Outer proof allows the permissive SDK policy/capabilities profile.
        # LocalAgentConfig.workspaces always includes the session workspace root
        # so workspace-scoped tools can see siblings of a nested agent cwd.
        # When the Bubblewrap-effective cwd is outside that root (supported
        # absolute agent.cwd override), declare it as an extra workspace so
        # tools do not reject the process cwd tree.
        extras: list[Path] = []
        if cwd != workspace:
            try:
                cwd.relative_to(workspace)
            except ValueError:
                extras.append(cwd)
        conversation = _default_conversation(
            agent,
            options,
            workspace,
            save_dir=save_dir,
            app_data_dir=app_data_dir,
            allow_all_policy=True,
            extra_workspaces=extras,
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
            raise RuntimeError("antigravity sdk worker is not open")
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        events: List[Any] = []
        clean_close = True
        try:
            turn = await self._conversation.run(prompt)
            clean_close = turn.response_clean_close
            mapped = list(map_antigravity_turn(turn.chunks, self._verbose, turn.usage_metadata))
            for event in mapped:
                await _deliver(emit, events, event)
            if any(
                event.type == "message" and event.source == "antigravity" and event.text.strip()
                for event in mapped
            ):
                evidence.add(TerminalEvidence("completed"))
            else:
                exception_code = "provider_empty_response"
            if turn.conversation_id:
                self._conversation.note_session_id(turn.conversation_id)
                await _deliver(
                    emit,
                    events,
                    provider_session_event(
                        "antigravity",
                        self._agent_id,
                        turn.conversation_id,
                        "conversation",
                    ),
                )
        except Exception as exc:
            # If chat() assigned a conversation id before resolve failed, surface
            # it so the daemon marks continuity and does not soft-drop a
            # resumable worker / block relaunch incorrectly.
            if self._conversation is not None:
                cid = getattr(self._conversation, "_conversation_id", None)
                if isinstance(cid, str) and cid:
                    await _deliver(
                        emit,
                        events,
                        provider_session_event(
                            "antigravity",
                            self._agent_id,
                            cid,
                            "conversation",
                        ),
                    )
            await _deliver(
                emit,
                events,
                Event.create(
                    "error",
                    "error",
                    sanitize_error_text(f"antigravity sdk error: {type(exc).__name__}"),
                    {
                        "error": sanitize_error_text(type(exc).__name__),
                        "exception": type(exc).__name__,
                        "fatal": True,
                    },
                ),
            )
            exception_code = "provider_transport_failed"
        if not clean_close and exception_code is None:
            exception_code = "provider_transport_failed"
        result = evidence.resolve(exception_code=exception_code)
        if result.outcome != "completed" and self._conversation is not None:
            await _reset_conversation_bounded(self._conversation)
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

    def __init__(
        self,
        *,
        agent_id: str,
        env: Mapping[str, str],
        backend_config: Mapping[str, Any],
    ) -> None:
        self.id = agent_id
        self.env = dict(env)
        self.command = None
        self.backend_config = dict(backend_config)
        self.type = "antigravity"
        self.backend = "sdk"

    def options_for(self, backend_id: str) -> Mapping[str, Any]:
        del backend_id
        return {}

    def default_options_for(self, backend_id: str) -> Mapping[str, Any]:
        del backend_id
        return {}
