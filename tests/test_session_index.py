import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.session_index import SessionIndex


class SessionIndexTests(unittest.TestCase):
    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(Path(tmp) / "session-index.json")
            self.assertEqual(index.load(), {})

    def test_load_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-index.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(SessionIndex(path).load(), {})

    def test_upsert_writes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-index.json"
            index = SessionIndex(path)

            index.upsert({"session_id": "one", "status": "running"})
            index.upsert({"session_id": "two", "status": "done"})
            index.upsert({"session_id": "one", "status": "done"})

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["sessions"]["one"]["status"], "done")
            self.assertEqual(data["sessions"]["two"]["status"], "done")
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_upsert_requires_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(Path(tmp) / "session-index.json")
            with self.assertRaises(ValueError):
                index.upsert({"status": "running"})


class SessionManagerIndexTests(unittest.IsolatedAsyncioTestCase):
    async def _run_session_to_done(self, manager, root, task="index task"):
        state = await manager.start_session(
            StartSessionRequest(task=task, mock=True, max_turns=1, timeout=5, workdir=root)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while loop.time() < deadline:
            current = manager.get_session(state.session_id)
            if current.status != "running":
                return current
            await asyncio.sleep(0.02)
        self.fail("session did not finish")

    async def test_sessions_survive_manager_restart_with_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "home" / "data" / "session-index.json"

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                final = await self._run_session_to_done(manager, root)

                restarted = SessionManager(index_path=index_path)

            restored = restarted.get_session(final.session_id)
            self.assertEqual(restored.status, "done")
            self.assertEqual(restored.workflow, "cross-review")
            self.assertEqual(restored.settings, final.settings)
            self.assertEqual(
                [state.session_id for state in restarted.list_sessions()],
                [final.session_id],
            )

    async def test_capabilities_summary_is_false_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "home" / "data" / "session-index.json"

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                final = await self._run_session_to_done(manager, root)
                # Every session this stage honestly reports all-false capabilities.
                self.assertEqual(final.capabilities, {"resumable": False, "interruptible": False})

                restarted = SessionManager(index_path=index_path)

            restored = restarted.get_session(final.session_id)
            self.assertEqual(restored.capabilities, {"resumable": False, "interruptible": False})

    async def test_running_sessions_marked_interrupted_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index.json"
            index = SessionIndex(index_path)
            index.upsert(
                {
                    "session_id": "daemon-dead",
                    "status": "running",
                    "task": "was running",
                    "workflow": "cross-review",
                    "workdir": str(root),
                    "jsonl_path": str(root / "daemon-dead.jsonl"),
                    "markdown_path": str(root / "daemon-dead.md"),
                    "created_at": "2026-07-08T00:00:00+00:00",
                    "updated_at": "2026-07-08T00:00:00+00:00",
                }
            )

            manager = SessionManager(index_path=index_path)

            restored = manager.get_session("daemon-dead")
            self.assertEqual(restored.status, "interrupted")
            self.assertIn("daemon restarted", restored.error)
            self.assertIsNotNone(restored.ended_at)
            self.assertEqual(index.load()["daemon-dead"]["status"], "interrupted")

    async def test_awaiting_input_sessions_marked_interrupted_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index.json"
            index = SessionIndex(index_path)
            index.upsert(
                {
                    "session_id": "daemon-awaiting",
                    "status": "awaiting_input",
                    "task": "was awaiting",
                    "workflow": "cross-review",
                    "workdir": str(root),
                    "jsonl_path": str(root / "daemon-awaiting.jsonl"),
                    "markdown_path": str(root / "daemon-awaiting.md"),
                    "created_at": "2026-07-08T00:00:00+00:00",
                    "updated_at": "2026-07-08T00:00:00+00:00",
                    "interactive": True,
                    "interactive_idle_timeout": 600,
                }
            )

            manager = SessionManager(index_path=index_path)

            restored = manager.get_session("daemon-awaiting")
            self.assertEqual(restored.status, "interrupted")
            self.assertIn("daemon restarted", restored.error)
            self.assertEqual(index.load()["daemon-awaiting"]["status"], "interrupted")

    async def test_read_events_replays_jsonl_for_restored_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "home" / "data" / "session-index.json"

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                final = await self._run_session_to_done(manager, root)
                live_events = manager.read_events(final.session_id, 0)

                restarted = SessionManager(index_path=index_path)
                replayed = restarted.read_events(final.session_id, 0)
                waited = await restarted.wait_events(final.session_id, replayed.cursor, timeout_ms=50)

            self.assertGreater(replayed.cursor, 0)
            self.assertEqual(
                [event["text"] for event in replayed.events],
                [event["text"] for event in live_events.events],
            )
            self.assertEqual(waited.events, [])

    async def test_restored_event_and_transcript_reads_do_not_block_loop(self):
        import agent_collab.daemon as daemon_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "home" / "data" / "session-index.json"

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                final = await self._run_session_to_done(manager, root)
                restarted = SessionManager(index_path=index_path)
                real_loader = daemon_module._load_events_from_jsonl

                for operation in ("events", "transcript"):
                    with self.subTest(operation=operation):
                        entered = threading.Event()
                        release = threading.Event()

                        def slow_loader(path):
                            entered.set()
                            release.wait(2.0)
                            return real_loader(path)

                        with mock.patch.object(
                            daemon_module,
                            "_load_events_from_jsonl",
                            side_effect=slow_loader,
                        ):
                            started_at = time.monotonic()
                            if operation == "events":
                                read_task = asyncio.create_task(
                                    restarted.read_events_async(final.session_id, 0)
                                )
                            else:
                                read_task = asyncio.create_task(
                                    restarted.read_transcript_async(final.session_id)
                                )
                            self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
                            try:
                                listed = restarted.list_sessions()
                            finally:
                                release.set()

                            self.assertLess(time.monotonic() - started_at, 0.5)
                            self.assertEqual(listed[0].session_id, final.session_id)
                            result = await read_task
                            if operation == "events":
                                self.assertGreater(result.cursor, 0)
                            else:
                                self.assertIn("# agent-collab session", result)

    async def test_restored_session_id_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "home" / "data" / "session-index.json"

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                final = await self._run_session_to_done(manager, root)

                restarted = SessionManager(index_path=index_path)
                with self.assertRaises(ValueError):
                    await restarted.start_session(
                        StartSessionRequest(
                            task="dup",
                            mock=True,
                            workdir=root,
                            session_id=final.session_id,
                        )
                    )

    async def test_corrupt_index_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index.json"
            index_path.write_text("nonsense", encoding="utf-8")

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                manager = SessionManager(index_path=index_path)
                self.assertEqual(manager.list_sessions(), [])
                final = await self._run_session_to_done(manager, root)

            self.assertEqual(final.status, "done")
            self.assertEqual(SessionIndex(index_path).load()[final.session_id]["status"], "done")


if __name__ == "__main__":
    unittest.main()
