import json
import unittest
from unittest import mock

from agent_collab.client import ClientError
from agent_collab.mcp_server import handle, handle_tool


def _payload(result):
    return json.loads(result["content"][0]["text"])


class McpServerTests(unittest.TestCase):
    def test_tools_list_includes_daemon_tools(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("agent_collab_start", names)
        self.assertIn("agent_collab_list_sessions", names)
        self.assertIn("agent_collab_status", names)
        self.assertIn("agent_collab_read_events", names)
        self.assertIn("agent_collab_wait_events", names)
        self.assertIn("agent_collab_read_transcript", names)
        self.assertIn("agent_collab_stop", names)

    def test_start_maps_to_client_start_session(self):
        args = {
            "task": "mcp test",
            "mode": "codex-leads",
            "workdir": "/repo",
            "max_turns": 5,
            "timeout": 120,
            "mock": True,
            "dry_run": False,
        }
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.start_session.return_value = {"session_id": "s1", "status": "running"}

            result = handle_tool("agent_collab_start", args)

        client.start_session.assert_called_once_with(args)
        self.assertEqual(_payload(result), {"session_id": "s1", "status": "running"})

    def test_list_maps_to_client_list_sessions(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.list_sessions.return_value = {"sessions": [{"session_id": "s1"}]}

            result = handle_tool("agent_collab_list_sessions", {})

        client.list_sessions.assert_called_once_with()
        self.assertEqual(_payload(result), {"sessions": [{"session_id": "s1"}]})

    def test_status_maps_to_client_get_session(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.get_session.return_value = {"session_id": "s1", "status": "done"}

            result = handle_tool("agent_collab_status", {"session_id": "s1"})

        client.get_session.assert_called_once_with("s1")
        self.assertEqual(_payload(result), {"session_id": "s1", "status": "done"})

    def test_read_events_maps_to_client_read_events(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.read_events.return_value = {"cursor": 4, "events": [{"text": "hello"}]}

            result = handle_tool("agent_collab_read_events", {"session_id": "s1", "cursor": 2})

        client.read_events.assert_called_once_with("s1", 2)
        self.assertEqual(_payload(result), {"cursor": 4, "events": [{"text": "hello"}]})

    def test_wait_events_maps_to_client_wait_events(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.wait_events.return_value = {"cursor": 4, "events": []}

            result = handle_tool(
                "agent_collab_wait_events",
                {"session_id": "s1", "cursor": 2, "timeout_ms": 30000},
            )

        client.wait_events.assert_called_once_with("s1", 2, 30000)
        self.assertEqual(_payload(result), {"cursor": 4, "events": []})

    def test_transcript_maps_to_client_read_transcript_as_direct_text(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.read_transcript.return_value = "# transcript\n\nhello\n"

            result = handle_tool("agent_collab_read_transcript", {"session_id": "s1"})

        client.read_transcript.assert_called_once_with("s1")
        self.assertEqual(result, {"content": [{"type": "text", "text": "# transcript\n\nhello\n"}]})

    def test_stop_maps_to_client_stop_session(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.stop_session.return_value = {"session_id": "s1", "status": "stopped"}

            result = handle_tool("agent_collab_stop", {"session_id": "s1"})

        client.stop_session.assert_called_once_with("s1")
        self.assertEqual(_payload(result), {"session_id": "s1", "status": "stopped"})

    def test_client_error_returns_tool_content_error(self):
        with mock.patch("agent_collab.mcp_server.AgentCollabClient") as client_cls:
            client = client_cls.return_value
            client.get_session.side_effect = ClientError("could not reach daemon")

            result = handle_tool("agent_collab_status", {"session_id": "s1"})

        self.assertEqual(_payload(result), {"error": "could not reach daemon"})

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
        self.assertEqual(_payload(response["result"]), {"error": "could not reach daemon"})


if __name__ == "__main__":
    unittest.main()
