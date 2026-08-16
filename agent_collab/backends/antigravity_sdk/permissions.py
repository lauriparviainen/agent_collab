"""Antigravity SDK ``ask_user`` park helper shared by worker and in-process paths.

Installed ``google-antigravity==0.1.8`` ``policy.ask_user(tool, handler=...)``
takes ``Callable[[ToolCall], bool | Awaitable[bool]]``. ``_execute_ask_user``
awaits it with **no timeout**. The decide hook runs as an asyncio background
task on the conversation/serve loop (``LocalHarnessEventProcessor``
``_run_in_background``), so the handler may be async and await the park
directly — do not use ``run_coroutine_threadsafe``.

Host approve maps to ``True``; anything else is ``False`` (fail-closed).
Full tool input stays off the worker frame; the summary is built at the
source. Do not write the worker socket from the handler.
"""

from __future__ import annotations

import asyncio
import enum
import secrets
from typing import Any, Awaitable, Callable, Mapping, Optional

from ...approvals import build_approval_summary, sanitize_tool_name


RequestApproval = Callable[..., Awaitable[Mapping[str, Any]]]


def approval_result_from_decision(envelope: Any) -> bool:
    """Map an ``approval_decision`` envelope to the SDK ask_user bool.

    Anything other than an explicit ``approve`` is a deny (fail-closed).
    """

    decision = envelope.get("decision") if isinstance(envelope, Mapping) else None
    return decision == "approve"


def mint_approval_id() -> str:
    return secrets.token_hex(16)


def tool_name_from_call(tool_call: Any) -> str:
    """Stable public tool name. Never raw command strings or secrets."""

    name = getattr(tool_call, "name", None)
    if isinstance(name, enum.Enum):
        raw = getattr(name, "value", None)
        name = raw if raw is not None else name.name
    return sanitize_tool_name(name or "tool")


def tool_input_from_call(tool_call: Any) -> Mapping[str, Any]:
    args = getattr(tool_call, "args", None)
    return args if isinstance(args, Mapping) else {}


async def park_antigravity_tool_approval(
    *,
    request_approval: Optional[RequestApproval],
    tool_call: Any,
) -> bool:
    """Park one ``ask_user`` invocation until an approve/deny decision.

    ``request_approval`` is the worker serve-loop helper (frames) or the
    in-process registry wrapper (no frames). Missing helper is a deny.
    """

    if request_approval is None:
        return False
    approval_id = mint_approval_id()
    summary, truncated = build_approval_summary(tool_input=tool_input_from_call(tool_call))
    envelope = await request_approval(
        approval_id,
        tool_name=tool_name_from_call(tool_call),
        summary=summary,
        summary_truncated=truncated,
    )
    return approval_result_from_decision(envelope)


async def park_in_process_antigravity_approval(
    *,
    callback: Optional[Callable[..., Any]],
    agent_id: str,
    turn_id: str,
    tool_call: Any,
) -> bool:
    """Park through the session approval registry. No worker frames.

    Registers, then awaits the bound ``send_decision``. Does not take a new
    lock across the human wait; the conversation ``run()`` lock is already
    held (deny-pending-before-close releases it).
    """

    if callback is None:
        return False

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
            "tool_name": fields.get("tool_name") or tool_name_from_call(tool_call),
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

    return await park_antigravity_tool_approval(
        request_approval=request_approval,
        tool_call=tool_call,
    )
