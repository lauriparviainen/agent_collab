import unittest

"""CLI parser tests."""

from agent_collab.backends.claude_cli import parse_claude_line
from agent_collab.backends.codex_cli import parse_codex_line


class EventParsingTests(unittest.TestCase):
    def test_claude_message_text(self):
        event = parse_claude_line('{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}')
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "claude")
        self.assertEqual(event.type, "message")
        self.assertEqual(event.text, "hello")

    def test_claude_thinking_signature_only_hidden_without_verbose(self):
        event = parse_claude_line(
            '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"","signature":"EustCokBCA8opaque"}]}}'
        )
        self.assertIsNone(event)

    def test_claude_thinking_signature_not_exposed_with_verbose(self):
        event = parse_claude_line(
            '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"","signature":"EustCokBCA8opaque"}]}}',
            verbose=True,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.type, "status")
        self.assertNotIn("EustCokBCA8opaque", event.text)

    def test_claude_text_alongside_thinking_signature(self):
        event = parse_claude_line(
            '{"type":"assistant","message":{"content":['
            '{"type":"thinking","thinking":"pondering","signature":"EustCokBCA8opaque"},'
            '{"type":"text","text":"final answer"}]}}'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.type, "message")
        self.assertEqual(event.text, "final answer")

    def test_claude_tool_use_classifies_as_tool_call(self):
        event = parse_claude_line(
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"a.py"}}]}}'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "tool")
        self.assertEqual(event.type, "tool_call")
        self.assertIn("Read", event.text)

    def test_claude_bash_tool_use_classifies_as_command(self):
        event = parse_claude_line(
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_2","name":"Bash","input":{"command":"ls"}}]}}'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "tool")
        self.assertEqual(event.type, "command")

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
