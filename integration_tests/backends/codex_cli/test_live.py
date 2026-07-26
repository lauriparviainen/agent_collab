import asyncio
import os
from pathlib import Path
import tempfile

from agent_collab.daemon import SessionManager, StartSessionRequest

from integration_tests.harness import LiveBackendTestCase, missing_reason


class CodexCliLiveTests(LiveBackendTestCase):
    provider = "codex"
    backend_id = "cli"

    def test_turn(self):
        self.assert_message(self.run_live())

    def test_outer_read_only_complete_state_acceptance(self):
        raw_state = os.environ.get("AGENT_COLLAB_IT_CODEX_SANDBOX_STATE")
        if not raw_state:
            self.skipTest(
                missing_reason(
                    self.provider,
                    self.backend_id,
                    "set AGENT_COLLAB_IT_CODEX_SANDBOX_STATE to an operator-authorized "
                    "dedicated complete CODEX_HOME for the paid outer-sandbox acceptance",
                )
            )
        state = Path(raw_state).expanduser().resolve(strict=True)
        if not state.is_dir():
            self.fail("AGENT_COLLAB_IT_CODEX_SANDBOX_STATE must be a directory")
        marker = state / ".agent-collab-sandbox-acceptance"
        if marker.exists():
            self.fail("the guarded Codex acceptance marker already exists")

        async def run() -> None:
            with (
                tempfile.TemporaryDirectory(prefix="agent-collab-codex-boundary-") as raw,
                tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
            ):
                workspace = Path(raw).resolve()
                workspace.chmod(0o700)
                (workspace / "input.txt").write_text("sandbox acceptance\n", encoding="utf-8")
                previous = {
                    "CODEX_HOME": os.environ.get("CODEX_HOME"),
                    "AGENT_COLLAB_HOME": os.environ.get("AGENT_COLLAB_HOME"),
                }
                os.environ["CODEX_HOME"] = str(state)
                os.environ["AGENT_COLLAB_HOME"] = home
                manager = SessionManager(
                    default_workdir=workspace,
                    default_log_dir=workspace / "logs",
                )
                try:
                    started = await manager.start_session(
                        StartSessionRequest(
                            task=(
                                "Use the shell tool once. Read input.txt. Attempt to create "
                                "workspace-forbidden and confirm that it fails. Then write the "
                                "word ready to $CODEX_HOME/.agent-collab-sandbox-acceptance "
                                "and report both results."
                            ),
                            workflow="solo",
                            members={"claude_cli": "codex_cli"},
                            workdir=workspace,
                            max_turns=1,
                            timeout=300,
                            sandbox="read-only",
                        )
                    )
                    result = await manager.wait_result(started.session_id, timeout_ms=300_000)
                    self.assertTrue(result.settled, "Codex sandbox acceptance timed out")
                    final = manager.get_session(started.session_id, detail="full")
                    self.assertEqual(final.status, "done", final.failure)
                    self.assertFalse((workspace / "workspace-forbidden").exists())
                    self.assertEqual(marker.read_text(encoding="utf-8").strip(), "ready")
                finally:
                    marker.unlink(missing_ok=True)
                    for key, value in previous.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value

        asyncio.run(run())
