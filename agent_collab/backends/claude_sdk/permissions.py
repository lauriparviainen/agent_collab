"""Claude SDK ``can_use_tool`` park helper shared by worker and in-process paths.

The callback mints a request id, parks through the supplied approval helper,
and maps the decision onto the SDK's approve/deny result types. Full tool
input stays off the worker frame; the summary is built at the source.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from ...approvals import build_approval_summary, sanitize_tool_name


RequestApproval = Callable[..., Awaitable[Mapping[str, Any]]]


@dataclass
class _FallbackAllow:
    behavior: str = "allow"
    updated_input: Any = None
    updated_permissions: Any = None


@dataclass
class _FallbackDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False


def permission_allow() -> Any:
    try:
        from claude_agent_sdk import PermissionResultAllow  # type: ignore
    except ImportError:
        return _FallbackAllow()
    return PermissionResultAllow()


def permission_deny(message: str = "denied") -> Any:
    try:
        from claude_agent_sdk import PermissionResultDeny  # type: ignore
    except ImportError:
        return _FallbackDeny(message=message)
    return PermissionResultDeny(message=message)


def permission_result_from_decision(envelope: Any) -> Any:
    """Map an ``approval_decision`` envelope to the SDK result type.

    Anything other than an explicit ``approve`` is a deny (fail-closed).
    """

    decision = envelope.get("decision") if isinstance(envelope, Mapping) else None
    if decision == "approve":
        return permission_allow()
    message = "denied"
    if isinstance(envelope, Mapping):
        raw = envelope.get("message")
        if raw:
            message = str(raw)
    return permission_deny(message=message)


def mint_approval_id() -> str:
    return secrets.token_hex(16)


async def park_claude_tool_permission(
    *,
    request_approval: Optional[RequestApproval],
    tool_name: Any,
    tool_input: Any,
    context: Any = None,
) -> Any:
    """Park one ``can_use_tool`` invocation until an approve/deny decision.

    ``request_approval`` is the worker serve-loop helper (frames) or the
    in-process registry wrapper (no frames). Missing helper is a deny.
    """

    del context
    if request_approval is None:
        return permission_deny(message="tool approval is not bound")
    approval_id = mint_approval_id()
    summary, truncated = build_approval_summary(tool_input=tool_input)
    envelope = await request_approval(
        approval_id,
        tool_name=sanitize_tool_name(tool_name),
        summary=summary,
        summary_truncated=truncated,
    )
    return permission_result_from_decision(envelope)


async def park_in_process_tool_permission(
    *,
    callback: Optional[Callable[..., Any]],
    agent_id: str,
    turn_id: str,
    tool_name: Any,
    tool_input: Any,
    context: Any = None,
) -> Any:
    """Park through the session approval registry. No worker frames.

    Registers, then awaits the bound ``send_decision``. Does not take a new
    lock across the human wait; the conversation ``run()`` lock is already
    held (deny-pending-before-close releases it).
    """

    if callback is None:
        return permission_deny(message="tool approval is not bound")

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
            "tool_name": fields.get("tool_name") or sanitize_tool_name(tool_name),
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

    return await park_claude_tool_permission(
        request_approval=request_approval,
        tool_name=tool_name,
        tool_input=tool_input,
        context=context,
    )
