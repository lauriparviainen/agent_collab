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
