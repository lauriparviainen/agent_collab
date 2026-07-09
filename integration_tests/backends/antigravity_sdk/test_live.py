import os
from pathlib import Path
import shutil
import subprocess

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

    def setUp(self):
        super().setUp()
        self.adc_path = Path(
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_ADC_PATH))
        ).expanduser().resolve()
        if not self.adc_path.is_file():
            self.skipTest(missing_reason(self.provider, self.backend_id, "Google ADC file is missing"))
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
