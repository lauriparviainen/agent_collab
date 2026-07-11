import asyncio
import json
import sys
import unittest
from pathlib import Path

from agent_collab.events import Event
from agent_collab.runners import SubprocessRunner


def _json_message_parser(line, verbose):
    payload = json.loads(line)
    return Event.create("claude", "message", payload["text"])


class SubprocessTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _events(self, runner):
        async def collect():
            return [event async for event in runner.run("prompt", Path("."))]

        return await asyncio.wait_for(collect(), timeout=5.0)

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
        self.assertTrue(any("exited with code 0" in event.text for event in events))

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
        self.assertTrue(any("exited with code 0" in event.text for event in events))

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
            return [event async for event in runner.run("prompt", Path("."))]

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
