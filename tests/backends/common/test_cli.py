import unittest

from agent_collab.backends.common.cli import insert_before_print_prompt


class PrintPromptInsertionTests(unittest.TestCase):
    def test_inserts_before_grok_short_single_turn_flag(self):
        self.assertEqual(
            insert_before_print_prompt(["grok", "-p"], ["--model", "grok-build"]),
            ["grok", "--model", "grok-build", "-p"],
        )

    def test_inserts_before_grok_long_single_turn_flag(self):
        self.assertEqual(
            insert_before_print_prompt(["grok", "--single"], ["--model", "grok-build"]),
            ["grok", "--model", "grok-build", "--single"],
        )


if __name__ == "__main__":
    unittest.main()
