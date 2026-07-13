import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest import mock

from agent_collab.referee import ParallelStageFailed, Referee, RefereeConfig
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


class ParallelRefereeTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, members, provider_types=None):
        provider_types = provider_types or {}
        agents = {}
        for agent_id in members:
            agent_type = provider_types.get(agent_id, agent_id)
            agents[agent_id] = AgentConfig(
                id=agent_id,
                type=agent_type,
                command=agent_type,
                backend="cli",
            )
        return CollaborationConfig(
            agents=agents,
            workflows={"parallel": WorkflowConfig(id="parallel", parallel=list(members))},
        )

    def _referee(
        self,
        root,
        members,
        runners,
        *,
        timeout=5,
        provider_types=None,
    ):
        records = []
        events = []
        turn_active = []

        async def commit(record, boundary):
            records.append((record, boundary))

        async def set_turn_active(active):
            turn_active.append(active)

        referee = Referee(
            RefereeConfig(
                workflow="parallel",
                collab_config=self._config(members, provider_types),
                workdir=root,
                log_dir=root,
                max_turns=3,
                timeout=timeout,
                color=False,
                outcome_commit_callback=commit,
                turn_active_callback=set_turn_active,
            ),
            printer=events.append,
        )
        referee._runners = lambda: runners
        return referee, records, events, turn_active

    async def test_three_members_run_concurrently_with_one_frozen_prompt(self):
        members = ["claude-primary", "claude-secondary", "codex"]
        provider_types = {
            "claude-primary": "claude",
            "claude-secondary": "claude",
            "codex": "codex",
        }
        started = set()
        all_started = asyncio.Event()
        release = asyncio.Event()
        prompts = {}

        class Runner:
            def __init__(self, agent_id, source):
                self.agent_id = agent_id
                self.source = source

            async def run_turn(self, prompt, workdir, emit):
                prompts[self.agent_id] = prompt
                started.add(self.agent_id)
                if len(started) == len(members):
                    all_started.set()
                await all_started.wait()
                await release.wait()
                await emit(
                    Event.create(
                        self.source,
                        "message",
                        f"{self.agent_id} review",
                        agent_id="forged",
                    )
                )
                return TurnOutcome("completed")

        runners = {agent_id: Runner(agent_id, provider_types[agent_id]) for agent_id in members}
        with tempfile.TemporaryDirectory() as tmp:
            referee, records, events, turn_active = self._referee(
                Path(tmp),
                members,
                runners,
                provider_types=provider_types,
            )
            task = asyncio.create_task(referee.run("review task"))
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
            self.assertFalse(task.done())
            self.assertEqual(turn_active, [True])
            release.set()
            await task

        self.assertEqual(len(set(prompts.values())), 1)
        self.assertIn("Reviewer agent:", next(iter(prompts.values())))
        self.assertEqual(turn_active, [True, False])
        self.assertEqual(
            {record.turn_id for record, _boundary in records},
            {"turn-1", "turn-2", "turn-3"},
        )
        self.assertTrue(all(record.stage_index == 1 for record, _boundary in records))
        self.assertTrue(all(boundary.agent_id == record.agent_id for record, boundary in records))

        messages = [event for event in events if event.agent_id in members]
        self.assertEqual({event.agent_id for event in messages}, set(members))
        self.assertNotIn("forged", {event.agent_id for event in messages})
        self.assertEqual(
            {event.agent_id for event in messages if event.source == "claude"},
            {"claude-primary", "claude-secondary"},
        )
        stage_start = next(event for event in events if "stage 1 (parallel):" in event.text)
        summary = next(
            event
            for event in events
            if isinstance(event.raw, dict) and event.raw.get("parallel") is True
        )
        self.assertIsNone(stage_start.agent_id)
        self.assertIsNone(summary.agent_id)
        self.assertEqual(summary.raw["stage"], 1)
        self.assertEqual(summary.raw["members"], {member: "completed" for member in members})
        self.assertEqual(summary.raw["accepted_members"], members)

    async def test_member_timeout_degrades_when_another_review_is_accepted(self):
        started = asyncio.Event()

        class FastRunner:
            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "fast review"))
                return TurnOutcome("completed")

        class SlowRunner:
            async def run_turn(self, prompt, workdir, emit):
                started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmp:
            referee, records, events, _turn_active = self._referee(
                Path(tmp),
                ["claude", "codex"],
                {"claude": FastRunner(), "codex": SlowRunner()},
                timeout=0,
            )
            await referee.run("review task")

        self.assertTrue(started.is_set())
        outcomes = {record.agent_id: record.outcome for record, _boundary in records}
        self.assertEqual(outcomes, {"claude": "completed", "codex": "timed_out"})
        summary = next(
            event
            for event in events
            if isinstance(event.raw, dict) and event.raw.get("parallel") is True
        )
        self.assertEqual(summary.raw["members"], outcomes)
        self.assertEqual(summary.raw["accepted_members"], ["claude"])

    async def test_all_member_timeouts_fail_with_committed_stage_outcomes(self):
        class SlowRunner:
            async def run_turn(self, prompt, workdir, emit):
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmp:
            referee, records, events, _turn_active = self._referee(
                Path(tmp),
                ["claude", "codex"],
                {"claude": SlowRunner(), "codex": SlowRunner()},
                timeout=0,
            )
            with self.assertRaises(ParallelStageFailed) as caught:
                await referee.run("review task")

        self.assertEqual(caught.exception.failure.code, "parallel_stage_no_accepted_member")
        outcomes = {record.agent_id: record.outcome for record, _boundary in records}
        self.assertEqual(outcomes, {"claude": "timed_out", "codex": "timed_out"})
        summary = next(
            event
            for event in events
            if isinstance(event.raw, dict) and event.raw.get("parallel") is True
        )
        self.assertEqual(summary.raw["members"], outcomes)
        self.assertEqual(summary.raw["accepted_members"], [])

    async def test_completed_turns_without_review_output_fail_the_stage(self):
        class EmptyRunner:
            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("error", "error", "no review"))
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            referee, records, events, _turn_active = self._referee(
                Path(tmp),
                ["claude", "codex"],
                {"claude": EmptyRunner(), "codex": EmptyRunner()},
            )
            with self.assertRaises(ParallelStageFailed) as caught:
                await referee.run("review task")

        self.assertEqual([record.outcome for record, _boundary in records], ["completed"] * 2)
        failure = caught.exception.failure.to_dict()
        self.assertEqual(failure["code"], "parallel_stage_no_accepted_member")
        self.assertEqual(failure["stage_index"], 1)
        self.assertIsNone(failure["turn_id"])
        self.assertIsNone(failure["agent_id"])
        self.assertIsNone(failure["backend"])
        summary = next(
            event
            for event in events
            if isinstance(event.raw, dict) and event.raw.get("parallel") is True
        )
        self.assertEqual(summary.raw["accepted_members"], [])

    async def test_whitespace_only_message_is_not_accepted(self):
        class Runner:
            def __init__(self, source, text):
                self.source = source
                self.text = text

            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create(self.source, "message", self.text))
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            referee, _records, events, _turn_active = self._referee(
                Path(tmp),
                ["claude", "codex"],
                {
                    "claude": Runner("claude", "  \n\t"),
                    "codex": Runner("codex", "real review"),
                },
            )
            await referee.run("review task")

        summary = next(
            event
            for event in events
            if isinstance(event.raw, dict) and event.raw.get("parallel") is True
        )
        self.assertEqual(summary.raw["members"], {"claude": "completed", "codex": "completed"})
        self.assertEqual(summary.raw["accepted_members"], ["codex"])

    async def test_failed_partial_message_is_not_accepted(self):
        class Runner:
            def __init__(self, source, outcome):
                self.source = source
                self.outcome = outcome

            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create(self.source, "message", "partial review"))
                return self.outcome

        with tempfile.TemporaryDirectory() as tmp:
            referee, _records, events, _turn_active = self._referee(
                Path(tmp),
                ["claude", "codex"],
                {
                    "claude": Runner("claude", TurnOutcome("failed", "provider_terminal_failure")),
                    "codex": Runner("codex", TurnOutcome("completed")),
                },
            )
            await referee.run("review task")

        summary = next(
            event
            for event in events
            if isinstance(event.raw, dict) and event.raw.get("parallel") is True
        )
        self.assertEqual(summary.raw["accepted_members"], ["codex"])

    async def test_registered_stop_interrupts_and_settles_every_member(self):
        started = {"claude": asyncio.Event(), "codex": asyncio.Event()}
        cleaned = {"claude": asyncio.Event(), "codex": asyncio.Event()}

        class BlockingRunner:
            def __init__(self, agent_id):
                self.agent_id = agent_id

            async def run_turn(self, prompt, workdir, emit):
                started[self.agent_id].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned[self.agent_id].set()

        with tempfile.TemporaryDirectory() as tmp:
            referee, records, _events, turn_active = self._referee(
                Path(tmp),
                ["claude", "codex"],
                {
                    "claude": BlockingRunner("claude"),
                    "codex": BlockingRunner("codex"),
                },
            )
            task = asyncio.create_task(referee.run("review task"))
            await asyncio.gather(*(event.wait() for event in started.values()))
            referee.request_stop()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(all(event.is_set() for event in cleaned.values()))
        self.assertEqual(turn_active, [True, False])
        outcomes = {record.agent_id: record.outcome for record, _boundary in records}
        self.assertEqual(outcomes, {"claude": "interrupted", "codex": "interrupted"})

    async def test_sequential_prompts_and_order_remain_unchanged(self):
        calls = []
        prompts = []
        emitted = []

        class Runner:
            def __init__(self, source):
                self.source = source

            async def run_turn(self, prompt, workdir, emit):
                calls.append(self.source)
                prompts.append(prompt)
                await emit(
                    Event.create(
                        self.source,
                        "message",
                        f"{self.source} output",
                        agent_id="forged",
                    )
                )
                return TurnOutcome("completed")

        config = CollaborationConfig(
            agents={
                name: AgentConfig(id=name, type=name, command=name, backend="cli")
                for name in ("claude", "codex")
            },
            workflows={"sequence": WorkflowConfig(id="sequence", sequence=["claude", "codex"])},
        )
        with tempfile.TemporaryDirectory() as tmp:
            referee = Referee(
                RefereeConfig(
                    workflow="sequence",
                    collab_config=config,
                    workdir=Path(tmp),
                    log_dir=Path(tmp),
                    max_turns=2,
                    timeout=5,
                    color=False,
                ),
                printer=emitted.append,
            )
            referee._runners = lambda: {
                "claude": Runner("claude"),
                "codex": Runner("codex"),
            }
            self.assertEqual(referee._stages(), [["claude"], ["codex"]])
            await referee.run("task")

        self.assertEqual(calls, ["claude", "codex"])
        self.assertIn("Lead agent:", prompts[0])
        self.assertIn("Reviewer agent:", prompts[1])
        messages = [event for event in emitted if event.source in {"claude", "codex"}]
        self.assertEqual([event.agent_id for event in messages], ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
