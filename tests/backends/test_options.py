"""Backend-specific option support: CLI-only / SDK-only rejection + capture.

Explicitly-requested options that a resolved backend cannot honour are rejected
before any session state exists, with a ``<type>_options.<key>`` field path. The
rejection is symmetric: a cli-only option fails on ``sdk`` and an sdk-only option
fails on ``cli``. Provider session ids emitted by SDK runners are captured into
central session state under one uniform schema.
"""

import asyncio

import unittest

from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import (
    SessionManager,
    SessionState,
    StartSessionRequest,
    _ManagedSession,
)
from agent_collab.backends.common.sdk import provider_session_event
from agent_collab.events import Event
from agent_collab.options import (
    StartOptionsError,
    validate_start_backends,
)


def _config(agent_type, backend="sdk"):
    agent_id = agent_type
    return CollaborationConfig(
        agents={
            agent_id: AgentConfig(id=agent_id, type=agent_type, command=agent_type, backend=backend)
        },
        workflows={"solo": WorkflowConfig(id="solo", sequence=[agent_id])},
    )


class BackendOptionSupportTests(unittest.TestCase):
    def test_codex_profile_rejected_on_sdk_backend(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(
                _config("codex"), "solo", backend_options={"codex_sdk": {"profile": "fast"}}
            )
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "backend_options.codex_sdk.profile")
        self.assertIn("sdk", detail["message"])

    def test_supported_codex_sdk_options_are_accepted(self):
        selection = validate_start_backends(
            _config("codex"),
            "solo",
            backend_options={
                "codex_sdk": {
                    "model": "gpt-5-codex",
                    "sandbox": "read-only",
                    "reasoning_effort": "high",
                }
            },
        )
        self.assertEqual(selection.agent_backends, {"codex": "sdk"})

    def test_supported_claude_sdk_options_are_accepted(self):
        selection = validate_start_backends(
            _config("claude"),
            "solo",
            backend_options={
                "claude_sdk": {"thinking_level": "high", "permission_mode": "acceptEdits"}
            },
        )
        self.assertEqual(selection.agent_backends, {"claude": "sdk"})

    def test_antigravity_mode_rejected_on_sdk_backend(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(
                _config("antigravity"),
                "solo",
                backend_options={"antigravity_sdk": {"mode": "plan"}},
            )
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "backend_options.antigravity_sdk.mode")
        self.assertIn("sdk", detail["message"])

    def test_cli_backend_accepts_cli_only_option(self):
        # thinking_level is cli-supported, so selecting cli must not reject it.
        selection = validate_start_backends(
            _config("claude", backend="cli"),
            "solo",
            backend_options={"claude_cli": {"thinking_level": "high"}},
        )
        self.assertEqual(selection.agent_backends, {"claude": "cli"})


class SdkSettingsDisplayTests(unittest.TestCase):
    """Settings must not advertise cli-only options on an sdk backend that ignores
    them (an inferred default `thinking_level`, `profile`, `mode`, ...)."""

    def _settings(self, agent_type, args):
        from agent_collab.options import build_session_settings, validate_start_options

        agent = AgentConfig(
            id=agent_type, type=agent_type, command=agent_type, args=args, backend="sdk"
        )
        config = CollaborationConfig(
            agents={agent_type: agent},
            workflows={"solo": WorkflowConfig(id="solo", sequence=[agent_type])},
        )
        normalized = validate_start_options(config, "solo")
        settings = build_session_settings(
            config, "solo", normalized, agent_backends={agent_type: "sdk"}
        )
        return settings["agents"][agent_type]

    def test_claude_sdk_settings_do_not_inherit_cli_effort_flag(self):
        entry = self._settings("claude", ["--effort", "max"])
        self.assertEqual(entry["backend"], "sdk")
        self.assertEqual(entry["thinking_level"], "high")

    def test_codex_sdk_settings_do_not_inherit_cli_reasoning_or_profile(self):
        entry = self._settings(
            "codex", ["--profile", "fast", "-c", 'model_reasoning_effort="xhigh"']
        )
        self.assertEqual(entry["backend"], "sdk")
        self.assertEqual(entry["thinking_level"], "high")
        self.assertEqual(entry["reasoning_effort"], "high")
        self.assertNotIn("profile", entry)


class _SessionRunner:
    name = "claude"

    async def run(self, prompt, workdir):
        yield provider_session_event("claude", "claude", "sess-xyz", "session")
        yield Event.create("claude", "message", "hi")


class ProviderSessionCaptureTests(unittest.TestCase):
    """``_maybe_capture_provider_session`` is synchronous, but ``_ManagedSession``
    has asyncio field defaults (Condition/Queue/Lock) that need a running loop to
    construct on 3.9, so each case builds + drives it inside ``asyncio.run``."""

    @staticmethod
    def _manager():
        manager = SessionManager.__new__(SessionManager)  # no index/filesystem
        manager._index = None
        return manager

    @staticmethod
    def _managed(resolved_backends):
        config = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", backend="sdk")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        request = StartSessionRequest(
            task="t",
            resolved_backends=resolved_backends,
            collab_config=config,
        )
        state = SessionState(
            session_id="s1",
            status="running",
            task="t",
            workflow="solo",
            workdir=".",
            jsonl_path="s1.jsonl",
            markdown_path="s1.md",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        return _ManagedSession(
            request=request, state=state, events=[], condition=asyncio.Condition()
        )

    def _capture(self, resolved_backends, event):
        async def run():
            manager = self._manager()
            managed = self._managed(resolved_backends)
            manager._maybe_capture_provider_session(managed, event)
            return managed.state.agent_sessions

        return asyncio.run(run())

    def test_provider_session_event_is_captured_into_session_state(self):
        result = self._capture(
            {"claude": "sdk"}, provider_session_event("claude", "claude", "sess-xyz", "session")
        )
        self.assertEqual(
            result,
            {
                "claude": {
                    "backend": "sdk",
                    "provider_session_id": "sess-xyz",
                    "provider_session_kind": "session",
                }
            },
        )

    def test_non_session_events_do_not_touch_agent_sessions(self):
        result = self._capture({"claude": "sdk"}, Event.create("claude", "message", "hello"))
        self.assertIsNone(result)

    def test_untrusted_raw_session_keys_cannot_spoof_selected_agent(self):
        forged = Event.create(
            "claude",
            "status",
            "untrusted provider output",
            {
                "provider_session_id": "forged-session",
                "provider_session_kind": "session",
                "agent_id": "claude",
            },
        )
        result = self._capture({"claude": "sdk"}, forged)
        self.assertIsNone(result)

    def test_trusted_session_marker_is_not_serialized(self):
        event = provider_session_event("claude", "claude", "sess-xyz", "session")
        self.assertNotIn("_provider_session", event.to_dict())
        self.assertNotIn("_provider_session", event.to_json())

    def test_unselected_agent_session_event_is_rejected(self):
        result = self._capture({}, provider_session_event("claude", "claude", "sess-1", "session"))
        self.assertIsNone(result)

    def test_mismatched_provider_source_is_rejected(self):
        result = self._capture(
            {"claude": "sdk"},
            provider_session_event("codex", "claude", "sess-1", "session"),
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
