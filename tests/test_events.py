import unittest

from agent_collab.events import parse_claude_line, parse_codex_line


class EventParsingTests(unittest.TestCase):
    def test_claude_message_text(self):
        event = parse_claude_line('{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}')
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "claude")
        self.assertEqual(event.type, "message")
        self.assertEqual(event.text, "hello")

    def test_claude_system_event_hidden_without_verbose(self):
        event = parse_claude_line('{"type":"system","subtype":"init","model":"claude-sonnet-4-6"}')
        self.assertIsNone(event)

    def test_claude_system_event_status_with_verbose(self):
        event = parse_claude_line('{"type":"system","subtype":"init","model":"claude-sonnet-4-6"}', verbose=True)
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "claude")
        self.assertEqual(event.type, "status")
        self.assertEqual(event.text, "init")

    def test_codex_tool_call(self):
        event = parse_codex_line('{"type":"tool_call","name":"exec_command","text":"running tests"}')
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "tool")
        self.assertEqual(event.type, "command")

    def test_codex_nested_agent_message(self):
        event = parse_codex_line('{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Understood."}}')
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "codex")
        self.assertEqual(event.type, "message")
        self.assertEqual(event.text, "Understood.")

    def test_codex_nested_command_event(self):
        event = parse_codex_line('{"type":"item.completed","item":{"type":"command_execution","output":"ran tests"}}')
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "tool")
        self.assertEqual(event.type, "command")
        self.assertEqual(event.text, "ran tests")

    def test_unknown_verbose(self):
        event = parse_codex_line('{"unexpected":true}', verbose=True)
        self.assertIsNotNone(event)
        self.assertEqual(event.type, "status")


if __name__ == "__main__":
    unittest.main()
