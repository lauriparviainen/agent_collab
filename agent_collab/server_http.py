from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .daemon import SessionManager, StartSessionRequest


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AgentCollabHttpServer:
    def __init__(self, manager: Optional[SessionManager] = None):
        self.manager = manager or SessionManager(lifecycle_logger=self._log)

    async def serve(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        server = await asyncio.start_server(self._handle_connection, host, port)
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        print(f"agent-collab daemon listening on {addresses}", flush=True)
        async with server:
            await server.serve_forever()

    def _log(self, message: str) -> None:
        print(f"agent-collab daemon {message}", flush=True)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
            result = await self._dispatch(*request)
            await self._write_json(writer, 200, result)
        except HttpError as exc:
            await self._write_json(writer, exc.status, {"error": exc.message})
        except KeyError as exc:
            await self._write_json(writer, 404, {"error": str(exc)})
        except ValueError as exc:
            await self._write_json(writer, 400, {"error": str(exc)})
        except Exception as exc:
            await self._write_json(writer, 500, {"error": str(exc)})
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_request(self, reader: asyncio.StreamReader) -> Tuple[str, str, Dict[str, str], bytes]:
        request_line = await reader.readline()
        if not request_line:
            raise HttpError(400, "empty request")
        parts = request_line.decode("iso-8859-1").strip().split()
        if len(parts) != 3:
            raise HttpError(400, "invalid request line")
        method, target, _version = parts

        headers: Dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            key, sep, value = line.decode("iso-8859-1").partition(":")
            if not sep:
                raise HttpError(400, "invalid header")
            headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        body = await reader.readexactly(content_length) if content_length else b""
        return method.upper(), target, headers, body

    async def _dispatch(self, method: str, target: str, headers: Dict[str, str], body: bytes) -> Any:
        parsed = urlparse(target)
        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)

        if method == "GET" and path_parts == ["health"]:
            return {"status": "ok", "sessions": len(self.manager.list_sessions())}

        if method == "POST" and path_parts == ["sessions"]:
            data = _decode_json_object(body)
            state = await self.manager.start_session(
                StartSessionRequest(
                    task=_required_str(data, "task"),
                    mode=str(data.get("mode", "claude-leads")),
                    workdir=Path(str(data.get("workdir", "."))),
                    max_turns=int(data.get("max_turns", 3)),
                    timeout=int(data.get("timeout", 900)),
                    mock=bool(data.get("mock", False)),
                    dry_run=bool(data.get("dry_run", False)),
                )
            )
            return state.to_dict()

        if method == "GET" and path_parts == ["sessions"]:
            return {"sessions": [state.to_dict() for state in self.manager.list_sessions()]}

        if len(path_parts) >= 2 and path_parts[0] == "sessions":
            session_id = path_parts[1]
            if method == "GET" and len(path_parts) == 2:
                return self.manager.get_session(session_id).to_dict()
            if method == "GET" and len(path_parts) == 3 and path_parts[2] == "events":
                cursor = _query_int(query, "cursor", 0)
                return self.manager.read_events(session_id, cursor).to_dict()
            if method == "GET" and len(path_parts) == 4 and path_parts[2:] == ["events", "wait"]:
                cursor = _query_int(query, "cursor", 0)
                timeout_ms = _query_int(query, "timeout_ms", 30000)
                return (await self.manager.wait_events(session_id, cursor, timeout_ms)).to_dict()
            if method == "GET" and len(path_parts) == 3 and path_parts[2] == "transcript":
                state = self.manager.get_session(session_id)
                path = Path(state.markdown_path)
                return {"transcript": path.read_text(encoding="utf-8") if path.exists() else ""}
            if method == "POST" and len(path_parts) == 3 and path_parts[2] == "stop":
                state = await self.manager.stop_session(session_id)
                return state.to_dict()

        raise HttpError(404, f"not found: {method} {parsed.path}")

    async def _write_json(self, writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "Error")
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()


def _decode_json_object(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HttpError(400, f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HttpError(400, "request body must be a JSON object")
    return data


def _required_str(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise HttpError(400, f"{key} is required")
    return value


def _query_int(query: Dict[str, Any], key: str, default: int) -> int:
    values = query.get(key)
    if not values:
        return default
    return int(values[0])


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    asyncio.run(AgentCollabHttpServer().serve(host, port))
