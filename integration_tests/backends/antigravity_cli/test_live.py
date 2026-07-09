from integration_tests.harness import LiveBackendTestCase


class AntigravityCliLiveTests(LiveBackendTestCase):
    provider = "antigravity"
    backend_id = "cli"

    def test_turn(self):
        self.assert_message(self.run_live())
