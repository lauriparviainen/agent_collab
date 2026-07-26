import asyncio
import os
from pathlib import Path
import subprocess
import tempfile

from agent_collab.daemon import SessionManager, StartSessionRequest

from integration_tests.harness import LiveBackendTestCase, missing_reason


class XaiCliLiveTests(LiveBackendTestCase):
    provider = "xai"
    backend_id = "cli"

    def requested_options(self):
        options = super().requested_options()
        # Keep this explicit so the live transport test follows the model
        # reported by the installed Grok CLI's local catalog.
        options["model"] = os.environ.get("AGENT_COLLAB_IT_XAI_MODEL", "grok-4.5")
        return options

    def prepare_workdir(self, workdir):
        subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)

    def test_turn_and_session(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "session")

    def test_outer_read_only_complete_state_bash_acceptance(self):
        raw_state = os.environ.get("AGENT_COLLAB_IT_XAI_SANDBOX_STATE")
        if not raw_state:
            self.skipTest(
                missing_reason(
                    self.provider,
                    self.backend_id,
                    "set AGENT_COLLAB_IT_XAI_SANDBOX_STATE to an operator-authorized "
                    "dedicated complete .grok directory for the paid outer-sandbox acceptance",
                )
            )
        state = Path(raw_state).expanduser().resolve(strict=True)
        if not state.is_dir() or state.name != ".grok":
            self.fail("AGENT_COLLAB_IT_XAI_SANDBOX_STATE must be a complete .grok directory")
        marker = state / ".agent-collab-sandbox-acceptance"
        descendant_marker = state / ".agent-collab-sandbox-descendant-survived"
        if marker.exists() or descendant_marker.exists():
            self.fail("a guarded xAI acceptance marker already exists")

        async def run() -> None:
            with (
                tempfile.TemporaryDirectory(prefix="agent-collab-xai-boundary-") as raw,
                tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as daemon_home,
            ):
                workspace = Path(raw).resolve()
                workspace.chmod(0o700)
                subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
                (workspace / "input.txt").write_text(
                    "xai sandbox acceptance\n",
                    encoding="utf-8",
                )
                legacy_host_marker = Path("/tmp/agent-collab-xai-legacy-acceptance")
                if legacy_host_marker.exists():
                    self.fail("the guarded host legacy-temp marker already exists")
                previous = {
                    "HOME": os.environ.get("HOME"),
                    "GROK_HOME": os.environ.get("GROK_HOME"),
                    "AGENT_COLLAB_HOME": os.environ.get("AGENT_COLLAB_HOME"),
                }
                os.environ["HOME"] = str(state.parent)
                os.environ["GROK_HOME"] = str(state)
                os.environ["AGENT_COLLAB_HOME"] = daemon_home
                manager = SessionManager(
                    default_workdir=workspace,
                    default_log_dir=workspace / "logs",
                )
                try:
                    started = await manager.start_session(
                        StartSessionRequest(
                            task=(
                                "Use Bash exactly once. In that single Bash action: read "
                                "input.txt; require direct and child-shell creation of files "
                                "workspace-direct-forbidden and workspace-child-forbidden to "
                                "fail; require `git tag forbidden` to fail; write and read a "
                                "scratch marker below $TMPDIR; write "
                                "/tmp/agent-collab-xai-legacy-acceptance (this is private "
                                "sandbox temporary space); start one background child whose "
                                "infinite loop waits 0.25 seconds and then overwrites "
                                "$GROK_HOME/.agent-collab-sandbox-descendant-survived with the "
                                "word survived on every iteration, with all of that child's "
                                "standard streams redirected from or to /dev/null; do not wait "
                                "for it; only "
                                "after every required denial/success, write the exact word "
                                "boundary-ok to "
                                "$GROK_HOME/.agent-collab-sandbox-acceptance. Report results. "
                                "Do not inspect, print, or modify credentials or configuration."
                            ),
                            workflow="solo",
                            members={"claude_cli": "xai_cli"},
                            workdir=workspace,
                            max_turns=1,
                            timeout=300,
                            sandbox="read-only",
                        )
                    )
                    result = await manager.wait_result(started.session_id, timeout_ms=300_000)
                    self.assertTrue(result.settled, "xAI sandbox acceptance timed out")
                    final = manager.get_session(started.session_id, detail="full")
                    self.assertEqual(final.status, "done", final.failure)
                    self.assertFalse((workspace / "workspace-direct-forbidden").exists())
                    self.assertFalse((workspace / "workspace-child-forbidden").exists())
                    self.assertEqual(
                        marker.read_text(encoding="utf-8").strip(),
                        "boundary-ok",
                    )
                    self.assertFalse(legacy_host_marker.exists())
                    # The child may write while the provider is legitimately
                    # still producing its final answer. Discard that evidence
                    # only after the supervised turn settles; a namespace
                    # survivor will recreate the marker on its next iteration.
                    descendant_marker.unlink(missing_ok=True)
                    await asyncio.sleep(2)
                    self.assertFalse(
                        descendant_marker.exists(),
                        "a Grok descendant survived namespace teardown",
                    )
                    scratch_anchor = Path(daemon_home) / "runtime" / "sandbox"
                    self.assertFalse(scratch_anchor.exists() and any(scratch_anchor.iterdir()))
                finally:
                    marker.unlink(missing_ok=True)
                    descendant_marker.unlink(missing_ok=True)
                    legacy_host_marker.unlink(missing_ok=True)
                    for key, value in previous.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value

        asyncio.run(run())
