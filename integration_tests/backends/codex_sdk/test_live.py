from integration_tests.harness import LiveBackendTestCase


class CodexSdkLiveTests(LiveBackendTestCase):
    provider = "codex"
    backend_id = "sdk"

    def test_turn_and_thread(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "thread")

    def test_model_catalog(self):
        observation = self.discover_live_models()
        self.assertEqual(observation.status, "ok")
        self.assertEqual(observation.source, "sdk")
        self.assertTrue(observation.complete)
        self.assertTrue(observation.models)
