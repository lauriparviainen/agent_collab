import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_collab.backends.antigravity_cli.backend import AntigravityCliBackend
from agent_collab.backends.antigravity_cli.invocation import (
    CLI_OWNERSHIP_FLAGS,
    finalize_antigravity_cli_invocation,
    reject_antigravity_ownership_flags,
)
from agent_collab.backends.antigravity_cli.parser import AntigravityStreamingParser
from agent_collab.backends.claude_cli.parser import ClaudeStreamingParser
from agent_collab.backends.common.cli import prepare_cli_invocation
from agent_collab.config import AgentConfig
from agent_collab.runners import (
    CLI_RESUME_ACTIVE,
    CLI_RESUME_EMPTY,
    CLI_RESUME_QUARANTINED,
    SubprocessRunner,
)
from agent_collab.sandbox.specs import SandboxFailure, SandboxPolicy


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "antigravity"
ROOT_CONVERSATION_ID = "00000000-0000-4000-8000-000000000001"
SUCCESS_FIXTURE = FIXTURES / "stream-json-success.ndjson"
FAILED_FIXTURE = FIXTURES / "stream-json-failed.ndjson"


def _print_fixture_command(path: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        f"from pathlib import Path; print(Path({str(path)!r}).read_text(), end='')",
    ]


def _runner(*, finalizer=finalize_antigravity_cli_invocation, prefix=None, parser=None, **kwargs):
    return SubprocessRunner(
        "antigravity",
        prefix or ["agy", "--output-format", "stream-json", "-p"],
        parser or AntigravityStreamingParser("antigravity"),
        resume_finalizer=finalizer,
        ownership_flags=CLI_OWNERSHIP_FLAGS if finalizer is not None else (),
        **kwargs,
    )


class AntigravityCliInvocationTests(unittest.TestCase):
    def test_finalizer_is_identity_without_descriptor(self):
        ordinary = ("agy", "--output-format", "stream-json", "-p")
        self.assertEqual(finalize_antigravity_cli_invocation(ordinary, None), ordinary)

    def test_finalizer_inserts_conversation_before_print_marker(self):
        prepared = (
            "agy",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--mode",
            "accept-edits",
            "--sandbox=false",
            "-p",
        )
        result = finalize_antigravity_cli_invocation(
            prepared, {"provider_session_id": ROOT_CONVERSATION_ID}
        )
        self.assertEqual(
            result,
            (
                "agy",
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
                "--mode",
                "accept-edits",
                "--sandbox=false",
                "--conversation",
                ROOT_CONVERSATION_ID,
                "-p",
            ),
        )
        self.assertLess(result.index("--conversation"), result.index("-p"))

    def test_finalizer_rejects_missing_or_post_dashdash_print_marker(self):
        with self.assertRaises(SandboxFailure) as missing:
            finalize_antigravity_cli_invocation(
                ("agy", "--output-format", "stream-json"),
                {"provider_session_id": ROOT_CONVERSATION_ID},
            )
        self.assertEqual(missing.exception.code, "outer_sandbox_inner_command_invalid")
        with self.assertRaises(SandboxFailure) as after_end:
            finalize_antigravity_cli_invocation(
                ("agy", "--", "-p"),
                {"provider_session_id": ROOT_CONVERSATION_ID},
            )
        self.assertEqual(after_end.exception.code, "outer_sandbox_inner_command_invalid")

    def test_user_ownership_flags_are_rejected_under_both_policies(self):
        for command in (
            ("agy", "--conversation", ROOT_CONVERSATION_ID, "-p"),
            ("agy", f"--conversation={ROOT_CONVERSATION_ID}", "-p"),
            ("agy", "--continue", "-p"),
            ("agy", "--continue=true", "-p"),
        ):
            with self.subTest(command=command):
                with self.assertRaises(SandboxFailure) as raised:
                    reject_antigravity_ownership_flags(command)
                self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
                none_plan = SimpleNamespace(prepare_inner=lambda argv: tuple(argv))
                with self.assertRaises(SandboxFailure):
                    prepare_cli_invocation(
                        command,
                        none_plan,
                        None,
                        finalizer=finalize_antigravity_cli_invocation,
                        ownership_flags=CLI_OWNERSHIP_FLAGS,
                    )


class AntigravityCliStateMachineTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, runner, prompt="prompt"):
        events = []

        async def emit(event):
            events.append(event)

        outcome = await runner.run_turn(prompt, Path("."), emit)
        return events, outcome

    async def test_empty_turn_without_id_stays_empty_and_inactive(self):
        runner = _runner(prefix=_print_fixture_command(FAILED_FIXTURE))
        self.assertFalse(runner.conversation_active())
        self.assertEqual(runner._cli_state, CLI_RESUME_EMPTY)
        events, outcome = await self._run(runner)
        self.assertEqual(outcome.outcome, "failed")
        self.assertFalse(any(event.provider_session for event in events))
        self.assertEqual(runner._cli_state, CLI_RESUME_EMPTY)
        self.assertFalse(runner.conversation_active())

    async def test_completed_id_promotes_to_active_and_second_turn_uses_conversation(self):
        captured = []

        async def fake_exec(*argv, **_kwargs):
            captured.append(list(argv))
            process = mock.Mock()
            process.stdout = __import__("asyncio").StreamReader()
            process.stderr = __import__("asyncio").StreamReader()
            process.stdout.feed_data(SUCCESS_FIXTURE.read_bytes())
            process.stdout.feed_eof()
            process.stderr.feed_eof()
            process.returncode = 0

            async def wait():
                return 0

            process.wait = wait
            return process

        runner = _runner()
        with mock.patch(
            "agent_collab.runners.asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            first_events, first = await self._run(runner, "first prompt")
            self.assertEqual(first.outcome, "completed")
            self.assertEqual(runner._cli_state, CLI_RESUME_ACTIVE)
            self.assertTrue(runner.conversation_active())
            self.assertEqual(runner._active_id, ROOT_CONVERSATION_ID)
            self.assertNotIn("--conversation", captured[0])
            self.assertEqual(captured[0][-1], "first prompt")

            second_events, second = await self._run(runner, "delta prompt")
            self.assertEqual(second.outcome, "completed")
            self.assertTrue(runner.conversation_active())
            self.assertIn("--conversation", captured[1])
            self.assertEqual(
                captured[1][captured[1].index("--conversation") + 1], ROOT_CONVERSATION_ID
            )
            self.assertLess(captured[1].index("--conversation"), captured[1].index("-p"))
            self.assertEqual(captured[1][-1], "delta prompt")
        self.assertTrue(any(event.provider_session for event in first_events))
        self.assertTrue(any(event.type == "command" for event in second_events))

    async def test_id_bearing_failed_turn_quarantines_and_later_turn_does_not_launch(self):
        conflicting = json.dumps(
            {
                "event": "init",
                "conversation_id": ROOT_CONVERSATION_ID,
                "init": {"cwd": "/workspace"},
            }
        )
        failed = json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": ROOT_CONVERSATION_ID,
                    "status": "ERROR",
                    "response": "",
                },
            }
        )
        runner = _runner(
            prefix=[
                sys.executable,
                "-c",
                f"print({conflicting!r}); print({failed!r})",
            ]
        )
        launched = []

        async def fake_exec(*argv, **kwargs):
            launched.append(list(argv))
            return await original(*argv, **kwargs)

        original = __import__("asyncio").create_subprocess_exec
        with mock.patch(
            "agent_collab.runners.asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            _events, first = await self._run(runner)
            self.assertEqual(first.outcome, "failed")
            self.assertEqual(runner._cli_state, CLI_RESUME_QUARANTINED)
            self.assertFalse(runner.conversation_active())
            _events, second = await self._run(runner)
        self.assertEqual((second.outcome, second.code), ("failed", "provider_session_quarantined"))
        self.assertEqual(len(launched), 1)

    async def test_conflicting_ids_quarantine(self):
        first = json.dumps(
            {
                "event": "init",
                "conversation_id": ROOT_CONVERSATION_ID,
                "init": {"cwd": "/workspace"},
            }
        )
        second = json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "00000000-0000-4000-8000-000000000002",
                    "status": "SUCCESS",
                    "response": "ready\n",
                },
            }
        )
        runner = _runner(prefix=[sys.executable, "-c", f"print({first!r}); print({second!r})"])
        _events, outcome = await self._run(runner)
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(runner._cli_state, CLI_RESUME_QUARANTINED)
        self.assertFalse(runner.conversation_active())

    async def test_no_finalizer_stays_empty_even_when_id_is_captured(self):
        runner = SubprocessRunner(
            "claude",
            [
                sys.executable,
                "-c",
                'print(\'{"type":"system","subtype":"init","session_id":"sess-1"}\'); '
                'print(\'{"type":"result","subtype":"success","session_id":"sess-1"}\')',
            ],
            ClaudeStreamingParser("claude"),
        )
        _events, outcome = await self._run(runner)
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(runner._cli_state, CLI_RESUME_EMPTY)
        self.assertFalse(runner.conversation_active())
        self.assertIsNone(runner.resume_finalizer)

    async def test_outer_sandbox_path_uses_prepared_prefix_once(self):
        prepared_prefix = (
            sys.executable,
            "-c",
            "raise SystemExit('prepared provider must not run')",
            "-p",
        )
        plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            operations=(),
            prepare_inner=mock.Mock(return_value=prepared_prefix),
        )
        runner = _runner(
            prefix=["agy", "--output-format", "stream-json", "-p"],
            sandbox_plan=plan,
        )
        runner._cli_state = CLI_RESUME_ACTIVE
        runner._id_seen = True
        runner._active_id = ROOT_CONVERSATION_ID
        supervisor = mock.Mock()
        supervisor.launch_prepared_cli = mock.AsyncMock(
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
            events, outcome = await self._run(runner, "USER PROMPT")
        self.assertEqual(outcome.code, "outer_sandbox_hardlink_alias")
        plan.prepare_inner.assert_called_once()
        launched = supervisor.launch_prepared_cli.await_args
        prefix = list(launched.args[1])
        self.assertIn("--conversation", prefix)
        self.assertLess(prefix.index("--conversation"), prefix.index("-p"))
        self.assertEqual(prefix[prefix.index("--conversation") + 1], ROOT_CONVERSATION_ID)
        self.assertNotIn("USER PROMPT", prefix)
        self.assertEqual(launched.kwargs["stream_limit"], runner.stream_limit)
        command = next(event for event in events if event.type == "command")
        self.assertEqual(command.raw["command_preview"], prefix)
        self.assertNotIn("USER PROMPT", command.raw["command_preview"])


class AntigravityCliCreateRunnerContinuityTests(unittest.TestCase):
    def test_create_runner_wires_finalizer_and_parser_agent_id(self):
        backend = AntigravityCliBackend()
        runner = backend.create_runner(
            AgentConfig(id="reviewer", type="antigravity", command="agy", args=["-p"]),
            False,
            {},
        )
        self.assertEqual(runner.resume_finalizer, finalize_antigravity_cli_invocation)
        self.assertEqual(runner.ownership_flags, CLI_OWNERSHIP_FLAGS)
        self.assertEqual(runner.parser.agent_id, "reviewer")
        self.assertFalse(runner.conversation_active())
        self.assertFalse(backend.capabilities.continuity)
        self.assertFalse(backend.capabilities.resume)
