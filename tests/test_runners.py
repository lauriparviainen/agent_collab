import asyncio
import json
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from pathlib import Path

from agent_collab.config import AgentConfig, ConfigError
from agent_collab.events import Event
from agent_collab.backends.claude_cli.parser import ClaudeStreamingParser
from agent_collab.backends.codex_cli.parser import CodexStreamingParser
from agent_collab.backends.antigravity_cli import parse_antigravity_line
from agent_collab.backends.xai_cli.parser import XaiStreamingParser
from agent_collab.runners import SubprocessRunner, configured_runner
from agent_collab.sandbox.specs import SandboxFailure, SandboxPolicy


def _json_message_parser(line, verbose):
    payload = json.loads(line)
    return Event.create("claude", "message", payload["text"])


class SubprocessTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _events(self, runner):
        async def collect():
            events = []

            async def emit(event):
                events.append(event)

            await runner.run_turn("prompt", Path("."), emit)
            return events

        return await asyncio.wait_for(collect(), timeout=5.0)

    async def _result(self, runner):
        events = []

        async def emit(event):
            events.append(event)

        outcome = await asyncio.wait_for(runner.run_turn("prompt", Path("."), emit), timeout=5.0)
        return events, outcome

    async def test_jsonl_event_larger_than_asyncio_default_is_supported(self):
        size = 100_000
        script = f"import json; print(json.dumps({{'text': 'x' * {size}}}))"
        runner = SubprocessRunner(
            "large-jsonl",
            [sys.executable, "-c", script],
            _json_message_parser,
        )

        events = await self._events(runner)

        message = next(event for event in events if event.source == "claude")
        self.assertEqual(len(message.text), size)
        self.assertFalse(any("exited with code 0" in event.text for event in events))

    async def test_over_limit_stdout_fails_immediately_and_closes_stream(self):
        script = "import json; print(json.dumps({'text': 'x' * 4096}))"
        runner = SubprocessRunner(
            "oversized-stdout",
            [sys.executable, "-c", script],
            _json_message_parser,
            stream_limit=1024,
        )

        events = await self._events(runner)

        errors = [event for event in events if event.type == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("stdout JSONL event exceeded the 1024-byte transport limit", errors[0].text)
        self.assertEqual(errors[0].raw["stream_limit"], 1024)

    async def test_over_limit_stderr_fails_immediately_and_closes_stream(self):
        script = "import sys; print('x' * 4096, file=sys.stderr)"
        runner = SubprocessRunner(
            "oversized-stderr",
            [sys.executable, "-c", script],
            _json_message_parser,
            stream_limit=1024,
        )

        events = await self._events(runner)

        errors = [event for event in events if event.type == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("stderr line exceeded the 1024-byte transport limit", errors[0].text)

    async def test_parser_can_emit_multiple_events_for_one_line(self):
        def parser(line, verbose):
            return [
                Event.create("claude", "message", "one"),
                Event.create("claude", "message", "two"),
            ]

        runner = SubprocessRunner(
            "multi-event",
            [sys.executable, "-c", "print('fixture')"],
            parser,
        )
        events = await self._events(runner)
        self.assertEqual(
            [event.text for event in events if event.source == "claude"],
            ["one", "two"],
        )

    async def test_command_not_found_returns_structured_error_without_transport_failure(self):
        missing_command = "/agent-collab-tests/missing-provider-command"
        runner = SubprocessRunner(
            "missing-provider",
            [missing_command],
            _json_message_parser,
        )

        events = await self._events(runner)

        self.assertEqual([event.type for event in events], ["command", "error"])
        error = events[-1]
        self.assertEqual(error.source, "error")
        self.assertEqual(
            error.text,
            f"missing-provider command not found: {missing_command}",
        )
        self.assertIn(missing_command, error.raw["error"])
        self.assertNotIn("output transport failed", error.text)

    async def test_launch_time_sandbox_failure_retains_its_stable_outcome_code(self):
        prepared_prefix = (
            sys.executable,
            "-c",
            "raise SystemExit('prepared provider must not run')",
        )
        plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            operations=(),
            prepare_inner=mock.Mock(return_value=prepared_prefix),
        )
        runner = SubprocessRunner(
            "codex",
            [sys.executable, "-c", "raise SystemExit('provider must not run')"],
            _json_message_parser,
            sandbox_plan=plan,
        )
        supervisor = mock.Mock()
        supervisor.launch_cli = mock.AsyncMock(
            side_effect=SandboxFailure(
                "outer_sandbox_hardlink_alias",
                "private diagnostic",
                phase="alias-audit",
                remediation=("remove the alias",),
            )
        )

        with (
            mock.patch(
                "agent_collab.sandbox.bubblewrap.discover_bubblewrap",
                return_value=object(),
            ),
            mock.patch(
                "agent_collab.sandbox.supervisor.SandboxSupervisor",
                return_value=supervisor,
            ),
        ):
            events, outcome = await self._result(runner)

        self.assertEqual(
            (outcome.outcome, outcome.code),
            ("failed", "outer_sandbox_hardlink_alias"),
        )
        command = next(event for event in events if event.type == "command")
        self.assertEqual(command.raw["command_preview"], list(prepared_prefix))
        plan.prepare_inner.assert_called_once_with(runner.command_prefix)
        supervisor.launch_cli.assert_awaited_once_with(
            plan,
            runner.command_prefix,
            "prompt",
            stream_limit=runner.stream_limit,
        )
        error = next(event for event in events if event.type == "error")
        self.assertEqual(error.raw["code"], "outer_sandbox_hardlink_alias")
        self.assertNotIn("private diagnostic", outcome.message)

    async def test_post_establishment_sandbox_failure_overrides_provider_evidence(self):
        class FailingSandboxProcess:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stdout.feed_data(b'{"type":"turn.completed"}\n')
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.returncode = 0

            async def wait(self):
                raise SandboxFailure(
                    "outer_sandbox_status_contradiction",
                    "private status diagnostic",
                    phase="runtime",
                )

            def terminate(self):
                raise AssertionError("reaped sandbox process must not be signalled")

            def kill(self):
                raise AssertionError("reaped sandbox process must not be signalled")

        runner = SubprocessRunner(
            "codex",
            [sys.executable, "-c", "raise SystemExit('provider must not run')"],
            CodexStreamingParser(),
            sandbox_plan=SimpleNamespace(
                policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
                operations=(),
                prepare_inner=lambda command: tuple(command),
            ),
        )
        supervisor = mock.Mock()
        supervisor.launch_cli = mock.AsyncMock(return_value=FailingSandboxProcess())

        with (
            mock.patch(
                "agent_collab.sandbox.bubblewrap.discover_bubblewrap",
                return_value=object(),
            ),
            mock.patch(
                "agent_collab.sandbox.supervisor.SandboxSupervisor",
                return_value=supervisor,
            ),
        ):
            events, outcome = await self._result(runner)

        self.assertEqual(
            (outcome.outcome, outcome.code, outcome.process_exit_code),
            ("failed", "outer_sandbox_status_contradiction", 0),
        )
        error = next(event for event in events if event.type == "error")
        self.assertEqual(error.raw["code"], "outer_sandbox_status_contradiction")
        self.assertNotIn("private status diagnostic", outcome.message)

    async def test_non_noisy_stderr_is_emitted_as_structured_error(self):
        script = "import sys; print('provider failed safely', file=sys.stderr)"
        runner = SubprocessRunner(
            "claude",
            [sys.executable, "-c", script],
            _json_message_parser,
        )

        events = await self._events(runner)

        errors = [event for event in events if event.type == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].source, "error")
        self.assertEqual(errors[0].text, "claude stderr: provider failed safely")
        self.assertEqual(errors[0].raw, {"line": "provider failed safely"})
        self.assertFalse(any("exited with code 0" in event.text for event in events))

    async def test_renamed_agent_verbose_stderr_is_attributed_to_provider(self):
        script = "import sys; print('WARN provider chatter', file=sys.stderr)"
        runner = SubprocessRunner(
            "reviewer",
            [sys.executable, "-c", script],
            _json_message_parser,
            verbose=True,
            source="claude",
        )

        events = await self._events(runner)

        noisy = [event for event in events if "stderr:" in event.text]
        self.assertEqual(len(noisy), 1)
        self.assertEqual((noisy[0].source, noisy[0].type), ("claude", "status"))
        self.assertEqual(noisy[0].text, "reviewer stderr: WARN provider chatter")

    def test_invalid_source_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            SubprocessRunner(
                "reviewer",
                [sys.executable, "-c", "pass"],
                _json_message_parser,
                source="reviewer",
            )

    async def test_noisy_stderr_is_suppressed_unless_verbose(self):
        script = (
            "import sys; "
            "print('Reading additional input from stdin...', file=sys.stderr); "
            "print('WARN provider chatter', file=sys.stderr); "
            "print('prefix WARN provider chatter', file=sys.stderr)"
        )
        quiet = SubprocessRunner(
            "claude",
            [sys.executable, "-c", script],
            _json_message_parser,
        )
        verbose = SubprocessRunner(
            "claude",
            [sys.executable, "-c", script],
            _json_message_parser,
            verbose=True,
        )

        quiet_events = await self._events(quiet)
        verbose_events = await self._events(verbose)

        self.assertFalse(any("stderr:" in event.text for event in quiet_events))
        noisy = [event for event in verbose_events if "stderr:" in event.text]
        self.assertEqual(len(noisy), 3)
        self.assertTrue(all((event.source, event.type) == ("claude", "status") for event in noisy))
        self.assertFalse(any(event.type == "error" for event in verbose_events))

    async def test_cancelling_runner_reaps_a_silent_child_promptly(self):
        runner = SubprocessRunner(
            "cancel-me",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            _json_message_parser,
        )

        async def collect():
            events = []

            async def emit(event):
                events.append(event)

            await runner.run_turn("prompt", Path("."), emit)
            return events

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    async def test_repeated_cancel_still_escalates_to_kill(self):
        runner = SubprocessRunner(
            "cancel-storm",
            [
                sys.executable,
                "-c",
                (
                    "import signal, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(60)"
                ),
            ],
            _json_message_parser,
        )
        original_create = asyncio.create_subprocess_exec
        process_holder = {}

        async def capture_process(*args, **kwargs):
            process = await original_create(*args, **kwargs)
            process_holder["process"] = process
            return process

        async def collect():
            async def emit(_event):
                return None

            await runner.run_turn("prompt", Path("."), emit)

        process = None
        try:
            with mock.patch(
                "agent_collab.runners.asyncio.create_subprocess_exec",
                side_effect=capture_process,
            ):
                task = asyncio.create_task(collect())
                for _ in range(100):
                    process = process_holder.get("process")
                    if process is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(process)
                await asyncio.sleep(0.1)
                task.cancel()
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3.0)
                await asyncio.wait_for(process.wait(), timeout=2.0)
                self.assertIsNotNone(process.returncode)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    async def test_post_kill_cancel_adopts_final_process_wait(self):
        wait_started = [asyncio.Event() for _ in range(3)]
        adopted = asyncio.Event()

        class Process:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.returncode = None
                self.pid = 12345
                self.wait_calls = 0
                self.terminated = False
                self.killed = False

            async def wait(self):
                self.wait_calls += 1
                call = self.wait_calls
                if call <= 3:
                    wait_started[call - 1].set()
                    await asyncio.Event().wait()
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                adopted.set()
                return self.returncode

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = Process()
        runner = SubprocessRunner(
            "cancel-storm",
            ["provider"],
            _json_message_parser,
        )

        async def collect():
            async def emit(_event):
                return None

            await runner.run_turn("prompt", Path("."), emit)

        with mock.patch(
            "agent_collab.runners.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            task = asyncio.create_task(collect())
            await asyncio.wait_for(wait_started[0].wait(), timeout=1.0)
            task.cancel()
            await asyncio.wait_for(wait_started[1].wait(), timeout=1.0)
            task.cancel()
            await asyncio.wait_for(wait_started[2].wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(adopted.wait(), timeout=1.0)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 4)

    def test_configured_runner_rejects_missing_resolved_sandbox_plan(self):
        backend = mock.Mock()
        backend.normalize_options.return_value = {}
        backend.create_runner.return_value = SubprocessRunner(
            "provider",
            [sys.executable, "-c", "pass"],
            _json_message_parser,
        )
        agent = AgentConfig(id="provider", type="claude", command="provider")

        with (
            mock.patch("agent_collab.backends.get_backend", return_value=backend),
            self.assertRaisesRegex(
                ConfigError,
                "runner requires a resolved sandbox plan",
            ),
        ):
            configured_runner(agent, backend_id="cli")

    async def test_fixture_backed_marker_contracts(self):
        fixture_root = Path(__file__).parent / "fixtures"
        cases = (
            ("claude/stream-json-success.ndjson", ClaudeStreamingParser(), "completed", None),
            (
                "claude/stream-json-error.ndjson",
                ClaudeStreamingParser(),
                "failed",
                "provider_terminal_failure",
            ),
            ("codex/jsonl-success.ndjson", CodexStreamingParser(), "completed", None),
            (
                "codex/jsonl-failed.ndjson",
                CodexStreamingParser(),
                "failed",
                "provider_terminal_failure",
            ),
            (
                "xai/streaming-json-reasoning.ndjson",
                XaiStreamingParser(),
                "completed",
                None,
            ),
            (
                "xai/streaming-json-cancelled.ndjson",
                XaiStreamingParser(),
                "cancelled",
                "provider_turn_cancelled",
            ),
        )
        for relative, parser, expected_outcome, expected_code in cases:
            with self.subTest(fixture=relative):
                path = fixture_root / relative
                script = f"from pathlib import Path; print(Path({str(path)!r}).read_text(), end='')"
                runner = SubprocessRunner(
                    "fixture-provider", [sys.executable, "-c", script], parser
                )
                _events, outcome = await self._result(runner)
                self.assertEqual((outcome.outcome, outcome.code), (expected_outcome, expected_code))

    async def test_success_marker_followed_by_nonzero_exit_fails(self):
        line = '{"type":"turn.completed"}'
        script = f"import sys; print({line!r}); sys.exit(7)"
        runner = SubprocessRunner("codex", [sys.executable, "-c", script], CodexStreamingParser())
        _events, outcome = await self._result(runner)
        self.assertEqual(outcome.code, "subprocess_exit_nonzero")
        self.assertEqual(outcome.process_exit_code, 7)

    async def test_marker_transport_fails_closed_after_partial_output(self):
        line = '{"type":"text","data":"partial"}'
        runner = SubprocessRunner(
            "xai",
            [sys.executable, "-c", f"print({line!r})"],
            XaiStreamingParser(),
        )
        events, outcome = await self._result(runner)
        self.assertTrue(any(event.type == "message" for event in events))
        self.assertEqual(outcome.code, "provider_output_incomplete")

    async def test_antigravity_provisional_clean_eof_requires_message(self):
        fixture = Path(__file__).parent / "fixtures/antigravity/agy-print-sample.stdout.txt"
        script = f"from pathlib import Path; print(Path({str(fixture)!r}).read_text(), end='')"
        with_message = SubprocessRunner(
            "antigravity",
            [sys.executable, "-c", script],
            parse_antigravity_line,
            clean_eof_fallback=True,
        )
        events, outcome = await self._result(with_message)
        self.assertTrue(any(event.type == "message" for event in events))
        self.assertEqual(outcome.outcome, "completed")

        empty = SubprocessRunner(
            "antigravity",
            [sys.executable, "-c", "pass"],
            lambda line, verbose: None,
            clean_eof_fallback=True,
        )
        _events, outcome = await self._result(empty)
        self.assertEqual(outcome.code, "provider_empty_response")


if __name__ == "__main__":
    unittest.main()
