import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon import SessionManager
from agent_collab.server_http import AgentCollabHttpServer


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

            with mock.patch.dict(os.environ, {"HOME": str(root / "home")}):
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


if __name__ == "__main__":
    unittest.main()
