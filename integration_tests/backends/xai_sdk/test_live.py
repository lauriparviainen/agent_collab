import os

from integration_tests.harness import LiveBackendTestCase, missing_reason


class XaiSdkLiveTests(LiveBackendTestCase):
    provider = "xai"
    backend_id = "sdk"

    def setUp(self):
        super().setUp()
        if not os.environ.get("XAI_API_KEY"):
            self.skipTest(missing_reason(self.provider, self.backend_id, "XAI_API_KEY is missing"))

    def test_turn_and_response(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "response")
        self.assertFalse(
            any(event.type in {"tool_call", "command", "file_change"} for event in events)
        )
