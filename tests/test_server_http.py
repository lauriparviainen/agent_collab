import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon import SessionManager
from agent_collab.options import StartOptionsError
from agent_collab.server_http import AgentCollabHttpServer, HttpError, HttpResponse


class HttpServerDispatchTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_options_route_describes_start_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentCollabHttpServer(manager=SessionManager(default_workdir=root))

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                response = await server._dispatch("POST", "/options", {}, json.dumps({"workdir": str(root)}).encode("utf-8"))

        self.assertIn("workflows", response)
        self.assertIn("codex_options", response)
        self.assertIn("claude_options", response)

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
                    "codex_options": {"reasoning_effort": "maximum"},
                }
            ).encode("utf-8")

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with self.assertRaises(StartOptionsError) as ctx:
                    await server._dispatch("POST", "/sessions", {}, body)

        self.assertEqual(manager.list_sessions(), [])
        self.assertEqual(ctx.exception.to_dict()["error"], "invalid_start_options")

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
                                "codex_options": {"reasoning_effort": "maximum"},
                            },
                        },
                    ),
                )

        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"], "invalid_start_options")
        self.assertEqual(payload["details"][0]["path"], "codex_options.reasoning_effort")

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
                                "codex_options": [],
                            },
                        },
                    ),
                )

        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"], "invalid_start_options")
        self.assertEqual(payload["details"][0]["path"], "codex_options")
        self.assertIn("object", payload["details"][0]["message"])


def _mcp_body(request_id, method, params=None):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return json.dumps(request).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
