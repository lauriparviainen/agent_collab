from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol

from .api_schema import ReadEventsRequestModel, TranscriptRequestModel, WaitEventsRequestModel
from .client import ClientError
from .daemon import (
    SessionManager,
    SessionNotFoundError,
    SessionRequestError,
    StartSessionRequest,
)
from .options import StartOptionsError


PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18", PROTOCOL_VERSION}

TOOLS = [
    {
        "name": "agent_collab_start",
        "description": (
            "Start a supervised Claude/Codex collaboration session and return a session id. "
            "workdir is required because it selects project config and subprocess cwd. "
            "Call agent_collab_describe_options first before passing non-default backend_options."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "workflow": {"type": "string"},
                "workdir": {"type": "string"},
                "max_turns": {"type": "integer"},
                "timeout": {"type": "integer"},
                "mock": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "interactive": {"type": "boolean"},
                "interactive_idle_timeout": {"type": "number"},
                "backend_options": {"type": "object", "additionalProperties": {"type": "object"}},
                "backend": {"type": "string"},
            },
            "required": ["task", "workdir"],
        },
    },
    {
        "name": "agent_collab_describe_options",
        "description": (
            "Run the versioned pre-start discovery protocol for one absolute workdir, including canonical "
            "backends, effective workflow selections, probe evidence, policy, remediation, and accepted options."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {"type": "string"},
                "health_refresh": {"type": "string", "enum": ["cached", "fresh"]},
            },
            "required": ["workdir"],
        },
    },
    {
        "name": "agent_collab_list_sessions",
        "description": "List daemon-owned agent-collab sessions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agent_collab_status",
        "description": "Return status and log paths for a daemon-owned agent-collab session.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_read_events",
        "description": (
            "Read daemon session events after a numeric cursor offset. Tool payloads default to one-line "
            "summaries carrying absolute event ids; re-fetch one with cursor=<id>, limit=1, tool_output='full'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cursor": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1},
                "tool_output": {"type": "string", "enum": ["summary", "full"]},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_wait_events",
        "description": (
            "Long-poll daemon session events after a numeric cursor offset. Tool payloads default to "
            "one-line summaries; pass tool_output='full' only when the payload is needed. After a "
            "routine nonterminal response, wait at least 20 seconds before another observation call "
            "unless the user requested tighter monitoring or an actionable event needs immediate follow-up."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cursor": {"type": "integer"},
                "timeout_ms": {"type": "integer"},
                "tool_output": {"type": "string", "enum": ["summary", "full"]},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_read_transcript",
        "description": (
            "Read the Markdown transcript for a daemon-owned session. Tool payloads are summarized by "
            "default; pass tool_output='full' for the stored transcript."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "tool_output": {"type": "string", "enum": ["summary", "full"]},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_post_message",
        "description": "Append referee input to an interactive live session, optionally targeting one agent for a directed turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string"},
                "source": {"type": "string", "enum": ["referee", "human"]},
                "target": {"type": "string"},
            },
            "required": ["session_id", "text"],
        },
    },
    {
        "name": "agent_collab_stop",
        "description": "Request cancellation of a running daemon-owned session.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_guidance",
        "description": "Return Markdown guidance for using agent-collab MCP tools safely and effectively.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["overview", "start", "watch", "options", "errors", "workflows"],
                }
            },
        },
    },
]
TOOL_NAMES = {str(tool["name"]) for tool in TOOLS}


class McpProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class McpToolError(ValueError):
    """A caller-visible validation error for one MCP tool invocation."""


class ToolBackend(Protocol):
    async def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    async def describe_options(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    async def list_sessions(self) -> Dict[str, Any]: ...

    async def get_session(self, session_id: str) -> Dict[str, Any]: ...

    async def read_events(
        self, session_id: str, cursor: int, limit: Optional[int], tool_output: str
    ) -> Dict[str, Any]: ...

    async def wait_events(
        self, session_id: str, cursor: int, timeout_ms: int, tool_output: str
    ) -> Dict[str, Any]: ...

    async def read_transcript(self, session_id: str, tool_output: str) -> str: ...

    async def post_message(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    async def stop_session(self, session_id: str) -> Dict[str, Any]: ...


class SessionManagerToolBackend:
    def __init__(self, manager: SessionManager):
        self.manager = manager

    async def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # StartSessionRequest.from_wire is the shared start-payload definition
        # (api_schema.StartSessionRequestModel); the HTTP server uses it too.
        state = await self.manager.start_session(StartSessionRequest.from_wire(payload))
        return state.to_dict()

    async def describe_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.manager.describe_options_async(
            Path(_required_str(payload, "workdir")),
            health_refresh=str(payload.get("health_refresh", "cached")),
        )

    async def list_sessions(self) -> Dict[str, Any]:
        return {"sessions": [state.to_dict() for state in self.manager.list_sessions()]}

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        return self.manager.get_session(session_id).to_dict()

    async def read_events(
        self, session_id: str, cursor: int, limit: Optional[int], tool_output: str
    ) -> Dict[str, Any]:
        return (
            await self.manager.read_events_async(
                session_id, cursor, limit=limit, tool_output=tool_output
            )
        ).to_dict()

    async def wait_events(
        self, session_id: str, cursor: int, timeout_ms: int, tool_output: str
    ) -> Dict[str, Any]:
        return (
            await self.manager.wait_events(session_id, cursor, timeout_ms, tool_output=tool_output)
        ).to_dict()

    async def read_transcript(self, session_id: str, tool_output: str) -> str:
        return await self.manager.read_transcript_async(session_id, tool_output=tool_output)

    async def post_message(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return (
            await self.manager.post_message(
                session_id,
                _required_str(payload, "text"),
                source=str(payload.get("source", "referee"))
                if payload.get("source") is not None
                else "referee",
                target=payload.get("target"),
            )
        ).to_dict()

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        return (await self.manager.stop_session(session_id)).to_dict()


class HttpClientToolBackend:
    """Adapts the typed ``AgentCollabClient`` to the dict-shaped ``ToolBackend``.

    The client returns ``api_schema`` DTOs; the MCP ``content()`` serializer
    wants JSON-ready dicts, so every DTO result is ``.to_dict()``-ed here.
    ``describe_options`` is already a raw dict and passes through.
    """

    def __init__(self, client_factory: Callable[[], Any]):
        self.client_factory = client_factory

    async def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client_factory().start_session(payload).to_dict()

    async def describe_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client_factory().describe_options(payload)

    async def list_sessions(self) -> Dict[str, Any]:
        return self.client_factory().list_sessions().to_dict()

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        return self.client_factory().get_session(session_id).to_dict()

    async def read_events(
        self, session_id: str, cursor: int, limit: Optional[int], tool_output: str
    ) -> Dict[str, Any]:
        return (
            self.client_factory()
            .read_events(session_id, cursor, limit=limit, tool_output=tool_output)
            .to_dict()
        )

    async def wait_events(
        self, session_id: str, cursor: int, timeout_ms: int, tool_output: str
    ) -> Dict[str, Any]:
        return (
            self.client_factory()
            .wait_events(session_id, cursor, timeout_ms, tool_output=tool_output)
            .to_dict()
        )

    async def read_transcript(self, session_id: str, tool_output: str) -> str:
        return self.client_factory().read_transcript(session_id, tool_output=tool_output)

    async def post_message(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return (
            self.client_factory()
            .post_message(
                session_id,
                _required_str(payload, "text"),
                source=str(payload.get("source", "referee"))
                if payload.get("source") is not None
                else "referee",
                target=payload.get("target"),
            )
            .to_dict()
        )

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        return self.client_factory().stop_session(session_id).to_dict()


def jsonrpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def content(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}],
        "isError": is_error,
    }


def text_content(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


GUIDANCE_TOPICS = ("overview", "start", "watch", "options", "errors", "workflows")
# Shipped as package data (see pyproject.toml) so installed daemons can serve
# it; a repo-relative ``doc/`` path would not exist under site-packages.
_GUIDANCE_PATH = Path(__file__).with_name("mcp-guidance.md")
_GUIDANCE_HEADINGS = {
    "start": "## Start",
    "watch": "## Watch",
    "options": "## Options",
    "errors": "## Errors",
    "workflows": "## Workflows",
    "overview": "## Overview",
}


def guidance_text(topic: Optional[str] = None) -> str:
    if topic is not None and topic not in GUIDANCE_TOPICS:
        raise McpToolError(
            f"unknown guidance topic {topic!r}; expected one of: {', '.join(GUIDANCE_TOPICS)}"
        )
    try:
        text = _GUIDANCE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("MCP guidance document is unavailable") from exc
    if topic is None or topic == "overview":
        return text
    return _guidance_section(text, _GUIDANCE_HEADINGS[topic], topic)


def _guidance_section(text: str, heading: str, topic: str) -> str:
    lines = text.splitlines()
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        if start is None:
            if line.strip() == heading:
                start = index
        elif line.startswith("## "):
            end = index
            break
    if start is None:
        raise RuntimeError(f"MCP guidance section is unavailable: {topic}")
    return "\n".join(lines[start:end]).strip() + "\n"


async def handle_tool(name: str, args: Dict[str, Any], backend: ToolBackend) -> Dict[str, Any]:
    if not isinstance(name, str) or name not in TOOL_NAMES:
        raise McpProtocolError(-32602, f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise McpProtocolError(-32602, "arguments must be an object")

    try:
        if name == "agent_collab_guidance":
            topic = args.get("topic")
            if topic is not None and not isinstance(topic, str):
                raise McpToolError("topic must be a string")
            return text_content(guidance_text(topic))
        if name == "agent_collab_start":
            return content(await backend.start_session(_start_payload(args)))
        if name == "agent_collab_describe_options":
            return content(await backend.describe_options(_describe_payload(args)))
        if name == "agent_collab_list_sessions":
            return content(await backend.list_sessions())

        session_id = _required_str(args, "session_id")
        if name == "agent_collab_status":
            return content(await backend.get_session(session_id))
        if name == "agent_collab_read_events":
            request = _parse_tool_request(
                ReadEventsRequestModel.from_dict,
                {key: args[key] for key in ("cursor", "limit", "tool_output") if key in args},
            )
            return content(
                await backend.read_events(
                    session_id,
                    request.cursor,
                    request.limit,
                    request.tool_output,
                )
            )
        if name == "agent_collab_wait_events":
            request = _parse_tool_request(
                WaitEventsRequestModel.from_dict,
                {key: args[key] for key in ("cursor", "timeout_ms", "tool_output") if key in args},
            )
            return content(
                await backend.wait_events(
                    session_id,
                    request.cursor,
                    request.timeout_ms,
                    request.tool_output,
                )
            )
        if name == "agent_collab_read_transcript":
            request = _parse_tool_request(
                TranscriptRequestModel.from_dict,
                {key: args[key] for key in ("tool_output",) if key in args},
            )
            return text_content(await backend.read_transcript(session_id, request.tool_output))
        if name == "agent_collab_post_message":
            return content(await backend.post_message(session_id, _post_message_payload(args)))
        if name == "agent_collab_stop":
            return content(await backend.stop_session(session_id))
    except StartOptionsError as exc:
        return content(exc.to_dict(), is_error=True)
    except (McpToolError, SessionNotFoundError, SessionRequestError) as exc:
        return content({"error": str(exc)}, is_error=True)
    except ClientError as exc:
        if isinstance(exc.payload, dict):
            return content(exc.payload, is_error=True)
        return content({"error": str(exc)}, is_error=True)

    raise McpProtocolError(-32602, f"Unknown tool: {name}")


async def handle_request(request: Dict[str, Any], backend: ToolBackend) -> Optional[Dict[str, Any]]:
    if not isinstance(request, dict):
        return jsonrpc_error(None, -32600, "request must be a JSON object")

    if _is_jsonrpc_response(request) or _is_jsonrpc_notification(request):
        return None

    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {})
    if params is None:
        params = {}
    if method == "initialize":
        return jsonrpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "instructions": (
                    "Call agent_collab_guidance for full usage guidance. "
                    "Resolve the intended project to an absolute workdir and call agent_collab_describe_options "
                    "before every start selection; its cached probe is advisory and start revalidates freshly per backend policy. "
                    "Use agent_collab_start with task, workdir, and workflow. "
                    "Use agent_collab_wait_events with a cursor and wait at least 20 seconds between "
                    "routine nonterminal observation calls; do not rapid-poll or make one unbounded call. "
                    "On validation errors, fix the named field paths instead of guessing."
                ),
                "serverInfo": {"name": "agent-collab", "version": "0.1"},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return jsonrpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        if not isinstance(params, dict):
            return jsonrpc_error(request_id, -32602, "params must be an object")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        try:
            result = await handle_tool(params.get("name"), arguments, backend)
        except McpProtocolError as exc:
            return jsonrpc_error(request_id, exc.code, exc.message)
        return jsonrpc_result(request_id, result)
    return jsonrpc_error(request_id, -32601, f"method not found: {method}")


def handle_tool_sync(name: str, args: Dict[str, Any], backend: ToolBackend) -> Dict[str, Any]:
    return asyncio.run(handle_tool(name, args, backend))


def handle_request_sync(request: Dict[str, Any], backend: ToolBackend) -> Optional[Dict[str, Any]]:
    return asyncio.run(handle_request(request, backend))


def _required_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise McpToolError(f"{key} is required")
    return value


def _start_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    from .api_schema import StartSessionRequestModel

    unknown = sorted(set(args) - set(StartSessionRequestModel.WIRE_FIELDS))
    if unknown:
        raise McpToolError(f"unknown start field {unknown[0]!r}")
    payload = {
        key: args[key]
        for key in (
            "task",
            "workflow",
            "workdir",
            "max_turns",
            "timeout",
            "mock",
            "dry_run",
            "interactive",
            "interactive_idle_timeout",
            "backend_options",
            "backend",
        )
        if key in args
    }
    if not isinstance(payload.get("task"), str) or not payload["task"]:
        raise McpToolError("task is required")
    if not isinstance(payload.get("workdir"), str) or not payload["workdir"].strip():
        raise McpToolError("workdir is required")
    # Match StartSessionRequestModel: an explicit null backend means "no
    # override" (same as omitting it); only a present, non-null, non-string
    # backend is rejected. Keeps the /mcp path consistent with REST/from_wire.
    if payload.get("backend") is not None and not isinstance(payload["backend"], str):
        raise McpToolError("backend must be a string")
    return payload


def _post_message_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = {key: args[key] for key in ("text", "source", "target") if key in args}
    if not isinstance(payload.get("text"), str) or not payload["text"]:
        raise McpToolError("text is required")
    if "source" in payload and (
        not isinstance(payload["source"], str) or payload["source"] not in {"human", "referee"}
    ):
        raise McpToolError("source must be 'human' or 'referee'")
    if "target" in payload and not isinstance(payload["target"], str):
        raise McpToolError("target must be a string")
    return payload


def _describe_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = {key: args[key] for key in ("workdir", "health_refresh") if key in args}
    if not isinstance(payload.get("workdir"), str) or not payload["workdir"].strip():
        raise McpToolError("workdir is required")
    if payload.get("health_refresh", "cached") not in {"cached", "fresh"}:
        raise McpToolError("health_refresh must be 'cached' or 'fresh'")
    return payload


def _parse_tool_request(parser: Callable[[Dict[str, Any]], Any], data: Dict[str, Any]) -> Any:
    try:
        return parser(data)
    except ValueError as exc:
        raise McpToolError(str(exc)) from exc


def _is_jsonrpc_notification(request: Dict[str, Any]) -> bool:
    return "method" in request and "id" not in request


def _is_jsonrpc_response(request: Dict[str, Any]) -> bool:
    return "method" not in request and ("result" in request or "error" in request)
