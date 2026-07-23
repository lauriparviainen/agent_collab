import io
import json
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.api_schema import (
    EventBatchModel,
    SessionListModel,
    SessionResultModel,
    SessionStateModel,
)
from agent_collab.client import ClientError
from agent_collab.mcp_server import handle, handle_tool, serve


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _result(**fields):
    data = {
        "session_id": "s1",
        "status": "done",
        "terminal": True,
        "settled": True,
        "cursor": 5,
        "answers": [
            {"agent_id": "claude_cli", "text": "the answer", "event_id": 3, "timestamp": "t"}
        ],
    }
    data.update(fields)
    return SessionResultModel.from_dict(data)


def _state(**fields):
    """Typed client result: the session DTO the real AgentCollabClient returns."""
    data = {"session_id": "s1", "status": "running"}
    data.update(fields)
    return SessionStateModel.from_dict(data)


def _event(text):
    return {
        "timestamp": "2026-07-10T00:00:00+00:00",
        "source": "referee",
        "type": "message",
        "text": text,
        "raw": None,
    }


def _batch(session_id="s1", cursor=0, events=()):
    return EventBatchModel.from_dict(
        {"session_id": session_id, "cursor": cursor, "events": list(events)}
    )


def _outcome_batch(session_id="s1"):
    return EventBatchModel.from_dict(
        {
            "session_id": session_id,
            "cursor": 4,
            "status": "failed",
            "terminal": True,
            "error": "The provider transport failed",
            "failure": {
                "code": "provider_transport_failed",
                "message": "The provider transport failed",
                "stage_index": None,
                "turn_id": None,
                "agent_id": None,
                "backend": None,
                "outcome": None,
                "provider_stop_reason": None,
                "process_exit_code": None,
            },
            "turn_outcomes": [],
            "events": [],
        }
    )


def _assert_tool_result(testcase, result, payload, is_error=False):
    testcase.assertEqual(_payload(result), payload)
    testcase.assertEqual(result.get("isError"), is_error)


class McpServerTests(unittest.TestCase):
    def test_event_polling_preserves_structured_terminal_snapshot(self):
        batch = _outcome_batch()
        client = mock.Mock()
        client.read_events.return_value = batch
        with mock.patch("agent_collab.mcp_server.AgentCollabClient", return_value=client):
            result = handle_tool("agent_collab_read_events", {"session_id": "s1"})
        self.assertEqual(_payload(result), batch.to_dict())
        self.assertEqual(_payload(result)["events"], [])
        self.assertTrue(_payload(result)["terminal"])

    def test_tools_list_includes_daemon_tools(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("agent_collab_describe_options", names)
        self.assertIn("agent_collab_start", names)
        self.assertIn("agent_collab_list_sessions", names)
        self.assertIn("agent_collab_status", names)
        self.assertIn("agent_collab_read_events", names)
        self.assertIn("agent_collab_wait_events", names)
        self.assertIn("agent_collab_wait_result", names)
        self.assertIn("agent_collab_read_transcript", names)
        self.assertIn("agent_collab_post_message", names)
        self.assertIn("agent_collab_stop", names)
        self.assertIn("agent_collab_guidance", names)

    def test_start_and_describe_options_require_workdir_in_schema(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}

        self.assertEqual(
            tools["agent_collab_start"]["inputSchema"]["required"], ["task", "workdir"]
        )
        self.assertEqual(
            tools["agent_collab_describe_options"]["inputSchema"]["required"], ["workdir"]
        )

    def test_guidance_without_topic_returns_full_markdown(self):
        result = handle_tool("agent_collab_guidance", {})

        text = result["content"][0]["text"]
        self.assertFalse(result.get("isError"))
        self.assertIn("# agent-collab MCP guidance", text)
        self.assertIn("## Start", text)
        self.assertIn("## Errors", text)

    def test_guidance_topic_returns_single_section(self):
        result = handle_tool("agent_collab_guidance", {"topic": "errors"})

        text = result["content"][0]["text"]
        self.assertFalse(result.get("isError"))
        self.assertTrue(text.startswith("## Errors"))
        self.assertNotIn("## Watch", text)
        self.assertIn("invalid_start_options", text)

    def test_review_recipe_guidance_is_discoverable_and_mechanical(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        topics = tools["agent_collab_guidance"]["inputSchema"]["properties"]["topic"]["enum"]
        self.assertIn("review-recipe", topics)

        result = handle_tool("agent_collab_guidance", {"topic": "review-recipe"})
        text = result["content"][0]["text"]
        self.assertFalse(result.get("isError"))
        self.assertTrue(text.startswith("## Review recipe"))
        for required in (
            "git diff --name-status -z",
            "interactive: false",
            "timeout_ms=20000",
            "[<session_id> <canonical_backend>]",
            "Advisory backend quirks (2026-07-15)",
        ):
            self.assertIn(required, text)
        self.assertNotIn("## Errors", text)

    def test_guidance_document_ships_inside_the_package(self):
        """Installed daemons must find the guidance file under site-packages.

        A repo-relative ``doc/`` path resolves only in a source checkout, so the
        document must live inside the ``agent_collab`` package (package-data
        coverage is asserted in test_ci_tooling).
        """
        import agent_collab
        from agent_collab.mcp_tools import _GUIDANCE_PATH

        package_dir = Path(agent_collab.__file__).resolve().parent
        self.assertEqual(_GUIDANCE_PATH.parent, package_dir)
        self.assertTrue(_GUIDANCE_PATH.is_file())

    def test_guidance_unknown_topic_is_an_error(self):
        result = handle_tool("agent_collab_guidance", {"topic": "bogus"})

        self.assertTrue(result.get("isError"))
        self.assertIn("unknown guidance topic", _payload(result)["error"])

    def test_initialize_instructions_mention_guidance_and_workflow(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

        instructions = response["result"]["instructions"]
        self.assertIn("agent_collab_guidance", instructions)
        self.assertIn("workflow", instructions)
        self.assertIn("agent_collab_describe_options", instructions)
        self.assertLess(len(instructions), 1000)
        standalone = instructions[:500]
        self.assertIn("absolute workdir", standalone)
        self.assertIn("agent_collab_describe_options", standalone)
        self.assertIn("returned cursor", standalone)
        self.assertIn("agent_collab_guidance", standalone)

    def test_start_maps_to_client_start_session(self):
        args = {
            "task": "mcp test",
            "workflow": "cross-review",
            "workdir": "/repo",
            "max_turns": 5,
            "timeout": 120,
            "mock": True,
            "dry_run": False,
            "interactive": True,
            "interactive_idle_timeout": 60,
        }
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            state = _state()
            client.start_session.return_value = state

            result = handle_tool("agent_collab_start", args)

        client.start_session.assert_called_once_with(args)
        _assert_tool_result(self, result, state.to_dict())

    def test_start_maps_typed_options_to_client_start_session(self):
        args = {
            "task": "mcp test",
            "workdir": "/repo",
            "backend_options": {
                "codex_cli": {"reasoning_effort": "medium"},
                "claude_cli": {"model": "sonnet"},
            },
        }
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            state = _state()
            client.start_session.return_value = state

            result = handle_tool("agent_collab_start", args)

        client.start_session.assert_called_once_with(args)
        _assert_tool_result(self, result, state.to_dict())

    def test_start_rejects_missing_workdir(self):
        result = handle_tool("agent_collab_start", {"task": "mcp test"})

        self.assertTrue(result.get("isError"))
        self.assertEqual(_payload(result), {"error": "workdir is required"})

    def test_start_rejects_blank_workdir(self):
        result = handle_tool("agent_collab_start", {"task": "mcp test", "workdir": "   "})

        self.assertTrue(result.get("isError"))
        self.assertEqual(_payload(result), {"error": "workdir is required"})

    def test_describe_options_maps_to_client_describe_options(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.describe_options.return_value = {"workflows": [], "backend_options": {}}

            result = handle_tool("agent_collab_describe_options", {"workdir": "/repo"})

        client.describe_options.assert_called_once_with({"workdir": "/repo"})
        _assert_tool_result(self, result, {"workflows": [], "backend_options": {}})

    def test_describe_options_maps_fresh_health_request(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.describe_options.return_value = {"discovery": {"health_request": "fresh"}}

            result = handle_tool(
                "agent_collab_describe_options",
                {"workdir": "/repo", "health_refresh": "fresh"},
            )

        client.describe_options.assert_called_once_with(
            {"workdir": "/repo", "health_refresh": "fresh"}
        )
        _assert_tool_result(self, result, {"discovery": {"health_request": "fresh"}})

    def test_describe_options_rejects_invalid_health_refresh(self):
        result = handle_tool(
            "agent_collab_describe_options",
            {"workdir": "/repo", "health_refresh": "eventually"},
        )
        self.assertTrue(result.get("isError"))
        self.assertEqual(_payload(result), {"error": "health_refresh must be 'cached' or 'fresh'"})

    def test_describe_options_maps_model_refresh_and_rejects_invalid(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.describe_options.return_value = {"discovery": {"model_request": "none"}}

            result = handle_tool(
                "agent_collab_describe_options",
                {"workdir": "/repo", "model_refresh": "none"},
            )

        client.describe_options.assert_called_once_with(
            {"workdir": "/repo", "model_refresh": "none"}
        )
        _assert_tool_result(self, result, {"discovery": {"model_request": "none"}})

        rejected = handle_tool(
            "agent_collab_describe_options",
            {"workdir": "/repo", "model_refresh": "eventually"},
        )
        self.assertTrue(rejected.get("isError"))
        self.assertEqual(
            _payload(rejected),
            {"error": "model_refresh must be 'none', 'cached', or 'fresh'"},
        )

    def test_describe_options_rejects_missing_workdir(self):
        result = handle_tool("agent_collab_describe_options", {})

        self.assertTrue(result.get("isError"))
        self.assertEqual(_payload(result), {"error": "workdir is required"})

    def test_describe_options_rejects_blank_workdir(self):
        result = handle_tool("agent_collab_describe_options", {"workdir": "   "})

        self.assertTrue(result.get("isError"))
        self.assertEqual(_payload(result), {"error": "workdir is required"})

    def test_list_maps_to_client_list_sessions(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            listing = SessionListModel(sessions=[_state()])
            client.list_sessions.return_value = listing

            result = handle_tool("agent_collab_list_sessions", {})

        client.list_sessions.assert_called_once_with()
        _assert_tool_result(self, result, listing.to_dict())

    def test_status_maps_to_client_get_session(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            state = _state(status="done")
            client.get_session.return_value = state

            result = handle_tool("agent_collab_status", {"session_id": "s1"})

        client.get_session.assert_called_once_with("s1")
        _assert_tool_result(self, result, state.to_dict())

    def test_read_events_maps_to_client_read_events(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            batch = _batch(cursor=4, events=[_event("hello")])
            client.read_events.return_value = batch

            result = handle_tool("agent_collab_read_events", {"session_id": "s1", "cursor": 2})

        client.read_events.assert_called_once_with("s1", 2, limit=None, tool_output="summary")
        _assert_tool_result(self, result, batch.to_dict())

    def test_wait_events_maps_to_client_wait_events(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            batch = _batch(cursor=4)
            client.wait_events.return_value = batch

            result = handle_tool(
                "agent_collab_wait_events",
                {"session_id": "s1", "cursor": 2, "timeout_ms": 30000},
            )

        client.wait_events.assert_called_once_with("s1", 2, 30000, tool_output="summary")
        _assert_tool_result(self, result, batch.to_dict())

    def test_wait_result_maps_to_client_wait_result(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            result_model = _result()
            client.wait_result.return_value = result_model

            result = handle_tool(
                "agent_collab_wait_result",
                {"session_id": "s1", "timeout_ms": 120000},
            )

        client.wait_result.assert_called_once_with("s1", 120000)
        _assert_tool_result(self, result, result_model.to_dict())

    def test_wait_result_defaults_timeout_and_rejects_out_of_bounds(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.wait_result.return_value = _result()

            handle_tool("agent_collab_wait_result", {"session_id": "s1"})
            client.wait_result.assert_called_once_with("s1", 60000)

            rejected = handle_tool(
                "agent_collab_wait_result", {"session_id": "s1", "timeout_ms": 600001}
            )
        self.assertTrue(rejected["isError"])
        self.assertIn("timeout_ms", _payload(rejected)["error"])

    def test_transcript_maps_to_client_read_transcript_as_direct_text(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.read_transcript.return_value = "# transcript\n\nhello\n"

            result = handle_tool("agent_collab_read_transcript", {"session_id": "s1"})

        client.read_transcript.assert_called_once_with("s1", tool_output="summary")
        self.assertEqual(
            result,
            {"content": [{"type": "text", "text": "# transcript\n\nhello\n"}], "isError": False},
        )

    def test_read_projection_options_map_to_client(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.read_events.return_value = _batch(cursor=8)
            client.wait_events.return_value = _batch(cursor=8)
            client.read_transcript.return_value = "full"

            handle_tool(
                "agent_collab_read_events",
                {"session_id": "s1", "cursor": 7, "limit": 1, "tool_output": "full"},
            )
            handle_tool(
                "agent_collab_wait_events",
                {"session_id": "s1", "cursor": 7, "timeout_ms": 5, "tool_output": "full"},
            )
            handle_tool(
                "agent_collab_read_transcript",
                {"session_id": "s1", "tool_output": "full"},
            )

        client.read_events.assert_called_once_with("s1", 7, limit=1, tool_output="full")
        client.wait_events.assert_called_once_with("s1", 7, 5, tool_output="full")
        client.read_transcript.assert_called_once_with("s1", tool_output="full")

    def test_read_projection_uses_shared_query_validation(self):
        for tool, args, message in (
            ("agent_collab_read_events", {"session_id": "s1", "cursor": -1}, "cursor must be >= 0"),
            ("agent_collab_read_events", {"session_id": "s1", "limit": 0}, "limit must be >= 1"),
            (
                "agent_collab_wait_events",
                {"session_id": "s1", "timeout_ms": -1},
                "timeout_ms must be >= 0",
            ),
            (
                "agent_collab_read_transcript",
                {"session_id": "s1", "tool_output": "everything"},
                "tool_output must be 'summary' or 'full'",
            ),
        ):
            with self.subTest(tool=tool):
                result = handle_tool(tool, args)
                self.assertTrue(result["isError"])
                self.assertEqual(_payload(result), {"error": message})

    def test_post_message_maps_to_client_post_message(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            batch = _batch(cursor=3, events=[_event("hello")])
            client.post_message.return_value = batch

            result = handle_tool(
                "agent_collab_post_message",
                {"session_id": "s1", "text": "hello", "source": "referee", "target": "claude"},
            )

        client.post_message.assert_called_once_with(
            "s1", "hello", source="referee", target="claude"
        )
        _assert_tool_result(self, result, batch.to_dict())

    def test_stop_maps_to_client_stop_session(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            state = _state(status="stopped")
            client.stop_session.return_value = state

            result = handle_tool("agent_collab_stop", {"session_id": "s1"})

        client.stop_session.assert_called_once_with("s1")
        _assert_tool_result(self, result, state.to_dict())

    def test_client_error_returns_tool_content_error(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.get_session.side_effect = ClientError("could not reach daemon")

            result = handle_tool("agent_collab_status", {"session_id": "s1"})

        _assert_tool_result(self, result, {"error": "could not reach daemon"}, is_error=True)

    def test_client_error_through_jsonrpc_call_is_not_jsonrpc_error(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.get_session.side_effect = ClientError("could not reach daemon")

            response = handle(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "agent_collab_status",
                        "arguments": {"session_id": "s1"},
                    },
                }
            )

        self.assertNotIn("error", response)
        self.assertEqual(response["id"], 7)
        _assert_tool_result(
            self, response["result"], {"error": "could not reach daemon"}, is_error=True
        )

    def test_unexpected_client_exception_is_not_converted_to_tool_content(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client_cls.return_value.get_session.side_effect = RuntimeError(
                "/private/client-state.json"
            )

            with self.assertRaisesRegex(RuntimeError, "private/client-state"):
                handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 71,
                        "method": "tools/call",
                        "params": {
                            "name": "agent_collab_status",
                            "arguments": {"session_id": "s1"},
                        },
                    }
                )

    def test_stdio_unexpected_exception_is_logged_and_sanitized(self):
        sensitive_detail = "/private/stdio-state.json"
        stdin = io.StringIO('{"jsonrpc":"2.0","id":72,"method":"tools/list"}\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("agent_collab.mcp_server.sys.stdin", stdin),
            mock.patch("agent_collab.mcp_server.sys.stdout", stdout),
            mock.patch("agent_collab.mcp_server.sys.stderr", stderr),
            mock.patch(
                "agent_collab.mcp_server.handle",
                side_effect=RuntimeError(sensitive_detail),
            ),
        ):
            serve()

        response = json.loads(stdout.getvalue())
        self.assertEqual(
            response,
            {
                "jsonrpc": "2.0",
                "id": 72,
                "error": {"code": -32603, "message": "internal server error"},
            },
        )
        self.assertNotIn(sensitive_detail, stdout.getvalue())
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertIn(sensitive_detail, stderr.getvalue())

    def test_unknown_tool_through_jsonrpc_call_is_protocol_error(self):
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            }
        )

        self.assertEqual(response["id"], 8)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unknown tool", response["error"]["message"])

    def test_non_object_tool_arguments_are_protocol_error(self):
        response = handle(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "agent_collab_list_sessions", "arguments": []},
            }
        )

        self.assertEqual(response["id"], 9)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("arguments must be an object", response["error"]["message"])

    def test_notification_returns_no_response(self):
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}))

    def test_client_response_returns_no_response(self):
        self.assertIsNone(handle({"jsonrpc": "2.0", "id": 10, "result": {}}))


if __name__ == "__main__":
    unittest.main()
