from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from .client import AgentCollabClient, ClientError


TOOLS = [
    {
        "name": "agent_collab_start",
        "description": "Start a supervised Claude/Codex collaboration session and return a session id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "mode": {"type": "string"},
                "workdir": {"type": "string"},
                "max_turns": {"type": "integer"},
                "timeout": {"type": "integer"},
                "mock": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["task"],
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


def _jsonrpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _content(payload: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]}


def _text_content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


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
    payload = {key: args[key] for key in ("task", "mode", "workdir", "max_turns", "timeout", "mock", "dry_run") if key in args}
    if not isinstance(payload.get("task"), str) or not payload["task"]:
        raise ValueError("task is required")
    return payload


def handle_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name not in TOOL_NAMES:
        return _content({"error": f"unknown tool {name}"})
    if not isinstance(args, dict):
        return _content({"error": "arguments must be an object"})

    try:
        client = AgentCollabClient()

        if name == "agent_collab_start":
            return _content(client.start_session(_start_payload(args)))
        if name == "agent_collab_list_sessions":
            return _content(client.list_sessions())

        session_id = _required_str(args, "session_id")
        if name == "agent_collab_status":
            return _content(client.get_session(session_id))
        if name == "agent_collab_read_events":
            return _content(client.read_events(session_id, _int_arg(args, "cursor", 0)))
        if name == "agent_collab_wait_events":
            return _content(
                client.wait_events(
                    session_id,
                    _int_arg(args, "cursor", 0),
                    _int_arg(args, "timeout_ms", 30000),
                )
            )
        if name == "agent_collab_read_transcript":
            return _text_content(client.read_transcript(session_id))
        if name == "agent_collab_stop":
            return _content(client.stop_session(session_id))
    except (ClientError, ValueError) as exc:
        return _content({"error": str(exc)})


def handle(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    try:
        if method == "initialize":
            return _jsonrpc_result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "instructions": (
                        "Use agent_collab_start to create daemon-owned collaboration sessions. "
                        "Use agent_collab_wait_events with a cursor for long-running watches; do not make one blocking call. "
                        "The agent-collab daemon must already be running and reachable through AGENT_COLLAB_SERVER or the default localhost URL."
                    ),
                    "serverInfo": {"name": "agent-collab", "version": "0.1.0"},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _jsonrpc_result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return _jsonrpc_result(request_id, handle_tool(params.get("name"), params.get("arguments") or {}))
        return _jsonrpc_error(request_id, -32601, f"method not found: {method}")
    except Exception as exc:
        return _jsonrpc_error(request_id, -32000, str(exc))


def serve() -> None:
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = handle(request)
            except Exception as exc:
                response = _jsonrpc_error(None, -32700, str(exc))
            if response is not None:
                print(json.dumps(response), flush=True)
    except KeyboardInterrupt:
        return


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
