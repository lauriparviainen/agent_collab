import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
from unittest import mock

from agent_collab.backends.antigravity_sdk.backend import AntigravitySdkRunner
from agent_collab.daemon import SessionManager, StartSessionRequest
from integration_tests.harness import LiveBackendTestCase, missing_reason


DEFAULT_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _configured_project() -> str:
    explicit = os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_PROJECT")
    if explicit:
        return explicit
    environment = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if environment:
        return environment
    gcloud = shutil.which("gcloud")
    if not gcloud:
        return ""
    try:
        result = subprocess.run(
            [gcloud, "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    project = result.stdout.strip()
    return "" if result.returncode or project == "(unset)" else project


class AntigravitySdkLiveTests(LiveBackendTestCase):
    provider = "antigravity"
    backend_id = "sdk"

    def requested_options(self):
        options = super().requested_options()
        if not os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_MODEL"):
            # The SDK passes model strings directly to Vertex. Keep its default
            # on the publisher model used by the Stage 6 continuity proof;
            # CLI-style names such as gemini-3.5-flash-low are not Vertex model
            # ids in the verified 0.1.8 path.
            options["model"] = "gemini-2.5-flash"
        return options

    def setUp(self):
        super().setUp()
        self.adc_path = (
            Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_ADC_PATH)))
            .expanduser()
            .resolve()
        )
        if not self.adc_path.is_file():
            self.skipTest(
                missing_reason(self.provider, self.backend_id, "Google ADC file is missing")
            )
        self.project = _configured_project()
        if not self.project:
            self.skipTest(
                missing_reason(
                    self.provider,
                    self.backend_id,
                    "Vertex project is missing; set AGENT_COLLAB_IT_ANTIGRAVITY_PROJECT",
                )
            )

    def agent_backend_config(self):
        return {
            "vertex": True,
            "project": self.project,
            "location": os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_LOCATION", "us-central1"),
        }

    def environment_overrides(self):
        return {"GOOGLE_APPLICATION_CREDENTIALS": str(self.adc_path)}

    def test_turn_tools_and_conversation(self):
        events = self.run_live(
            "Create sdk-smoke.txt containing ready, read it, then reply with the single word ready."
        )
        self.assert_message(events)
        self.assert_session_kind(events, "conversation")
        self.assertTrue(any(event.source == "tool" for event in events))

    def test_provider_memory_across_interactive_turns(self):
        codeword = f"SABLE-{secrets.token_hex(4).upper()}"
        prompts = []
        original_run_turn = AntigravitySdkRunner.run_turn

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
                    members={"claude_cli": "antigravity_sdk"},
                    backend_options={"antigravity_sdk": self.requested_options()},
                    max_turns=1,
                    timeout=180,
                    workdir=workdir,
                    interactive=True,
                    interactive_idle_timeout=300,
                )
            )
            try:
                first = await manager.wait_result(
                    state.session_id,
                    timeout_ms=240_000,
                )
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
                second = await manager.wait_result(
                    state.session_id,
                    timeout_ms=240_000,
                )
                self.assertTrue(second.settled)
                self.assertEqual(second.status, "awaiting_input")
                self.assertEqual(len(second.answers), 1)
                self.assertIn(codeword, second.answers[0]["text"].upper())

                events = manager.read_events(
                    state.session_id,
                    0,
                    tool_output="full",
                ).events
                conversation_ids = [
                    event["raw"]["provider_session_id"]
                    for event in events
                    if isinstance(event.get("raw"), dict)
                    and event["raw"].get("provider_session_kind") == "conversation"
                ]
                self.assertGreaterEqual(len(conversation_ids), 2)
                self.assertEqual(len(set(conversation_ids)), 1)
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["antigravity_sdk"]["provider_session_id"],
                    conversation_ids[0],
                )

                # The follow-up must be the Stage 3 delta prompt. The codeword
                # reaches turn 2 only through provider-held context.
                self.assertEqual(len(prompts), 2)
                self.assertIn("TASK:", prompts[0])
                self.assertIn(
                    "NEW EVENTS SINCE YOUR LAST TURN:",
                    prompts[1],
                )
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
                (
                    "schema_version = 10\n\n"
                    "[backends.antigravity_sdk]\n"
                    "enabled = true\n"
                    "vertex = true\n"
                    f"project = {json.dumps(self.project)}\n"
                    "location = "
                    f"{json.dumps(os.environ.get('AGENT_COLLAB_IT_ANTIGRAVITY_LOCATION', 'us-central1'))}\n"
                ),
                encoding="utf-8",
            )
            overrides = {
                **self.environment_overrides(),
                "AGENT_COLLAB_HOME": str(home_path),
            }
            previous = {key: os.environ.get(key) for key in overrides}
            os.environ.update(overrides)
            try:
                with mock.patch.object(
                    AntigravitySdkRunner,
                    "run_turn",
                    recording_run_turn,
                ):
                    asyncio.run(scenario(Path(tmp).resolve()))
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
