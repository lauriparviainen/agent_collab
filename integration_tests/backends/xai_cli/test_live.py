import os
import subprocess

from integration_tests.harness import LiveBackendTestCase


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
