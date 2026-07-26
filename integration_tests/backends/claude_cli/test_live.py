import asyncio
import os
from pathlib import Path
import tempfile

from agent_collab.daemon import SessionManager, StartSessionRequest

from integration_tests.harness import LiveBackendTestCase
from integration_tests.harness import missing_reason


class ClaudeCliLiveTests(LiveBackendTestCase):
    provider = "claude"
    backend_id = "cli"

    def test_turn(self):
        self.assert_message(self.run_live())

    def test_outer_read_only_complete_state_acceptance(self):
        raw_state = os.environ.get("AGENT_COLLAB_IT_CLAUDE_SANDBOX_STATE")
        if not raw_state:
            self.skipTest(
                missing_reason(
                    self.provider,
                    self.backend_id,
                    "set AGENT_COLLAB_IT_CLAUDE_SANDBOX_STATE to an operator-authorized "
                    "dedicated complete CLAUDE_CONFIG_DIR for the paid outer-sandbox acceptance",
                )
            )
        state = Path(raw_state).expanduser().resolve(strict=True)
        if not state.is_dir():
            self.fail("AGENT_COLLAB_IT_CLAUDE_SANDBOX_STATE must be a directory")
        marker = state / ".agent-collab-sandbox-acceptance"
        if marker.exists():
            self.fail("the guarded Claude acceptance marker already exists")

        async def run() -> None:
            with (
                tempfile.TemporaryDirectory(prefix="agent-collab-claude-boundary-") as raw,
                tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
            ):
                workspace = Path(raw).resolve()
                workspace.chmod(0o700)
                (workspace / "input.txt").write_text(
                    "claude sandbox acceptance\n",
                    encoding="utf-8",
                )
                previous = {
                    "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR"),
                    "AGENT_COLLAB_HOME": os.environ.get("AGENT_COLLAB_HOME"),
                }
                os.environ["CLAUDE_CONFIG_DIR"] = str(state)
                os.environ["AGENT_COLLAB_HOME"] = home
                manager = SessionManager(
                    default_workdir=workspace,
                    default_log_dir=workspace / "logs",
                )
                try:
                    started = await manager.start_session(
                        StartSessionRequest(
                            task=(
                                "Use the Bash tool once. Read input.txt. In a child shell, "
                                "attempt to create workspace-child-forbidden and require that "
                                "the write fails. Only after that failure, write the exact word "
                                "child-blocked to "
                                "$CLAUDE_CONFIG_DIR/.agent-collab-sandbox-acceptance. "
                                "Report the observed results."
                            ),
                            workflow="solo",
                            workdir=workspace,
                            max_turns=1,
                            timeout=300,
                            sandbox="read-only",
                        )
                    )
                    result = await manager.wait_result(started.session_id, timeout_ms=300_000)
                    self.assertTrue(result.settled, "Claude sandbox acceptance timed out")
                    final = manager.get_session(started.session_id, detail="full")
                    self.assertEqual(final.status, "done", final.failure)
                    self.assertFalse((workspace / "workspace-child-forbidden").exists())
                    self.assertEqual(
                        marker.read_text(encoding="utf-8").strip(),
                        "child-blocked",
                    )
                    scratch_anchor = Path(home) / "runtime" / "sandbox"
                    self.assertFalse(scratch_anchor.exists() and any(scratch_anchor.iterdir()))
                finally:
                    marker.unlink(missing_ok=True)
                    for key, value in previous.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value

        asyncio.run(run())
