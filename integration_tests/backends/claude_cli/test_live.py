from integration_tests.harness import LiveBackendTestCase


class ClaudeCliLiveTests(LiveBackendTestCase):
    provider = "claude"
    backend_id = "cli"

    def test_turn(self):
        self.assert_message(self.run_live())
