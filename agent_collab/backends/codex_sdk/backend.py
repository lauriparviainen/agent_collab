"""The Codex ``sdk`` backend (``openai-codex``), lazy + first-class.

The installed ``openai-codex==0.144.4`` surface keeps one ``AsyncCodex`` client
and ``AsyncThread`` open across collected turns. Each turn starts through
``AsyncThread.turn(...)`` so the live ``AsyncTurnHandle`` can issue
``turn/interrupt``. A captured thread id reconnects through
``AsyncCodex.thread_resume(...)`` after an abnormal turn resets the live
client. The conversation adapter serializes run/reset/close because cancelling
the local asyncio waiter does not stop the provider worker; interrupt must go
through the handle.

``run`` returns one collected ``TurnResult``. Its ``final_response`` is the
stable, message-first surface; its ``items`` are ``ThreadItem`` root models.
Only installed public item roots are mapped. No SDK import, client construction,
or model call happens at module import time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Protocol

from ...approvals import worker_session_run_kwargs
from ...config import AgentConfig
from ...events import Event, compact_json
from ...outcomes import TerminalEvidence, TerminalEvidenceAccumulator, TurnOutcome
from ...runners import AgentRunner, AsyncEventSink
from ...sandbox.specs import SandboxPolicy
from .permissions import (
    host_review_start_payload,
    install_host_approval_handler,
    make_sync_approval_handler,
    park_in_process_codex_approval,
)
from .sandbox import CodexSdkSandboxAdapter
from ..base import (
    BackendCapabilities,
    BackendHealth,
    BackendUnavailable,
    OptionSpec,
    load_option_schema,
    normalize_declared_options,
)
from ..common.health import codex_api_key_credentials, probe_sdk_backend
from ..common.sdk import (
    SDK_CLOSE_GRACE_SECONDS,
    agent_environment,
    backend_unavailable_event,
    package_version,
    sdk_settings_summary,
    provider_session_event,
    sdk_error_event,
    stringify,
)
from ..common.options import configured_choices, resolve_codex_effort

MODULE_NAME = "openai_codex"
PACKAGE_NAME = "openai-codex"
INSTALL_HINT = (
    "install the Codex SDK: pip install openai-codex, or re-run ./agent_collab.sh install"
)

CODEX_SDK_OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))

# ``codex_sdk.sandbox`` -> the verified Codex SDK ``Sandbox`` member.
_SANDBOX_MEMBERS = {
    "read-only": "read_only",
    "workspace-write": "workspace_write",
    "danger-full-access": "full_access",
}


@dataclass(frozen=True)
class CodexTurnOutcome:
    """One collected SDK turn and the public id of its owning thread."""

    thread_id: str
    result: Any


class CodexConversation(Protocol):
    """One runner-owned provider conversation; fakeable without the real SDK."""

    def active(self) -> bool: ...

    async def run(self, prompt: str) -> CodexTurnOutcome: ...

    def note_session_id(self, thread_id: str) -> None: ...

    async def interrupt(self) -> bool: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


ConversationFactory = Callable[
    [AgentConfig, Dict[str, Any], Path],
    CodexConversation,
]


class CodexSdkBackend:
    sandbox_adapter = CodexSdkSandboxAdapter()
    """Registered as ``(codex, "sdk")`` with live-session continuity."""

    id = "sdk"
    agent_type = "codex"
    brand_color = "#10A37F"
    event_fidelity = "message_first"
    provider_session_id_kind = "thread"

    def __init__(self, conversation_factory: Optional[ConversationFactory] = None) -> None:
        self.capabilities = BackendCapabilities(resume=True, interrupt=True, continuity=True)
        self.checks_credentials = True
        self.block_on_unavailable = True
        self._conversation_factory = conversation_factory

    def probe(self) -> BackendHealth:
        return probe_sdk_backend(
            MODULE_NAME,
            package_version=lambda: package_version(PACKAGE_NAME),
            credentials=codex_api_key_credentials,
            extra_hint=INSTALL_HINT,
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(CODEX_SDK_OPTION_SCHEMA)

    def normalize_options(
        self,
        agent: AgentConfig,
        requested: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        configured = agent.options_for(self.id)
        normalized = normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=configured,
            configured_defaults=agent.default_options_for(self.id),
        )
        return resolve_codex_effort(normalized, configured_choices(configured, requested))

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return None

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        factory = self._conversation_factory or _default_conversation
        return CodexSdkRunner(agent, verbose, dict(options or {}), conversation_factory=factory)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        summary = sdk_settings_summary(PACKAGE_NAME, _map_sdk_options(options))
        summary["conversation"] = "persistent"
        codex_bin = _configured_codex_bin(agent)
        summary["runtime"] = "configured_cli" if codex_bin else "sdk_pinned"
        if codex_bin:
            summary["codex_bin"] = codex_bin
        summary["outer_sandbox"] = {
            "support": "sdk_worker",
            "read_only": "complete_worker_inside_bubblewrap",
            "none": "in_process_daemon_runner",
        }
        return summary


class CodexSdkRunner(AgentRunner):
    def __init__(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Dict[str, Any],
        conversation_factory: ConversationFactory,
    ) -> None:
        self.name = agent.id
        self.agent = agent
        self.verbose = verbose
        self.options = options
        self._conversation_factory = conversation_factory
        self._conversation: Optional[CodexConversation] = None
        self._workdir: Optional[Path] = None
        self.sandbox_plan: Optional[object] = None
        self._worker_session: Optional[object] = None
        self._worker_terminal = False
        # Provider continuity is established only after a captured session id,
        # not merely because a Bubblewrap worker process is still alive.
        self._worker_provider_active = False
        self._worker_soft_drop_cancelled = False
        self._resume_session_id: Optional[str] = None
        self._resume_kind = "thread"
        self._resume_quarantined = False

    def conversation_active(self) -> bool:
        if self._resume_quarantined:
            return False
        if self._resume_session_id:
            return True
        if self._worker_terminal:
            return False
        if self._worker_session is not None:
            session = self._worker_session
            if getattr(session, "terminal", False):
                return False
            return self._worker_provider_active
        return self._conversation is not None and self._conversation.active()

    async def interrupt_request(self) -> bool:
        from ...sandbox.worker_session import interrupt_active_session

        if self._worker_session is not None:
            return await interrupt_active_session(self._worker_session)
        conversation = self._conversation
        if conversation is None:
            return False
        interrupt = getattr(conversation, "interrupt", None)
        if not callable(interrupt):
            return False
        return bool(await interrupt())

    def seed_resume_descriptor(self, descriptor: Mapping[str, Any]) -> None:
        session_id = descriptor.get("provider_session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        self._resume_session_id = session_id
        kind = descriptor.get("provider_session_kind")
        if isinstance(kind, str) and kind:
            self._resume_kind = kind
        self._resume_quarantined = False

    async def close(self) -> None:
        if self._worker_session is not None:
            session = self._worker_session
            self._worker_session = None
            self._worker_terminal = True
            self._worker_provider_active = False
            try:
                await session.close()  # type: ignore[union-attr]
            except asyncio.CancelledError:
                try:
                    await session.force_teardown()  # type: ignore[union-attr]
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    await session.force_teardown()  # type: ignore[union-attr]
                except Exception:
                    pass
        if self._conversation is not None:
            await self._conversation.close()

    async def run_turn(self, prompt: str, workdir: Path, emit: AsyncEventSink) -> TurnOutcome:
        if self._resume_quarantined:
            await emit(
                Event.create(
                    "error",
                    "error",
                    f"{self.name} provider conversation is quarantined",
                    {"code": "provider_session_quarantined", "fatal": True},
                )
            )
            return TurnOutcome("failed", "provider_session_quarantined")
        policy = getattr(getattr(self.sandbox_plan, "policy", None), "effective", None)
        if policy is SandboxPolicy.READ_ONLY:
            return await self._run_turn_worker(prompt, workdir, emit)
        return await self._run_turn_in_process(prompt, workdir, emit)

    async def _run_turn_worker(
        self, prompt: str, workdir: Path, emit: AsyncEventSink
    ) -> TurnOutcome:
        from ...sandbox.specs import SandboxFailure
        from ...sandbox.worker_codec import WorkerProtocolError

        if self._worker_terminal:
            await emit(
                Event.create(
                    "error",
                    "error",
                    f"{self.name} outer sandbox worker is terminal",
                    {
                        "code": "outer_sandbox_worker_terminated",
                        "phase": "worker",
                        "fatal": True,
                    },
                )
            )
            return TurnOutcome("failed", "outer_sandbox_worker_terminated")

        if self.verbose:
            await emit(Event.create("codex", "status", f"codex sdk worker starting in {workdir}"))
        try:
            session = await self._worker_for(workdir)
            scratch = getattr(session, "_scratch", None)
            effective_prompt = self.sandbox_plan.render_prompt(prompt, scratch)  # type: ignore[union-attr]

            async def tracking_emit(event: Any) -> None:
                session_meta = getattr(event, "provider_session", None)
                if isinstance(session_meta, Mapping) and session_meta.get("provider_session_id"):
                    self._worker_provider_active = True
                elif isinstance(getattr(event, "raw", None), Mapping):
                    raw = event.raw
                    if raw.get("provider_session_id"):
                        self._worker_provider_active = True
                await emit(event)

            _buffered, outcome = await session.run(
                effective_prompt, **worker_session_run_kwargs(self, emit=tracking_emit)
            )
            # Without a captured provider session, conversation_active is false
            # and the referee re-issues a full task. Drop the live worker so
            # hidden client context or an undelivered prompt cannot join that
            # full task. Soft-drop keeps the runner eligible for a fresh worker.
            if not self._worker_provider_active:
                await self._drop_worker_session()
            return outcome
        except asyncio.CancelledError:
            await self._terminate_worker_session()
            raise
        except SandboxFailure as exc:
            await self._terminate_worker_session()
            await self._emit_sandbox_failure(emit, exc)
            return self._resume_or_plain_failure(exc, exc.code)
        except WorkerProtocolError as exc:
            await self._terminate_worker_session()
            await self._emit_sandbox_failure(emit, exc)
            return self._resume_or_plain_failure(exc, exc.code)
        except Exception as exc:
            await self._terminate_worker_session()
            await emit(sdk_error_event("codex", exc))
            return self._resume_or_plain_failure(exc, "provider_transport_failed")

    def _resume_or_plain_failure(self, exc: BaseException, fallback: str) -> TurnOutcome:
        if not self._resume_session_id:
            return TurnOutcome("failed", fallback)
        from ...resume import resume_failure_status_for_exception

        self._resume_quarantined = True
        return TurnOutcome("failed", resume_failure_status_for_exception(exc))

    async def _emit_sandbox_failure(self, emit: AsyncEventSink, exc: Any) -> None:
        """Best-effort failure event; never block on a stalled sink."""

        try:
            await asyncio.wait_for(
                emit(
                    Event.create(
                        "error",
                        "error",
                        f"{self.name} outer sandbox failed during {exc.phase}: {exc}",
                        {
                            "code": exc.code,
                            "phase": exc.phase,
                            "fatal": True,
                            "remediation": list(getattr(exc, "remediation", ()) or ()),
                        },
                    )
                ),
                1.0,
            )
        except Exception:
            pass

    async def _drop_worker_session(self) -> None:
        """Kill the current worker but remain eligible for a later relaunch."""

        session = self._worker_session
        self._worker_session = None
        self._worker_provider_active = False
        if session is None:
            return
        try:
            await session.force_teardown()  # type: ignore[union-attr]
        except asyncio.CancelledError:
            self._worker_soft_drop_cancelled = True
            raise
        except Exception:
            try:
                session.kill()  # type: ignore[union-attr]
            except Exception:
                try:
                    session.terminate()  # type: ignore[union-attr]
                except Exception:
                    pass
            try:
                await asyncio.shield(session.wait())  # type: ignore[union-attr]
            except asyncio.CancelledError:
                self._worker_soft_drop_cancelled = True
                raise
            except Exception:
                pass

    async def _terminate_worker_session(self) -> None:
        self._turn_ended_locally = True
        session = self._worker_session
        if session is None and self._worker_soft_drop_cancelled:
            self._worker_soft_drop_cancelled = False
            return
        # Mark terminal and drop the reference only after signals are delivered.
        # Kill synchronously first so sticky CancelledError cannot skip SIGKILL.
        if session is not None:
            try:
                session.kill()  # type: ignore[union-attr]
            except Exception:
                try:
                    session.terminate()  # type: ignore[union-attr]
                except Exception:
                    pass
        self._worker_session = None
        self._worker_terminal = True
        self._worker_provider_active = False
        self._worker_soft_drop_cancelled = False
        if session is None:
            return
        try:
            await asyncio.shield(session.cancel_active())  # type: ignore[union-attr]
        except asyncio.CancelledError:
            # cancel_active already killed; wait in background if still needed.
            try:
                asyncio.create_task(session.wait())  # type: ignore[union-attr]
            except Exception:
                pass
            raise
        except Exception:
            try:
                session.kill()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                await asyncio.shield(session.wait())  # type: ignore[union-attr]
            except Exception:
                pass

    async def _worker_for(self, workdir: Path) -> Any:
        from ...sandbox.bubblewrap import discover_bubblewrap
        from ...sandbox.supervisor import SandboxSupervisor
        from ...sandbox.worker_session import SupervisedWorkerSession, handshake_worker

        resolved = workdir.resolve()
        if self._worker_terminal:
            raise RuntimeError("codex sdk worker session is terminal")
        if self._worker_session is not None:
            if getattr(self._worker_session, "terminal", False):
                self._worker_session = None
                self._worker_terminal = True
                self._worker_provider_active = False
                raise RuntimeError("codex sdk worker session is terminal")
            if self._workdir != resolved:
                raise RuntimeError("codex sdk worker workdir changed between turns")
            return self._worker_session
        plan = self.sandbox_plan
        if plan is None:
            raise RuntimeError("codex sdk worker requires a resolved sandbox plan")
        installation = await asyncio.to_thread(discover_bubblewrap)
        process, worker_sock = await SandboxSupervisor(installation).launch_sdk_worker(
            plan,  # type: ignore[arg-type]
            stream_limit=8 * 1024 * 1024,
        )
        assert worker_sock is not None
        reader = writer = None
        try:
            worker_sock.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=worker_sock)
            hello = await handshake_worker(reader, writer)
            adapter = CodexSdkSandboxAdapter()
            effective_cwd = getattr(getattr(plan, "context", None), "cwd", None) or resolved
            resume = None
            if self._resume_session_id:
                resume = {
                    "provider_session_id": self._resume_session_id,
                    "provider_session_kind": self._resume_kind,
                }
            payload = adapter.worker_open_payload_for_agent(
                agent_id=self.name,
                options=self.options,
                workspace=getattr(getattr(plan, "context", None), "workspace", None) or resolved,
                cwd=effective_cwd,
                agent_env=agent_environment(self.agent),
                codex_bin=_configured_codex_bin(self.agent),
                verbose=self.verbose,
                resume=resume,
            )
            from .. import capabilities_for

            payload["tool_gate"] = capabilities_for(self.agent.type, "sdk").tool_gate
            session = SupervisedWorkerSession(
                process,
                reader,
                writer,
                instance=hello.instance,
                control_frames=hello.control_frames,
            )
            session._scratch = process._scratch  # type: ignore[attr-defined]
            await session.open(payload)
        except BaseException:
            # Cover failures before open_connection adopts the socket, and any
            # later handshake/open failure, so the Bubblewrap tree cannot leak.
            try:
                process.kill()
            except Exception:
                pass
            try:
                await process.wait()
            except Exception:
                pass
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            else:
                try:
                    worker_sock.close()
                except Exception:
                    pass
            self._worker_terminal = True
            self._worker_provider_active = False
            raise
        self._worker_session = session
        self._workdir = resolved
        return session

    async def _run_turn_in_process(
        self, prompt: str, workdir: Path, emit: AsyncEventSink
    ) -> TurnOutcome:
        if self.verbose:
            await emit(Event.create("codex", "status", f"codex sdk starting in {workdir}"))

        conversation: Optional[CodexConversation] = None
        thread_id: Optional[str] = None
        evidence = TerminalEvidenceAccumulator()
        exception_code: Optional[str] = None
        try:
            conversation = self._conversation_for(workdir)
            outcome = await conversation.run(prompt)
            thread_id = outcome.thread_id or stringify(getattr(conversation, "_thread_id", None))
            if thread_id:
                conversation.note_session_id(thread_id)
                await emit(provider_session_event("codex", self.name, thread_id, "thread"))
            mapped = _collected_turn_evidence(outcome.result)
            if mapped is None:
                exception_code = "provider_output_invalid"
            else:
                evidence.add(mapped)
            for event in iter_codex_turn_events(outcome.result, self.verbose):
                await emit(event)
        except asyncio.CancelledError:
            if conversation is not None:
                await _reset_conversation_bounded(conversation)
            raise
        except BackendUnavailable as exc:
            await emit(backend_unavailable_event(exc))
            exception_code = "provider_transport_failed"
            if self._resume_session_id:
                from ...resume import resume_failure_status_for_exception

                self._resume_quarantined = True
                exception_code = resume_failure_status_for_exception(exc)
        except Exception as exc:  # startup, auth, and turn errors reach the transcript
            await emit(sdk_error_event("codex", exc))
            exception_code = "provider_transport_failed"
            if self._resume_session_id:
                from ...resume import resume_failure_status_for_exception

                self._resume_quarantined = True
                exception_code = resume_failure_status_for_exception(exc)

        result = evidence.resolve(exception_code=exception_code)
        if _should_reset_after_outcome(result.outcome) and conversation is not None:
            await _reset_conversation_bounded(conversation)
        if self.verbose:
            await emit(Event.create("codex", "status", "codex sdk turn complete"))
        return result

    async def _park_tool_approval(self, method: str, params: Any) -> Mapping[str, Any]:
        """In-process ``approval_handler`` park: session registry, no frames."""

        return await park_in_process_codex_approval(
            callback=getattr(self, "_approval_callback", None),
            agent_id=getattr(self, "_bound_agent_id", None) or self.name,
            turn_id=getattr(self, "_bound_turn_id", None) or "",
            method=method,
            params=params,
        )

    def _conversation_for(self, workdir: Path) -> CodexConversation:
        resolved = workdir.resolve()
        if self._conversation is None:
            factory = self._conversation_factory
            approval_handler = None
            from .. import capabilities_for

            if (
                getattr(self, "_approval_callback", None) is not None
                and capabilities_for(self.agent.type, "sdk").tool_gate
            ):
                approval_handler = make_sync_approval_handler(
                    loop=asyncio.get_running_loop(),
                    park_async=self._park_tool_approval,
                )
            if factory is _default_conversation:
                self._conversation = factory(
                    self.agent,
                    self.options,
                    resolved,
                    approval_handler=approval_handler,
                )
            else:
                self._conversation = factory(self.agent, self.options, resolved)
            if self._resume_session_id:
                self._conversation.note_session_id(self._resume_session_id)
            self._workdir = resolved
        elif self._workdir != resolved:
            raise RuntimeError("codex sdk conversation workdir changed between turns")
        return self._conversation


def _collected_turn_evidence(result: Any) -> Optional[TerminalEvidence]:
    """Map one collected ``TurnResult.status`` onto turn evidence.

    Distinguishable ``interrupted`` becomes ``interrupted`` /
    ``local_turn_interrupted``. Other known statuses keep the shipped
    completed/failed mapping. Unknown shapes return None so the caller
    can fail closed as ``provider_output_invalid``.
    """

    status = _enum_value(getattr(result, "status", None))
    if status == "completed":
        return TerminalEvidence("completed")
    if status == "interrupted":
        return TerminalEvidence("interrupted", "local_turn_interrupted")
    if status == "failed":
        return TerminalEvidence("failed", "provider_terminal_failure")
    return None


def _should_reset_after_outcome(outcome: str) -> bool:
    # A clean interrupt win keeps the live thread so the next delta can
    # continue the same provider session. Transport/failure still resets.
    return outcome not in ("completed", "interrupted")


def iter_codex_turn_events(result: Any, verbose: bool) -> Iterator[Event]:
    """Map one verified ``TurnResult`` onto standard transcript events.

    The final response is emitted first because it is the SDK's stable collected
    message surface.  The corresponding final ``AgentMessageThreadItem`` is
    suppressed to avoid duplicating that response; other captured item roots are
    still mapped afterward.
    """

    turn_id = stringify(getattr(result, "id", None))
    status = _enum_value(getattr(result, "status", None))
    final_response = stringify(getattr(result, "final_response", None))

    if final_response:
        yield Event.create(
            "codex",
            "message",
            final_response,
            {
                "text": final_response,
                "turn_id": turn_id or None,
                "status": status,
                "final": True,
            },
        )

    items = getattr(result, "items", None)
    if isinstance(items, list):
        for wrapped_item in items:
            item = _item_root(wrapped_item)
            if _is_collected_final_message(item, final_response):
                continue
            yield from iter_codex_events(item, verbose)

    error = getattr(result, "error", None)
    if error is not None:
        text = stringify(getattr(error, "message", None)) or "codex sdk turn failed"
        raw: Dict[str, Any] = {"turn_id": turn_id or None, "status": status}
        details = stringify(getattr(error, "additional_details", None))
        if details:
            raw["additional_details"] = details
        yield Event.create("error", "error", text, {**raw, "fatal": True})
    elif status == "failed":
        yield Event.create(
            "error",
            "error",
            "codex sdk turn failed",
            {"turn_id": turn_id or None, "status": status, "fatal": True},
        )


def iter_codex_events(item: Any, verbose: bool) -> Iterator[Event]:
    """Map one installed-SDK ``ThreadItem`` root onto standard events."""

    item = _item_root(item)
    item_type = stringify(getattr(item, "type", None))
    item_id = stringify(getattr(item, "id", None))

    if item_type == "agentMessage":
        text = stringify(getattr(item, "text", None))
        if text:
            yield Event.create(
                "codex",
                "message",
                text,
                {
                    "text": text,
                    "item_id": item_id or None,
                    "phase": _enum_value(getattr(item, "phase", None)),
                },
            )
        return

    if item_type == "reasoning":
        if verbose:
            summary = _string_list(getattr(item, "summary", None))
            content = _string_list(getattr(item, "content", None))
            reasoning = summary or content
            if reasoning:
                yield Event.create(
                    "codex",
                    "status",
                    "\n".join(reasoning),
                    {
                        "reasoning": True,
                        "item_id": item_id or None,
                        "summary": summary,
                        "content": content,
                    },
                )
        return

    if item_type == "commandExecution":
        command = stringify(getattr(item, "command", None))
        if command:
            yield Event.create(
                "tool",
                "command",
                command,
                {
                    "item_id": item_id or None,
                    "command": command,
                    "cwd": _scalar_value(getattr(item, "cwd", None)),
                    "status": _enum_value(getattr(item, "status", None)),
                    "exit_code": getattr(item, "exit_code", None),
                    "aggregated_output": getattr(item, "aggregated_output", None),
                    "duration_ms": getattr(item, "duration_ms", None),
                },
            )
        return

    if item_type == "fileChange":
        changes = _file_changes(getattr(item, "changes", None))
        paths = [change["path"] for change in changes if change.get("path")]
        text = ", ".join(paths) or compact_json(changes)
        yield Event.create(
            "tool",
            "file_change",
            text,
            {
                "item_id": item_id or None,
                "changes": changes,
                "status": _enum_value(getattr(item, "status", None)),
            },
        )
        return

    if verbose and item_type:
        yield Event.create(
            "codex",
            "status",
            f"codex sdk item {item_type}",
            {"item_type": item_type, "item_id": item_id or None},
        )


def _item_root(item: Any) -> Any:
    """Unwrap the SDK's ``ThreadItem(RootModel[...])`` object."""

    root = getattr(item, "root", None)
    return root if root is not None else item


def _is_collected_final_message(item: Any, final_response: str) -> bool:
    if not final_response or stringify(getattr(item, "type", None)) != "agentMessage":
        return False
    if stringify(getattr(item, "text", None)) != final_response:
        return False
    phase = _enum_value(getattr(item, "phase", None))
    # TurnResult derives final_response from final_answer, falling back to an
    # agent message whose phase is absent.
    return phase in (None, "final_answer")


def _enum_value(value: Any) -> Optional[str]:
    # Most generated statuses are Enum values. PatchChangeKind is instead a
    # RootModel whose root has a literal ``type`` discriminator.
    root = getattr(value, "root", value)
    raw = getattr(root, "value", root)
    if not isinstance(raw, str):
        raw = getattr(root, "type", None)
    return raw if isinstance(raw, str) and raw else None


def _scalar_value(value: Any) -> Any:
    root = getattr(value, "root", value)
    enum_value = getattr(root, "value", root)
    if enum_value is None or isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return str(enum_value)


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [part.strip() for part in value if isinstance(part, str) and part.strip()]


def _file_changes(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    changes: List[Dict[str, Any]] = []
    for change in value:
        path = stringify(getattr(change, "path", None))
        diff = stringify(getattr(change, "diff", None))
        entry: Dict[str, Any] = {
            "path": path or None,
            "kind": _enum_value(getattr(change, "kind", None)),
            "diff": diff or None,
        }
        changes.append(entry)
    return changes


def _map_sdk_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only options with a verified SDK equivalent."""

    mapped: Dict[str, Any] = {}
    if "model" in options:
        mapped["model"] = options["model"]
    if "sandbox" in options:
        mapped["sandbox"] = options["sandbox"]
    effort = options.get("reasoning_effort", options.get("thinking_level"))
    if effort is not None:
        mapped["reasoning_effort"] = effort
    return mapped


def sandbox_member_name(value: Any) -> Optional[str]:
    """Map a ``codex_sdk.sandbox`` value to the SDK enum member name."""

    return _SANDBOX_MEMBERS.get(str(value))


def _backend_unavailable(reason: str) -> BackendUnavailable:
    return BackendUnavailable("codex", "sdk", reason, INSTALL_HINT)


def _configured_codex_bin(agent: AgentConfig) -> Optional[str]:
    """Resolve the agent's configured Codex CLI for an intentional SDK override.

    The latest Python beta can lag newly selected Codex models because it pins a
    CLI runtime. Reusing the explicitly configured local executable keeps the
    SDK transport/API while honoring the project's normal Codex runtime. The
    SDK-pinned runtime remains the fallback when no executable is configured or
    resolvable.
    """

    command = agent.command
    if not isinstance(command, str) or not command.strip():
        return None
    return shutil.which(command.strip())


def _default_conversation(
    agent: AgentConfig,
    options: Dict[str, Any],
    workdir: Path,
    *,
    approval_handler: Any = None,
) -> CodexConversation:
    """Build one lazy-imported persistent conversation for a runner.

    ``approval_handler`` is the host sync adapter installed on the private
    ``AsyncCodex._client._sync._approval_handler`` hook after the client
    exists. Omit it for ungated in-process sessions so the SDK default
    accept remains.
    """

    try:
        import openai_codex  # type: ignore
    except ImportError as exc:
        raise _backend_unavailable(f"{MODULE_NAME} is not importable") from exc

    async_codex = getattr(openai_codex, "AsyncCodex", None)
    if (
        async_codex is None
        or not hasattr(async_codex, "thread_start")
        or not hasattr(async_codex, "thread_resume")
        or not hasattr(async_codex, "close")
    ):
        raise _backend_unavailable(
            "openai_codex has no compatible AsyncCodex thread_start/thread_resume/close API"
        )

    client_config = None
    config_kwargs: Dict[str, Any] = {}
    codex_bin = _configured_codex_bin(agent)
    if codex_bin:
        config_kwargs["codex_bin"] = codex_bin
    env = agent_environment(agent)
    if env:
        config_kwargs["env"] = env
    if config_kwargs:
        config_cls = getattr(openai_codex, "CodexConfig", None)
        if config_cls is None:
            raise _backend_unavailable("openai_codex has no compatible CodexConfig API")
        try:
            client_config = config_cls(**config_kwargs)
        except Exception as exc:
            raise _backend_unavailable(f"could not configure Codex SDK client: {exc}") from exc

    mapped = _map_sdk_options(options)
    start_kwargs: Dict[str, Any] = {"cwd": str(workdir)}
    if "model" in mapped:
        start_kwargs["model"] = mapped["model"]

    if "sandbox" in mapped:
        member = sandbox_member_name(mapped["sandbox"])
        sandbox = getattr(openai_codex, "Sandbox", None)
        if member is None or sandbox is None or not hasattr(sandbox, member):
            raise _backend_unavailable(
                f"openai_codex has no compatible Sandbox value for {mapped['sandbox']!r}"
            )
        start_kwargs["sandbox"] = getattr(sandbox, member)

    run_kwargs: Dict[str, Any] = {}
    if "reasoning_effort" in mapped:
        generated = getattr(openai_codex, "generated", None)
        v2_all = getattr(generated, "v2_all", None)
        effort_cls = getattr(v2_all, "ReasoningEffort", None)
        effort_name = str(mapped["reasoning_effort"])
        if effort_cls is None or not hasattr(effort_cls, effort_name):
            raise _backend_unavailable(
                f"openai_codex has no compatible ReasoningEffort value for {effort_name!r}"
            )
        run_kwargs["effort"] = getattr(effort_cls, effort_name)

    return _PersistentCodexConversation(
        async_codex,
        client_config,
        start_kwargs,
        run_kwargs,
        approval_handler=approval_handler,
    )


def _require_host_review_types() -> None:
    """Fail closed if the installed pin lost ``on-request`` / ``user``."""

    try:
        from openai_codex.types import ApprovalsReviewer, AskForApproval
    except ImportError as exc:
        raise _backend_unavailable("openai_codex has no host-review approval start params") from exc
    try:
        AskForApproval.model_validate("on-request")
    except Exception as exc:
        raise _backend_unavailable(
            "openai_codex AskForApproval does not accept on-request"
        ) from exc
    if getattr(ApprovalsReviewer, "user", None) is None:
        raise _backend_unavailable("openai_codex has no ApprovalsReviewer.user")


async def _thread_start(client: Any, thread_kwargs: Dict[str, Any], *, host_review: bool) -> Any:
    """Start a thread; gated sessions force host review on the inner client."""

    if host_review:
        inner_start = getattr(getattr(client, "_client", None), "thread_start", None)
        if not callable(inner_start):
            raise _backend_unavailable("openai_codex has no inner thread_start for host review")
        _require_host_review_types()
        started = await inner_start(host_review_start_payload(thread_kwargs))
        return _wrap_started_thread(client, started)
    return await client.thread_start(**thread_kwargs)


async def _thread_resume(
    client: Any,
    resume_id: str,
    thread_kwargs: Dict[str, Any],
    *,
    host_review: bool,
) -> Any:
    """Resume a thread; gated sessions force host review on the inner client."""

    if host_review:
        inner_resume = getattr(getattr(client, "_client", None), "thread_resume", None)
        if not callable(inner_resume):
            raise _backend_unavailable("openai_codex has no inner thread_resume for host review")
        _require_host_review_types()
        payload = host_review_start_payload(thread_kwargs)
        payload["threadId"] = resume_id
        resumed = await inner_resume(resume_id, payload)
        return _wrap_started_thread(client, resumed)
    return await client.thread_resume(resume_id, **thread_kwargs)


def _wrap_started_thread(client: Any, started: Any) -> Any:
    thread_id = stringify(getattr(getattr(started, "thread", None), "id", None))
    if not thread_id:
        raise _backend_unavailable("openai_codex host-review start returned no thread id")
    try:
        import openai_codex  # type: ignore
    except ImportError as exc:
        raise _backend_unavailable(f"{MODULE_NAME} is not importable") from exc
    async_thread = getattr(openai_codex, "AsyncThread", None)
    if async_thread is None:
        raise _backend_unavailable("openai_codex has no compatible AsyncThread")
    return async_thread(client, thread_id)


class _PersistentCodexConversation:
    """Serialize one live SDK client/thread and its reconnect identity."""

    def __init__(
        self,
        client_factory: Any,
        client_config: Any,
        thread_kwargs: Dict[str, Any],
        run_kwargs: Dict[str, Any],
        *,
        approval_handler: Any = None,
    ) -> None:
        self._client_factory = client_factory
        self._client_config = client_config
        self._thread_kwargs = dict(thread_kwargs)
        self._run_kwargs = dict(run_kwargs)
        self._host_approval_handler = approval_handler
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._thread: Any = None
        self._thread_id: Optional[str] = None
        self._live_handle: Any = None
        self._pending_prompt: Optional[str] = None
        self._closed = False

    def active(self) -> bool:
        # A reset drops only the live transport. The retained id still names
        # provider-side context that the next run will resume, so the referee
        # must keep sending delta prompts rather than replaying the full task.
        return not self._closed and (
            self._thread_id is not None or self._pending_prompt is not None
        )

    def note_session_id(self, thread_id: str) -> None:
        if self._thread_id is not None and self._thread_id != thread_id:
            raise RuntimeError("Codex resumed a different provider thread")
        self._thread_id = thread_id

    async def run(self, prompt: str) -> CodexTurnOutcome:
        # Referee watermarks advance when a prompt is built, before transport
        # delivery. Queue it before waiting for the lifecycle lock so a failed
        # connect/resume—or cancellation behind a slow reset—cannot orphan that
        # delta. Once handed to thread.turn(), delivery is uncertain and replay
        # would risk duplication, so clear it at that boundary.
        self._pending_prompt = _join_pending_prompt(self._pending_prompt, prompt)
        async with self._lock:
            if self._closed:
                raise RuntimeError("codex sdk conversation is closed")
            if self._thread is None:
                await self._connect_locked()
            thread_id = stringify(getattr(self._thread, "id", None))
            turn = getattr(self._thread, "turn", None)
            if not thread_id or not callable(turn):
                raise _backend_unavailable("openai_codex returned an incompatible AsyncThread")
            effective_prompt = self._pending_prompt
            if effective_prompt is None:
                raise RuntimeError("codex sdk pending prompt was lost")
            self._pending_prompt = None
            handle = await turn(effective_prompt, **self._run_kwargs)
            collect = getattr(handle, "run", None)
            if not callable(collect):
                raise _backend_unavailable("openai_codex returned an incompatible AsyncTurnHandle")
            # Publish before collect so interrupt() can fire without _lock.
            self._live_handle = handle
            try:
                result = await _await_provider_run(collect())
                if not hasattr(result, "final_response") or not hasattr(result, "items"):
                    raise _backend_unavailable("openai_codex returned an incompatible TurnResult")
                return CodexTurnOutcome(thread_id=thread_id, result=result)
            finally:
                self._live_handle = None

    async def interrupt(self) -> bool:
        """Issue a provider abort on the live turn handle, if any.

        Must not take ``_lock``: ``run()`` holds it for the whole turn, so
        an in-band acquire would deadlock with the consumer we need to
        unblock. Idle, closed, or missing-handle is a no-op, not an error.

        Starts ``handle.interrupt()`` as a background task and does not
        await the SDK's ``turn/interrupt`` ACK. Acknowledgement is the
        turn's own collected result. An immediate raise is not treated
        as issued.
        """

        if self._closed:
            return False
        handle = self._live_handle
        if handle is None:
            return False
        method = getattr(handle, "interrupt", None)
        if not callable(method):
            return False
        task = asyncio.create_task(method())
        await asyncio.sleep(0)
        if task.done():
            try:
                task.result()
            except BaseException:
                return False
            return True
        task.add_done_callback(_consume_background_result)
        return True

    async def reset(self) -> None:
        async with self._lock:
            client = self._drop_live_locked(keep_thread_id=True)
            if client is not None:
                await client.close()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._drop_live_locked(keep_thread_id=False)
            if client is not None:
                await client.close()

    async def _connect_locked(self) -> None:
        client = (
            self._client_factory(self._client_config)
            if self._client_config is not None
            else self._client_factory()
        )
        try:
            await client.__aenter__()
            if self._host_approval_handler is not None:
                try:
                    install_host_approval_handler(client, self._host_approval_handler)
                except AttributeError as exc:
                    raise _backend_unavailable(str(exc)) from exc
            resume_id = self._thread_id
            host_review = self._host_approval_handler is not None
            if resume_id is None:
                thread = await _thread_start(client, self._thread_kwargs, host_review=host_review)
            else:
                thread = await _thread_resume(
                    client, resume_id, self._thread_kwargs, host_review=host_review
                )
            thread_id = stringify(getattr(thread, "id", None))
            if not thread_id or not callable(getattr(thread, "turn", None)):
                raise _backend_unavailable("openai_codex returned an incompatible AsyncThread")
            if resume_id is not None and thread_id != resume_id:
                raise _backend_unavailable("openai_codex resumed a different provider thread")
        except BaseException:
            await client.close()
            raise
        self._client = client
        self._thread = thread
        self._thread_id = thread_id

    def _drop_live_locked(self, *, keep_thread_id: bool) -> Any:
        client = self._client
        self._client = None
        self._thread = None
        self._live_handle = None
        if not keep_thread_id:
            self._thread_id = None
            self._pending_prompt = None
        return client


def _join_pending_prompt(pending: Optional[str], prompt: str) -> str:
    if not pending:
        return prompt
    return f"{pending}\n\n{prompt}"


async def _await_provider_run(awaitable: Any) -> Any:
    """Keep SDK worker ownership until a cancellation-insensitive run settles."""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise


async def _reset_conversation_bounded(conversation: CodexConversation) -> bool:
    """Reset once; a slow SDK close continues as a background reaper."""

    task = asyncio.create_task(conversation.reset())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=SDK_CLOSE_GRACE_SECONDS)
        return True
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_background_result)
        return False
    except asyncio.CancelledError:
        task.add_done_callback(_consume_background_result)
        raise
    except Exception:
        return False


def _consume_background_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except BaseException:
        pass
