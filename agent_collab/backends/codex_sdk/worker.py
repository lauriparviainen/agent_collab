"""Codex SDK conversation owned entirely by the sandboxed worker process."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from .backend import (
    CodexTurnOutcome,
    _default_conversation,
    _enum_value,
    _reset_conversation_bounded,
    iter_codex_turn_events,
)


class CodexSdkWorkerBackend:
    """Worker-side backend for open/run/reset/close of a Codex SDK conversation."""

    def __init__(self) -> None:
        self._conversation: Any = None
        self._verbose = False
        self._workspace: Optional[Path] = None
        self._agent_id = "codex_sdk"

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
        conversation = _default_conversation(agent, options, cwd)
        self._conversation = conversation
        self._verbose = verbose
        self._workspace = workspace

    async def run(self, prompt: str, *, run_id: str) -> Tuple[List[Any], TurnOutcome]:
        del run_id
        if self._conversation is None:
            raise RuntimeError("codex sdk worker is not open")
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        events: List[Any] = []
        try:
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
            status = _enum_value(getattr(outcome.result, "status", None))
            if status == "completed":
                evidence.add(TerminalEvidence("completed"))
            elif status == "interrupted":
                evidence.add(
                    TerminalEvidence(
                        "cancelled",
                        "provider_turn_cancelled",
                        provider_stop_reason="interrupted",
                    )
                )
            elif status == "failed":
                evidence.add(TerminalEvidence("failed", "provider_terminal_failure"))
            else:
                exception_code = "provider_output_invalid"
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
        if result.outcome != "completed" and self._conversation is not None:
            await _reset_conversation_bounded(self._conversation)
        return events, result

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
