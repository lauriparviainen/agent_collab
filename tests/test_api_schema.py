"""Contract test: the daemon REST API's single source of truth stays in sync.

Guards, in one place:
* every ``ROUTES`` entry has a server handler (driven live) and a client method,
  and the client exposes no wire method without a route;
* response DTOs faithfully model the *current* live wire (round-trip equality
  against the real server in mock mode);
* request/response DTOs round-trip through ``from_dict``/``to_dict``;
* the start payload shape is identical across ``StartSessionRequestModel``, the
  ``agent_collab_start`` MCP ``inputSchema``, and ``mcp_tools._start_payload``,
  and never leaks the non-user ``StartSessionRequest`` fields.
"""

import json
import os
import tempfile
import unittest
from dataclasses import fields as dataclass_fields
from pathlib import Path
from unittest import mock

from agent_collab import api_schema
from agent_collab.api_schema import (
    NON_USER_START_FIELDS,
    ROUTES,
    SERVER_ONLY_ROUTES,
    EventBatchModel,
    ErrorModel,
    HealthModel,
    OptionsRequestModel,
    PostMessageRequestModel,
    SessionListModel,
    SessionStateModel,
    StartSessionRequestModel,
    TranscriptModel,
)
from agent_collab.api_schema import API_VERSION, API_VERSION_HEADER
from agent_collab.client import AgentCollabClient, ClientError, _assert_compatible_api
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.mcp_tools import TOOLS, _start_payload
from agent_collab.options import StartOptionsError
from agent_collab.server_http import AgentCollabHttpServer, HttpError


def _start_input_schema():
    tool = next(tool for tool in TOOLS if tool["name"] == "agent_collab_start")
    return tool["inputSchema"]


class RouteRegistryTests(unittest.TestCase):
    def test_every_client_route_has_a_client_method(self):
        for route in ROUTES:
            if route.client_method is None:
                continue
            with self.subTest(route=f"{route.method} {route.path}"):
                self.assertTrue(
                    callable(getattr(AgentCollabClient, route.client_method, None)),
                    f"AgentCollabClient is missing {route.client_method!r}",
                )

    def test_client_wire_methods_match_routes_exactly(self):
        # Bidirectional drift guard: a client-wrapped route with no client method,
        # or a public client method with no route, both fail here.
        route_methods = {route.client_method for route in ROUTES if route.client_method is not None}
        public_methods = {
            name
            for name in dir(AgentCollabClient)
            if not name.startswith("_") and callable(getattr(AgentCollabClient, name))
        }
        self.assertEqual(public_methods, route_methods)

    def test_only_documented_routes_are_server_only(self):
        # Pin exactly which routes may lack a client method, so a route that
        # silently loses its client_method (or a new server-only route) is caught.
        server_only = {(route.method, route.path) for route in ROUTES if route.client_method is None}
        self.assertEqual(server_only, set(SERVER_ONLY_ROUTES))

    def test_routes_are_unique(self):
        keys = [(route.method, route.path) for route in ROUTES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_start_model_fields_match_declared_wire_fields(self):
        model_fields = {f.name for f in dataclass_fields(StartSessionRequestModel)}
        self.assertEqual(model_fields, set(StartSessionRequestModel.WIRE_FIELDS))


class StartPayloadSyncTests(unittest.TestCase):
    def test_wire_fields_match_mcp_input_schema(self):
        schema = _start_input_schema()
        self.assertEqual(
            set(StartSessionRequestModel.WIRE_FIELDS),
            set(schema["properties"].keys()),
        )

    def test_required_fields_match_mcp_input_schema(self):
        schema = _start_input_schema()
        self.assertEqual(
            set(StartSessionRequestModel.REQUIRED_FIELDS),
            set(schema["required"]),
        )

    def test_start_payload_passes_exactly_the_wire_fields(self):
        # A payload carrying every wire field comes out with no field lost.
        args = {
            "task": "t",
            "workflow": "cross-review",
            "workdir": "/tmp/does-not-need-to-exist",
            "max_turns": 1,
            "timeout": 5,
            "mock": True,
            "dry_run": False,
            "interactive": False,
            "interactive_idle_timeout": 1.0,
            "backend_options": {},
            "backend": "cli",
        }
        result = _start_payload(args)
        self.assertEqual(set(result), set(StartSessionRequestModel.WIRE_FIELDS))

    def test_start_payload_rejects_non_wire_fields(self):
        for name in (*NON_USER_START_FIELDS, "codex_options", "__unknown__"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                _start_payload({"task": "t", "workdir": "/w", name: {}})

    def test_non_user_fields_are_exactly_daemon_minus_wire(self):
        daemon_fields = {f.name for f in dataclass_fields(StartSessionRequest)}
        wire = set(StartSessionRequestModel.WIRE_FIELDS)
        # Every wire field is a real StartSessionRequest field...
        self.assertTrue(wire <= daemon_fields)
        # ...and the leftover fields are exactly the documented non-user set, so a
        # newly added StartSessionRequest field forces a wire/non-user decision.
        self.assertEqual(daemon_fields - wire, set(NON_USER_START_FIELDS))

    def test_from_dict_requires_task_and_workdir(self):
        with self.assertRaises(ValueError):
            StartSessionRequestModel.from_dict({"workdir": "/w"})
        with self.assertRaises(ValueError):
            StartSessionRequestModel.from_dict({"task": "t"})
        with self.assertRaises(ValueError):
            StartSessionRequestModel.from_dict({"task": "t", "workdir": "   "})

    def test_start_payload_backend_matches_dto_null_and_type_rules(self):
        # The /mcp _start_payload path must agree with StartSessionRequestModel on
        # backend: an explicit null is accepted (no override), a present non-null
        # non-string is rejected. Guards the REST/MCP consistency fix.
        base = {"task": "t", "workdir": "/w"}
        self.assertIsNone(StartSessionRequestModel.from_dict({**base, "backend": None}).backend)
        self.assertIsNone(_start_payload({**base, "backend": None}).get("backend"))
        with self.assertRaises(ValueError):
            _start_payload({**base, "backend": 5})
        with self.assertRaises(ValueError):
            StartSessionRequestModel.from_dict({**base, "backend": 5})

    def test_removed_provider_option_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "codex_options"):
            StartSessionRequestModel.from_dict(
                {"task": "t", "workdir": "/w", "codex_options": {"model": "old"}}
            )


class ModelRoundTripTests(unittest.TestCase):
    def test_health_round_trips_without_version(self):
        payload = {"status": "ok", "sessions": 3}
        self.assertEqual(HealthModel.from_dict(payload).to_dict(), payload)

    def test_health_round_trips_with_version(self):
        payload = {"status": "ok", "sessions": 0, "api_version": api_schema.API_VERSION}
        self.assertEqual(HealthModel.from_dict(payload).to_dict(), payload)

    def test_error_envelopes_round_trip(self):
        message_only = {"error": "not found: GET /nope"}
        self.assertEqual(ErrorModel.from_dict(message_only).to_dict(), message_only)
        with_details = {
            "error": "invalid_start_options",
            "details": [{"path": "backend_options.codex_cli.model", "message": "unknown field"}],
        }
        self.assertEqual(ErrorModel.from_dict(with_details).to_dict(), with_details)

    def test_error_model_matches_real_server_error_producers(self):
        # Tie ErrorModel to the actual error shapes the server emits, not just
        # hand-written dicts: the StartOptionsError body (with details) and the
        # HttpError -> {"error": message} envelope.
        options_error = StartOptionsError(
            [{"path": "backend_options.codex_cli.model", "message": "unknown field"}]
        ).to_dict()
        self.assertEqual(ErrorModel.from_dict(options_error).to_dict(), options_error)
        http_error = {"error": HttpError(400, "workdir is required").message}
        self.assertEqual(ErrorModel.from_dict(http_error).to_dict(), http_error)

    def test_transcript_round_trips(self):
        payload = {"transcript": "# hello\n"}
        self.assertEqual(TranscriptModel.from_dict(payload).to_dict(), payload)

    def test_event_batch_round_trips(self):
        payload = {
            "session_id": "daemon-abc",
            "cursor": 2,
            "events": [
                {
                    "timestamp": "2026-07-09T00:00:00+00:00",
                    "source": "referee",
                    "type": "message",
                    "text": "hi",
                    "raw": {"source": "referee", "target": None, "queued": False},
                }
            ],
        }
        self.assertEqual(EventBatchModel.from_dict(payload).to_dict(), payload)

    def test_request_models_are_idempotent(self):
        cases = [
            (StartSessionRequestModel, {"task": "t", "workdir": "/w"}),
            (StartSessionRequestModel, {"task": "t", "workdir": "/w", "backend": "cli", "max_turns": 2}),
            (OptionsRequestModel, {"workdir": "/w"}),
            (PostMessageRequestModel, {"text": "go"}),
            (PostMessageRequestModel, {"text": "go", "source": "human", "target": "codex"}),
        ]
        for model, payload in cases:
            with self.subTest(model=model.__name__, payload=payload):
                once = model.from_dict(payload).to_dict()
                twice = model.from_dict(once).to_dict()
                self.assertEqual(once, twice)

    def test_options_request_requires_workdir(self):
        with self.assertRaises(ValueError):
            OptionsRequestModel.from_dict({})


class LiveWireFidelityTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_dtos_match_the_live_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}):
                server = AgentCollabHttpServer(manager=SessionManager(default_workdir=root))

                health = await server._dispatch("GET", "/health", {}, b"")
                self.assertEqual(HealthModel.from_dict(health).to_dict(), health)

                options_body = json.dumps({"workdir": str(root)}).encode("utf-8")
                options = await server._dispatch("POST", "/options", {}, options_body)
                # /options is the runtime authority: opaque dict, not statically modeled.
                self.assertIsInstance(options, dict)

                # GET /options (workdir via query) is the server-only route.
                options_get = await server._dispatch("GET", f"/options?workdir={root}", {}, b"")
                self.assertIsInstance(options_get, dict)

                start_body = json.dumps(
                    {"task": "contract", "workdir": str(root), "mock": True, "max_turns": 1, "timeout": 5}
                ).encode("utf-8")
                state = await server._dispatch("POST", "/sessions", {}, start_body)
                self.assertEqual(SessionStateModel.from_dict(state).to_dict(), state)
                session_id = state["session_id"]

                listing = await server._dispatch("GET", "/sessions", {}, b"")
                self.assertEqual(SessionListModel.from_dict(listing).to_dict(), listing)

                got = await server._dispatch("GET", f"/sessions/{session_id}", {}, b"")
                self.assertEqual(SessionStateModel.from_dict(got).to_dict(), got)

                events = await server._dispatch("GET", f"/sessions/{session_id}/events", {}, b"")
                self.assertEqual(EventBatchModel.from_dict(events).to_dict(), events)

                waited = await server._dispatch(
                    "GET", f"/sessions/{session_id}/events/wait?cursor=1000000&timeout_ms=0", {}, b""
                )
                self.assertEqual(EventBatchModel.from_dict(waited).to_dict(), waited)

                transcript = await server._dispatch("GET", f"/sessions/{session_id}/transcript", {}, b"")
                self.assertEqual(TranscriptModel.from_dict(transcript).to_dict(), transcript)

                stopped = await server._dispatch("POST", f"/sessions/{session_id}/stop", {}, b"")
                self.assertEqual(SessionStateModel.from_dict(stopped).to_dict(), stopped)


class _CaptureWriter:
    def __init__(self):
        self.buffer = bytearray()

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        pass


class VersioningTests(unittest.TestCase):
    def test_client_tolerates_missing_or_matching_or_garbage_header(self):
        # None headers, absent header, matching major, and an unparseable value
        # must all pass (an old daemon predates versioning; the wire is otherwise
        # unchanged).
        _assert_compatible_api(None)
        _assert_compatible_api({})
        _assert_compatible_api({API_VERSION_HEADER: str(API_VERSION)})
        _assert_compatible_api({API_VERSION_HEADER: "not-a-number"})

    def test_client_rejects_incompatible_major(self):
        with self.assertRaises(ClientError):
            _assert_compatible_api({API_VERSION_HEADER: str(API_VERSION + 1)})


class VersioningWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_dispatch_carries_api_version(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        health = await server._dispatch("GET", "/health", {}, b"")
        self.assertEqual(health["api_version"], API_VERSION)

    async def test_responses_carry_the_version_header(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        writer = _CaptureWriter()
        await server._write_json(writer, 200, {"ok": True})
        self.assertIn(
            f"{API_VERSION_HEADER}: {API_VERSION}".encode("ascii"),
            bytes(writer.buffer),
        )

    async def test_empty_responses_carry_the_version_header(self):
        server = AgentCollabHttpServer(manager=SessionManager())
        writer = _CaptureWriter()
        await server._write_empty(writer, 202)
        self.assertIn(
            f"{API_VERSION_HEADER}: {API_VERSION}".encode("ascii"),
            bytes(writer.buffer),
        )


if __name__ == "__main__":
    unittest.main()
