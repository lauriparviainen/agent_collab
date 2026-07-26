import asyncio
import os
from pathlib import Path
import tempfile

from agent_collab.daemon import SessionManager, StartSessionRequest

from integration_tests.harness import LiveBackendTestCase, missing_reason


class AntigravityCliLiveTests(LiveBackendTestCase):
    provider = "antigravity"
    backend_id = "cli"

    def test_turn(self):
        self.assert_message(self.run_live())

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
                    final = await manager.wait_session(started.session_id)
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
