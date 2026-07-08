import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.daemon import StartSessionRequest, SessionManager
from agent_collab.events import Event
from agent_collab.options import StartOptionsError
from agent_collab.paths import GlobalDataPaths
from agent_collab.referee import Referee


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
            global_sessions = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "home")}).session_dir
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

[workflows.antigravity-solo]
sequence = ["antigravity"]
""",
                encoding="utf-8",
            )
            manager = SessionManager()
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                state = await manager.start_session(
                    StartSessionRequest(
                        task="sdk mode task",
                        workflow="antigravity-solo",
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
                wait_task = asyncio.create_task(manager.wait_events(state.session_id, cursor, timeout_ms=1000))
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
            self.assertIn("please inspect this", Path(stopped.jsonl_path).read_text(encoding="utf-8"))
            self.assertIn("please inspect this", Path(stopped.markdown_path).read_text(encoding="utf-8"))

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
                batch = await manager.post_message(state.session_id, "what do you think?", target="codex")
                directed = await manager.wait_events(state.session_id, batch.cursor, timeout_ms=1000)
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
                    awaiting = await self._wait_for_status(manager, state.session_id, "awaiting_input")
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
                    StartSessionRequest(task="noninteractive task", mock=True, max_turns=1, timeout=5, workdir=root)
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
