import unittest

from agent_collab.backends.common.cli import config_value, flag_value, insert_before_print_prompt


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


class CliValueInferenceTests(unittest.TestCase):
    def test_flag_value_uses_last_separate_or_equals_occurrence(self):
        self.assertEqual(
            flag_value(["--model", "first", "--model=second"], "--model"),
            "second",
        )
        self.assertEqual(
            flag_value(["--model=first", "--model", "second"], "--model"),
            "second",
        )

    def test_config_value_uses_last_short_long_or_equals_occurrence(self):
        args = [
            "-c",
            'model_reasoning_effort="low"',
            "--config=model_reasoning_effort='medium'",
            "--config",
            'model_reasoning_effort="high"',
        ]
        self.assertEqual(config_value(args, "model_reasoning_effort"), "high")


if __name__ == "__main__":
    unittest.main()
