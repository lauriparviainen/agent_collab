import asyncio
from dataclasses import replace
import os
from pathlib import Path
import secrets
import tempfile
from unittest import mock

from agent_collab.backends.claude_sdk.backend import ClaudeSdkRunner
from agent_collab.config import builtin_config
from agent_collab.daemon import SessionManager, StartSessionRequest
from integration_tests.harness import LiveBackendTestCase


class ClaudeSdkLiveTests(LiveBackendTestCase):
    provider = "claude"
    backend_id = "sdk"

    def live_agent(self):
        return replace(
            builtin_config().agents["claude_cli"],
            id="claude_sdk",
            backend="sdk",
            command=None,
            enabled=True,
        )

    def test_turn_and_session(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "session")

    def test_provider_memory_across_interactive_turns(self):
        codeword = f"SABLE-{secrets.token_hex(4).upper()}"
        prompts = []
        original_run_turn = ClaudeSdkRunner.run_turn

        async def recording_run_turn(runner, prompt, workdir, emit):
            prompts.append(prompt)
            return await original_run_turn(runner, prompt, workdir, emit)

        async def scenario(workdir):
            manager = SessionManager()
            state = await manager.start_session(
                StartSessionRequest(
                    task=(
                        f"For this session the project id is {codeword}. "
                        "Reply exactly STORED without repeating the project id."
                    ),
                    workflow="solo",
                    members={"claude_cli": "claude_sdk"},
                    backend_options={"claude_sdk": self.requested_options()},
                    max_turns=1,
                    timeout=180,
                    workdir=workdir,
                    interactive=True,
                    interactive_idle_timeout=300,
                )
            )
            try:
                first = await manager.wait_result(state.session_id, timeout_ms=240_000)
                self.assertTrue(first.settled)
                if first.status != "awaiting_input":
                    events = manager.read_events(
                        state.session_id,
                        0,
                        tool_output="full",
                    ).events
                    errors = [event["text"] for event in events if event.get("type") == "error"]
                    self.fail(f"first turn failed: {first.failure}; errors={errors}")

                await manager.post_message(
                    state.session_id,
                    "What is the project id? Reply with only the id.",
                )
                second = await manager.wait_result(state.session_id, timeout_ms=240_000)
                self.assertTrue(second.settled)
                self.assertEqual(second.status, "awaiting_input")
                self.assertEqual(len(second.answers), 1)
                self.assertIn(codeword, second.answers[0]["text"].upper())

                events = manager.read_events(state.session_id, 0, tool_output="full").events
                session_ids = [
                    event["raw"]["provider_session_id"]
                    for event in events
                    if isinstance(event.get("raw"), dict)
                    and event["raw"].get("provider_session_kind") == "session"
                ]
                self.assertGreaterEqual(len(session_ids), 2)
                self.assertEqual(len(set(session_ids)), 1)
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["claude_sdk"]["provider_session_id"],
                    session_ids[0],
                )

                # The follow-up used the Stage 3 delta continuation prompt: no
                # guardrails/task/window re-send, and the codeword reached the
                # model only through provider-held context.
                self.assertEqual(len(prompts), 2)
                self.assertIn("TASK:", prompts[0])
                self.assertIn("NEW EVENTS SINCE YOUR LAST TURN:", prompts[1])
                self.assertNotIn("TASK:", prompts[1])
                self.assertNotIn("RECENT TRANSCRIPT:", prompts[1])
                self.assertNotIn(codeword, prompts[1])
            finally:
                await manager.stop_session(state.session_id)

        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            home_path = Path(home)
            (home_path / "config.toml").write_text(
                ("schema_version = 10\n\n[backends.claude_sdk]\nenabled = true\n"),
                encoding="utf-8",
            )
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            try:
                with mock.patch.object(ClaudeSdkRunner, "run_turn", recording_run_turn):
                    asyncio.run(scenario(Path(tmp).resolve()))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_COLLAB_HOME", None)
                else:
                    os.environ["AGENT_COLLAB_HOME"] = previous
