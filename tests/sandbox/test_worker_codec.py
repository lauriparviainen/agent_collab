"""Hermetic coverage for the SDK worker frame codec."""

from __future__ import annotations

import unittest

from agent_collab.events import Event
from agent_collab.outcomes import TurnOutcome
from agent_collab.sandbox.sdk_worker import event_source_for_backend
from agent_collab.sandbox.worker_codec import (
    WorkerProtocolError,
    decode_frame,
    encode_frame,
    event_from_payload,
    event_to_payload,
    make_frame,
    outcome_to_payload,
    validate_envelope,
)


class WorkerCodecTests(unittest.TestCase):
    def test_round_trip_frame(self) -> None:
        payload = make_frame("hello", instance="abc", worker_pid=7)
        restored = decode_frame(encode_frame(payload)[4:])
        self.assertEqual(restored["type"], "hello")
        self.assertEqual(restored["instance"], "abc")

    def test_rejects_duplicate_json_fields(self) -> None:
        raw = b'{"protocol":"agent-collab-sdk-worker","protocol":"x","type":"hello","version":1}'
        with self.assertRaises(WorkerProtocolError):
            decode_frame(raw)

    def test_validate_direction(self) -> None:
        frame = make_frame("hello", instance="x")
        validate_envelope(frame, expected_direction="worker")
        with self.assertRaises(WorkerProtocolError):
            validate_envelope(frame, expected_direction="daemon")

    def test_event_and_outcome_payloads(self) -> None:
        event = Event.create("codex", "message", "hello", {"text": "hello"})
        event.mark_provider_session(
            agent_id="reviewer",
            session_id="thread-9",
            kind="thread",
        )
        payload = event_to_payload(event)
        self.assertEqual(payload["provider_session"]["provider_session_id"], "thread-9")
        restored = event_from_payload(payload)
        self.assertEqual(restored.text, "hello")
        self.assertEqual(restored.source, "codex")
        self.assertEqual(restored.provider_session["agent_id"], "reviewer")
        outcome = TurnOutcome("completed")
        self.assertEqual(outcome_to_payload(outcome)["outcome"], "completed")
        failed = TurnOutcome("failed", "provider_transport_failed")
        self.assertEqual(outcome_to_payload(failed)["code"], "provider_transport_failed")

    def test_event_source_for_backend_strips_shape_suffix(self) -> None:
        self.assertEqual(event_source_for_backend("claude_sdk"), "claude")
        self.assertEqual(event_source_for_backend("codex_sdk"), "codex")
        self.assertEqual(event_source_for_backend("claude_cli"), "claude")

    def test_encode_frame_counts_utf8_bytes_not_python_chars(self) -> None:
        from agent_collab.sandbox.worker_codec import encode_frame, make_frame

        # Multibyte text is larger on the wire than len(text) or len(repr(...)).
        text = "测" * 1000
        payload = make_frame(
            "event",
            run_id="r1",
            sequence=1,
            event={"source": "claude", "type": "message", "text": text},
        )
        encoded = encode_frame(payload)
        self.assertGreater(len(encoded), len(text))
        self.assertGreater(len(encoded), len(repr(payload.get("event"))))


if __name__ == "__main__":
    unittest.main()
