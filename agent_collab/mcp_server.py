from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from .client import AgentCollabClient
from .mcp_tools import HttpClientToolBackend, handle_request_sync, handle_tool_sync


def _backend() -> HttpClientToolBackend:
    return HttpClientToolBackend(lambda: AgentCollabClient())


def handle_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return handle_tool_sync(name, args, _backend())


def handle(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return handle_request_sync(request, _backend())


def serve() -> None:
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request_id = None
            try:
                request = json.loads(line)
                if isinstance(request, dict):
                    request_id = request.get("id")
                response = handle(request)
            except json.JSONDecodeError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "invalid JSON"},
                }
            except Exception as exc:
                print(f"agent-collab MCP request error {exc!r}", file=sys.stderr, flush=True)
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "internal server error"},
                }
            if response is not None:
                print(json.dumps(response), flush=True)
    except KeyboardInterrupt:
        return


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
