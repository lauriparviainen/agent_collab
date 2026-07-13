import io
import unittest
from contextlib import redirect_stdout

from agent_collab.events import Event
from agent_collab.terminal import print_event


class PrintEventTests(unittest.TestCase):
    def _render(self, event: Event) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_event(event, color=False)
        return buffer.getvalue()

    def test_attribution_suffix_when_agent_id_differs_from_source(self):
        event = Event.create("tool", "tool_call", "Read file", agent_id="claude")
        self.assertEqual(self._render(event), "TOOL (claude) Read file\n")

    def test_no_suffix_when_agent_id_matches_source(self):
        event = Event.create("claude", "message", "hello", agent_id="claude")
        self.assertEqual(self._render(event), "CLAUDE  hello\n")

    def test_no_suffix_for_unattributed_events(self):
        event = Event.create("referee", "status", "turn 1: claude")
        self.assertEqual(self._render(event), "REFEREE turn 1: claude\n")

    def test_fatal_provider_evidence_is_hidden(self):
        event = Event.create("error", "error", "detail", {"fatal": True})
        self.assertEqual(self._render(event), "")
