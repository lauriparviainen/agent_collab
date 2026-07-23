import asyncio
from dataclasses import replace
import os
from pathlib import Path
import secrets
import tempfile

from agent_collab.config import builtin_config
from agent_collab.daemon import SessionManager, StartSessionRequest
from integration_tests.harness import LiveBackendTestCase


class CodexSdkLiveTests(LiveBackendTestCase):
    provider = "codex"
    backend_id = "sdk"

    def live_agent(self):
        return replace(
            builtin_config().agents["codex_cli"],
            id="codex_sdk",
            backend="sdk",
            command=None,
            enabled=True,
        )

    def test_provider_memory_across_interactive_turns(self):
        codeword = f"SABLE-{secrets.token_hex(4).upper()}"

        async def scenario(workdir):
            manager = SessionManager()
            state = await manager.start_session(
                StartSessionRequest(
                    task=(
                        f"Memorize the codeword {codeword}. Reply exactly STORED "
                        "without repeating the codeword."
                    ),
                    workflow="solo",
                    members={"claude_cli": "codex_sdk"},
                    backend_options={"codex_sdk": self.requested_options()},
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
                    "Reply with only the codeword you were asked to remember.",
                )
                second = await manager.wait_result(state.session_id, timeout_ms=240_000)
                self.assertTrue(second.settled)
                self.assertEqual(second.status, "awaiting_input")
                self.assertEqual(len(second.answers), 1)
                self.assertIn(codeword, second.answers[0]["text"].upper())

                events = manager.read_events(state.session_id, 0, tool_output="full").events
                thread_ids = [
                    event["raw"]["provider_session_id"]
                    for event in events
                    if isinstance(event.get("raw"), dict)
                    and event["raw"].get("provider_session_kind") == "thread"
                ]
                self.assertGreaterEqual(len(thread_ids), 2)
                self.assertEqual(len(set(thread_ids)), 1)
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["codex_sdk"]["provider_session_id"],
                    thread_ids[0],
                )
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
                    "[backends.codex_sdk]\n"
                    "enabled = true\n"
                    'command = "codex"\n'
                ),
                encoding="utf-8",
            )
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            try:
                asyncio.run(scenario(Path(tmp).resolve()))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_COLLAB_HOME", None)
                else:
                    os.environ["AGENT_COLLAB_HOME"] = previous

    def test_model_catalog(self):
        observation = self.discover_live_models()
        self.assertEqual(observation.status, "ok")
        self.assertEqual(observation.source, "sdk")
        self.assertTrue(observation.complete)
        self.assertTrue(observation.models)
