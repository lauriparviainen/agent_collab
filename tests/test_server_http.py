import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.backends.base import BackendHealth, HEALTH_UNAVAILABLE
from agent_collab.daemon import SessionManager, SessionRequestError
from agent_collab.options import StartOptionsError
from agent_collab.session_index import SessionIndex
from agent_collab.server_http import (
    INTERNAL_SERVER_ERROR_MESSAGE,
    MAX_REQUEST_BODY_BYTES,
    MAX_REQUEST_HEADER_BYTES,
    MAX_REQUEST_HEADERS,
    AgentCollabHttpServer,
    HttpError,
    HttpResponse,
)


class _CaptureWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def _request_reader(data: bytes, *, limit: int = 2**16) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class HttpRequestParsingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = AgentCollabHttpServer(manager=SessionManager())

    async def test_request_body_limit_is_16_mib_and_accepts_boundary(self):
        self.assertEqual(MAX_REQUEST_BODY_BYTES, 16 * 1024 * 1024)
        reader = _request_reader(b"POST /mcp HTTP/1.1\r\nContent-Length: 4\r\n\r\ntest")
        with mock.patch("agent_collab.server_http.MAX_REQUEST_BODY_BYTES", 4):
            method, target, _headers, body = await self.server._read_request(reader)

        self.assertEqual((method, target, body), ("POST", "/mcp", b"test"))

    async def test_oversized_body_is_rejected_before_body_read(self):
        for value in (b"5", b"9" * 5000):
            with self.subTest(length_digits=len(value)):
                reader = _request_reader(
                    b"POST /mcp HTTP/1.1\r\nContent-Length: " + value + b"\r\n\r\n"
                )
                with mock.patch("agent_collab.server_http.MAX_REQUEST_BODY_BYTES", 4):
                    with self.assertRaises(HttpError) as ctx:
                        await asyncio.wait_for(self.server._read_request(reader), timeout=0.1)

                self.assertEqual(ctx.exception.status, 413)
                self.assertIn("4-byte limit", ctx.exception.message)

    async def test_malformed_content_lengths_are_bad_requests(self):
        for value in (b"", b"-1", b"+1", b"1.0", b"one", b"1, 1"):
            with self.subTest(value=value):
                reader = _request_reader(
                    b"POST /mcp HTTP/1.1\r\nContent-Length: " + value + b"\r\n\r\n"
                )
                with self.assertRaises(HttpError) as ctx:
                    await self.server._read_request(reader)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.message, "invalid Content-Length header")

    async def test_duplicate_content_length_is_a_bad_request(self):
        reader = _request_reader(
            b"POST /mcp HTTP/1.1\r\nContent-Length: 4\r\ncontent-length: 4\r\n\r\ntest"
        )
        with self.assertRaises(HttpError) as ctx:
            await self.server._read_request(reader)

        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.message, "duplicate Content-Length header")

    async def test_header_count_and_aggregate_bytes_are_bounded(self):
        self.assertEqual(MAX_REQUEST_HEADERS, 100)
        self.assertEqual(MAX_REQUEST_HEADER_BYTES, 64 * 1024)
        cases = (
            (
                b"GET /health HTTP/1.1\r\nX-1: a\r\nX-2: b\r\nX-3: c\r\n\r\n",
                {"MAX_REQUEST_HEADERS": 2},
            ),
            (
                b"GET /health HTTP/1.1\r\nX-Long: abcdef\r\n\r\n",
                {"MAX_REQUEST_HEADER_BYTES": 8},
            ),
        )
        for request, limits in cases:
            with (
                self.subTest(limits=limits),
                mock.patch.multiple("agent_collab.server_http", **limits),
            ):
                with self.assertRaises(HttpError) as ctx:
                    await self.server._read_request(_request_reader(request))
                self.assertEqual(ctx.exception.status, 431)
                self.assertEqual(ctx.exception.message, "request headers too large")

    async def test_stream_reader_line_limits_map_to_controlled_errors(self):
        cases = (
            (b"G" * 17 + b"\n", 400, "request line too long"),
            (
                b"GET / HTTP/1.1\r\nX-Long: " + b"a" * 17 + b"\r\n\r\n",
                431,
                "request headers too large",
            ),
        )
        for request, status, message in cases:
            with self.subTest(status=status):
                with self.assertRaises(HttpError) as ctx:
                    await self.server._read_request(_request_reader(request, limit=16))
                self.assertEqual(ctx.exception.status, status)
                self.assertEqual(ctx.exception.message, message)

    async def test_invalid_header_name_whitespace_is_rejected(self):
        for header in (b"Content-Length : 4", b" Content-Length: 4", b"Bad Name: value"):
            with self.subTest(header=header):
                reader = _request_reader(b"POST /mcp HTTP/1.1\r\n" + header + b"\r\n\r\n")
                with self.assertRaises(HttpError) as ctx:
                    await self.server._read_request(reader)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.message, "invalid header name")

    async def test_transfer_encoding_is_explicitly_rejected(self):
        for extra in (b"", b"Content-Length: 4\r\n"):
            with self.subTest(has_content_length=bool(extra)):
                reader = _request_reader(
                    b"POST /mcp HTTP/1.1\r\n"
                    b"Transfer-Encoding: chunked\r\n" + extra + b"\r\n4\r\ntest\r\n0\r\n\r\n"
                )
                with self.assertRaises(HttpError) as ctx:
                    await self.server._read_request(reader)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.message, "Transfer-Encoding is not supported")

    async def test_incomplete_request_body_is_a_bad_request(self):
        reader = _request_reader(b"POST /mcp HTTP/1.1\r\nContent-Length: 5\r\n\r\nabc")
        with self.assertRaises(HttpError) as ctx:
            await self.server._read_request(reader)

        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.message, "incomplete request body")

    async def test_oversized_connection_gets_structured_413_response(self):
        reader = _request_reader(b"POST /mcp HTTP/1.1\r\nContent-Length: 5\r\n\r\n")
        writer = _CaptureWriter()
        with mock.patch("agent_collab.server_http.MAX_REQUEST_BODY_BYTES", 4):
            await self.server._handle_connection(reader, writer)

        head, body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 413 Payload Too Large", head)
        self.assertEqual(json.loads(body), {"error": "request body exceeds 4-byte limit"})
        self.assertTrue(writer.closed)

    async def test_excessive_headers_get_structured_431_response(self):
        reader = _request_reader(b"GET /health HTTP/1.1\r\nX-1: a\r\nX-2: b\r\n\r\n")
        writer = _CaptureWriter()
        with mock.patch("agent_collab.server_http.MAX_REQUEST_HEADERS", 1):
            await self.server._handle_connection(reader, writer)

        head, body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 431 Request Header Fields Too Large", head)
        self.assertEqual(json.loads(body), {"error": "request headers too large"})

    async def test_unexpected_exception_details_are_logged_but_not_sent_on_wire(self):
        sensitive_detail = "/private/runtime/session-index.json: access denied"
        server = AgentCollabHttpServer(
            manager=mock.Mock(spec=SessionManager),
            log_requests=False,
        )
        logged = []
        server._log = logged.append

        for exception_type in (RuntimeError, ValueError, KeyError):
            with self.subTest(exception_type=exception_type.__name__):
                server.manager.list_sessions.side_effect = exception_type(sensitive_detail)
                writer = _CaptureWriter()

                await server._handle_connection(
                    _request_reader(b"GET /health HTTP/1.1\r\n\r\n"),
                    writer,
                )

                head, body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
                self.assertIn(b"HTTP/1.1 500 Internal Server Error", head)
                self.assertEqual(
                    json.loads(body),
                    {"error": INTERNAL_SERVER_ERROR_MESSAGE},
                )
                self.assertNotIn(sensitive_detail.encode(), bytes(writer.buffer))
                self.assertIn(exception_type.__name__, logged[-1])
                self.assertIn(sensitive_detail, logged[-1])
                self.assertTrue(writer.closed)

        self.assertEqual(len(logged), 3)

    async def test_unknown_session_keeps_structured_404_wire_contract(self):
        writer = _CaptureWriter()

        await self.server._handle_connection(
            _request_reader(b"GET /sessions/missing HTTP/1.1\r\n\r\n"),
            writer,
        )

        head, body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 404 Not Found", head)
        self.assertEqual(json.loads(body), {"error": "'unknown session_id missing'"})

    async def test_session_request_error_keeps_structured_400_wire_contract(self):
        manager = mock.Mock(spec=SessionManager)
        manager.post_message = mock.AsyncMock(
            side_effect=SessionRequestError("session is not live: done")
        )
        server = AgentCollabHttpServer(manager=manager)
        body = json.dumps({"text": "too late"}).encode()
        request = (
            b"POST /sessions/finished/messages HTTP/1.1\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        writer = _CaptureWriter()

        await server._handle_connection(_request_reader(request), writer)

        head, response_body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 400 Bad Request", head)
        self.assertEqual(
            json.loads(response_body),
            {"error": "session is not live: done"},
        )

    async def test_unexpected_mcp_tool_exception_is_logged_and_sanitized_on_wire(self):
        sensitive_detail = "/private/mcp/backend.json: corrupt"
        server = AgentCollabHttpServer(
            manager=mock.Mock(spec=SessionManager),
            log_requests=False,
        )
        logged = []
        server._log = logged.append
        body = _mcp_body(91, "tools/call", {"name": "agent_collab_list_sessions"})

        for exception_type in (RuntimeError, ValueError, KeyError):
            with self.subTest(exception_type=exception_type.__name__):
                server.manager.list_sessions.side_effect = exception_type(sensitive_detail)
                request = (
                    b"POST /mcp HTTP/1.1\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                writer = _CaptureWriter()

                await server._handle_connection(_request_reader(request), writer)

                head, response_body = bytes(writer.buffer).split(b"\r\n\r\n", 1)
                self.assertIn(b"HTTP/1.1 500 Internal Server Error", head)
                self.assertEqual(
                    json.loads(response_body),
                    {
                        "jsonrpc": "2.0",
                        "id": 91,
                        "error": {
                            "code": -32603,
                            "message": INTERNAL_SERVER_ERROR_MESSAGE,
                        },
                    },
                )
                self.assertNotIn(sensitive_detail.encode(), bytes(writer.buffer))
                self.assertIn(exception_type.__name__, logged[-1])
                self.assertIn(sensitive_detail, logged[-1])

        self.assertEqual(len(logged), 3)


class HttpServerDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_start_probe_does_not_block_sessions_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager(default_workdir=root)
            server = AgentCollabHttpServer(manager=manager)
            entered = threading.Event()
            release = threading.Event()

            def slow_unavailable_probe(*_args):
                entered.set()
                release.wait(2.0)
                return BackendHealth(status=HEALTH_UNAVAILABLE, reason="test probe unavailable")

            body = json.dumps(
                {"task": "slow probe", "workdir": str(root), "backend": "sdk"}
            ).encode("utf-8")
            with (
                mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}),
                mock.patch.object(
                    manager,
                    "_backend_health",
                    side_effect=slow_unavailable_probe,
                ),
            ):
                started_at = time.monotonic()
                start_task = asyncio.create_task(server._dispatch("POST", "/sessions", {}, body))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
                try:
                    listed = await server._dispatch("GET", "/sessions", {}, b"")
                finally:
                    release.set()

                self.assertLess(time.monotonic() - started_at, 0.5)
                self.assertEqual(listed, {"sessions": []})
                with self.assertRaises(StartOptionsError):
                    await start_task

    async def test_start_status_and_events_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager())
            body = json.dumps(
                {
                    "task": "http dispatch task",
                    "workdir": str(root),
                    "mock": True,
                    "max_turns": 1,
                    "timeout": 5,
                }
            ).encode("utf-8")

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                started = await server._dispatch("POST", "/sessions", {}, body)
                session_id = started["session_id"]
                waited = await server._dispatch(
                    "GET",
                    f"/sessions/{session_id}/events/wait?cursor=0&timeout_ms=1000",
                    {},
                    b"",
                )
                status = await server._dispatch("GET", f"/sessions/{session_id}", {}, b"")
                listed = await server._dispatch("GET", "/sessions", {}, b"")

            self.assertGreater(waited["cursor"], 0)
            self.assertIn(status["status"], {"running", "done"})
            self.assertEqual(listed["sessions"][0]["session_id"], session_id)

    async def test_mcp_initialize_dispatch(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        body = _mcp_body(1, "initialize", {"protocolVersion": "2025-11-25"})

        response = await server._dispatch("POST", "/mcp", {}, body)

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")
        self.assertIn("tools", response["result"]["capabilities"])

    async def test_mcp_tools_list_dispatch(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch("POST", "/mcp", {}, _mcp_body(2, "tools/list"))

        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("agent_collab_describe_options", names)
        self.assertIn("agent_collab_start", names)
        self.assertIn("agent_collab_wait_events", names)
        self.assertIn("agent_collab_post_message", names)

    async def test_options_route_describes_start_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager(default_workdir=root))

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch(
                    "POST", "/options", {}, json.dumps({"workdir": str(root)}).encode("utf-8")
                )

        self.assertIn("workflows", response)
        self.assertIn("backend_options", response)

    async def test_options_get_route_accepts_workdir_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager(default_workdir=root))

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch("GET", f"/options?workdir={root}", {}, b"")

        self.assertIn("workflows", response)
        self.assertIn("backend_options", response)

    async def test_options_route_accepts_fresh_health_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager(default_workdir=root))
            body = json.dumps({"workdir": str(root), "health_refresh": "fresh"}).encode("utf-8")
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch("POST", "/options", {}, body)
        self.assertEqual(response["discovery"]["health_request"], "fresh")

    async def test_options_route_requires_workdir(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        for method, target, body in (
            ("GET", "/options", b""),
            ("GET", "/options?workdir=%20%20%20", b""),
            ("POST", "/options", json.dumps({}).encode("utf-8")),
            ("POST", "/options", json.dumps({"workdir": "   "}).encode("utf-8")),
        ):
            with self.subTest(method=method, target=target, body=body):
                with self.assertRaises(HttpError) as ctx:
                    await server._dispatch(method, target, {}, body)

                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.message, "workdir is required")

    async def test_post_message_route_returns_event_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            server = AgentCollabHttpServer(manager=manager)
            body = json.dumps(
                {
                    "task": "http interactive task",
                    "workdir": str(root),
                    "mock": True,
                    "max_turns": 0,
                    "timeout": 5,
                    "interactive": True,
                    "interactive_idle_timeout": 5,
                }
            ).encode("utf-8")

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                started = await server._dispatch("POST", "/sessions", {}, body)
                session_id = started["session_id"]
                await _wait_for_status(manager, session_id, "awaiting_input")
                response = await server._dispatch(
                    "POST",
                    f"/sessions/{session_id}/messages",
                    {},
                    json.dumps({"text": "from http", "target": "claude"}).encode("utf-8"),
                )
                await manager.stop_session(session_id)

        self.assertEqual(response["session_id"], session_id)
        self.assertEqual(response["events"][0]["text"], "from http")
        self.assertEqual(response["events"][0]["raw"]["resolved_target"], "claude")

    async def test_mcp_start_is_visible_and_readable_through_session_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager())
            body = _mcp_body(
                3,
                "tools/call",
                {
                    "name": "agent_collab_start",
                    "arguments": {
                        "task": "mcp http dispatch task",
                        "workdir": str(root),
                        "mock": True,
                        "max_turns": 1,
                        "timeout": 5,
                    },
                },
            )

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch("POST", "/mcp", {}, body)
                started = json.loads(response["result"]["content"][0]["text"])
                session_id = started["session_id"]
                listed = await server._dispatch("GET", "/sessions", {}, b"")
                waited = await server._dispatch(
                    "GET",
                    f"/sessions/{session_id}/events/wait?cursor=0&timeout_ms=1000",
                    {},
                    b"",
                )

            self.assertEqual(listed["sessions"][0]["session_id"], session_id)
            self.assertGreater(waited["cursor"], 0)

    async def test_invalid_start_options_raise_before_session_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            server = AgentCollabHttpServer(manager=manager)
            body = json.dumps(
                {
                    "task": "bad http options",
                    "workdir": str(root),
                    "mock": True,
                    "backend_options": {"codex_cli": {"reasoning_effort": "maximum"}},
                }
            ).encode("utf-8")

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with self.assertRaises(StartOptionsError) as ctx:
                    await server._dispatch("POST", "/sessions", {}, body)

        self.assertEqual(manager.list_sessions(), [])
        self.assertEqual(ctx.exception.to_dict()["error"], "invalid_start_options")

    async def test_sessions_route_requires_workdir(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        for payload in ({"task": "missing workdir"}, {"task": "blank workdir", "workdir": "   "}):
            with self.subTest(payload=payload):
                with self.assertRaises(HttpError) as ctx:
                    await server._dispatch(
                        "POST", "/sessions", {}, json.dumps(payload).encode("utf-8")
                    )

                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.message, "workdir is required")

    async def test_null_and_malformed_numeric_requests_are_bad_requests(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        for field, value in (("max_turns", None), ("timeout", [])):
            payload = {"task": "bad number", "workdir": "/tmp", field: value}
            with self.subTest(field=field), self.assertRaises(HttpError) as ctx:
                await server._dispatch("POST", "/sessions", {}, json.dumps(payload).encode())
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(HttpError) as ctx:
            await server._dispatch("GET", "/sessions/s/events?limit=null", {}, b"")
        self.assertEqual(ctx.exception.status, 400)

    async def test_bearer_auth_protects_everything_except_health(self):
        server = AgentCollabHttpServer(manager=SessionManager(), auth_token="secret")
        health = await server._dispatch("GET", "/health", {}, b"")
        self.assertEqual(health["status"], "ok")

        for headers in ({}, {"authorization": "Bearer wrong"}):
            with self.subTest(headers=headers), self.assertRaises(HttpError) as ctx:
                await server._dispatch("GET", "/sessions", headers, b"")
            self.assertEqual(ctx.exception.status, 401)

        listed = await server._dispatch("GET", "/sessions", {"authorization": "Bearer secret"}, b"")
        self.assertEqual(listed, {"sessions": []})
        health_with_slash = await server._dispatch("GET", "/health/", {}, b"")
        self.assertEqual(health_with_slash["status"], "ok")
        with self.assertRaises(HttpError) as ctx:
            await server._dispatch("POST", "/health", {}, b"")
        self.assertEqual(ctx.exception.status, 401)

    async def test_mcp_requires_bearer_token_when_auth_is_enabled(self):
        server = AgentCollabHttpServer(manager=SessionManager(), auth_token="secret")
        with self.assertRaises(HttpError) as ctx:
            await server._dispatch("POST", "/mcp", {}, _mcp_body(1, "tools/list"))
        self.assertEqual(ctx.exception.status, 401)
        response = await server._dispatch(
            "POST",
            "/mcp/",
            {"authorization": "Bearer secret"},
            _mcp_body(1, "tools/list"),
        )
        self.assertEqual(response["id"], 1)

    async def test_mcp_rejects_non_local_origin(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        for method, origin in (
            ("POST", "https://example.invalid"),
            ("POST", "http://127.example.invalid"),
            ("GET", "https://example.invalid"),
        ):
            with self.subTest(method=method, origin=origin):
                with self.assertRaises(HttpError) as ctx:
                    await server._dispatch(
                        method,
                        "/mcp",
                        {"origin": origin},
                        _mcp_body(4, "tools/list") if method == "POST" else b"",
                    )

                self.assertEqual(ctx.exception.status, 403)

    async def test_mcp_allows_localhost_origin(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch(
            "POST",
            "/mcp",
            {"origin": "http://localhost:3000"},
            _mcp_body(5, "tools/list"),
        )

        self.assertEqual(response["id"], 5)
        self.assertIn("tools", response["result"])

    async def test_mcp_rejects_unsupported_protocol_version(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        with self.assertRaises(HttpError) as ctx:
            await server._dispatch(
                "POST",
                "/mcp",
                {"mcp-protocol-version": "1900-01-01"},
                _mcp_body(6, "tools/list"),
            )

        self.assertEqual(ctx.exception.status, 400)

    async def test_mcp_accepts_supported_protocol_version(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch(
            "POST",
            "/mcp",
            {"mcp-protocol-version": "2025-11-25"},
            _mcp_body(7, "tools/list"),
        )

        self.assertEqual(response["id"], 7)
        self.assertIn("tools", response["result"])

    async def test_mcp_notification_returns_accepted_response(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled"}).encode("utf-8")

        response = await server._dispatch("POST", "/mcp", {}, body)

        self.assertEqual(response, HttpResponse(202))

    async def test_mcp_client_response_returns_accepted_response(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        body = json.dumps({"jsonrpc": "2.0", "id": 8, "result": {}}).encode("utf-8")

        response = await server._dispatch("POST", "/mcp", {}, body)

        self.assertEqual(response, HttpResponse(202))

    async def test_mcp_unknown_tool_is_jsonrpc_error(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch(
            "POST",
            "/mcp",
            {},
            _mcp_body(9, "tools/call", {"name": "not_a_tool", "arguments": {}}),
        )

        self.assertEqual(response["id"], 9)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unknown tool", response["error"]["message"])

    async def test_mcp_tool_execution_error_sets_is_error(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch(
            "POST",
            "/mcp",
            {},
            _mcp_body(10, "tools/call", {"name": "agent_collab_start", "arguments": {}}),
        )

        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), {"error": "task is required"})

    async def test_mcp_start_requires_workdir_even_with_task(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch(
            "POST",
            "/mcp",
            {},
            _mcp_body(
                10,
                "tools/call",
                {"name": "agent_collab_start", "arguments": {"task": "missing workdir"}},
            ),
        )

        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), {"error": "workdir is required"})

    async def test_mcp_describe_options_requires_workdir(self):
        server = AgentCollabHttpServer(manager=SessionManager())

        response = await server._dispatch(
            "POST",
            "/mcp",
            {},
            _mcp_body(10, "tools/call", {"name": "agent_collab_describe_options", "arguments": {}}),
        )

        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), {"error": "workdir is required"})

    async def test_mcp_start_option_validation_error_sets_is_error_with_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager())

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch(
                    "POST",
                    "/mcp",
                    {},
                    _mcp_body(
                        11,
                        "tools/call",
                        {
                            "name": "agent_collab_start",
                            "arguments": {
                                "task": "bad mcp options",
                                "workdir": str(root),
                                "mock": True,
                                "backend_options": {"codex_cli": {"reasoning_effort": "maximum"}},
                            },
                        },
                    ),
                )

        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"], "invalid_start_options")
        self.assertEqual(
            payload["details"][0]["path"], "backend_options.codex_cli.reasoning_effort"
        )

    async def test_mcp_start_unknown_workflow_is_structured_not_internal_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager())

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch(
                    "POST",
                    "/mcp",
                    {},
                    _mcp_body(
                        13,
                        "tools/call",
                        {
                            "name": "agent_collab_start",
                            "arguments": {
                                "task": "bogus workflow",
                                "workdir": str(root),
                                "workflow": "solo-antigravity",
                                "mock": True,
                            },
                        },
                    ),
                )

        # A structured tool result (isError), not the sanitized -32603 internal
        # error that a bare 500 would produce: a 500 carries no "result" key.
        self.assertIn("result", response)
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"], "invalid_start_options")
        detail = payload["details"][0]
        self.assertEqual(detail["path"], "workflow")
        self.assertIn("solo-antigravity", detail["message"])

    async def test_mcp_start_rejects_non_object_option_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager())

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch(
                    "POST",
                    "/mcp",
                    {},
                    _mcp_body(
                        12,
                        "tools/call",
                        {
                            "name": "agent_collab_start",
                            "arguments": {
                                "task": "bad mcp options",
                                "workdir": str(root),
                                "mock": True,
                                "backend_options": [],
                            },
                        },
                    ),
                )

        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"], "invalid_start_options")
        self.assertEqual(payload["details"][0]["path"], "backend_options")
        self.assertIn("object", payload["details"][0]["message"])


def _mcp_body(request_id, method, params=None):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return json.dumps(request).encode("utf-8")


async def _wait_for_status(manager, session_id, expected):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while loop.time() < deadline:
        state = manager.get_session(session_id)
        if state.status == expected:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError(f"session {session_id} did not reach {expected}")


class PruneRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.session_dir = root / "sessions"
        self.session_dir.mkdir()
        self.index_path = root / "session-index.json"

    def _add_terminal_record(self, session_id, *, days_ago=60):
        from datetime import datetime, timedelta, timezone

        timestamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        SessionIndex(self.index_path).upsert(
            {
                "session_id": session_id,
                "status": "done",
                "task": "old",
                "workflow": "cross-review",
                "workdir": self._tmp.name,
                "jsonl_path": str(self.session_dir / f"{session_id}.jsonl"),
                "markdown_path": str(self.session_dir / f"{session_id}.md"),
                "created_at": timestamp,
                "updated_at": timestamp,
                "ended_at": timestamp,
            }
        )
        (self.session_dir / f"{session_id}.jsonl").write_text("x", encoding="utf-8")
        (self.session_dir / f"{session_id}.md").write_text("x", encoding="utf-8")

    def _server(self, sessions_config=None):
        manager = SessionManager(index_path=self.index_path, default_log_dir=self.session_dir)
        return AgentCollabHttpServer(manager=manager, sessions_config=sessions_config)

    async def test_prune_route_requires_bearer_token(self):
        server = AgentCollabHttpServer(manager=SessionManager(), auth_token="secret")

        with self.assertRaises(HttpError) as ctx:
            await server._dispatch("POST", "/sessions/prune", {}, b"{}")

        self.assertEqual(ctx.exception.status, 401)

    async def test_invalid_prune_payloads_are_bad_requests(self):
        server = self._server()
        for payload in ({"older_than": "0d"}, {"keep": -1}, {"bogus": 1}):
            with self.subTest(payload=payload), self.assertRaises(HttpError) as ctx:
                await server._dispatch(
                    "POST", "/sessions/prune", {}, json.dumps(payload).encode("utf-8")
                )
            self.assertEqual(ctx.exception.status, 400)

    async def test_disabled_retention_requires_explicit_older_than(self):
        from agent_collab.config import SessionsConfig

        self._add_terminal_record("old-1")
        server = self._server(sessions_config=SessionsConfig(retention_days=0))

        with self.assertRaises(HttpError) as ctx:
            await server._dispatch("POST", "/sessions/prune", {}, b"{}")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("older_than", ctx.exception.message)

        body = json.dumps({"older_than": "30d", "apply": True}).encode("utf-8")
        result = await server._dispatch("POST", "/sessions/prune", {}, body)
        self.assertEqual(result["pruned"], 1)

    async def test_broken_user_config_fails_safe_to_disabled_retention(self):
        # A config error must never silently re-enable deletion the user may
        # have opted out of; the fail-safe is retention disabled, not 30 days.
        from agent_collab.server_http import _load_sessions_config

        home = Path(self._tmp.name).resolve() / "home"
        home.mkdir()
        (home / "config.toml").write_text("[agents.broken]\ntype = 12345\n", encoding="utf-8")

        config = _load_sessions_config(home)

        self.assertEqual(config.retention_days, 0)

    async def test_retention_config_load_does_not_treat_daemon_home_as_a_workdir(self):
        from agent_collab.server_http import _load_sessions_config

        root = Path(self._tmp.name).resolve()
        home = root / "home"
        allowed = root / "projects"
        home.mkdir()
        allowed.mkdir()
        (home / "config.toml").write_text(
            (
                f"[sessions]\nretention_days = 7\n\n[workdir]\n"
                f'restrict_workdir_roots = ["{allowed}"]\n'
            ),
            encoding="utf-8",
        )

        config = _load_sessions_config(home)

        self.assertEqual(config.retention_days, 7)

    async def test_preview_then_apply_uses_configured_retention(self):
        self._add_terminal_record("old-1")
        self._add_terminal_record("fresh", days_ago=1)
        server = self._server()

        preview = await server._dispatch("POST", "/sessions/prune", {}, b"{}")

        self.assertFalse(preview["apply"])
        self.assertEqual(preview["candidates"], 1)
        self.assertEqual(preview["pruned"], 0)
        self.assertTrue((self.session_dir / "old-1.jsonl").exists())

        applied = await server._dispatch(
            "POST", "/sessions/prune", {}, json.dumps({"apply": True}).encode("utf-8")
        )

        self.assertEqual(applied["pruned"], 1)
        self.assertFalse((self.session_dir / "old-1.jsonl").exists())
        self.assertEqual(sorted(SessionIndex(self.index_path).load()), ["fresh"])


class RetentionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.session_dir = root / "sessions"
        self.session_dir.mkdir()
        self.index_path = root / "session-index.json"

    def _add_terminal_record(self, session_id, *, days_ago=60):
        from datetime import datetime, timedelta, timezone

        timestamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        SessionIndex(self.index_path).upsert(
            {
                "session_id": session_id,
                "status": "done",
                "task": "old",
                "workflow": "cross-review",
                "workdir": self._tmp.name,
                "jsonl_path": str(self.session_dir / f"{session_id}.jsonl"),
                "markdown_path": str(self.session_dir / f"{session_id}.md"),
                "created_at": timestamp,
                "updated_at": timestamp,
                "ended_at": timestamp,
            }
        )
        (self.session_dir / f"{session_id}.jsonl").write_text("x", encoding="utf-8")
        (self.session_dir / f"{session_id}.md").write_text("x", encoding="utf-8")

    def _server(self, sessions_config=None):
        manager = SessionManager(index_path=self.index_path, default_log_dir=self.session_dir)
        return AgentCollabHttpServer(manager=manager, sessions_config=sessions_config)

    async def _wait_for(self, predicate, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached before timeout")

    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_startup_run_prunes_restored_expired_sessions(self):
        self._add_terminal_record("old-1")
        self._add_terminal_record("fresh", days_ago=1)
        server = self._server()
        server._retention_interval_seconds = 60.0

        task = server.start_retention_task()
        self.assertIsNotNone(task)
        await self._wait_for(lambda: sorted(SessionIndex(self.index_path).load()) == ["fresh"])
        await self._cancel(task)

        self.assertFalse((self.session_dir / "old-1.jsonl").exists())
        self.assertTrue((self.session_dir / "fresh.jsonl").exists())

    async def test_scheduler_reruns_on_the_configured_interval(self):
        server = self._server()
        server._retention_interval_seconds = 0.01
        calls = []

        async def counting_prune(**kwargs):
            calls.append(kwargs)
            return mock.Mock(pruned=0, failed=0, bytes_reclaimed=0)

        with mock.patch.object(server.manager, "prune_sessions", side_effect=counting_prune):
            task = server.start_retention_task()
            await self._wait_for(lambda: len(calls) >= 3)
            await self._cancel(task)

        self.assertTrue(all(call["apply"] for call in calls))

    async def test_disabled_retention_starts_no_task(self):
        from agent_collab.config import SessionsConfig

        server = self._server(sessions_config=SessionsConfig(retention_days=0))

        self.assertIsNone(server.start_retention_task())

    async def test_scheduled_and_manual_runs_never_overlap(self):
        import threading
        from datetime import timedelta

        import agent_collab.daemon as daemon_module

        for index in range(3):
            self._add_terminal_record(f"old-{index}")
        server = self._server()
        server._retention_interval_seconds = 0.01
        state = {"active": 0, "max": 0, "calls": 0}
        guard = threading.Lock()
        real_unlinks = daemon_module._execute_transcript_unlinks

        def tracked_unlinks(plans, apply):
            # Every prune run (scheduled or manual) passes through here; the
            # sleep widens any overlap window the prune lock fails to close.
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
                state["calls"] += 1
            time.sleep(0.02)
            try:
                return real_unlinks(plans, apply)
            finally:
                with guard:
                    state["active"] -= 1

        with mock.patch.object(
            daemon_module, "_execute_transcript_unlinks", side_effect=tracked_unlinks
        ):
            task = server.start_retention_task()
            await asyncio.gather(
                *(
                    server.manager.prune_sessions(apply=True, retention=timedelta(days=30))
                    for _ in range(3)
                )
            )
            await self._wait_for(lambda: state["calls"] >= 5)
            await self._cancel(task)

        self.assertEqual(state["max"], 1)

    async def test_mid_run_cancellation_propagates_and_next_run_converges(self):
        from datetime import timedelta

        self._add_terminal_record("old-1")
        server = self._server()
        server._retention_interval_seconds = 60.0
        entered = asyncio.Event()
        release = asyncio.Event()
        real_prune = server.manager.prune_sessions

        async def blocking_prune(**kwargs):
            entered.set()
            # Cancellation lands here, mid "run", and must not be swallowed
            # by the loop's failure handling.
            await release.wait()
            return await real_prune(**kwargs)

        with mock.patch.object(server.manager, "prune_sessions", side_effect=blocking_prune):
            task = server.start_retention_task()
            await asyncio.wait_for(entered.wait(), 2.0)
            await self._cancel(task)

        # Nothing was mutated mid-run; a later manual run converges normally.
        result = await server.manager.prune_sessions(apply=True, retention=timedelta(days=30))
        self.assertEqual(result.pruned, 1)
        self.assertEqual(SessionIndex(self.index_path).load(), {})

    async def test_failing_run_is_logged_and_the_loop_survives(self):
        server = self._server()
        server._retention_interval_seconds = 0.01
        calls = []
        logs = []
        server._log = logs.append

        async def flaky_prune(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("simulated retention failure")
            return mock.Mock(pruned=0, failed=0, bytes_reclaimed=0)

        with mock.patch.object(server.manager, "prune_sessions", side_effect=flaky_prune):
            task = server.start_retention_task()
            await self._wait_for(lambda: len(calls) >= 2)
            await self._cancel(task)

        self.assertTrue(any("simulated retention failure" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
