import asyncio
import os
from pathlib import Path
import secrets
import tempfile
from unittest import mock

from agent_collab import backends
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.runners import SubprocessRunner

from integration_tests.harness import LiveBackendTestCase, missing_reason


class AntigravityCliLiveTests(LiveBackendTestCase):
    provider = "antigravity"
    backend_id = "cli"

    def test_turn(self):
        backend = backends.get_backend(self.provider, self.backend_id)
        self.assertFalse(backend.clean_eof_fallback)
        self.assertEqual(backend.event_fidelity, "typed")
        self.assertFalse(backend.capabilities.continuity)
        self.assertFalse(backend.capabilities.resume)
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "conversation")
        conversation_ids = [
            event.raw["provider_session_id"]
            for event in events
            if event.provider_session and event.raw.get("provider_session_kind") == "conversation"
        ]
        self.assertTrue(conversation_ids)
        self.assertTrue(all(conversation_ids))

    def test_outer_read_only_keyring_helper_shell_state_acceptance(self):
        raw_state = os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE")
        if not raw_state:
            self.skipTest(
                missing_reason(
                    self.provider,
                    self.backend_id,
                    "set AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE to an "
                    "operator-authorized dedicated complete .gemini directory for the paid "
                    "outer-sandbox acceptance",
                )
            )
        state = Path(raw_state).expanduser().resolve(strict=True)
        if not state.is_dir() or state.name != ".gemini":
            self.fail(
                "AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE must be a complete .gemini directory"
            )
        marker = state / ".agent-collab-sandbox-acceptance"
        if marker.exists():
            self.fail("the guarded Antigravity acceptance marker already exists")

        async def run() -> None:
            with (
                tempfile.TemporaryDirectory(prefix="agent-collab-antigravity-boundary-") as raw,
                tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as daemon_home,
            ):
                workspace = Path(raw).resolve()
                workspace.chmod(0o700)
                (workspace / "input.txt").write_text(
                    "antigravity sandbox acceptance\n",
                    encoding="utf-8",
                )
                previous = {
                    "HOME": os.environ.get("HOME"),
                    "AGENT_COLLAB_HOME": os.environ.get("AGENT_COLLAB_HOME"),
                }
                os.environ["HOME"] = str(state.parent)
                os.environ["AGENT_COLLAB_HOME"] = daemon_home
                manager = SessionManager(
                    default_workdir=workspace,
                    default_log_dir=workspace / "logs",
                )
                try:
                    started = await manager.start_session(
                        StartSessionRequest(
                            task=(
                                "Use the terminal/shell action exactly once; this must exercise "
                                "the materialized agentapi helper. Read input.txt. In a child "
                                "shell, attempt to create workspace-child-forbidden and require "
                                "that the write fails. Only after that failure, write the exact "
                                "word child-blocked to "
                                "~/.gemini/.agent-collab-sandbox-acceptance. Report the observed "
                                "results. Do not inspect, print, or modify credentials."
                            ),
                            workflow="solo",
                            members={"claude_cli": "antigravity_cli"},
                            workdir=workspace,
                            max_turns=1,
                            timeout=300,
                            sandbox="read-only",
                        )
                    )
                    result = await manager.wait_result(started.session_id, timeout_ms=300_000)
                    self.assertTrue(result.settled, "Antigravity sandbox acceptance timed out")
                    final = manager.get_session(started.session_id, detail="full")
                    self.assertEqual(final.status, "done", final.failure)
                    self.assertFalse((workspace / "workspace-child-forbidden").exists())
                    self.assertEqual(
                        marker.read_text(encoding="utf-8").strip(),
                        "child-blocked",
                    )
                    helper = state / "antigravity-cli" / "bin" / "agentapi"
                    self.assertTrue(helper.is_file(), "agentapi helper was not materialized")
                    scratch_anchor = Path(daemon_home) / "runtime" / "sandbox"
                    self.assertFalse(scratch_anchor.exists() and any(scratch_anchor.iterdir()))
                finally:
                    marker.unlink(missing_ok=True)
                    for key, value in previous.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value

        asyncio.run(run())

    def test_provider_memory_across_interactive_turns_direct(self):
        self._run_provider_memory_across_turns(sandbox="none")

    def test_provider_memory_across_interactive_turns_outer(self):
        raw_state = os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE")
        if not raw_state:
            self.skipTest(
                missing_reason(
                    self.provider,
                    self.backend_id,
                    "set AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE to an "
                    "operator-authorized dedicated complete .gemini directory for the paid "
                    "outer-sandbox two-turn continuity proof",
                )
            )
        state = Path(raw_state).expanduser().resolve(strict=True)
        if not state.is_dir() or state.name != ".gemini":
            self.fail(
                "AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE must be a complete .gemini directory"
            )
        self._run_provider_memory_across_turns(
            sandbox="read-only",
            home=str(state.parent),
        )

    def _run_provider_memory_across_turns(self, *, sandbox: str, home: str | None = None) -> None:
        codeword = f"SABLE-{secrets.token_hex(4).upper()}"
        prompts = []
        original_run_turn = SubprocessRunner.run_turn

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
                    members={"claude_cli": "antigravity_cli"},
                    backend_options={"antigravity_cli": self.requested_options()},
                    max_turns=1,
                    timeout=180,
                    workdir=workdir,
                    interactive=True,
                    interactive_idle_timeout=300,
                    sandbox=sandbox,
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
                    and event["raw"].get("provider_session_kind") == "conversation"
                ]
                self.assertGreaterEqual(len(session_ids), 1)
                self.assertEqual(len(set(session_ids)), 1)
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["antigravity_cli"]["provider_session_id"],
                    session_ids[0],
                )
                command_previews = [
                    event["raw"]["command_preview"]
                    for event in events
                    if event.get("type") == "command"
                ]
                self.assertEqual(len(command_previews), 2)
                self.assertNotIn("--conversation", command_previews[0])
                self.assertIn("--conversation", command_previews[1])
                self.assertEqual(
                    command_previews[1][command_previews[1].index("--conversation") + 1],
                    session_ids[0],
                )
                self.assertLess(
                    command_previews[1].index("--conversation"),
                    command_previews[1].index("-p"),
                )

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
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as daemon_home,
        ):
            home_path = Path(daemon_home)
            (home_path / "config.toml").write_text(
                ("schema_version = 12\n\n[backends.antigravity_cli]\nenabled = true\n"),
                encoding="utf-8",
            )
            previous = {
                "AGENT_COLLAB_HOME": os.environ.get("AGENT_COLLAB_HOME"),
                "HOME": os.environ.get("HOME"),
            }
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            if home is not None:
                os.environ["HOME"] = home
            try:
                with mock.patch.object(SubprocessRunner, "run_turn", recording_run_turn):
                    asyncio.run(scenario(Path(tmp).resolve()))
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
