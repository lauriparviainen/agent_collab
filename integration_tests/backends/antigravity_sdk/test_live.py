from integration_tests.harness import LiveBackendTestCase


class AntigravitySdkLiveTests(LiveBackendTestCase):
    provider = "antigravity"
    backend_id = "sdk"

    def test_turn_tools_and_conversation(self):
        events = self.run_live(
            "Create sdk-smoke.txt containing ready, read it, then reply with the single word ready."
        )
        self.assert_message(events)
        self.assert_session_kind(events, "conversation")
        self.assertTrue(any(event.source == "tool" for event in events))
