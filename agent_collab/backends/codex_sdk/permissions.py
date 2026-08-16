"""Codex SDK ``approval_handler`` park helper shared by worker and in-process paths.

The installed ``openai-codex==0.144.4`` surface is a **synchronous** handler on
``CodexClient``. Public ``AsyncCodex`` / ``AsyncCodexClient`` do not accept
``approval_handler``; ``AsyncCodexClient`` always constructs
``CodexClient(config=config)``. The only installed host hook is therefore the
private chain ``async_codex._client._sync._approval_handler``. Missing links
fail closed — never leave the SDK default accept in place when a host gate
was requested.

The SDK default handler auto-accepts both known ``requestApproval`` methods.
The handler runs on the stdout reader thread; blocking it blocks all further
JSON-RPC reads, including ``turn/interrupt`` responses. Capture the running
event loop at bind/open (not inside the handler) and return the JSON-RPC
result only after the host decision via ``run_coroutine_threadsafe``.

Do not write the worker socket from the handler. Worker parks go through
``request_approval`` on the serve loop. Full tool input stays off the worker
frame; the summary is built at the source.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Awaitable, Callable, Mapping, Optional

from ...approvals import build_approval_summary, sanitize_tool_name


RequestApproval = Callable[..., Awaitable[Mapping[str, Any]]]
ApprovalHandler = Callable[[str, Optional[Mapping[str, Any]]], Mapping[str, Any]]

COMMAND_EXECUTION_APPROVAL = "item/commandExecution/requestApproval"
FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
KNOWN_APPROVAL_METHODS = frozenset({COMMAND_EXECUTION_APPROVAL, FILE_CHANGE_APPROVAL})

# Wheel 0.144.4 names only ``accept`` on the default handler. Item statuses
# ``CommandExecutionStatus`` / ``PatchApplyStatus`` include ``declined``, but
# no requestApproval deny token is generated. ``decline`` is the fail-closed
# non-accept decision recorded in Appendix A.
ACCEPT_DECISION = "accept"
DENY_DECISION = "decline"


def accept_payload() -> dict[str, str]:
    return {"decision": ACCEPT_DECISION}


def deny_payload() -> dict[str, str]:
    return {"decision": DENY_DECISION}


def tool_name_for_method(method: str, params: Any = None) -> str:
    """Stable public tool name. Never the raw command string or secrets."""

    if method == COMMAND_EXECUTION_APPROVAL:
        return "command"
    if method == FILE_CHANGE_APPROVAL:
        return "file_change"
    if isinstance(params, Mapping):
        for key in ("name", "tool", "type"):
            raw = params.get(key)
            if raw:
                return sanitize_tool_name(raw)
    return sanitize_tool_name(method or "tool")


def approval_result_from_decision(envelope: Any) -> Mapping[str, str]:
    """Map an ``approval_decision`` envelope to the SDK JSON-RPC result.

    Anything other than an explicit ``approve`` is a deny (fail-closed).
    """

    decision = envelope.get("decision") if isinstance(envelope, Mapping) else None
    if decision == "approve":
        return accept_payload()
    return deny_payload()


def mint_approval_id() -> str:
    return secrets.token_hex(16)


def install_host_approval_handler(async_codex: Any, handler: ApprovalHandler) -> None:
    """Replace the sync client's ``_approval_handler`` via the private chain.

    Public ``AsyncCodex`` / ``AsyncCodexClient`` do not accept
    ``approval_handler`` (openai-codex 0.144.4). The only installed hook is
    ``async_codex._client._sync._approval_handler``. Missing ``_client`` /
    ``_sync`` / ``_approval_handler`` raises ``AttributeError`` so the caller
    can fail closed — never silently keep the SDK default accept.
    """

    client = getattr(async_codex, "_client", None)
    sync = getattr(client, "_sync", None) if client is not None else None
    if sync is None or not hasattr(sync, "_approval_handler"):
        raise AttributeError("openai_codex has no installable approval_handler hook")
    sync._approval_handler = handler


def make_sync_approval_handler(
    *,
    loop: Optional[asyncio.AbstractEventLoop],
    park_async: Optional[Callable[[str, Any], Awaitable[Mapping[str, Any]]]],
) -> ApprovalHandler:
    """Adapt the async park helper to the SDK's synchronous reader-thread hook.

    ``loop`` must be the host/serve loop captured at bind/open, not looked up
    inside the handler. The handler body blocks the reader until the host
    decision returns; call it from a worker thread, never from that loop.
    """

    def handler(method: str, params: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if method not in KNOWN_APPROVAL_METHODS:
            return {}
        if park_async is None or loop is None:
            return deny_payload()
        try:
            return dict(asyncio.run_coroutine_threadsafe(park_async(method, params), loop).result())
        except Exception:
            return deny_payload()

    return handler


async def park_codex_tool_approval(
    *,
    request_approval: Optional[RequestApproval],
    method: str,
    params: Any = None,
) -> Mapping[str, Any]:
    """Park one known ``requestApproval`` until an approve/deny decision.

    ``request_approval`` is the worker serve-loop helper (frames) or the
    in-process registry wrapper (no frames). Missing helper is a deny.
    Unknown methods return ``{}`` (same as the SDK default handler).
    """

    if method not in KNOWN_APPROVAL_METHODS:
        return {}
    if request_approval is None:
        return deny_payload()
    approval_id = mint_approval_id()
    tool_input = params if isinstance(params, Mapping) else {}
    summary, truncated = build_approval_summary(tool_input=tool_input)
    envelope = await request_approval(
        approval_id,
        tool_name=tool_name_for_method(method, params),
        summary=summary,
        summary_truncated=truncated,
    )
    return approval_result_from_decision(envelope)


async def park_in_process_codex_approval(
    *,
    callback: Optional[Callable[..., Any]],
    agent_id: str,
    turn_id: str,
    method: str,
    params: Any = None,
) -> Mapping[str, Any]:
    """Park through the session approval registry. No worker frames.

    Registers, then awaits the bound ``send_decision``. Does not take a new
    lock across the human wait; the conversation ``run()`` lock is already
    held (deny-pending-before-close releases it).
    """

    if callback is None:
        return deny_payload()

    async def request_approval(approval_id: str, **fields: Any) -> Mapping[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()

        async def send_decision(decision: str) -> bool:
            if future.done():
                return False
            future.set_result({"decision": decision, "approval_id": approval_id})
            return True

        payload = {
            "request_id": approval_id,
            "approval_id": approval_id,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "tool_name": fields.get("tool_name") or tool_name_for_method(method, params),
            "summary": fields.get("summary"),
            "summary_truncated": bool(fields.get("summary_truncated")),
            "send_decision": send_decision,
        }
        try:
            result = callback(payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            return {"decision": "deny", "approval_id": approval_id}
        return await future

    return await park_codex_tool_approval(
        request_approval=request_approval,
        method=method,
        params=params,
    )
