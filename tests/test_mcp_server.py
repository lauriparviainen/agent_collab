import json
import unittest
from unittest import mock

from agent_collab.client import ClientError
from agent_collab.mcp_server import handle, handle_tool


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _assert_tool_result(testcase, result, payload, is_error=False):
    testcase.assertEqual(_payload(result), payload)
    testcase.assertEqual(result.get("isError"), is_error)


class McpServerTests(unittest.TestCase):
    def test_tools_list_includes_daemon_tools(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("agent_collab_describe_options", names)
        self.assertIn("agent_collab_start", names)
        self.assertIn("agent_collab_list_sessions", names)
        self.assertIn("agent_collab_status", names)
        self.assertIn("agent_collab_read_events", names)
        self.assertIn("agent_collab_wait_events", names)
        self.assertIn("agent_collab_read_transcript", names)
        self.assertIn("agent_collab_post_message", names)
        self.assertIn("agent_collab_stop", names)
        self.assertIn("agent_collab_guidance", names)

    def test_start_and_describe_options_require_workdir_in_schema(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}

        self.assertEqual(tools["agent_collab_start"]["inputSchema"]["required"], ["task", "workdir"])
        self.assertEqual(tools["agent_collab_describe_options"]["inputSchema"]["required"], ["workdir"])

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

    def test_start_maps_to_client_start_session(self):
        args = {
            "task": "mcp test",
            "workflow": "compare",
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
            client.start_session.return_value = {"session_id": "s1", "status": "running"}

            result = handle_tool("agent_collab_start", args)

        client.start_session.assert_called_once_with(args)
        _assert_tool_result(self, result, {"session_id": "s1", "status": "running"})

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
            client.start_session.return_value = {"session_id": "s1", "status": "running"}

            result = handle_tool("agent_collab_start", args)

        client.start_session.assert_called_once_with(args)
        _assert_tool_result(self, result, {"session_id": "s1", "status": "running"})

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
            client.list_sessions.return_value = {"sessions": [{"session_id": "s1"}]}

            result = handle_tool("agent_collab_list_sessions", {})

        client.list_sessions.assert_called_once_with()
        _assert_tool_result(self, result, {"sessions": [{"session_id": "s1"}]})

    def test_status_maps_to_client_get_session(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.get_session.return_value = {"session_id": "s1", "status": "done"}

            result = handle_tool("agent_collab_status", {"session_id": "s1"})

        client.get_session.assert_called_once_with("s1")
        _assert_tool_result(self, result, {"session_id": "s1", "status": "done"})

    def test_read_events_maps_to_client_read_events(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.read_events.return_value = {"cursor": 4, "events": [{"text": "hello"}]}

            result = handle_tool("agent_collab_read_events", {"session_id": "s1", "cursor": 2})

        client.read_events.assert_called_once_with("s1", 2)
        _assert_tool_result(self, result, {"cursor": 4, "events": [{"text": "hello"}]})

    def test_wait_events_maps_to_client_wait_events(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.wait_events.return_value = {"cursor": 4, "events": []}

            result = handle_tool(
                "agent_collab_wait_events",
                {"session_id": "s1", "cursor": 2, "timeout_ms": 30000},
            )

        client.wait_events.assert_called_once_with("s1", 2, 30000)
        _assert_tool_result(self, result, {"cursor": 4, "events": []})

    def test_transcript_maps_to_client_read_transcript_as_direct_text(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.read_transcript.return_value = "# transcript\n\nhello\n"

            result = handle_tool("agent_collab_read_transcript", {"session_id": "s1"})

        client.read_transcript.assert_called_once_with("s1")
        self.assertEqual(result, {"content": [{"type": "text", "text": "# transcript\n\nhello\n"}], "isError": False})

    def test_post_message_maps_to_client_post_message(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.post_message.return_value = {"session_id": "s1", "cursor": 3, "events": [{"text": "hello"}]}

            result = handle_tool(
                "agent_collab_post_message",
                {"session_id": "s1", "text": "hello", "source": "referee", "target": "claude"},
            )

        client.post_message.assert_called_once_with("s1", "hello", source="referee", target="claude")
        _assert_tool_result(self, result, {"session_id": "s1", "cursor": 3, "events": [{"text": "hello"}]})

    def test_stop_maps_to_client_stop_session(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.stop_session.return_value = {"session_id": "s1", "status": "stopped"}

            result = handle_tool("agent_collab_stop", {"session_id": "s1"})

        client.stop_session.assert_called_once_with("s1")
        _assert_tool_result(self, result, {"session_id": "s1", "status": "stopped"})

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
        _assert_tool_result(self, response["result"], {"error": "could not reach daemon"}, is_error=True)

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
