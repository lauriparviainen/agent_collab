import json
from pathlib import Path
import tempfile
import unittest

from agent_collab.daemon import _render_transcript
from agent_collab.events import Event
from agent_collab.logging import SessionLogger


class EventAttributionRenderingTests(unittest.TestCase):
    def test_session_logger_suffixes_only_distinct_agent_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with SessionLogger(Path(tmp), "render", "session") as logger:
                logger.write(
                    Event.create(
                        "claude",
                        "message",
                        "distinct",
                        agent_id="claude-reviewer",
                    )
                )
                logger.write(Event.create("codex", "message", "same", agent_id="codex"))

            markdown = (Path(tmp) / "session.md").read_text(encoding="utf-8")
            self.assertIn("## CLAUDE (claude-reviewer) `message`", markdown)
            self.assertIn("## CODEX `message`", markdown)
            self.assertNotIn("CODEX (codex)", markdown)

            events = [
                json.loads(line)
                for line in (Path(tmp) / "session.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["agent_id"], "claude-reviewer")
            self.assertEqual(events[1]["agent_id"], "codex")

    def test_daemon_transcript_suffixes_only_distinct_agent_ids(self):
        events = [
            Event.create(
                "claude",
                "message",
                "distinct",
                agent_id="claude-reviewer",
            ).to_dict(),
            Event.create("codex", "message", "same", agent_id="codex").to_dict(),
            {
                "timestamp": "legacy",
                "source": "referee",
                "type": "status",
                "text": "old event",
                "raw": None,
            },
        ]

        rendered = _render_transcript("session", events, "full")

        self.assertIn("## CLAUDE (claude-reviewer) `message`", rendered)
        self.assertIn("## CODEX `message`", rendered)
        self.assertNotIn("CODEX (codex)", rendered)
        self.assertIn("## REFEREE `status`", rendered)
