from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol

from .config import DEFAULT_WORKFLOW
from .daemon import SessionManager, StartSessionRequest
from .options import StartOptionsError


PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18", PROTOCOL_VERSION}

TOOLS = [
    {
        "name": "agent_collab_start",
        "description": (
            "Start a supervised Claude/Codex collaboration session and return a session id. "
            "Call agent_collab_describe_options first before passing non-default codex_options or claude_options."
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
                "codex_options": {"type": "object", "additionalProperties": True},
                "claude_options": {"type": "object", "additionalProperties": True},
            },
            "required": ["task"],
        },
    },
    {
        "name": "agent_collab_describe_options",
        "description": "Describe workflows, configured agents, and accepted codex_options and claude_options for starts.",
        "inputSchema": {
            "type": "object",
            "properties": {"workdir": {"type": "string"}},
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
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
    {
        "name": "agent_collab_read_events",
        "description": "Read daemon session events after a numeric cursor offset.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "cursor": {"type": "integer"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_wait_events",
        "description": "Long-poll daemon session events after a numeric cursor offset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cursor": {"type": "integer"},
                "timeout_ms": {"type": "integer"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "agent_collab_read_transcript",
        "description": "Read the Markdown transcript for a daemon-owned session.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
    {
        "name": "agent_collab_stop",
        "description": "Request cancellation of a running daemon-owned session.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
]
TOOL_NAMES = {str(tool["name"]) for tool in TOOLS}


class McpProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolBackend(Protocol):
    async def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...

    async def describe_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...

    async def list_sessions(self) -> Dict[str, Any]:
        ...

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        ...

    async def read_events(self, session_id: str, cursor: int) -> Dict[str, Any]:
        ...

    async def wait_events(self, session_id: str, cursor: int, timeout_ms: int) -> Dict[str, Any]:
        ...

    async def read_transcript(self, session_id: str) -> str:
        ...

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        ...


class SessionManagerToolBackend:
    def __init__(self, manager: SessionManager):
        self.manager = manager

    async def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = await self.manager.start_session(
            StartSessionRequest(
                task=_required_str(payload, "task"),
                workflow=str(payload.get("workflow", DEFAULT_WORKFLOW)),
                workdir=Path(str(payload.get("workdir", "."))),
                max_turns=_int_arg(payload, "max_turns", 3),
                timeout=_int_arg(payload, "timeout", 900),
                mock=bool(payload.get("mock", False)),
                dry_run=bool(payload.get("dry_run", False)),
                codex_options=_optional_payload(payload, "codex_options"),
                claude_options=_optional_payload(payload, "claude_options"),
            )
        )
        return state.to_dict()

    async def describe_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workdir = payload.get("workdir")
        return self.manager.describe_options(Path(str(workdir)) if workdir else None)

    async def list_sessions(self) -> Dict[str, Any]:
        return {"sessions": [state.to_dict() for state in self.manager.list_sessions()]}

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        return self.manager.get_session(session_id).to_dict()

    async def read_events(self, session_id: str, cursor: int) -> Dict[str, Any]:
        return self.manager.read_events(session_id, cursor).to_dict()

    async def wait_events(self, session_id: str, cursor: int, timeout_ms: int) -> Dict[str, Any]:
        return (await self.manager.wait_events(session_id, cursor, timeout_ms)).to_dict()

    async def read_transcript(self, session_id: str) -> str:
        state = self.manager.get_session(session_id)
        path = Path(state.markdown_path)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        return (await self.manager.stop_session(session_id)).to_dict()


class HttpClientToolBackend:
    def __init__(self, client_factory: Callable[[], Any]):
        self.client_factory = client_factory

    async def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client_factory().start_session(payload)

    async def describe_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client_factory().describe_options(payload)

    async def list_sessions(self) -> Dict[str, Any]:
        return self.client_factory().list_sessions()

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        return self.client_factory().get_session(session_id)

    async def read_events(self, session_id: str, cursor: int) -> Dict[str, Any]:
        return self.client_factory().read_events(session_id, cursor)

    async def wait_events(self, session_id: str, cursor: int, timeout_ms: int) -> Dict[str, Any]:
        return self.client_factory().wait_events(session_id, cursor, timeout_ms)

    async def read_transcript(self, session_id: str) -> str:
        return self.client_factory().read_transcript(session_id)

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        return self.client_factory().stop_session(session_id)


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


async def handle_tool(name: str, args: Dict[str, Any], backend: ToolBackend) -> Dict[str, Any]:
    if not isinstance(name, str) or name not in TOOL_NAMES:
        raise McpProtocolError(-32602, f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise McpProtocolError(-32602, "arguments must be an object")

    try:
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
            return content(await backend.read_events(session_id, _int_arg(args, "cursor", 0)))
        if name == "agent_collab_wait_events":
            return content(
                await backend.wait_events(
                    session_id,
                    _int_arg(args, "cursor", 0),
                    _int_arg(args, "timeout_ms", 30000),
                )
            )
        if name == "agent_collab_read_transcript":
            return text_content(await backend.read_transcript(session_id))
        if name == "agent_collab_stop":
            return content(await backend.stop_session(session_id))
    except StartOptionsError as exc:
        return content(exc.to_dict(), is_error=True)
    except Exception as exc:
        error_payload = getattr(exc, "payload", None)
        if isinstance(error_payload, dict):
            return content(error_payload, is_error=True)
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
    try:
        if method == "initialize":
            return jsonrpc_result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "instructions": (
                        "Call agent_collab_describe_options before starting a session when you need non-default model, reasoning, sandbox, or permission settings. "
                        "Use agent_collab_start with task, workflow, workdir, max_turns, timeout, and typed codex_options or claude_options. "
                        "Use agent_collab_wait_events with a cursor for long-running watches; do not make one blocking call. "
                        "If agent_collab_start returns isError, fix the invalid option and retry instead of guessing. "
                        "The foreground agent-collab server owns sessions and exposes this MCP endpoint at /mcp."
                    ),
                    "serverInfo": {"name": "agent-collab", "version": "0.1.0"},
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
    except Exception as exc:
        return jsonrpc_error(request_id, -32000, str(exc))


def handle_tool_sync(name: str, args: Dict[str, Any], backend: ToolBackend) -> Dict[str, Any]:
    return asyncio.run(handle_tool(name, args, backend))


def handle_request_sync(request: Dict[str, Any], backend: ToolBackend) -> Optional[Dict[str, Any]]:
    return asyncio.run(handle_request(request, backend))


def _required_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _int_arg(args: Dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _start_payload(args: Dict[str, Any]) -> Dict[str, Any]:
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
            "codex_options",
            "claude_options",
        )
        if key in args
    }
    if not isinstance(payload.get("task"), str) or not payload["task"]:
        raise ValueError("task is required")
    return payload


def _describe_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = {key: args[key] for key in ("workdir",) if key in args}
    if "workdir" in payload and not isinstance(payload["workdir"], str):
        raise ValueError("workdir must be a string")
    return payload


def _optional_payload(args: Dict[str, Any], key: str) -> Any:
    return {} if key not in args or args[key] is None else args[key]


def _is_jsonrpc_notification(request: Dict[str, Any]) -> bool:
    return "method" in request and "id" not in request


def _is_jsonrpc_response(request: Dict[str, Any]) -> bool:
    return "method" not in request and ("result" in request or "error" in request)
