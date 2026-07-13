import unittest

from agent_collab import events
from agent_collab.events import Event


class EventCreateCoercionTests(unittest.TestCase):
    """Invalid source/type coercion must be logged, never silent (M4)."""

    def setUp(self):
        events._warned_coercions.clear()

    def test_invalid_source_is_coerced_to_error_with_a_warning(self):
        with self.assertLogs("agent_collab.events", level="WARNING") as logs:
            event = Event.create("not-a-source", "status", "hello")

        self.assertEqual(event.source, "error")
        self.assertEqual(event.type, "status")
        self.assertIn("'not-a-source'", logs.output[0])
        self.assertIn("coercing invalid event source", logs.output[0])

    def test_invalid_type_is_coerced_to_status_with_a_warning(self):
        with self.assertLogs("agent_collab.events", level="WARNING") as logs:
            event = Event.create("claude", "not-a-type", "hello")

        self.assertEqual(event.source, "claude")
        self.assertEqual(event.type, "status")
        self.assertIn("'not-a-type'", logs.output[0])
        self.assertIn("coercing invalid event type", logs.output[0])

    def test_invalid_source_and_type_log_the_original_values(self):
        with self.assertLogs("agent_collab.events", level="WARNING") as logs:
            event = Event.create("bogus-source", "bogus-type", "hello")

        self.assertEqual((event.source, event.type), ("error", "status"))
        self.assertEqual(len(logs.output), 2)
        # The type warning must carry the pre-coercion source, not "error".
        self.assertIn("source='bogus-source'", logs.output[1])
        self.assertIn("'bogus-type'", logs.output[1])

    def test_repeated_identical_coercion_warns_once(self):
        with self.assertLogs("agent_collab.events", level="WARNING") as logs:
            Event.create("repeat-source", "status", "one")
            Event.create("repeat-source", "status", "two")

        self.assertEqual(len(logs.output), 1)

    def test_warned_set_stays_bounded_and_keeps_warning_past_the_cap(self):
        with self.assertLogs("agent_collab.events", level="WARNING") as logs:
            for index in range(events._WARNED_COERCIONS_CAP + 5):
                Event.create(f"bad-{index}", "status", "hello")

        self.assertEqual(len(logs.output), events._WARNED_COERCIONS_CAP + 5)
        self.assertLessEqual(len(events._warned_coercions), events._WARNED_COERCIONS_CAP)

    def test_valid_inputs_do_not_log(self):
        with self.assertNoLogs("agent_collab.events", level="WARNING"):
            event = Event.create("referee", "message", "hello")

        self.assertEqual((event.source, event.type), ("referee", "message"))

    def test_agent_id_is_additive_and_defaults_to_null(self):
        unattributed = Event.create("referee", "status", "ready")
        attributed = Event.create("claude", "message", "review", agent_id="claude-reviewer")

        self.assertIsNone(unattributed.agent_id)
        self.assertIsNone(unattributed.to_dict()["agent_id"])
        self.assertEqual(attributed.agent_id, "claude-reviewer")
        self.assertEqual(attributed.to_dict()["agent_id"], "claude-reviewer")


if __name__ == "__main__":
    unittest.main()
