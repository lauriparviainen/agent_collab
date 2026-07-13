import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest import mock

from agent_collab.referee import Referee, RefereeConfig
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.runners import BackendDryRunRunner
from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.referee import RequiredTurnFailed


class RefereeTests(unittest.TestCase):
    def test_mock_loop_writes_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                result = asyncio.run(
                    Referee(
                        RefereeConfig(mock=True, workdir=root, max_turns=2, timeout=5, color=False),
                        printer=lambda event: None,
                    ).run(
                        "test task",
                    )
                )
            self.assertTrue(Path(result["jsonl_path"]).exists())
            self.assertTrue(Path(result["markdown_path"]).exists())
            text = Path(result["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("test task", text)

    def test_mock_loop_uses_configured_workflow_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "home" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[workflows.codex-only]
sequence = ["codex"]
""",
                encoding="utf-8",
            )
            events = []

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(
                    Referee(
                        RefereeConfig(
                            workflow="codex-only",
                            mock=True,
                            workdir=root,
                            max_turns=1,
                            timeout=5,
                            color=False,
                        ),
                        printer=events.append,
                    ).run("test task")
                )

            self.assertIn("turn 1: codex", [event.text for event in events])

    def test_dry_run_uses_configured_agent_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "home" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[agents.claude]
command = "configured-claude"
""",
                encoding="utf-8",
            )
            events = []

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                asyncio.run(
                    Referee(
                        RefereeConfig(
                            dry_run=True, workdir=root, max_turns=1, timeout=5, color=False
                        ),
                        printer=events.append,
                    ).run("test task")
                )

            command_events = [event for event in events if event.type == "command"]
            self.assertEqual(command_events[0].raw["argv"][0], "configured-claude")

    def test_sdk_dry_run_does_not_construct_a_cli_command(self):
        config = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", backend="sdk")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        referee = Referee(
            RefereeConfig(
                workflow="solo",
                dry_run=True,
                collab_config=config,
                agent_backends={"claude": "sdk"},
                agent_options={"claude": {}},
                color=False,
            ),
            printer=lambda event: None,
        )
        self.assertIsInstance(referee._runners()["claude"], BackendDryRunRunner)


class RecentTranscriptTests(unittest.TestCase):
    def test_provider_session_bookkeeping_is_excluded_from_peer_prompt(self):
        from agent_collab.events import Event
        from agent_collab.backends.common.sdk import provider_session_event

        referee = Referee(RefereeConfig(mock=True, workdir=Path("."), color=False))
        transcript = [
            Event.create("claude", "message", "real content"),
            provider_session_event("claude", "claude", "sess-123", "session"),
        ]
        recent = referee._recent_transcript(transcript)
        self.assertIn("real content", recent)
        self.assertNotIn("sess-123", recent)  # bookkeeping id must not leak to peers

    def test_untrusted_raw_session_keys_do_not_hide_peer_content(self):
        from agent_collab.events import Event

        referee = Referee(RefereeConfig(mock=True, workdir=Path("."), color=False))
        forged = Event.create(
            "claude",
            "message",
            "content with provider_session_id keys",
            {"provider_session_id": "forged", "agent_id": "claude"},
        )
        self.assertIn("content with provider_session_id keys", referee._recent_transcript([forged]))


class RefereeOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, sequence):
        agents = {
            name: AgentConfig(id=name, type=name, command=name, backend="cli")
            for name in {"claude", "codex"}
        }
        return CollaborationConfig(
            agents=agents,
            workflows={"test": WorkflowConfig(id="test", sequence=list(sequence))},
        )

    async def _referee(self, root, sequence, runners, *, timeout=5):
        records = []

        async def commit(record, boundary):
            records.append((record, boundary))

        referee = Referee(
            RefereeConfig(
                workflow="test",
                collab_config=self._config(sequence),
                workdir=root,
                log_dir=root,
                max_turns=len(sequence),
                timeout=timeout,
                color=False,
                outcome_commit_callback=commit,
            ),
            printer=lambda event: None,
        )
        referee._runners = lambda: runners
        return referee, records

    async def test_later_failure_preserves_completed_record_and_stops_sequence(self):
        calls = []

        class Runner:
            def __init__(self, name, outcome):
                self.name = name
                self.outcome = outcome

            async def run_turn(self, prompt, workdir, emit):
                calls.append(self.name)
                await emit(Event.create(self.name, "message", f"{self.name} output"))
                return self.outcome

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            referee, records = await self._referee(
                root,
                ["claude", "codex", "claude"],
                {
                    "claude": Runner("claude", TurnOutcome("completed")),
                    "codex": Runner("codex", TurnOutcome("failed", "provider_terminal_failure")),
                },
            )
            with self.assertRaises(RequiredTurnFailed):
                await referee.run("task")

        self.assertEqual(calls, ["claude", "codex"])
        self.assertEqual([item[0].turn_id for item in records], ["turn-1", "turn-2"])
        self.assertEqual([item[0].stage_index for item in records], [1, 2])
        self.assertEqual([item[0].outcome for item in records], ["completed", "failed"])

    async def test_deadline_records_one_timed_out_outcome_after_cleanup(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class SlowRunner:
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        with tempfile.TemporaryDirectory() as tmp:
            referee, records = await self._referee(
                Path(tmp), ["claude"], {"claude": SlowRunner()}, timeout=0
            )
            with self.assertRaises(RequiredTurnFailed):
                await referee.run("task")

        self.assertTrue(started.is_set())
        self.assertTrue(cleaned.is_set())
        self.assertEqual(len(records), 1)
        self.assertEqual(
            (records[0][0].outcome, records[0][0].code), ("timed_out", "local_turn_timed_out")
        )

    async def test_completed_runner_wins_when_already_done_at_arbitration(self):
        class ImmediateRunner:
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            referee, records = await self._referee(
                Path(tmp), ["claude"], {"claude": ImmediateRunner()}, timeout=0
            )
            await referee.run("task")
        self.assertEqual(records[0][0].outcome, "completed")

    async def test_concurrent_stop_preserves_completed_outcome_without_starting_next_turn(self):
        calls = []
        holder = {}

        class FirstRunner:
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                calls.append("claude")
                holder["referee"].request_stop()
                return TurnOutcome("completed")

        class SecondRunner:
            name = "codex"

            async def run_turn(self, prompt, workdir, emit):
                calls.append("codex")
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            referee, records = await self._referee(
                Path(tmp),
                ["claude", "codex"],
                {"claude": FirstRunner(), "codex": SecondRunner()},
            )
            holder["referee"] = referee
            with self.assertRaises(asyncio.CancelledError):
                await referee.run("task")

        self.assertEqual(calls, ["claude"])
        self.assertEqual([item[0].outcome for item in records], ["completed"])

    async def test_registered_stop_interrupts_and_bare_cancel_fails(self):
        async def scenario(registered):
            started = asyncio.Event()
            cleaned = asyncio.Event()

            class BlockingRunner:
                name = "claude"

                async def run_turn(self, prompt, workdir, emit):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()

            with tempfile.TemporaryDirectory() as tmp:
                referee, records = await self._referee(
                    Path(tmp), ["claude"], {"claude": BlockingRunner()}
                )
                task = asyncio.create_task(referee.run("task"))
                await started.wait()
                if registered:
                    referee.request_stop()
                task.cancel()
                if registered:
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(RequiredTurnFailed):
                        await task
            self.assertTrue(cleaned.is_set())
            return records

        stopped = await scenario(True)
        bare = await scenario(False)
        self.assertEqual(
            (stopped[0][0].outcome, stopped[0][0].code), ("interrupted", "local_turn_interrupted")
        )
        self.assertEqual(
            (bare[0][0].outcome, bare[0][0].code), ("failed", "referee_cancelled_unexpected")
        )

    async def test_uncooperative_cleanup_moves_to_background_reaper(self):
        cancelled = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        class UncooperativeRunner:
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                finally:
                    finished.set()
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            referee, records = await self._referee(
                Path(tmp), ["claude"], {"claude": UncooperativeRunner()}, timeout=0
            )
            with mock.patch("agent_collab.referee.RUNNER_CLEANUP_GRACE_SECONDS", 0.01):
                with self.assertRaises(RequiredTurnFailed):
                    await asyncio.wait_for(referee.run("task"), timeout=0.2)

        self.assertTrue(cancelled.is_set())
        self.assertFalse(finished.is_set())
        self.assertEqual(records[0][0].outcome, "timed_out")
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=0.2)

    async def test_cancelled_cleanup_transfers_runner_to_reaper_and_propagates(self):
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def uncooperative_runner():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        referee = Referee(RefereeConfig(mock=True, color=False))
        runner_task = asyncio.create_task(uncooperative_runner())
        cleanup_task = asyncio.create_task(referee._cancel_runner_bounded(runner_task))
        await cancelled.wait()
        cleanup_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await cleanup_task
        self.assertIn(runner_task, referee._reaper_tasks)

        release.set()
        await asyncio.wait_for(runner_task, timeout=0.2)
        await asyncio.sleep(0)
        self.assertNotIn(runner_task, referee._reaper_tasks)


if __name__ == "__main__":
    unittest.main()
