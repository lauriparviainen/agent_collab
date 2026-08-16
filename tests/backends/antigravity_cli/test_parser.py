import json
import unittest
from pathlib import Path

from agent_collab.backends.antigravity_cli.parser import (
    AntigravityStreamingParser,
    parse_antigravity_line,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "antigravity"
ROOT_CONVERSATION_ID = "00000000-0000-4000-8000-000000000001"
CHILD_CONVERSATION_ID = "00000000-0000-4000-8000-000000000099"


def _identities(events):
    return [
        event.provider_session
        for event in events
        if event is not None and event.provider_session is not None
    ]


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _parse_fixture(name: str, *, verbose: bool = False):
    parser = AntigravityStreamingParser()
    events = []
    for line in _lines(name):
        parsed = parser(line, verbose)
        if parsed is None:
            continue
        events.extend(parsed if isinstance(parsed, list) else [parsed])
    finish = parser.finish()
    if finish is not None:
        events.extend(finish if isinstance(finish, list) else [finish])
    return events, parser.take_terminal_evidence()


class AntigravityStreamJsonParserTests(unittest.TestCase):
    def test_success_fixture_types_init_step_update_and_result(self):
        events, evidence = _parse_fixture("stream-json-success.ndjson", verbose=True)
        self.assertEqual([(item.outcome, item.code) for item in evidence], [("completed", None)])
        self.assertTrue(any(event.type == "status" and event.text == "init" for event in events))
        self.assertTrue(
            any(event.type == "status" and event.text == "user_input" for event in events)
        )
        messages = [event for event in events if event.type == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("ready", messages[0].text)
        identities = _identities(events)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["provider_session_id"], ROOT_CONVERSATION_ID)
        self.assertEqual(identities[0]["provider_session_kind"], "conversation")
        self.assertEqual(identities[0]["agent_id"], "antigravity")

    def test_failed_terminal_result_is_structural_failure(self):
        events, evidence = _parse_fixture("stream-json-failed.ndjson")
        self.assertEqual(
            [(item.outcome, item.code) for item in evidence],
            [("failed", "provider_terminal_failure")],
        )
        self.assertEqual(evidence[0].provider_stop_reason, "ERROR")
        failures = [event for event in events if event.raw.get("fatal")]
        self.assertEqual(len(failures), 1)
        self.assertNotIn("invalid model selection", failures[0].text)
        self.assertTrue(all(event.provider_session is None for event in events))

    def test_malformed_ndjson_raises(self):
        parser = AntigravityStreamingParser()
        with self.assertRaises(ValueError):
            parser("this is not json")
        with self.assertRaises(ValueError):
            parse_antigravity_line("{not-json")

    def test_unknown_nonterminal_does_not_complete_or_capture_identity(self):
        events, evidence = _parse_fixture("stream-json-unknown-nonterminal.ndjson", verbose=True)
        self.assertEqual([(item.outcome, item.code) for item in evidence], [("completed", None)])
        self.assertTrue(
            any(event.type == "status" and "progress" in event.text for event in events)
        )
        identities = _identities(events)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["provider_session_id"], ROOT_CONVERSATION_ID)
        self.assertTrue(any(event.type == "message" for event in events))

    def test_unknown_terminal_status_fails(self):
        _events, evidence = _parse_fixture("stream-json-unknown-terminal.ndjson")
        self.assertEqual(
            [(item.outcome, item.code) for item in evidence],
            [("failed", "provider_terminal_failure")],
        )
        self.assertEqual(evidence[0].provider_stop_reason, "WEIRD_STATUS")

    def test_subagent_conversation_id_is_not_root_identity(self):
        events, evidence = _parse_fixture("stream-json-subagent.ndjson", verbose=True)
        self.assertEqual([(item.outcome, item.code) for item in evidence], [("completed", None)])
        identities = _identities(events)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["provider_session_id"], ROOT_CONVERSATION_ID)
        self.assertNotEqual(identities[0]["provider_session_id"], CHILD_CONVERSATION_ID)
        raw = json.dumps([event.raw for event in events])
        self.assertIn(CHILD_CONVERSATION_ID, raw)
        self.assertTrue(
            all(
                (event.provider_session or {}).get("provider_session_id") != CHILD_CONVERSATION_ID
                for event in events
            )
        )

    def test_plain_text_sample_is_not_success(self):
        parser = AntigravityStreamingParser()
        with self.assertRaises(ValueError):
            parser(
                (FIXTURES / "agy-print-sample.stdout.txt")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
        with self.assertRaises(ValueError):
            parse_antigravity_line("### Supported Modes")
        self.assertEqual(parser.take_terminal_evidence(), [])

    def test_missing_result_leaves_no_terminal_evidence(self):
        events, evidence = _parse_fixture("stream-json-missing-result.ndjson")
        self.assertEqual(evidence, [])
        self.assertTrue(any(event.type == "message" for event in events))

    def test_invalid_result_is_structural_failure(self):
        events, evidence = _parse_fixture("stream-json-invalid-result.ndjson")
        self.assertEqual(
            [(item.outcome, item.code) for item in evidence],
            [("failed", "provider_terminal_failure")],
        )
        self.assertTrue(any(event.raw.get("fatal") for event in events))

    def test_blank_lines_are_ignored(self):
        self.assertIsNone(parse_antigravity_line(""))
        self.assertIsNone(parse_antigravity_line("   \n"))
        self.assertIsNone(AntigravityStreamingParser()("\t\n"))

    def test_stateless_helper_captures_root_id_and_not_prose(self):
        parsed = parse_antigravity_line(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": ROOT_CONVERSATION_ID,
                        "status": "SUCCESS",
                        "response": "ready\n",
                    },
                }
            ),
            agent_id="reviewer",
        )
        events = parsed if isinstance(parsed, list) else [parsed]
        identities = _identities(events)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["provider_session_id"], ROOT_CONVERSATION_ID)
        self.assertEqual(identities[0]["provider_session_kind"], "conversation")
        self.assertEqual(identities[0]["agent_id"], "reviewer")
        messages = [event for event in events if event.type == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "ready\n")
        with self.assertRaises(ValueError):
            parse_antigravity_line("### Supported Modes")

    def test_reset_discards_prior_turn_deltas_without_hiding_result_response(self):
        parser = AntigravityStreamingParser()
        leaked = json.dumps(
            {
                "event": "step_update",
                "step_update": {"text_delta": "LEAKED FROM PRIOR TURN\n"},
            }
        )
        self.assertIsNone(parser(leaked))
        with self.assertRaises(ValueError):
            parser("not-json")
        self.assertIsNone(parser.reset())
        self.assertIsNone(parser.finish())
        self.assertEqual(parser.take_terminal_evidence(), [])

        parsed = parser(
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "actual response\n"},
                }
            )
        )
        events = parsed if isinstance(parsed, list) else [parsed]
        messages = [event for event in events if event.type == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "actual response\n")
        self.assertNotIn("LEAKED", messages[0].text)
        self.assertEqual(
            [(item.outcome, item.code) for item in parser.take_terminal_evidence()],
            [("completed", None)],
        )

    def test_reset_discards_leftover_terminal_evidence_without_emitting(self):
        parser = AntigravityStreamingParser()
        parsed = parser(
            json.dumps({"event": "result", "result": {"status": "ERROR", "response": "boom"}})
        )
        events = [] if parsed is None else (parsed if isinstance(parsed, list) else [parsed])
        self.assertTrue(any(event.raw.get("fatal") for event in events))
        self.assertIsNone(parser.reset())
        self.assertEqual(parser.take_terminal_evidence(), [])
        self.assertIsNone(parser.finish())
