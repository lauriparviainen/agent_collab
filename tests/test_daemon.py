import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.daemon import StartSessionRequest, SessionManager
from agent_collab.options import StartOptionsError
from agent_collab.paths import GlobalDataPaths


TERMINAL_STATUSES = {"done", "failed", "stopped"}


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_terminal(self, manager, session_id, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            state = manager.get_session(session_id)
            if state.status in TERMINAL_STATUSES:
                return state
            await asyncio.sleep(0.02)
        self.fail(f"session {session_id} did not finish before timeout")

    async def test_start_mock_session_runs_to_done_and_writes_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="daemon mock task",
                        mock=True,
                        max_turns=1,
                        timeout=5,
                        workdir=root,
                    )
                )
                self.assertEqual(state.status, "running")

                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertEqual(final.status, "done")
            global_sessions = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "home")}).session_dir
            self.assertEqual(Path(final.jsonl_path).parent, global_sessions)
            self.assertTrue(Path(final.jsonl_path).exists())
            self.assertTrue(Path(final.markdown_path).exists())

            batch = manager.read_events(state.session_id, 0)
            self.assertEqual(batch.cursor, len(batch.events))
            self.assertGreater(batch.cursor, 0)
            self.assertEqual(batch.events[0]["source"], "human")
            self.assertIn("daemon mock task", batch.events[0]["text"])

    async def test_read_events_uses_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(task="cursor task", mock=True, max_turns=1, timeout=5, workdir=root)
                )
                await self._wait_for_terminal(manager, state.session_id)

            all_events = manager.read_events(state.session_id, 0)
            tail = manager.read_events(state.session_id, 1)
            empty = manager.read_events(state.session_id, all_events.cursor)

            self.assertEqual(tail.cursor, all_events.cursor)
            self.assertEqual(tail.events, all_events.events[1:])
            self.assertEqual(empty.cursor, all_events.cursor)
            self.assertEqual(empty.events, [])

    async def test_wait_events_returns_new_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(task="wait task", mock=True, max_turns=2, timeout=5, workdir=root)
                )
                cursor = manager.read_events(state.session_id, 0).cursor
                batch = await manager.wait_events(state.session_id, cursor, timeout_ms=1000)
                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertGreater(batch.cursor, cursor)
            self.assertTrue(batch.events)
            self.assertEqual(final.status, "done")

    async def test_multiple_workdirs_sessions_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root_a = base / "a"
            root_b = base / "b"
            root_a.mkdir()
            root_b.mkdir()
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(base / "home")}):
                first, second = await asyncio.gather(
                    manager.start_session(
                        StartSessionRequest(task="first workdir", mock=True, max_turns=1, timeout=5, workdir=root_a)
                    ),
                    manager.start_session(
                        StartSessionRequest(task="second workdir", mock=True, max_turns=1, timeout=5, workdir=root_b)
                    ),
                )
                first_done, second_done = await asyncio.gather(
                    self._wait_for_terminal(manager, first.session_id),
                    self._wait_for_terminal(manager, second.session_id),
                )

            self.assertNotEqual(first.session_id, second.session_id)
            self.assertEqual(first_done.status, "done")
            self.assertEqual(second_done.status, "done")
            self.assertEqual(first_done.workdir, str(root_a.resolve()))
            self.assertEqual(second_done.workdir, str(root_b.resolve()))
            global_sessions = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(base / "home")}).session_dir
            self.assertEqual(Path(first_done.jsonl_path).parent, global_sessions)
            self.assertEqual(Path(second_done.jsonl_path).parent, global_sessions)
            self.assertEqual({state.session_id for state in manager.list_sessions()}, {first.session_id, second.session_id})

    async def test_default_log_dir_overrides_global_session_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom_log_dir = root / "custom-logs"
            manager = SessionManager(default_workdir=root, default_log_dir=custom_log_dir)

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(task="data log task", mock=True, max_turns=1, timeout=5, workdir=root)
                )
                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertEqual(final.status, "done")
            self.assertEqual(Path(final.jsonl_path).parent, custom_log_dir)
            self.assertTrue(Path(final.jsonl_path).exists())

    async def test_invalid_options_fail_before_session_state_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with self.assertRaises(StartOptionsError):
                    await manager.start_session(
                        StartSessionRequest(
                            task="bad options",
                            mock=True,
                            workdir=root,
                            codex_options={"reasoning_effort": "maximum"},
                        )
                    )

            self.assertEqual(manager.list_sessions(), [])

    async def test_stop_session_transitions_running_session_to_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(task="stop task", mock=True, max_turns=100, timeout=5, workdir=root)
                )
                first_batch = await manager.wait_events(state.session_id, 0, timeout_ms=1000)
                stopped = await manager.stop_session(state.session_id)

            self.assertGreater(first_batch.cursor, 0)
            self.assertEqual(stopped.status, "stopped")
            self.assertEqual(manager.get_session(state.session_id).status, "stopped")
            self.assertTrue(Path(stopped.jsonl_path).exists())

    async def test_lifecycle_logger_reports_start_and_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messages = []
            manager = SessionManager(lifecycle_logger=messages.append)

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(task="logging task", mock=True, max_turns=1, timeout=5, workdir=root)
                )
                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertEqual(final.status, "done")
            self.assertTrue(any(f"session {state.session_id} started" in message for message in messages))
            self.assertTrue(any(f"session {state.session_id} done" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
