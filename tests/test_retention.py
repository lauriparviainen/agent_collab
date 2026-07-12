import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_collab.retention import (
    TERMINAL_STATUSES,
    classify_transcript_paths,
    effective_timestamp,
    is_valid_session_id,
    parse_duration,
    parse_timestamp,
    select_expired_sessions,
    transcript_unlink_blocker,
)


NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _record(session_id, status="done", ended_at=None, updated_at=None, **extra):
    record = {"session_id": session_id, "status": status, "updated_at": updated_at}
    if ended_at is not None:
        record["ended_at"] = ended_at
    record.update(extra)
    return record


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


class ParseDurationTests(unittest.TestCase):
    def test_accepts_whole_number_units(self):
        self.assertEqual(parse_duration("1h"), timedelta(hours=1))
        self.assertEqual(parse_duration("24h"), timedelta(hours=24))
        self.assertEqual(parse_duration("7d"), timedelta(days=7))
        self.assertEqual(parse_duration("2w"), timedelta(weeks=2))
        self.assertEqual(parse_duration(" 30d "), timedelta(days=30))

    def test_rejects_invalid_durations(self):
        for value in (
            "0d",
            "-1d",
            "1.5d",
            "d",
            "7",
            "7m",
            "7 d",
            "",
            "7D",
            "٧d",  # non-ASCII digit
            None,
            7,
        ):
            with self.assertRaises(ValueError, msg=repr(value)):
                parse_duration(value)


class TimestampTests(unittest.TestCase):
    def test_parses_daemon_format(self):
        parsed = parse_timestamp("2026-07-12T10:00:00.123456+00:00")
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_naive_timestamp_is_treated_as_utc(self):
        parsed = parse_timestamp("2026-07-12T10:00:00")
        self.assertIsNotNone(parsed.tzinfo)

    def test_invalid_values_return_none(self):
        for value in (None, "", "not a date", 12345, {"nested": True}):
            self.assertIsNone(parse_timestamp(value), msg=repr(value))

    def test_effective_timestamp_prefers_ended_at(self):
        record = _record("s1", ended_at=_iso(10), updated_at=_iso(5))
        self.assertEqual(effective_timestamp(record), NOW - timedelta(days=10))

    def test_effective_timestamp_falls_back_to_updated_at(self):
        record = _record("s1", updated_at=_iso(5))
        self.assertEqual(effective_timestamp(record), NOW - timedelta(days=5))

    def test_effective_timestamp_none_when_both_unusable(self):
        self.assertIsNone(effective_timestamp(_record("s1", ended_at="junk", updated_at=None)))


class SelectExpiredSessionsTests(unittest.TestCase):
    def _select(self, records, retention_days=30, keep=0):
        return select_expired_sessions(
            records, now=NOW, retention=timedelta(days=retention_days), keep=keep
        )

    def test_exact_cutoff_boundary_is_expired(self):
        at_cutoff = _record("boundary", ended_at=_iso(30))
        just_inside = _record(
            "fresh", ended_at=(NOW - timedelta(days=30) + timedelta(seconds=1)).isoformat()
        )

        selection = self._select([at_cutoff, just_inside])

        self.assertEqual([c.session_id for c in selection.expired], ["boundary"])

    def test_every_terminal_status_is_eligible_and_live_never_is(self):
        records = [
            _record(f"t-{status}", status=status, ended_at=_iso(60))
            for status in sorted(TERMINAL_STATUSES)
        ]
        records += [
            _record("live-running", status="running", ended_at=_iso(60)),
            _record("live-waiting", status="awaiting_input", ended_at=_iso(60)),
            _record("odd-status", status="archived", ended_at=_iso(60)),
        ]

        selection = self._select(records)

        self.assertEqual(
            sorted(c.session_id for c in selection.expired),
            [f"t-{status}" for status in sorted(TERMINAL_STATUSES)],
        )

    def test_missing_timestamps_are_skipped_not_expired(self):
        selection = self._select([_record("no-ts", ended_at="junk", updated_at="also junk")])

        self.assertEqual(selection.expired, [])
        self.assertEqual(selection.skipped_no_timestamp, ["no-ts"])

    def test_updated_at_fallback_selects_legacy_records(self):
        selection = self._select([_record("legacy", updated_at=_iso(45))])

        self.assertEqual([c.session_id for c in selection.expired], ["legacy"])

    def test_keep_protects_newest_terminal_sessions(self):
        records = [
            _record("old-1", ended_at=_iso(100)),
            _record("old-2", ended_at=_iso(90)),
            _record("old-3", ended_at=_iso(80)),
            _record("recent", ended_at=_iso(1)),
        ]

        selection = self._select(records, keep=2)

        # "recent" and "old-3" are the newest two; "recent" is not expired so
        # only "old-3" is reported as kept.
        self.assertEqual([c.session_id for c in selection.expired], ["old-2", "old-1"])
        self.assertEqual([c.session_id for c in selection.kept], ["old-3"])

    def test_keep_ties_break_deterministically_by_session_id(self):
        same_time = _iso(60)
        records = [
            _record("b", ended_at=same_time),
            _record("a", ended_at=same_time),
            _record("c", ended_at=same_time),
        ]

        selection = self._select(records, keep=1)

        self.assertEqual([c.session_id for c in selection.kept], ["c"])
        self.assertEqual([c.session_id for c in selection.expired], ["b", "a"])

    def test_keep_larger_than_population_prunes_nothing(self):
        selection = self._select([_record("old", ended_at=_iso(100))], keep=5)

        self.assertEqual(selection.expired, [])
        self.assertEqual([c.session_id for c in selection.kept], ["old"])

    def test_negative_keep_is_rejected(self):
        with self.assertRaises(ValueError):
            self._select([], keep=-1)

    def test_expired_ordering_is_newest_first(self):
        records = [
            _record("oldest", ended_at=_iso(100)),
            _record("newer", ended_at=_iso(40)),
        ]

        selection = self._select(records)

        self.assertEqual([c.session_id for c in selection.expired], ["newer", "oldest"])


class TranscriptOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session_dir = Path(self._tmp.name) / "sessions"
        self.session_dir.mkdir()

    def _managed_record(self, session_id):
        return {
            "session_id": session_id,
            "jsonl_path": str(self.session_dir / f"{session_id}.jsonl"),
            "markdown_path": str(self.session_dir / f"{session_id}.md"),
        }

    def test_exact_managed_paths_are_deletable(self):
        plan = classify_transcript_paths(self._managed_record("daemon-abc"), self.session_dir)

        self.assertEqual(
            plan.deletable,
            [self.session_dir / "daemon-abc.jsonl", self.session_dir / "daemon-abc.md"],
        )
        self.assertEqual(plan.preserved, [])

    def test_custom_log_dir_paths_are_preserved(self):
        record = {
            "session_id": "daemon-abc",
            "jsonl_path": "/somewhere/else/daemon-abc.jsonl",
            "markdown_path": str(self.session_dir / "daemon-abc.md"),
        }

        plan = classify_transcript_paths(record, self.session_dir)

        self.assertEqual(plan.deletable, [self.session_dir / "daemon-abc.md"])
        self.assertEqual(
            plan.preserved,
            [("/somewhere/else/daemon-abc.jsonl", "outside the managed session directory")],
        )

    def test_invalid_session_ids_preserve_everything(self):
        for bad_id in ("../evil", "a/b", "a\\b", ".", "..", ""):
            record = self._managed_record(bad_id)
            plan = classify_transcript_paths(record, self.session_dir)
            self.assertEqual(plan.deletable, [], msg=repr(bad_id))

    def test_swapped_suffixes_are_not_deletable(self):
        record = {
            "session_id": "daemon-abc",
            "jsonl_path": str(self.session_dir / "daemon-abc.md"),
            "markdown_path": str(self.session_dir / "daemon-abc.jsonl"),
        }

        plan = classify_transcript_paths(record, self.session_dir)

        self.assertEqual(plan.deletable, [])
        self.assertEqual(len(plan.preserved), 2)

    def test_is_valid_session_id(self):
        self.assertTrue(is_valid_session_id("daemon-abc123"))
        for bad_id in ("", ".", "..", "a/b", "a\\b"):
            self.assertFalse(is_valid_session_id(bad_id), msg=repr(bad_id))

    def test_unlink_blocker_allows_regular_and_missing_files(self):
        regular = self.session_dir / "s.jsonl"
        regular.write_text("{}", encoding="utf-8")

        self.assertIsNone(transcript_unlink_blocker(regular))
        self.assertIsNone(transcript_unlink_blocker(self.session_dir / "absent.jsonl"))

    def test_unlink_blocker_rejects_symlinks_and_special_files(self):
        target = self.session_dir / "target.jsonl"
        target.write_text("{}", encoding="utf-8")
        link = self.session_dir / "link.jsonl"
        link.symlink_to(target)
        fifo = self.session_dir / "fifo.jsonl"
        os.mkfifo(fifo)

        self.assertEqual(transcript_unlink_blocker(link), "symlink")
        self.assertEqual(transcript_unlink_blocker(fifo), "not a regular file")


if __name__ == "__main__":
    unittest.main()
