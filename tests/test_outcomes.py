import unittest

from agent_collab.outcomes import (
    CANONICAL_MESSAGES,
    SessionFailure,
    TerminalEvidence,
    TerminalEvidenceAccumulator,
    TurnOutcome,
    TurnOutcomeRecord,
)


class EvidencePrecedenceTests(unittest.TestCase):
    def test_normally_finishing_precedence(self):
        cases = []

        fatal = TerminalEvidenceAccumulator()
        fatal.add(TerminalEvidence("completed"))
        fatal.add(TerminalEvidence("failed", "provider_terminal_failure"))
        cases.append(
            (fatal.resolve(exception_code="provider_output_invalid"), "provider_protocol_conflict")
        )

        parser_error = TerminalEvidenceAccumulator()
        parser_error.add(TerminalEvidence("completed"))
        cases.append(
            (
                parser_error.resolve(exception_code="provider_output_invalid"),
                "provider_output_invalid",
            )
        )

        nonzero = TerminalEvidenceAccumulator()
        nonzero.add(TerminalEvidence("completed"))
        cases.append((nonzero.resolve(process_exit_code=9), "subprocess_exit_nonzero"))

        success = TerminalEvidenceAccumulator()
        success.add(TerminalEvidence("completed"))
        cases.append((success.resolve(process_exit_code=0), None))

        fallback = TerminalEvidenceAccumulator()
        cases.append(
            (
                fallback.resolve(
                    process_exit_code=0, clean_eof_fallback=True, produced_message=True
                ),
                None,
            )
        )

        self.assertEqual([outcome.code for outcome, _ in cases], [code for _, code in cases])
        self.assertEqual(
            [outcome.outcome for outcome, _ in cases],
            ["failed", "failed", "failed", "completed", "completed"],
        )

    def test_explicit_fatal_evidence_beats_exception_and_exit(self):
        evidence = TerminalEvidenceAccumulator()
        evidence.add(
            TerminalEvidence(
                "cancelled", "provider_turn_cancelled", provider_stop_reason="Cancelled"
            )
        )
        outcome = evidence.resolve(exception_code="provider_output_invalid", process_exit_code=7)
        self.assertEqual(outcome.outcome, "cancelled")
        self.assertEqual(outcome.code, "provider_turn_cancelled")
        self.assertEqual(outcome.process_exit_code, 7)

    def test_identical_duplicate_terminal_evidence_is_deduplicated(self):
        evidence = TerminalEvidenceAccumulator()
        marker = TerminalEvidence("completed", provider_stop_reason="EndTurn")
        evidence.add(marker)
        evidence.add(marker)
        self.assertEqual(evidence.resolve(process_exit_code=0).outcome, "completed")

    def test_partial_output_does_not_complete_marker_transport(self):
        outcome = TerminalEvidenceAccumulator().resolve(process_exit_code=0, produced_message=True)
        self.assertEqual((outcome.outcome, outcome.code), ("failed", "provider_output_incomplete"))


class OutcomeSanitizationTests(unittest.TestCase):
    def test_canonical_message_cannot_be_replaced_by_hostile_exception_text(self):
        hostile = "Bearer secret-token /home/private prompt contents"
        with self.assertRaises(ValueError):
            TurnOutcome("failed", "provider_transport_failed", hostile)
        outcome = TurnOutcome("failed", "provider_transport_failed")
        self.assertEqual(outcome.message, CANONICAL_MESSAGES["provider_transport_failed"])
        self.assertNotIn("secret-token", str(outcome.to_dict()))

    def test_unknown_and_oversized_provider_tokens_are_dropped(self):
        for token in ("Bearer secret-token", "X" * 500):
            outcome = TurnOutcome("failed", "provider_terminal_failure", provider_stop_reason=token)
            self.assertIsNone(outcome.provider_stop_reason)

    def test_retry_after_is_typed_bounded_and_round_trips(self):
        outcome = TurnOutcome("refused", "provider_turn_refused", retry_after_seconds=90)
        record = TurnOutcomeRecord.from_outcome(
            turn_id="turn-1",
            stage_index=1,
            agent_id="reviewer",
            backend="xai_sdk",
            outcome=outcome,
        )
        self.assertEqual(record.to_dict()["retry_after_seconds"], 90.0)
        self.assertIsNone(
            TurnOutcome(
                "refused", "provider_turn_refused", retry_after_seconds=999999999
            ).retry_after_seconds
        )

    def test_forged_raw_fields_are_ignored_when_loading_a_record(self):
        record = TurnOutcomeRecord.from_dict(
            {
                "turn_id": "turn-2",
                "stage_index": 2,
                "agent_id": "reviewer",
                "backend": "xai_cli",
                "outcome": "cancelled",
                "code": "provider_turn_cancelled",
                "message": CANONICAL_MESSAGES["provider_turn_cancelled"],
                "provider_stop_reason": "Cancelled",
                "process_exit_code": 0,
                "authorization": "Bearer secret-token",
                "headers": {"Authorization": "secret"},
                "prompt": "private prompt",
                "path": "/home/private/repo",
                "stderr": "unrestricted",
            }
        )
        rendered = record.to_dict()
        self.assertEqual(rendered["turn_id"], "turn-2")
        for forbidden in ("authorization", "headers", "prompt", "path", "stderr"):
            self.assertNotIn(forbidden, rendered)

    def test_stage_failure_has_nullable_turn_fields(self):
        failure = SessionFailure(code="parallel_stage_no_accepted_member", stage_index=1).to_dict()
        self.assertIsNone(failure["turn_id"])
        self.assertIsNone(failure["agent_id"])
        self.assertIsNone(failure["backend"])
        self.assertEqual(failure["message"], "No parallel reviewer produced an accepted review")

    def test_machine_paths_and_invalid_backend_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            TurnOutcomeRecord(
                turn_id="turn-1",
                stage_index=1,
                agent_id="/home/private",
                backend="xai_cli",
                outcome="completed",
            )
        with self.assertRaises(ValueError):
            TurnOutcomeRecord(
                turn_id="turn-1",
                stage_index=1,
                agent_id="reviewer",
                backend="forged_backend",
                outcome="completed",
            )


if __name__ == "__main__":
    unittest.main()
