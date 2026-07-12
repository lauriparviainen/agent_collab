import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.daemon import StartSessionRequest, SessionManager, _ManagedSession
from agent_collab.events import Event
from agent_collab.options import StartOptionsError
from agent_collab.paths import GlobalDataPaths
from agent_collab.referee import Referee
from agent_collab.session_index import SessionIndex


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

    async def _wait_for_status(self, manager, session_id, expected, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            state = manager.get_session(session_id)
            if state.status == expected:
                return state
            await asyncio.sleep(0.02)
        self.fail(f"session {session_id} did not reach {expected!r} before timeout")

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
            global_sessions = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "home")}
            ).session_dir
            self.assertEqual(Path(final.jsonl_path).parent, global_sessions)
            self.assertTrue(Path(final.jsonl_path).exists())
            self.assertTrue(Path(final.markdown_path).exists())

            batch = manager.read_events(state.session_id, 0)
            self.assertEqual(batch.cursor, len(batch.events))
            self.assertGreater(batch.cursor, 0)
            self.assertEqual(batch.events[0]["source"], "human")
            self.assertIn("daemon mock task", batch.events[0]["text"])

            self.assertEqual(state.workflow, "cross-review")
            self.assertEqual(state.settings["workflow"]["name"], "cross-review")
            self.assertEqual(state.settings["workflow"]["sequence"], ["claude", "codex", "claude"])
            self.assertFalse(state.interactive)
            self.assertFalse(state.settings["interactive"])
            self.assertEqual(final.settings, state.settings)
            for agent in state.settings["agents"].values():
                for part in agent.get("command_preview", []):
                    self.assertNotIn("daemon mock task", part)

    async def test_tool_output_defaults_to_summary_and_supports_single_full_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="projection task", mock=True, max_turns=1, timeout=5, workdir=root
                    )
                )
                await self._wait_for_terminal(manager, state.session_id)

            full = manager.read_events(state.session_id, 0, tool_output="full")
            summary = manager.read_events(state.session_id, 0)
            tool_id = next(
                index for index, event in enumerate(full.events) if event["source"] == "tool"
            )
            self.assertEqual(summary.events[tool_id]["raw"], None)
            self.assertIn(f"[event {tool_id}]", summary.events[tool_id]["text"])
            self.assertIn("result", summary.events[tool_id]["text"])

            refetched = manager.read_events(state.session_id, tool_id, limit=1, tool_output="full")
            self.assertEqual(refetched.cursor, tool_id + 1)
            self.assertEqual(refetched.events, [full.events[tool_id]])

            summary_transcript = manager.read_transcript(state.session_id)
            full_transcript = manager.read_transcript(state.session_id, tool_output="full")
            self.assertIn(f"[event {tool_id}]", summary_transcript)
            self.assertNotIn("[event", full_transcript)
            self.assertIn("inspects repository state", full_transcript)

    async def test_config_is_loaded_once_and_snapshot_carried_into_execution(self):
        # Guards the start/run divergence: start_session validates a config
        # snapshot and _run_session must reuse it, not reload (which could resolve
        # a different agent type/backend than the start response advertised).
        import agent_collab.daemon as daemon_module

        calls = []
        real_load_config = daemon_module.load_config

        def counting_load_config(*args, **kwargs):
            calls.append(1)
            return real_load_config(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(daemon_module, "load_config", counting_load_config):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="snapshot task", mock=True, max_turns=1, timeout=5, workdir=root
                        )
                    )
                    await self._wait_for_terminal(manager, state.session_id)

            # Exactly one load at start; execution reused the carried snapshot.
            self.assertEqual(sum(calls), 1)

    async def test_sdk_backend_selection_is_not_blocked_by_inferred_cli_mode(self):
        # Regression: the built-in antigravity agent carries `--mode accept-edits`
        # (cli posture); selecting backend="sdk" must reach runner construction
        # rather than being rejected by the inferred mode. mock skips health so
        # this exercises the validation path end to end.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / ".agent-collab" / "config.toml"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                """
[agents.antigravity]
enabled = true
backend = "sdk"

[workflows.solo-antigravity]
sequence = ["antigravity"]
""",
                encoding="utf-8",
            )
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="sdk mode task",
                        workflow="solo-antigravity",
                        mock=True,
                        max_turns=1,
                        timeout=5,
                        workdir=root,
                    )
                )
                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertEqual(final.status, "done")
            self.assertEqual(final.settings["agents"]["antigravity"]["backend"], "sdk")

    async def test_interactive_session_awaits_input_and_post_message_appends_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="interactive note task",
                        mock=True,
                        max_turns=1,
                        timeout=5,
                        workdir=root,
                        interactive=True,
                        interactive_idle_timeout=5,
                    )
                )
                awaiting = await self._wait_for_status(manager, state.session_id, "awaiting_input")
                awaiting_events = manager.read_events(state.session_id, 0).events
                cursor = manager.read_events(state.session_id, 0).cursor
                wait_task = asyncio.create_task(
                    manager.wait_events(state.session_id, cursor, timeout_ms=1000)
                )
                batch = await manager.post_message(state.session_id, " please inspect this ")
                waited = await wait_task
                stopped = await manager.stop_session(state.session_id)

            self.assertEqual(awaiting.status, "awaiting_input")
            self.assertTrue(awaiting.interactive)
            self.assertTrue(awaiting.settings["interactive"])
            self.assertFalse(any("final summary" in event["text"] for event in awaiting_events))
            self.assertEqual(batch.session_id, state.session_id)
            self.assertEqual(batch.events[0]["source"], "referee")
            self.assertEqual(batch.events[0]["text"], "please inspect this")
            self.assertEqual(batch.events[0]["raw"]["target"], None)
            self.assertEqual(batch.events[0]["raw"]["resolved_target"], None)
            self.assertEqual(waited.events, batch.events)
            all_events = manager.read_events(state.session_id, 0).events
            self.assertEqual(
                [event["text"] for event in all_events].count("please inspect this"),
                1,
            )
            self.assertIn(
                "please inspect this", Path(stopped.jsonl_path).read_text(encoding="utf-8")
            )
            self.assertIn(
                "please inspect this", Path(stopped.markdown_path).read_text(encoding="utf-8")
            )

    async def test_stop_session_transitions_awaiting_input_to_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="stop awaiting task",
                        mock=True,
                        max_turns=0,
                        timeout=5,
                        workdir=root,
                        interactive=True,
                        interactive_idle_timeout=5,
                    )
                )
                awaiting = await self._wait_for_status(manager, state.session_id, "awaiting_input")
                stopped = await manager.stop_session(state.session_id)

            self.assertEqual(awaiting.status, "awaiting_input")
            self.assertEqual(stopped.status, "stopped")

    async def test_interactive_directed_message_runs_one_target_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="interactive directed task",
                        mock=True,
                        max_turns=0,
                        timeout=5,
                        workdir=root,
                        interactive=True,
                        interactive_idle_timeout=5,
                    )
                )
                await self._wait_for_status(manager, state.session_id, "awaiting_input")
                before = manager.read_events(state.session_id, 0).cursor
                batch = await manager.post_message(
                    state.session_id, "what do you think?", target="codex"
                )
                directed = await manager.wait_events(
                    state.session_id, batch.cursor, timeout_ms=1000
                )
                await manager.stop_session(state.session_id)

            self.assertGreaterEqual(batch.cursor, before + 1)
            self.assertEqual(batch.events[0]["raw"]["target"], "codex")
            self.assertEqual(batch.events[0]["raw"]["resolved_target"], "codex")
            texts = [event["text"] for event in directed.events]
            self.assertIn("directed turn: codex", texts)
            self.assertEqual(texts.count("directed turn: codex"), 1)
            self.assertEqual(sum("mock codex received prompt" in text for text in texts), 1)

    async def test_mid_turn_referee_note_is_queued_and_visible_to_next_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()
            prompts = []
            first_turn_started = asyncio.Event()
            release_first_turn = asyncio.Event()
            second_turn_started = asyncio.Event()

            class CaptureRunner:
                def __init__(self, name, pause=False):
                    self.name = name
                    self.pause = pause

                async def run(self, prompt, workdir):
                    prompts.append((self.name, prompt))
                    if self.pause:
                        first_turn_started.set()
                        yield Event.create(self.name, "status", f"{self.name} started")
                        await release_first_turn.wait()
                    else:
                        second_turn_started.set()
                    yield Event.create(self.name, "message", f"{self.name} done")

            runners = {
                "claude": CaptureRunner("claude", pause=True),
                "codex": CaptureRunner("codex"),
            }

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                with mock.patch.object(Referee, "_runners", return_value=runners):
                    state = await manager.start_session(
                        StartSessionRequest(
                            task="queued note task",
                            workflow="compare",
                            max_turns=2,
                            timeout=5,
                            workdir=root,
                            interactive=True,
                            interactive_idle_timeout=5,
                        )
                    )
                    await asyncio.wait_for(first_turn_started.wait(), timeout=1)
                    batch = await manager.post_message(state.session_id, "remember mid-turn note")
                    release_first_turn.set()
                    await asyncio.wait_for(second_turn_started.wait(), timeout=1)
                    awaiting = await self._wait_for_status(
                        manager, state.session_id, "awaiting_input"
                    )
                    await manager.stop_session(state.session_id)

            self.assertEqual(batch.events[0]["raw"]["queued"], True)
            self.assertEqual(awaiting.status, "awaiting_input")
            self.assertGreaterEqual(len(prompts), 2)
            self.assertEqual(prompts[1][0], "codex")
            self.assertIn("remember mid-turn note", prompts[1][1])

    async def test_post_message_rejects_unknown_and_ambiguous_targets_in_manager_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".agent-collab" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[agents.claude-a]
type = "claude"
command = "claude"

[agents.claude-b]
type = "claude"
command = "claude"

[workflows.two-claudes]
sequence = ["claude-a", "claude-b"]
""",
                encoding="utf-8",
            )
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="target validation task",
                        workflow="two-claudes",
                        mock=True,
                        max_turns=0,
                        timeout=5,
                        workdir=root,
                        interactive=True,
                        interactive_idle_timeout=5,
                    )
                )
                await self._wait_for_status(manager, state.session_id, "awaiting_input")
                cursor = manager.read_events(state.session_id, 0).cursor

                with self.assertRaises(ValueError) as unknown_ctx:
                    await manager.post_message(state.session_id, "hello", target="reviewer")
                with self.assertRaises(ValueError) as ambiguous_ctx:
                    await manager.post_message(state.session_id, "hello", target="claude")

                await manager.stop_session(state.session_id)

            self.assertIn("unknown target", str(unknown_ctx.exception))
            self.assertIn("claude-a", str(unknown_ctx.exception))
            self.assertIn("ambiguous agent type", str(ambiguous_ctx.exception))
            self.assertIn("claude-b", str(ambiguous_ctx.exception))
            self.assertEqual(manager.read_events(state.session_id, 0).cursor, cursor)

    async def test_post_message_rejects_noninteractive_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="noninteractive task", mock=True, max_turns=1, timeout=5, workdir=root
                    )
                )
                with self.assertRaises(ValueError) as ctx:
                    await manager.post_message(state.session_id, "hello")
                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertIn("interactive", str(ctx.exception))
            self.assertEqual(final.status, "done")

    async def test_interactive_idle_timeout_transitions_to_done_with_visible_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="idle timeout task",
                        mock=True,
                        max_turns=0,
                        timeout=5,
                        workdir=root,
                        interactive=True,
                        interactive_idle_timeout=0.05,
                    )
                )
                final = await self._wait_for_terminal(manager, state.session_id)
                events = manager.read_events(state.session_id, 0).events

            self.assertEqual(final.status, "done")
            self.assertTrue(any("interactive idle timeout" in event["text"] for event in events))
            self.assertTrue(any("final summary" in event["text"] for event in events))

    async def test_read_events_uses_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="cursor task", mock=True, max_turns=1, timeout=5, workdir=root
                    )
                )
                await self._wait_for_terminal(manager, state.session_id)

            all_events = manager.read_events(state.session_id, 0)
            tail = manager.read_events(state.session_id, 1)
            empty = manager.read_events(state.session_id, all_events.cursor)

            self.assertEqual(tail.cursor, all_events.cursor)
            self.assertEqual(tail.events, all_events.events[1:])
            self.assertEqual(empty.cursor, all_events.cursor)
            self.assertEqual(empty.events, [])

    async def test_rapid_events_coalesce_into_one_notification(self):
        """A burst of recorded events schedules one notify task, not one each.

        The recorded events themselves are visible to woken watchers because
        they are appended before the notification is scheduled.
        """
        manager = SessionManager()
        # _record_event only touches events, provider-session capture (a no-op
        # for plain events), and notification scheduling, so no session state
        # is needed here.
        managed = _ManagedSession(
            request=None, state=None, events=[], condition=asyncio.Condition()
        )

        woke = asyncio.Event()

        async def waiter():
            async with managed.condition:
                await managed.condition.wait()
            woke.set()

        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)  # let the waiter start waiting on the condition

        for index in range(5):
            manager._record_event(managed, Event.create("referee", "status", f"burst {index}"))

        self.assertEqual(len(manager._notify_tasks), 1)
        self.assertTrue(managed.notify_pending)

        await asyncio.wait_for(woke.wait(), timeout=1.0)
        await asyncio.wait_for(waiter_task, timeout=1.0)
        self.assertEqual(len(managed.events), 5)
        self.assertFalse(managed.notify_pending)

        # After delivery, the next event schedules a fresh notification.
        manager._record_event(managed, Event.create("referee", "status", "after burst"))
        self.assertTrue(managed.notify_pending)
        await asyncio.gather(*manager._notify_tasks)

    async def test_wait_events_returns_new_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="wait task", mock=True, max_turns=2, timeout=5, workdir=root
                    )
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
                        StartSessionRequest(
                            task="first workdir", mock=True, max_turns=1, timeout=5, workdir=root_a
                        )
                    ),
                    manager.start_session(
                        StartSessionRequest(
                            task="second workdir", mock=True, max_turns=1, timeout=5, workdir=root_b
                        )
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
            global_sessions = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(base / "home")}
            ).session_dir
            self.assertEqual(Path(first_done.jsonl_path).parent, global_sessions)
            self.assertEqual(Path(second_done.jsonl_path).parent, global_sessions)
            self.assertEqual(
                {state.session_id for state in manager.list_sessions()},
                {first.session_id, second.session_id},
            )

    async def test_default_log_dir_overrides_global_session_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom_log_dir = root / "custom-logs"
            manager = SessionManager(default_workdir=root, default_log_dir=custom_log_dir)

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="data log task", mock=True, max_turns=1, timeout=5, workdir=root
                    )
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
                            backend_options={"codex_cli": {"reasoning_effort": "maximum"}},
                        )
                    )

            self.assertEqual(manager.list_sessions(), [])

    async def test_disabled_backend_fails_before_session_state_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (home / "config.toml").write_text(
                "schema_version = 4\n[backends.claude_cli]\nenabled = false\n",
                encoding="utf-8",
            )
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}):
                with self.assertRaises(StartOptionsError) as ctx:
                    await manager.start_session(
                        StartSessionRequest(
                            task="disabled backend",
                            workflow="solo-claude",
                            mock=True,
                            workdir=root,
                        )
                    )
            detail = ctx.exception.to_dict()["details"][0]
            self.assertEqual(detail["code"], "backend_disabled")
            self.assertEqual(detail["canonical_backend"], "claude_cli")
            self.assertEqual(manager.list_sessions(), [])

    async def test_stop_session_transitions_running_session_to_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="stop task", mock=True, max_turns=100, timeout=5, workdir=root
                    )
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
                    StartSessionRequest(
                        task="logging task", mock=True, max_turns=1, timeout=5, workdir=root
                    )
                )
                final = await self._wait_for_terminal(manager, state.session_id)

            self.assertEqual(final.status, "done")
            self.assertTrue(
                any(f"session {state.session_id} started" in message for message in messages)
            )
            self.assertTrue(
                any(f"session {state.session_id} done" in message for message in messages)
            )


class SessionManagerPruneTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
    RETENTION = timedelta(days=30)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.session_dir = root / "sessions"
        self.session_dir.mkdir()
        self.index_path = root / "session-index.json"
        self.index = SessionIndex(self.index_path)

    def _add_record(
        self,
        session_id,
        *,
        days_ago=60,
        status="done",
        write_files=True,
        jsonl_path=None,
        markdown_path=None,
        ended_at="unset",
        updated_at="unset",
    ):
        timestamp = (self.NOW - timedelta(days=days_ago)).isoformat()
        record = {
            "session_id": session_id,
            "status": status,
            "task": "old task",
            "workflow": "cross-review",
            "workdir": str(Path(self._tmp.name)),
            "jsonl_path": jsonl_path or str(self.session_dir / f"{session_id}.jsonl"),
            "markdown_path": markdown_path or str(self.session_dir / f"{session_id}.md"),
            "created_at": timestamp,
            "updated_at": timestamp if updated_at == "unset" else updated_at,
            "ended_at": timestamp if ended_at == "unset" else ended_at,
        }
        self.index.upsert(record)
        if write_files:
            for key in ("jsonl_path", "markdown_path"):
                path = Path(record[key])
                if path.parent == self.session_dir:
                    path.write_text("x" * 10, encoding="utf-8")
        return record

    def _manager(self):
        return SessionManager(index_path=self.index_path, default_log_dir=self.session_dir)

    async def _prune(self, manager, apply, keep=0):
        return await manager.prune_sessions(
            apply=apply, retention=self.RETENTION, keep=keep, now=self.NOW
        )

    async def test_preview_mutates_nothing_and_reports_candidates(self):
        self._add_record("old-1")
        self._add_record("old-2")
        self._add_record("fresh", days_ago=1)
        manager = self._manager()

        result = await self._prune(manager, apply=False)

        self.assertFalse(result.apply)
        self.assertEqual(result.candidates, 2)
        self.assertEqual(result.pruned, 0)
        self.assertGreater(result.bytes_reclaimed, 0)
        self.assertEqual(
            sorted(d.session_id for d in result.sessions if d.disposition == "preview"),
            ["old-1", "old-2"],
        )
        self.assertTrue((self.session_dir / "old-1.jsonl").exists())
        self.assertEqual(len(self.index.load()), 3)
        self.assertEqual(len(manager.list_sessions()), 3)

    async def test_apply_removes_files_index_records_and_registry(self):
        self._add_record("old-1")
        self._add_record("fresh", days_ago=1)
        manager = self._manager()

        result = await self._prune(manager, apply=True)

        self.assertEqual(result.pruned, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.bytes_reclaimed, 20)
        self.assertFalse((self.session_dir / "old-1.jsonl").exists())
        self.assertFalse((self.session_dir / "old-1.md").exists())
        self.assertEqual(sorted(self.index.load()), ["fresh"])
        self.assertEqual([s.session_id for s in manager.list_sessions()], ["fresh"])

    async def test_every_terminal_status_is_pruned_and_live_is_not(self):
        for status in ("done", "failed", "stopped", "interrupted"):
            self._add_record(f"old-{status}", status=status)
        self._add_record("still-running", status="done")
        manager = self._manager()
        manager._sessions["still-running"].state.status = "running"

        result = await self._prune(manager, apply=True)

        self.assertEqual(result.pruned, 4)
        self.assertIn("still-running", self.index.load())
        self.assertNotIn("still-running", [detail.session_id for detail in result.sessions])

    async def test_terminal_record_with_live_task_is_skipped(self):
        self._add_record("racing")
        manager = self._manager()
        blocker = asyncio.create_task(asyncio.sleep(30))
        self.addCleanup(blocker.cancel)
        manager._sessions["racing"].task = blocker

        result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "racing")
        self.assertEqual(detail.disposition, "skipped_live")
        self.assertIn("racing", self.index.load())
        self.assertTrue((self.session_dir / "racing.jsonl").exists())

    async def test_custom_log_dir_files_are_preserved_but_record_is_removed(self):
        external = Path(self._tmp.name).resolve() / "elsewhere"
        external.mkdir()
        external_jsonl = external / "custom.jsonl"
        external_jsonl.write_text("external", encoding="utf-8")
        self._add_record(
            "custom",
            jsonl_path=str(external_jsonl),
            markdown_path=str(external / "custom.md"),
            write_files=False,
        )
        manager = self._manager()

        result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "custom")
        self.assertEqual(detail.disposition, "pruned")
        self.assertEqual(detail.removed_files, [])
        self.assertEqual(len(detail.preserved_files), 2)
        self.assertTrue(external_jsonl.exists())
        self.assertNotIn("custom", self.index.load())

    async def test_missing_files_count_as_already_absent(self):
        self._add_record("ghost", write_files=False)
        manager = self._manager()

        result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "ghost")
        self.assertEqual(detail.disposition, "pruned")
        self.assertEqual(detail.removed_files, [])
        self.assertEqual(detail.bytes_reclaimed, 0)
        self.assertNotIn("ghost", self.index.load())

    async def test_symlinked_transcript_is_preserved_and_reported(self):
        self._add_record("linked", write_files=False)
        target = self.session_dir / "target-data.jsonl"
        target.write_text("precious", encoding="utf-8")
        (self.session_dir / "linked.jsonl").symlink_to(target)
        (self.session_dir / "linked.md").write_text("x", encoding="utf-8")
        manager = self._manager()

        result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "linked")
        self.assertEqual(detail.disposition, "pruned")
        self.assertIn(("symlink"), [entry["reason"] for entry in detail.preserved_files])
        self.assertTrue((self.session_dir / "linked.jsonl").exists())
        self.assertTrue(target.exists())
        self.assertFalse((self.session_dir / "linked.md").exists())

    async def test_unlink_failure_keeps_record_and_next_run_converges(self):
        import agent_collab.daemon as daemon_module

        self._add_record("stubborn")
        manager = self._manager()
        real_unlink = os.unlink

        def failing_unlink(path, *args, **kwargs):
            if "stubborn" in str(path):
                raise PermissionError("simulated unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(daemon_module.os, "unlink", side_effect=failing_unlink):
            result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "stubborn")
        self.assertEqual(detail.disposition, "failed")
        self.assertIn("simulated unlink failure", detail.error)
        self.assertIn("stubborn", self.index.load())
        self.assertIn("stubborn", [s.session_id for s in manager.list_sessions()])

        retry = await self._prune(manager, apply=True)

        self.assertEqual(retry.pruned, 1)
        self.assertNotIn("stubborn", self.index.load())

    async def test_index_rewrite_failure_reports_failed_and_next_run_converges(self):
        self._add_record("half-done")
        manager = self._manager()

        with mock.patch.object(SessionIndex, "remove_many", side_effect=OSError("disk full")):
            result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "half-done")
        self.assertEqual(detail.disposition, "failed")
        self.assertIn("disk full", detail.error)
        # Files are already gone, but the record survives for the retry.
        self.assertFalse((self.session_dir / "half-done.jsonl").exists())
        self.assertIn("half-done", self.index.load())
        self.assertIn("half-done", [s.session_id for s in manager.list_sessions()])

        retry = await self._prune(manager, apply=True)

        self.assertEqual(retry.pruned, 1)
        self.assertNotIn("half-done", self.index.load())

    async def test_keep_protects_newest_and_reports_kept(self):
        self._add_record("old-1", days_ago=100)
        self._add_record("old-2", days_ago=90)
        manager = self._manager()

        result = await self._prune(manager, apply=True, keep=1)

        kept = [d.session_id for d in result.sessions if d.disposition == "kept"]
        self.assertEqual(kept, ["old-2"])
        self.assertEqual(result.pruned, 1)
        self.assertIn("old-2", self.index.load())
        self.assertNotIn("old-1", self.index.load())

    async def test_unusable_timestamps_are_skipped_and_preserved(self):
        self._add_record("no-clock", ended_at="junk", updated_at="also junk")
        manager = self._manager()

        result = await self._prune(manager, apply=True)

        detail = next(d for d in result.sessions if d.session_id == "no-clock")
        self.assertEqual(detail.disposition, "skipped_no_timestamp")
        self.assertIn("no-clock", self.index.load())
        self.assertTrue((self.session_dir / "no-clock.jsonl").exists())

    async def test_unparseable_index_records_are_counted_not_deleted(self):
        self._add_record("old-1")
        # A record without a status fails restoration and stays invisible.
        self.index.upsert({"session_id": "garbage", "task": "??"})
        manager = self._manager()

        result = await self._prune(manager, apply=True)

        self.assertEqual(result.unparseable_records, 1)
        self.assertIn("garbage", self.index.load())

    async def test_manual_and_concurrent_prunes_serialize(self):
        self._add_record("old-1")
        self._add_record("old-2")
        manager = self._manager()

        results = await asyncio.gather(
            self._prune(manager, apply=True), self._prune(manager, apply=True)
        )

        self.assertEqual(sorted(r.pruned for r in results), [0, 2])
        self.assertEqual(self.index.load(), {})


if __name__ == "__main__":
    unittest.main()
