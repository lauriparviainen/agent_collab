"""The CLI client keeps the daemon's structured error code on its surface."""

from __future__ import annotations

import unittest

from agent_collab.client import _format_error_payload


class FormatErrorPayloadTests(unittest.TestCase):
    def test_structured_code_is_kept(self):
        text = _format_error_payload(
            {"error": "session status 'done' is not resumable", "code": "ineligible"}
        )
        self.assertEqual(text, "session status 'done' is not resumable (code=ineligible)")

    def test_missing_code_leaves_message_alone(self):
        self.assertEqual(_format_error_payload({"error": "unknown session"}), "unknown session")

    def test_code_equal_to_error_is_not_repeated(self):
        self.assertEqual(
            _format_error_payload({"error": "not_found", "code": "not_found"}), "not_found"
        )

    def test_invalid_start_options_details_still_win(self):
        text = _format_error_payload(
            {
                "error": "invalid_start_options",
                "code": "invalid_start_options",
                "details": [{"path": "workflow", "message": "unknown workflow"}],
            }
        )
        self.assertEqual(text, "invalid_start_options\nworkflow: unknown workflow")


if __name__ == "__main__":
    unittest.main()
