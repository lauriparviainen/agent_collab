from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .api_schema import (
    API_VERSION,
    API_VERSION_HEADER,
    HealthModel,
    OptionsRequestModel,
    PostMessageRequestModel,
    ROUTES,
    ReadEventsRequestModel,
    Route,
    TranscriptRequestModel,
    WaitEventsRequestModel,
)
from .daemon import SessionManager, StartSessionRequest
from .paths import GlobalDataPaths, atomic_write_private_text
from .mcp_tools import SUPPORTED_PROTOCOL_VERSIONS, SessionManagerToolBackend, handle_request as handle_mcp_request
from .options import StartOptionsError


MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_REQUEST_HEADER_BYTES = 64 * 1024
MAX_REQUEST_HEADERS = 100
_HEADER_NAME_PUNCTUATION = frozenset("!#$%&'*+-.^_`|~")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class HttpResponse:
    status: int
    payload: Optional[Any] = None


class AgentCollabHttpServer:
    def __init__(
        self,
        manager: Optional[SessionManager] = None,
        log_requests: Optional[bool] = None,
        default_workdir: Path = Path("."),
        session_log_dir: Optional[Path] = None,
        session_index_path: Optional[Path] = None,
        auth_token: Optional[str] = None,
    ):
        owns_manager = manager is None
        self.manager = manager or SessionManager(
            lifecycle_logger=self._log,
            default_workdir=default_workdir,
            default_log_dir=session_log_dir,
            index_path=session_index_path or GlobalDataPaths.resolve().session_index_path,
        )
        self.log_requests = owns_manager if log_requests is None else bool(log_requests)
        self.auth_token = auth_token

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
            method, target, headers, body = await self._read_request(reader)
            path = urlparse(target).path
            self._log_request(f"request {method} {path}")
            result = await self._dispatch(method, target, headers, body)
            if isinstance(result, HttpResponse):
                if result.payload is None:
                    await self._write_empty(writer, result.status)
                else:
                    await self._write_json(writer, result.status, result.payload)
            else:
                await self._write_json(writer, 200, result)
        except HttpError as exc:
            self._log_request(f"request error {exc.status} {exc.message}")
            await self._write_json(writer, exc.status, {"error": exc.message})
        except KeyError as exc:
            self._log_request(f"request error 404 {exc}")
            await self._write_json(writer, 404, {"error": str(exc)})
        except StartOptionsError as exc:
            self._log_request(f"request error 400 {exc.code}")
            await self._write_json(writer, 400, exc.to_dict())
        except ValueError as exc:
            self._log_request(f"request error 400 {exc}")
            await self._write_json(writer, 400, {"error": str(exc)})
        except Exception as exc:
            self._log_request(f"request error 500 {exc}")
            await self._write_json(writer, 500, {"error": str(exc)})
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_request(self, reader: asyncio.StreamReader) -> Tuple[str, str, Dict[str, str], bytes]:
        try:
            request_line = await reader.readline()
        except ValueError as exc:
            raise HttpError(400, "request line too long") from exc
        if not request_line:
            raise HttpError(400, "empty request")
        parts = request_line.decode("iso-8859-1").strip().split()
        if len(parts) != 3:
            raise HttpError(400, "invalid request line")
        method, target, _version = parts

        headers: Dict[str, str] = {}
        header_bytes = 0
        header_count = 0
        while True:
            try:
                line = await reader.readline()
            except ValueError as exc:
                raise HttpError(431, "request headers too large") from exc
            if line in {b"\r\n", b"\n", b""}:
                break
            header_bytes += len(line)
            header_count += 1
            if header_bytes > MAX_REQUEST_HEADER_BYTES or header_count > MAX_REQUEST_HEADERS:
                raise HttpError(431, "request headers too large")
            key, sep, value = line.decode("iso-8859-1").partition(":")
            if not sep:
                raise HttpError(400, "invalid header")
            if not _is_valid_header_name(key):
                raise HttpError(400, "invalid header name")
            normalized_key = key.lower()
            if normalized_key in {"content-length", "transfer-encoding"} and normalized_key in headers:
                label = (
                    "Content-Length"
                    if normalized_key == "content-length"
                    else "Transfer-Encoding"
                )
                raise HttpError(400, f"duplicate {label} header")
            headers[normalized_key] = value.strip()

        if "transfer-encoding" in headers:
            raise HttpError(400, "Transfer-Encoding is not supported")

        raw_content_length = headers.get("content-length")
        if raw_content_length is None:
            content_length = 0
        elif not raw_content_length.isascii() or not raw_content_length.isdigit():
            raise HttpError(400, "invalid Content-Length header")
        else:
            normalized_length = raw_content_length.lstrip("0") or "0"
            maximum = str(MAX_REQUEST_BODY_BYTES)
            if len(normalized_length) > len(maximum) or (
                len(normalized_length) == len(maximum)
                and normalized_length > maximum
            ):
                raise HttpError(
                    413,
                    f"request body exceeds {MAX_REQUEST_BODY_BYTES}-byte limit",
                )
            content_length = int(normalized_length)
        try:
            body = await reader.readexactly(content_length) if content_length else b""
        except asyncio.IncompleteReadError as exc:
            raise HttpError(400, "incomplete request body") from exc
        return method.upper(), target, headers, body

    async def _dispatch(self, method: str, target: str, headers: Dict[str, str], body: bytes) -> Any:
        parsed = urlparse(target)
        normalized_path = parsed.path.rstrip("/") or "/"
        self._authorize(method, normalized_path, headers)
        if normalized_path == "/mcp":
            return await self._dispatch_mcp(method, headers, body)

        matched = _match_route(method, normalized_path)
        if matched is None:
            raise HttpError(404, f"not found: {method} {parsed.path}")
        route, path_params = matched
        handler = getattr(self, f"_route_{route.handler}", None)
        if not callable(handler):
            raise RuntimeError(f"route handler is not implemented: {route.handler}")
        query = {key: values[0] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        return await handler(route, path_params, query, body)

    def _authorize(self, method: str, path: str, headers: Dict[str, str]) -> None:
        if self.auth_token is None or (method == "GET" and path == "/health"):
            return
        authorization = headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not token
            or not hmac.compare_digest(token, self.auth_token)
        ):
            raise HttpError(401, "unauthorized")

    async def _route_health(
        self, _route: Route, _path: Dict[str, str], _query: Dict[str, str], _body: bytes
    ) -> Any:
        return HealthModel(
            status="ok",
            sessions=len(self.manager.list_sessions()),
            api_version=API_VERSION,
        ).to_dict()

    async def _route_options(
        self, route: Route, _path: Dict[str, str], query: Dict[str, str], body: bytes
    ) -> Any:
        data = _decode_json_object(body) if route.method == "POST" else query
        options_request = _parse(OptionsRequestModel.from_dict, data)
        return await self.manager.describe_options_async(
            Path(options_request.workdir),
            health_refresh=options_request.health_refresh,
        )

    async def _route_start_session(
        self, _route: Route, _path: Dict[str, str], _query: Dict[str, str], body: bytes
    ) -> Any:
        request = _parse(StartSessionRequest.from_wire, _decode_json_object(body))
        return (await self.manager.start_session(request)).to_dict()

    async def _route_list_sessions(
        self, _route: Route, _path: Dict[str, str], _query: Dict[str, str], _body: bytes
    ) -> Any:
        return {"sessions": [state.to_dict() for state in self.manager.list_sessions()]}

    async def _route_get_session(
        self, _route: Route, path: Dict[str, str], _query: Dict[str, str], _body: bytes
    ) -> Any:
        return self.manager.get_session(path["session_id"]).to_dict()

    async def _route_read_events(
        self, _route: Route, path: Dict[str, str], query: Dict[str, str], _body: bytes
    ) -> Any:
        request = _parse(ReadEventsRequestModel.from_dict, query)
        return (
            await self.manager.read_events_async(
                path["session_id"],
                request.cursor,
                limit=request.limit,
                tool_output=request.tool_output,
            )
        ).to_dict()

    async def _route_wait_events(
        self, _route: Route, path: Dict[str, str], query: Dict[str, str], _body: bytes
    ) -> Any:
        request = _parse(WaitEventsRequestModel.from_dict, query)
        return (
            await self.manager.wait_events(
                path["session_id"],
                request.cursor,
                request.timeout_ms,
                tool_output=request.tool_output,
            )
        ).to_dict()

    async def _route_post_message(
        self, _route: Route, path: Dict[str, str], _query: Dict[str, str], body: bytes
    ) -> Any:
        message = _parse(PostMessageRequestModel.from_dict, _decode_json_object(body))
        return (
            await self.manager.post_message(
                path["session_id"],
                message.text,
                source=message.source,
                target=message.target,
            )
        ).to_dict()

    async def _route_read_transcript(
        self, _route: Route, path: Dict[str, str], query: Dict[str, str], _body: bytes
    ) -> Any:
        request = _parse(TranscriptRequestModel.from_dict, query)
        return {
            "transcript": await self.manager.read_transcript_async(
                path["session_id"], tool_output=request.tool_output
            )
        }

    async def _route_stop_session(
        self, _route: Route, path: Dict[str, str], _query: Dict[str, str], _body: bytes
    ) -> Any:
        return (await self.manager.stop_session(path["session_id"])).to_dict()

    async def _dispatch_mcp(self, method: str, headers: Dict[str, str], body: bytes) -> Any:
        _validate_mcp_origin(headers.get("origin"))
        _validate_mcp_protocol_version(headers.get("mcp-protocol-version"))

        if method == "GET":
            raise HttpError(405, "MCP SSE streams are not implemented")
        if method != "POST":
            raise HttpError(405, "MCP endpoint only supports POST")

        request = _decode_json_object(body)
        self._log_mcp(request)
        response = await handle_mcp_request(request, SessionManagerToolBackend(self.manager))
        return HttpResponse(202) if response is None else response

    def _log_request(self, message: str) -> None:
        if self.log_requests:
            self._log(message)

    def _log_mcp(self, request: Dict[str, Any]) -> None:
        if not self.log_requests:
            return
        method = request.get("method")
        if method == "tools/call":
            params = request.get("params") or {}
            tool_name = params.get("name") if isinstance(params, dict) else None
            self._log(f"MCP tools/call {tool_name}")
        elif method in {"initialize", "tools/list"}:
            self._log(f"MCP {method}")

    async def _write_json(self, writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = _http_reason(status)
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"{API_VERSION_HEADER}: {API_VERSION}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()

    async def _write_empty(self, writer: asyncio.StreamWriter, status: int) -> None:
        writer.write(
            (
                f"HTTP/1.1 {status} {_http_reason(status)}\r\n"
                f"{API_VERSION_HEADER}: {API_VERSION}\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
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


def _is_valid_header_name(name: str) -> bool:
    return bool(name) and name.isascii() and all(
        char.isalnum() or char in _HEADER_NAME_PUNCTUATION for char in name
    )


def _parse(parser: Any, data: Dict[str, Any]) -> Any:
    """Run a DTO `from_dict`/`from_wire` parser, mapping its validation
    `ValueError` to a 400 `HttpError` so request-shape errors keep the REST
    error contract. `StartOptionsError` is raised later (inside
    `start_session`), not by these parsers, so it is unaffected."""
    try:
        return parser(data)
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc


def _match_route(method: str, path: str) -> Optional[Tuple[Route, Dict[str, str]]]:
    actual = [part for part in path.split("/") if part]
    for route in ROUTES:
        if route.method != method:
            continue
        template = [part for part in route.path.split("/") if part]
        if len(template) != len(actual):
            continue
        params: Dict[str, str] = {}
        for expected, value in zip(template, actual):
            if expected.startswith("{") and expected.endswith("}"):
                params[expected[1:-1]] = value
            elif expected != value:
                break
        else:
            return route, params
    return None


def _validate_mcp_origin(origin: Optional[str]) -> None:
    if not origin:
        return
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        raise HttpError(403, "non-local Origin rejected for /mcp")
    host = parsed.hostname
    if host == "localhost":
        return
    if host is not None:
        try:
            if ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise HttpError(403, "non-local Origin rejected for /mcp")


def _validate_mcp_protocol_version(protocol_version: Optional[str]) -> None:
    if not protocol_version:
        return
    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise HttpError(400, f"unsupported MCP-Protocol-Version: {protocol_version}")


def _http_reason(status: int) -> str:
    return {
        200: "OK",
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        431: "Request Header Fields Too Large",
        500: "Internal Server Error",
    }.get(status, "Error")


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    default_workdir: Path = Path("."),
    session_log_dir: Optional[Path] = None,
    token_path: Optional[Path] = None,
) -> None:
    paths = GlobalDataPaths.resolve()
    paths.ensure_dirs()
    resolved_token_path = Path(token_path).expanduser().resolve() if token_path else paths.token_path
    resolved_token_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_token_path.parent.chmod(0o700)
    token = mint_auth_token(resolved_token_path)
    asyncio.run(
        AgentCollabHttpServer(
            default_workdir=default_workdir,
            session_log_dir=session_log_dir,
            auth_token=token,
        ).serve(host, port)
    )


def mint_auth_token(token_path: Path) -> str:
    token = secrets.token_urlsafe(32)
    atomic_write_private_text(token_path, token + "\n")
    return token
