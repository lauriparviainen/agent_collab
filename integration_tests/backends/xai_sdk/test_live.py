import asyncio
from dataclasses import replace
import os
from pathlib import Path
import secrets
import tempfile
from unittest import mock

from agent_collab.backends.xai_sdk.backend import XaiSdkRunner
from agent_collab.config import builtin_config
from agent_collab.daemon import SessionManager, StartSessionRequest
from integration_tests.harness import LiveBackendTestCase, missing_reason


class XaiSdkLiveTests(LiveBackendTestCase):
    provider = "xai"
    backend_id = "sdk"

    def live_agent(self):
        return replace(
            builtin_config().agents["xai_cli"],
            id="xai_sdk",
            backend="sdk",
            command=None,
            args=[],
            enabled=True,
            options={},
            default_options={"model": "grok-4.5", "thinking_level": "high"},
        )

    def setUp(self):
        super().setUp()
        if not os.environ.get("XAI_API_KEY"):
            self.skipTest(missing_reason(self.provider, self.backend_id, "XAI_API_KEY is missing"))

    def test_turn_and_response(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "response")
        self.assertFalse(
            any(event.type in {"tool_call", "command", "file_change"} for event in events)
        )

    def test_model_catalog(self):
        observation = self.discover_live_models()
        self.assertEqual(observation.status, "ok")
        self.assertEqual(observation.source, "sdk")
        self.assertTrue(observation.complete)
        self.assertIn("grok-4.5", observation.models)

    def test_provider_memory_across_stored_response_chain(self):
        from agent_collab.backends.xai_sdk.compat import import_xai_sdk

        import_xai_sdk()
        from xai_sdk.aio.chat import Chat

        codeword = f"SABLE-{secrets.token_hex(4).upper()}"
        prompts = []
        requests = []
        original_run_turn = XaiSdkRunner.run_turn
        original_sample = Chat.sample

        async def recording_run_turn(runner, prompt, workdir, emit):
            prompts.append(prompt)
            return await original_run_turn(runner, prompt, workdir, emit)

        async def recording_sample(chat):
            request = chat._make_request(1)
            requests.append(
                {
                    "message_count": len(request.messages),
                    "texts": [
                        "".join(content.text for content in message.content)
                        for message in request.messages
                    ],
                    "store_messages": request.store_messages,
                    "previous_response_id": bool(request.previous_response_id),
                }
            )
            return await original_sample(chat)

        async def scenario(workdir):
            manager = SessionManager()
            state = await manager.start_session(
                StartSessionRequest(
                    task=(
                        f"For this session the project id is {codeword}. "
                        "Reply exactly STORED without repeating the project id."
                    ),
                    workflow="solo",
                    members={"claude_cli": "xai_sdk"},
                    backend_options={"xai_sdk": self.requested_options()},
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
                response_ids = [
                    event["raw"]["provider_session_id"]
                    for event in events
                    if isinstance(event.get("raw"), dict)
                    and event["raw"].get("provider_session_kind") == "response"
                ]
                self.assertGreaterEqual(len(response_ids), 2)
                self.assertNotEqual(response_ids[-2], response_ids[-1])
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["xai_sdk"]["provider_session_id"],
                    response_ids[-1],
                )

                self.assertEqual(len(prompts), 2)
                self.assertIn("TASK:", prompts[0])
                self.assertIn("NEW EVENTS SINCE YOUR LAST TURN:", prompts[1])
                self.assertNotIn("TASK:", prompts[1])
                self.assertNotIn("RECENT TRANSCRIPT:", prompts[1])
                self.assertNotIn(codeword, prompts[1])

                # Inspect the exact request proto handed to Chat.sample().
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[1]["message_count"], 1)
                self.assertTrue(requests[1]["store_messages"])
                self.assertTrue(requests[1]["previous_response_id"])
                self.assertEqual(requests[1]["texts"], [prompts[1]])
                self.assertNotIn(codeword, requests[1]["texts"][0])
                self.assertNotIn(prompts[0], requests[1]["texts"][0])
            finally:
                await manager.stop_session(state.session_id)

        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            home_path = Path(home)
            (home_path / "config.toml").write_text(
                "schema_version = 10\n\n[backends.xai_sdk]\nenabled = true\n",
                encoding="utf-8",
            )
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            try:
                with (
                    mock.patch.object(XaiSdkRunner, "run_turn", recording_run_turn),
                    mock.patch.object(Chat, "sample", recording_sample),
                ):
                    asyncio.run(scenario(Path(tmp).resolve()))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_COLLAB_HOME", None)
                else:
                    os.environ["AGENT_COLLAB_HOME"] = previous
