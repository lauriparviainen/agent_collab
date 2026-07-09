from integration_tests.harness import LiveBackendTestCase


class ClaudeSdkLiveTests(LiveBackendTestCase):
    provider = "claude"
    backend_id = "sdk"

    def test_turn_and_session(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "session")
