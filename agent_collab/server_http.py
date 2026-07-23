from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .api_schema import (
    API_VERSION,
    API_VERSION_HEADER,
    HealthModel,
    OptionsRequestModel,
    PostMessageRequestModel,
    PruneSessionsRequestModel,
    ROUTES,
    ReadEventsRequestModel,
    Route,
    TranscriptRequestModel,
    WaitEventsRequestModel,
    WaitResultRequestModel,
)
from .config import CollaborationConfig, SessionsConfig
from .daemon import (
    SessionManager,
    SessionNotFoundError,
    SessionRequestError,
    StartSessionRequest,
)
from .paths import GlobalDataPaths
from .net import is_loopback_host
from .mcp_tools import (
    SUPPORTED_PROTOCOL_VERSIONS,
    SessionManagerToolBackend,
    handle_request as handle_mcp_request,
    jsonrpc_error,
)
from .options import StartOptionsError


MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_REQUEST_HEADER_BYTES = 64 * 1024
MAX_REQUEST_HEADERS = 100
INTERNAL_SERVER_ERROR_MESSAGE = "internal server error"
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
        sessions_config: Optional[SessionsConfig] = None,
        daemon_config: Optional[CollaborationConfig] = None,
        data_paths: Optional[GlobalDataPaths] = None,
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
        # Daemon-global retention policy; run_server passes the user-config
        # value, direct construction (tests) gets the built-in defaults.
        self.daemon_config = daemon_config
        self.sessions_config = sessions_config or (
            daemon_config.sessions if daemon_config is not None else SessionsConfig()
        )
        self.data_paths = data_paths or GlobalDataPaths.resolve()
        # Seconds between scheduled retention runs; tests shrink this.
        self._retention_interval_seconds = float(self.sessions_config.cleanup_interval_hours * 3600)

    async def serve(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        server = await asyncio.start_server(self._handle_connection, host, port)
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        print(f"agent-collab daemon listening on {addresses}", flush=True)
        retention_task = self.start_retention_task()
        usage_window_task = self.start_usage_window_task()
        # Started only after the listener is up: catalog refresh must never
        # delay daemon readiness, and running sessions are untouched (options
        # are snapshotted into session settings at start).
        model_catalog_task = self.start_model_catalog_task()
        try:
            async with server:
                await server.serve_forever()
        finally:
            # The daemon has no graceful shutdown beyond this unwinding
            # (SIGTERM maps to KeyboardInterrupt); pruning is convergent, so
            # cancelling mid-run is always safe.
            if retention_task is not None:
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task
            if usage_window_task is not None:
                usage_window_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await usage_window_task
            if model_catalog_task is not None:
                model_catalog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_catalog_task

    def start_retention_task(self) -> Optional[asyncio.Task]:
        """Start the periodic retention task; None when retention is disabled."""

        if self.sessions_config.retention_days <= 0:
            return None
        return asyncio.get_running_loop().create_task(
            self._retention_loop(), name="agent-collab-retention"
        )

    def start_usage_window_task(self) -> Optional[asyncio.Task]:
        """Start daemon-global usage-window alignment when a target is enabled."""

        if self.daemon_config is None:
            return None
        from .usage_windows import UsageWindowScheduler, enabled_usage_window_targets

        if not enabled_usage_window_targets(self.daemon_config):
            return None
        scheduler = UsageWindowScheduler(
            config=self.daemon_config,
            manager=self.manager,
            paths=self.data_paths,
            logger=self._log,
        )
        return asyncio.get_running_loop().create_task(
            scheduler.run(), name="agent-collab-usage-windows"
        )

    def start_model_catalog_task(self) -> Optional[asyncio.Task]:
        """Start the background model-catalog refresher; None outside a full
        daemon (direct construction in tests passes no daemon config)."""

        if self.daemon_config is None:
            return None
        from .model_catalog import ModelCatalogRefresher

        refresher = ModelCatalogRefresher(logger=self._log)
        self.manager.model_catalog_kick = refresher.kick
        return asyncio.get_running_loop().create_task(
            refresher.run(), name="agent-collab-model-catalogs"
        )

    async def _retention_loop(self) -> None:
        """Apply configured retention once after startup, then every interval.

        Index restoration already happened in the manager constructor, so the
        first run sees every restored record. A failing run is logged and the
        loop continues; scheduled and manual pruning serialize through the
        manager's prune lock, so runs never overlap. The loop only ends by
        cancellation.
        """

        from datetime import timedelta

        retention = timedelta(days=self.sessions_config.retention_days)
        while True:
            try:
                await self.manager.prune_sessions(apply=True, retention=retention)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The manager reports per-session failures in its result; this
                # catches unexpected errors so the daemon stays healthy.
                self._log(f"scheduled retention run failed: {exc!r}")
            await asyncio.sleep(self._retention_interval_seconds)

    def _log(self, message: str) -> None:
        print(f"agent-collab daemon {message}", flush=True)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
        except SessionNotFoundError as exc:
            self._log_request(f"request error 404 {exc}")
            await self._write_json(writer, 404, {"error": str(exc)})
        except StartOptionsError as exc:
            self._log_request(f"request error 400 {exc.code}")
            await self._write_json(writer, 400, exc.to_dict())
        except SessionRequestError as exc:
            self._log_request(f"request error 400 {exc}")
            await self._write_json(writer, 400, {"error": str(exc)})
        except Exception as exc:
            # Unexpected failures are always operationally visible, even when
            # routine request logging is disabled.  Keep their type and detail
            # in the daemon log, but never put exception text on the wire.
            self._log_unexpected_error(exc)
            await self._write_json(
                writer,
                500,
                {"error": INTERNAL_SERVER_ERROR_MESSAGE},
            )
        finally:
            writer.close()
            await writer.wait_closed()

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> Tuple[str, str, Dict[str, str], bytes]:
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
            if (
                normalized_key in {"content-length", "transfer-encoding"}
                and normalized_key in headers
            ):
                label = (
                    "Content-Length" if normalized_key == "content-length" else "Transfer-Encoding"
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
                len(normalized_length) == len(maximum) and normalized_length > maximum
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

    async def _dispatch(
        self, method: str, target: str, headers: Dict[str, str], body: bytes
    ) -> Any:
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
        query = {
            key: values[0] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        return await handler(route, path_params, query, body)

    def _authorize(self, method: str, path: str, headers: Dict[str, str]) -> None:
        # Intentional asymmetry: GET /health alone bypasses auth so liveness
        # checks work without the token, while the supervisor's readiness
        # probe deliberately uses the authenticated /sessions endpoint to
        # prove token auth end-to-end. Do not "simplify" the probe to /health
        # — that would stop verifying the token path at startup.
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
            model_refresh=options_request.model_refresh,
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

    async def _route_wait_result(
        self, _route: Route, path: Dict[str, str], query: Dict[str, str], _body: bytes
    ) -> Any:
        request = _parse(WaitResultRequestModel.from_dict, query)
        return (await self.manager.wait_result(path["session_id"], request.timeout_ms)).to_dict()

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

    async def _route_prune_sessions(
        self, _route: Route, _path: Dict[str, str], _query: Dict[str, str], body: bytes
    ) -> Any:
        from datetime import timedelta

        from .retention import parse_duration

        request = _parse(PruneSessionsRequestModel.from_dict, _decode_json_object(body))
        if request.older_than is not None:
            retention = parse_duration(request.older_than)
        elif self.sessions_config.retention_days > 0:
            retention = timedelta(days=self.sessions_config.retention_days)
        else:
            raise HttpError(
                400,
                "automatic retention is disabled (sessions.retention_days = 0); "
                "pass older_than to prune manually",
            )
        result = await self.manager.prune_sessions(
            apply=request.apply, retention=retention, keep=request.keep
        )
        return result.to_dict()

    async def _dispatch_mcp(self, method: str, headers: Dict[str, str], body: bytes) -> Any:
        _validate_mcp_origin(headers.get("origin"))
        _validate_mcp_protocol_version(headers.get("mcp-protocol-version"))

        if method == "GET":
            raise HttpError(405, "MCP SSE streams are not implemented")
        if method != "POST":
            raise HttpError(405, "MCP endpoint only supports POST")

        request = _decode_json_object(body)
        self._log_mcp(request)
        try:
            response = await handle_mcp_request(
                request,
                SessionManagerToolBackend(self.manager),
            )
        except Exception as exc:
            self._log_unexpected_error(exc)
            return HttpResponse(
                500,
                jsonrpc_error(
                    request.get("id"),
                    -32603,
                    INTERNAL_SERVER_ERROR_MESSAGE,
                ),
            )
        return HttpResponse(202) if response is None else response

    def _log_request(self, message: str) -> None:
        if self.log_requests:
            self._log(message)

    def _log_unexpected_error(self, exc: Exception) -> None:
        self._log(f"request error 500 {exc!r}")

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
    return (
        bool(name)
        and name.isascii()
        and all(char.isalnum() or char in _HEADER_NAME_PUNCTUATION for char in name)
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
    if parsed.scheme in {"http", "https"} and is_loopback_host(parsed.hostname):
        return
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


def _load_sessions_config(home: Path) -> SessionsConfig:
    """Compatibility helper over the unified daemon-policy load."""

    return _load_daemon_policy(home).sessions


def _load_daemon_policy(home: Path) -> CollaborationConfig:
    """Load all daemon-global policy once, disabling side effects on error."""

    from .config import ConfigError, load_user_config
    from .paths import AgentCollabHome

    resolved = AgentCollabHome(root=home, config_path=home / "config.toml")
    try:
        return load_user_config(home=resolved)
    except (ConfigError, OSError) as exc:
        print(
            "agent-collab daemon disabling automatic session retention and usage-window "
            f"alignment; global config could not be loaded ({exc.__class__.__name__}); "
            "run 'agent-collab config show' for details",
            flush=True,
        )
        return CollaborationConfig(sessions=SessionsConfig(retention_days=0))


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    default_workdir: Path = Path("."),
    session_log_dir: Optional[Path] = None,
) -> None:
    from .config import ensure_daemon_token

    paths = GlobalDataPaths.resolve()
    paths.ensure_dirs()
    # The permanent token lives in the user config; the legacy per-lifetime
    # token file is no longer written, so drop a stale copy.
    try:
        paths.token_path.unlink()
    except FileNotFoundError:
        pass
    token = ensure_daemon_token()
    daemon_policy = _load_daemon_policy(paths.home)
    asyncio.run(
        AgentCollabHttpServer(
            default_workdir=default_workdir,
            session_log_dir=session_log_dir,
            auth_token=token,
            daemon_config=daemon_policy,
            data_paths=paths,
        ).serve(host, port)
    )
