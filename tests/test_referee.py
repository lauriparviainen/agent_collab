import os
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest import mock

from agent_collab.referee import ParallelStageFailed, Referee, RefereeConfig, RefereeInput
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.runners import AgentRunner, BackendDryRunRunner
from agent_collab.events import Event
from agent_collab.logging import SessionLogger
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
schema_version = 8

[workflows.codex-only]
sequence = ["codex_cli"]
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

            self.assertIn("turn 1: codex_cli", [event.text for event in events])

    def test_dry_run_uses_configured_agent_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "home" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
schema_version = 8

[backends.claude_cli]
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

        class Runner(AgentRunner):
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

        class SlowRunner(AgentRunner):
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
        class ImmediateRunner(AgentRunner):
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

        class FirstRunner(AgentRunner):
            name = "claude"

            async def run_turn(self, prompt, workdir, emit):
                calls.append("claude")
                holder["referee"].request_stop()
                return TurnOutcome("completed")

        class SecondRunner(AgentRunner):
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

            class BlockingRunner(AgentRunner):
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

        class UncooperativeRunner(AgentRunner):
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

        class Runner(AgentRunner):
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

        class FastRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "fast review"))
                return TurnOutcome("completed")

        class SlowRunner(AgentRunner):
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
        class SlowRunner(AgentRunner):
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
        class EmptyRunner(AgentRunner):
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
        class Runner(AgentRunner):
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
        class Runner(AgentRunner):
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

        class BlockingRunner(AgentRunner):
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

        class Runner(AgentRunner):
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

    async def test_parallel_stage_advances_member_watermarks_to_snapshot(self):
        # The prompt-snapshot invariant must hold for parallel builds too: each
        # member's watermark advances to the build-time snapshot length (not 0,
        # not the post-stage length), so a later continuation turn for that
        # member would start its delta after the shared parallel prompt rather
        # than re-sending pre-stage events like the task.
        class Runner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "review"))
                return TurnOutcome("completed")

        members = ["claude", "codex"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            referee, *_ = self._referee(root, members, {m: Runner() for m in members})
            transcript = [
                Event.create("human", "message", "task"),
                Event.create("referee", "status", "workflow=..."),
            ]
            with SessionLogger(root, "task") as logger:
                await referee._run_parallel_stage(
                    logger, transcript, {m: Runner() for m in members}, "task", members, 1
                )
            for member in members:
                # == len(snapshot) captured at stage start (two pre-seeded events).
                self.assertEqual(referee._agent_watermarks[member], 2)
                self.assertLess(referee._agent_watermarks[member], len(transcript))


class InteractiveAcceptingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_timeout_clears_accepting_before_closing_emit(self):
        # The idle-timeout branch awaits an emit before returning; input_accepting
        # must already be false by then, or a post landing during that await would
        # enqueue input the closing loop never consumes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            order = []

            async def status_cb(status):
                order.append(("status", status))

            async def accepting_cb(value):
                order.append(("accepting", value))

            def printer(event):
                order.append(("event", event.text))

            config = RefereeConfig(
                mock=True,
                workdir=root,
                max_turns=0,
                timeout=5,
                color=False,
                interactive=True,
                interactive_idle_timeout=0.05,
                input_queue=asyncio.Queue(),
                status_callback=status_cb,
                input_accepting_callback=accepting_cb,
            )
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                await Referee(config, printer=printer).run("idle task")

        idle_index = next(
            index
            for index, item in enumerate(order)
            if item[0] == "event" and "interactive idle timeout" in item[1]
        )
        self.assertIn(("accepting", True), order[:idle_index])
        # accepting is cleared before the closing idle-timeout status is emitted.
        self.assertIn(("accepting", False), order[:idle_index])
        # awaiting_input is still the announced status at that point (not yet done).
        self.assertNotIn(("status", "done"), order[:idle_index])


class UntargetedRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Stage 2: an untargeted post runs a directed turn of the sole agent in a
    solo session; multi-agent sessions keep the append-only behavior."""

    def _config(self, sequence):
        agents = {
            name: AgentConfig(id=name, type=name, command=name, backend="cli")
            for name in {"claude", "codex"}
        }
        return CollaborationConfig(
            agents=agents,
            workflows={"test": WorkflowConfig(id="test", sequence=list(sequence))},
        )

    def _referee(self, root, sequence):
        return Referee(
            RefereeConfig(
                workflow="test",
                collab_config=self._config(sequence),
                workdir=root,
                log_dir=root,
                max_turns=len(sequence),
                timeout=5,
                color=False,
            ),
            printer=lambda event: None,
        )

    async def _process_untargeted(self, referee, root, runners):
        transcript = []
        item = RefereeInput(event=Event.create("referee", "message", "ping"), target=None)
        with SessionLogger(root, "task") as logger:
            return await referee._process_input_item(logger, transcript, runners, "task", item)

    async def test_untargeted_solo_runs_the_single_agent(self):
        calls = []

        class Runner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                calls.append(prompt)
                await emit(Event.create("claude", "message", "answer"))
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            referee = self._referee(root, ["claude"])
            record = await self._process_untargeted(referee, root, {"claude": Runner()})

        self.assertIsNotNone(record)
        self.assertEqual(record.agent_id, "claude")
        self.assertEqual(record.outcome, "completed")
        self.assertEqual(len(calls), 1)
        # The turn is directed: the prompt carries the posted text, not a TASK block.
        self.assertIn("ping", calls[0])

    async def test_untargeted_multi_agent_is_append_only(self):
        ran = []

        class Runner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                ran.append(prompt)
                return TurnOutcome("completed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            referee = self._referee(root, ["claude", "codex"])
            record = await self._process_untargeted(
                referee, root, {"claude": Runner(), "codex": Runner()}
            )

        self.assertIsNone(record)
        self.assertEqual(ran, [])


class _StubRunner(AgentRunner):
    """A runner whose provider-context and close behavior a test controls."""

    def __init__(self, *, active=False):
        self._active = active

    def conversation_active(self):
        return self._active


class ContinuationPromptTests(unittest.IsolatedAsyncioTestCase):
    """Stage 3: prompt-snapshot watermarks + delta continuation prompts."""

    def _referee(self):
        return Referee(RefereeConfig(mock=True, workdir=Path("."), color=False))

    def test_stateless_build_returns_the_stateless_prompt_verbatim(self):
        # conversation_active() False (every CLI/mock runner) -> the stateless
        # prompt is passed through byte-for-byte; the watermark still advances to
        # the prompt-snapshot length so a later continuity turn has a correct base.
        referee = self._referee()
        transcript = [
            Event.create("human", "message", "task"),
            Event.create("referee", "status", "workflow=..."),
        ]
        sentinel = "GUARDRAILS\nLead agent: ...\n\nTASK:\nx\n\nRECENT TRANSCRIPT:\n...\n"
        prompt = referee._build_turn_prompt(
            _StubRunner(active=False), transcript, "claude", "ROLE", lambda: sentinel
        )
        self.assertEqual(prompt, sentinel)
        self.assertEqual(referee._agent_watermarks["claude"], len(transcript))

    def test_active_build_sends_only_the_post_watermark_delta(self):
        from agent_collab.backends.common.sdk import provider_session_event

        referee = self._referee()
        transcript = [
            Event.create("human", "message", "before watermark"),  # 0: pre-wm
            Event.create("codex", "message", "peer after", agent_id="codex"),  # 1
            Event.create("claude", "message", "own after", agent_id="claude"),  # 2
            provider_session_event("claude", "claude", "sess-XYZ", "session"),  # 3
        ]
        referee._agent_watermarks["claude"] = 1
        prompt = referee._build_turn_prompt(
            _StubRunner(active=True),
            transcript,
            "claude",
            "DIRECTED ROLE",
            lambda: "STATELESS-SHOULD-NOT-BE-USED",
            question="what now?",
        )
        # No guardrails/task re-send, and the stateless builder is never called.
        self.assertNotIn("TASK:", prompt)
        self.assertNotIn("STATELESS-SHOULD-NOT-BE-USED", prompt)
        self.assertIn("NEW EVENTS SINCE YOUR LAST TURN:", prompt)
        self.assertTrue(prompt.startswith("DIRECTED ROLE"))
        # Only the post-watermark peer event; not pre-watermark, own, or the
        # provider-session bookkeeping id.
        self.assertIn("CODEX: peer after", prompt)
        self.assertNotIn("before watermark", prompt)
        self.assertNotIn("own after", prompt)
        self.assertNotIn("sess-XYZ", prompt)
        self.assertIn("DIRECTED QUESTION:\nwhat now?", prompt)
        # Watermark advances to the prompt-snapshot length (no cap), so the next
        # delta starts exactly where this one ended.
        self.assertEqual(referee._agent_watermarks["claude"], len(transcript))

    async def test_directed_continuation_turn_omits_task_and_window(self):
        # End-to-end at the directed call site: a solo session whose runner holds
        # provider context sends a continuation prompt, not a re-sent task.
        prompts = []

        class ContinuityRunner(AgentRunner):
            def conversation_active(self):
                return True

            async def run_turn(self, prompt, workdir, emit):
                prompts.append(prompt)
                await emit(Event.create("claude", "message", "ack"))
                return TurnOutcome("completed")

        config = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", command="claude")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            referee = Referee(
                RefereeConfig(
                    workflow="solo",
                    collab_config=config,
                    workdir=root,
                    log_dir=root,
                    timeout=5,
                    color=False,
                ),
                printer=lambda event: None,
            )
            transcript = [Event.create("claude", "message", "turn 1 output", agent_id="claude")]
            item = RefereeInput(
                event=Event.create("human", "message", "recall the codeword"), target=None
            )
            with SessionLogger(root, "task") as logger:
                await referee._process_input_item(
                    logger, transcript, {"claude": ContinuityRunner()}, "task", item
                )

        self.assertEqual(len(prompts), 1)
        self.assertNotIn("TASK:", prompts[0])
        self.assertNotIn("RECENT TRANSCRIPT:", prompts[0])
        self.assertIn("NEW EVENTS SINCE YOUR LAST TURN:", prompts[0])
        self.assertIn("recall the codeword", prompts[0])


class RunnerCloseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Stage 3: the referee closes every runner on any exit, bounded/shielded."""

    def _config(self, sequence):
        agents = {
            name: AgentConfig(id=name, type=name, command=name, backend="cli")
            for name in {"claude", "codex"}
        }
        return CollaborationConfig(
            agents=agents,
            workflows={"test": WorkflowConfig(id="test", sequence=list(sequence))},
        )

    def _referee(self, root, sequence, runners, *, timeout=5):
        referee = Referee(
            RefereeConfig(
                workflow="test",
                collab_config=self._config(sequence),
                workdir=root,
                log_dir=root,
                max_turns=len(sequence),
                timeout=timeout,
                color=False,
            ),
            printer=lambda event: None,
        )
        referee._runners = lambda: runners
        return referee

    async def test_runners_closed_on_completion(self):
        class ClosingRunner(AgentRunner):
            def __init__(self):
                self.closed = 0

            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "done"))
                return TurnOutcome("completed")

            async def close(self):
                self.closed += 1

        runner = ClosingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            referee = self._referee(Path(tmp), ["claude"], {"claude": runner})
            await referee.run("task")
        self.assertEqual(runner.closed, 1)

    async def test_runners_closed_on_failure(self):
        class FailingRunner(AgentRunner):
            def __init__(self):
                self.closed = 0

            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "partial"))
                return TurnOutcome("failed", "provider_terminal_failure")

            async def close(self):
                self.closed += 1

        runner = FailingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            referee = self._referee(Path(tmp), ["claude"], {"claude": runner})
            with self.assertRaises(RequiredTurnFailed):
                await referee.run("task")
        self.assertEqual(runner.closed, 1)

    async def test_runners_closed_on_stop(self):
        started = asyncio.Event()

        class BlockingClosingRunner(AgentRunner):
            def __init__(self):
                self.closed = 0

            async def run_turn(self, prompt, workdir, emit):
                started.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed += 1

        runner = BlockingClosingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            referee = self._referee(Path(tmp), ["claude"], {"claude": runner})
            task = asyncio.create_task(referee.run("task"))
            await started.wait()
            referee.request_stop()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(runner.closed, 1)

    async def test_hanging_close_does_not_hang_teardown(self):
        close_started = asyncio.Event()

        class HangingCloseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "done"))
                return TurnOutcome("completed")

            async def close(self):
                close_started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmp:
            referee = self._referee(Path(tmp), ["claude"], {"claude": HangingCloseRunner()})
            with mock.patch("agent_collab.referee.RUNNER_CLEANUP_GRACE_SECONDS", 0.02):
                # wait_for proves teardown returned rather than hanging on close.
                await asyncio.wait_for(referee.run("task"), timeout=1.0)
        self.assertTrue(close_started.is_set())
        # The uncooperative close was adopted as a background reaper, not awaited.
        self.assertTrue(referee._reaper_tasks)
        for reaper in list(referee._reaper_tasks):
            reaper.cancel()
        await asyncio.gather(*referee._reaper_tasks, return_exceptions=True)

    async def test_close_serializes_behind_a_cancellation_ignoring_adopted_turn(self):
        # Finding 6: the bounded cancel can adopt a non-cooperative run_turn that
        # outlives the turn; close() must be concurrency-safe against it. A runner
        # that serializes the two internally (one shared lock) must never run
        # close's critical section while the adopted turn still holds the lock,
        # and teardown must stay bounded rather than block on it.
        lock = asyncio.Lock()
        run_holding = asyncio.Event()
        release_run = asyncio.Event()
        order = []

        class SerializingRunner(AgentRunner):
            def __init__(self):
                self.closed = False

            async def run_turn(self, prompt, workdir, emit):
                async with lock:
                    run_holding.set()
                    order.append("run-enter")
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await release_run.wait()  # uncooperative: keep the lock
                    order.append("run-exit")
                return TurnOutcome("completed")

            async def close(self):
                async with lock:  # serialized behind the live turn
                    order.append("close-crit")
                    self.closed = True

        runner = SerializingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            referee = self._referee(Path(tmp), ["claude"], {"claude": runner}, timeout=0)
            with mock.patch("agent_collab.referee.RUNNER_CLEANUP_GRACE_SECONDS", 0.02):
                task = asyncio.create_task(referee.run("task"))
                await run_holding.wait()
                # The deadline (timeout=0) cancels the turn; it ignores the cancel
                # and is adopted. Teardown fires close(), which blocks on the lock.
                with self.assertRaises(RequiredTurnFailed):
                    await task
                # close() has not entered its critical section: the turn holds it.
                self.assertNotIn("close-crit", order)
                self.assertFalse(runner.closed)
                # Let the adopted turn finish; close then runs, strictly after it.
                release_run.set()
                for _ in range(200):
                    if runner.closed:
                        break
                    await asyncio.sleep(0.01)
        self.assertTrue(runner.closed)
        self.assertEqual(order, ["run-enter", "run-exit", "close-crit"])

    async def test_cancel_during_close_preserves_completed_run(self):
        # A cancel landing purely during the close finally — the stages already
        # completed and committed their outcomes — must not convert the run into
        # a cancellation; run() returns its result so the daemon still reports
        # `done`, honoring "close never alters an already-committed outcome".
        close_started = asyncio.Event()
        release_close = asyncio.Event()

        class SlowCloseRunner(AgentRunner):
            async def run_turn(self, prompt, workdir, emit):
                await emit(Event.create("claude", "message", "done"))
                return TurnOutcome("completed")

            async def close(self):
                close_started.set()
                await release_close.wait()

        with tempfile.TemporaryDirectory() as tmp:
            referee = self._referee(Path(tmp), ["claude"], {"claude": SlowCloseRunner()})
            task = asyncio.create_task(referee.run("task"))
            # Stages have finished and the close is now blocking.
            await asyncio.wait_for(close_started.wait(), timeout=1.0)
            task.cancel()  # cancel purely during cleanup
            release_close.set()
            result = await task  # returns the result rather than raising
        self.assertIsInstance(result, dict)
        self.assertIn("session_id", result)


if __name__ == "__main__":
    unittest.main()
