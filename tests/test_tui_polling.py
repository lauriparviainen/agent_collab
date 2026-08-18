from __future__ import annotations

import threading
import unittest
from unittest import mock

from agent_collab.api_schema import EventBatchModel, EventModel, SessionStateModel
from agent_collab.events import Event
from agent_collab.tui import TuiApp
from agent_collab.tui_core import CursorState, parse_input


SESSION_ID = "session-1"


class _Screen:
    def getmaxyx(self):
        return (24, 80)


def _session(status: str = "running") -> SessionStateModel:
    return SessionStateModel.from_dict(
        {
            "session_id": SESSION_ID,
            "status": status,
            "interactive": True,
        }
    )


def _event(source: str, text: str, event_type: str = "message") -> EventModel:
    return EventModel.from_dict(Event.create(source, event_type, text).to_dict())


def _batch(events: list[EventModel], cursor: int | None = None) -> EventBatchModel:
    return EventBatchModel(
        session_id=SESSION_ID,
        cursor=len(events) if cursor is None else cursor,
        events=events,
    )


def _app(client) -> TuiApp:
    app = TuiApp(_Screen(), client, initial_session_id=None)
    app.session_id = SESSION_ID
    app.session = _session()
    app.cursor_state = CursorState(session_id=SESSION_ID, cursor=0, epoch=1)
    return app


def _texts(app: TuiApp) -> list[str]:
    return [line.text.split(maxsplit=1)[-1] for line in app.transcript_lines]


class RefereePostPollerTests(unittest.TestCase):
    def test_successful_post_catches_up_inflight_provider_event_before_note(self):
        class Client:
            def __init__(self):
                self.events: list[EventModel] = []
                self.waiting = threading.Event()
                self.release_wait = threading.Event()

            def wait_events(self, session_id, cursor, timeout_ms):
                self.waiting.set()
                self.release_wait.wait(2)
                return _batch(self.events[cursor:])

            def post_message(self, session_id, text, source):
                self.events.extend([_event("claude", "provider output"), _event(source, text)])
                self.release_wait.set()
                return _batch([self.events[-1]], cursor=len(self.events))

            def read_events(self, session_id, cursor):
                return _batch(self.events[cursor:], cursor=len(self.events))

            def get_session(self, session_id):
                return _session()

        client = Client()
        app = _app(client)
        app._start_poller()
        self.assertTrue(client.waiting.wait(1))
        with mock.patch.object(app, "_start_poller") as restart:
            app._post_referee_message("check this")

        app._drain_events()
        self.assertEqual(_texts(app), ["provider output", "check this"])
        self.assertEqual(app.cursor_state.cursor, 2)
        self.assertEqual(app.cursor_state.epoch, 2)
        restart.assert_called_once_with()

    def test_failed_post_does_not_duplicate_already_queued_batch(self):
        provider_event = _event("claude", "one batch")

        class Client:
            def __init__(self):
                self.first_wait = threading.Event()
                self.block = threading.Event()
                self.calls = 0

            def wait_events(self, session_id, cursor, timeout_ms):
                self.calls += 1
                if self.calls == 1:
                    self.first_wait.set()
                    return _batch([provider_event])
                self.block.wait(2)
                return _batch([], cursor=1)

            def post_message(self, session_id, text, source):
                self.block.set()
                raise RuntimeError("post failed")

            def read_events(self, session_id, cursor):
                return _batch([provider_event], cursor=1)

        client = Client()
        app = _app(client)
        app._start_poller()
        self.assertTrue(client.first_wait.wait(1))
        while app.events.empty():
            self.assertTrue(app.poll_thread and app.poll_thread.is_alive())

        with mock.patch.object(app, "_start_poller"):
            app._post_referee_message("not sent")
        app._drain_events()

        self.assertEqual(_texts(app), ["one batch"])
        self.assertEqual(app.cursor_state.cursor, 1)
        self.assertEqual(app.message, "post failed")


class StopPollerTests(unittest.TestCase):
    def test_stop_reads_final_events_before_settling_terminal(self):
        final_events = [
            _event("claude", "final output"),
            _event("referee", "session stopped", event_type="status"),
        ]

        class Client:
            def __init__(self):
                self.waiting = threading.Event()
                self.release_wait = threading.Event()
                self.events: list[EventModel] = []

            def wait_events(self, session_id, cursor, timeout_ms):
                self.waiting.set()
                self.release_wait.wait(2)
                return _batch(self.events[cursor:], cursor=len(self.events))

            def stop_session(self, session_id):
                self.events.extend(final_events)
                self.release_wait.set()
                return _session("stopped")

            def read_events(self, session_id, cursor):
                return _batch(self.events[cursor:], cursor=len(self.events))

        client = Client()
        app = _app(client)
        app._start_poller()
        self.assertTrue(client.waiting.wait(1))
        app._dispatch(parse_input("/stop"))
        app._drain_events()

        self.assertEqual(_texts(app), ["final output", "session stopped"])
        self.assertEqual(app.cursor_state.cursor, 2)
        self.assertEqual(app.cursor_state.epoch, 2)
        self.assertEqual(app.session.status, "stopped")
        self.assertEqual(app.message, f"stopped {SESSION_ID}")

    def test_interrupt_slash_keeps_poller_and_refreshes(self):
        class Client:
            def __init__(self):
                self.interrupt_calls = []

            def interrupt_session(self, session_id):
                self.interrupt_calls.append(session_id)
                return _session("awaiting_input")

            def get_session(self, session_id, detail="compact"):
                del detail
                return _session("awaiting_input")

            def read_events(self, session_id, cursor):
                del session_id
                return _batch([], cursor=cursor)

            def wait_events(self, session_id, cursor, timeout_ms):
                del session_id, cursor, timeout_ms
                return _batch([], cursor=0)

        client = Client()
        app = _app(client)
        with mock.patch.object(app, "activate_session") as activate:
            with mock.patch.object(app, "_stop_poller") as stop_poller:
                app._dispatch(parse_input("/interrupt"))

        self.assertEqual(client.interrupt_calls, [SESSION_ID])
        activate.assert_called_once_with(SESSION_ID)
        stop_poller.assert_not_called()
        self.assertEqual(app.message, f"interrupted {SESSION_ID}")

    def test_approval_slash_dispatches_decision(self):
        from agent_collab.api_schema import ApprovalDecisionResponseModel

        class Client:
            def __init__(self):
                self.calls = []

            def resolve_approval(self, session_id, request_id, decision):
                self.calls.append((session_id, request_id, decision))
                return ApprovalDecisionResponseModel.from_dict(
                    {
                        "session_id": session_id,
                        "request_id": request_id,
                        "outcome": "denied",
                        "reason": "rest",
                        "status": "ok",
                    }
                )

            def get_session(self, session_id, detail="compact"):
                return _session("awaiting_approval")

        client = Client()
        app = _app(client)
        app._dispatch(parse_input("/approval a1 deny"))

        self.assertEqual(client.calls, [(SESSION_ID, "a1", "deny")])
        self.assertEqual(app.message, "denied a1")

    def test_error_from_stopped_poller_is_ignored(self):
        class Client:
            def __init__(self):
                self.waiting = threading.Event()
                self.release_wait = threading.Event()

            def wait_events(self, session_id, cursor, timeout_ms):
                self.waiting.set()
                self.release_wait.wait(2)
                raise RuntimeError("late poller error")

        client = Client()
        app = _app(client)
        app._start_poller()
        self.assertTrue(client.waiting.wait(1))
        self.assertIsNotNone(app.poll_stop)
        self.assertIsNotNone(app.poll_thread)
        app.poll_stop.set()
        client.release_wait.set()
        app.poll_thread.join(1)
        app._drain_events()

        self.assertTrue(app.events.empty())
        self.assertNotEqual(app.message, "late poller error")


if __name__ == "__main__":
    unittest.main()
