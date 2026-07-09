"""Backend-specific option support: CLI-only / SDK-only rejection + capture.

Explicitly-requested options that a resolved backend cannot honour are rejected
before any session state exists, with a ``<type>_options.<key>`` field path. The
rejection is symmetric: a cli-only option fails on ``sdk`` and an sdk-only option
fails on ``cli``. Provider session ids emitted by SDK runners are captured into
central session state under one uniform schema.
"""

import asyncio
import unittest

from agent_collab.backends.base import BackendCapabilities, BackendHealth
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import (
    SessionManager,
    SessionState,
    StartSessionRequest,
    _ManagedSession,
)
from agent_collab.backends.sdk_common import provider_session_event
from agent_collab.events import Event
from agent_collab.options import (
    StartOptionsError,
    _reject_backend_unsupported_options,
    validate_start_backends,
)


def _config(agent_type, backend="sdk"):
    agent_id = agent_type
    return CollaborationConfig(
        agents={agent_id: AgentConfig(id=agent_id, type=agent_type, command=agent_type, backend=backend)},
        workflows={"solo": WorkflowConfig(id="solo", sequence=[agent_id])},
    )


class BackendOptionSupportTests(unittest.TestCase):
    def test_codex_profile_rejected_on_sdk_backend(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(_config("codex"), "solo", codex_options={"profile": "fast"})
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "codex_options.profile")
        self.assertIn("sdk", detail["message"])

    def test_supported_codex_sdk_options_are_accepted(self):
        selection = validate_start_backends(
            _config("codex"),
            "solo",
            codex_options={
                "model": "gpt-5-codex",
                "sandbox": "read-only",
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(selection.agent_backends, {"codex": "sdk"})

    def test_supported_claude_sdk_options_are_accepted(self):
        selection = validate_start_backends(
            _config("claude"),
            "solo",
            claude_options={"thinking_level": "high", "permission_mode": "acceptEdits"},
        )
        self.assertEqual(selection.agent_backends, {"claude": "sdk"})

    def test_antigravity_mode_rejected_on_sdk_backend(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(_config("antigravity"), "solo", antigravity_options={"mode": "plan"})
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "antigravity_options.mode")
        self.assertIn("sdk", detail["message"])

    def test_cli_backend_accepts_cli_only_option(self):
        # thinking_level is cli-supported, so selecting cli must not reject it.
        selection = validate_start_backends(
            _config("claude", backend="cli"), "solo", claude_options={"thinking_level": "high"}
        )
        self.assertEqual(selection.agent_backends, {"claude": "cli"})


class SdkOnlyOptionRejectionTests(unittest.TestCase):
    """The rejection is symmetric. No provider ships an sdk-only option today, so
    the sdk-only-on-cli direction is exercised via an injected support map — the
    same code path that rejects cli-only options on sdk."""

    def test_sdk_only_option_rejected_on_cli_backend(self):
        config = _config("claude", backend="cli")
        support = {"claude": {"cli": {"model"}, "sdk": {"model", "stream_partial"}}}
        errors = []
        _reject_backend_unsupported_options(
            config,
            {"claude": "cli"},
            {"claude": {"stream_partial": True}},
            errors,
            support,
        )
        self.assertEqual(errors[0]["path"], "claude_options.stream_partial")
        self.assertIn("cli", errors[0]["message"])

    def test_same_option_accepted_on_the_backend_that_supports_it(self):
        config = _config("claude", backend="sdk")
        support = {"claude": {"cli": {"model"}, "sdk": {"model", "stream_partial"}}}
        errors = []
        _reject_backend_unsupported_options(
            config,
            {"claude": "sdk"},
            {"claude": {"stream_partial": True}},
            errors,
            support,
        )
        self.assertEqual(errors, [])


class SdkSettingsDisplayTests(unittest.TestCase):
    """Settings must not advertise cli-only options on an sdk backend that ignores
    them (an inferred default `thinking_level`, `profile`, `mode`, ...)."""

    def _settings(self, agent_type, args):
        from agent_collab.options import build_session_settings, validate_start_options

        agent = AgentConfig(id=agent_type, type=agent_type, command=agent_type, args=args, backend="sdk")
        config = CollaborationConfig(
            agents={agent_type: agent},
            workflows={"solo": WorkflowConfig(id="solo", sequence=[agent_type])},
        )
        normalized = validate_start_options(config, "solo")
        settings = build_session_settings(config, "solo", normalized, agent_backends={agent_type: "sdk"})
        return settings["agents"][agent_type]

    def test_claude_sdk_settings_show_supported_thinking_level(self):
        entry = self._settings("claude", ["--effort", "high"])
        self.assertEqual(entry["backend"], "sdk")
        self.assertEqual(entry["thinking_level"], "high")

    def test_codex_sdk_settings_show_reasoning_but_hide_profile(self):
        entry = self._settings("codex", ["--profile", "fast", "-c", 'model_reasoning_effort="high"'])
        self.assertEqual(entry["backend"], "sdk")
        self.assertEqual(entry["thinking_level"], "high")
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
        request = StartSessionRequest(task="t", resolved_backends=resolved_backends)
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
        return _ManagedSession(request=request, state=state, events=[], condition=asyncio.Condition())

    def _capture(self, resolved_backends, event):
        async def run():
            manager = self._manager()
            managed = self._managed(resolved_backends)
            manager._maybe_capture_provider_session(managed, event)
            return managed.state.agent_sessions

        return asyncio.run(run())

    def test_provider_session_event_is_captured_into_session_state(self):
        result = self._capture({"claude": "sdk"}, provider_session_event("claude", "claude", "sess-xyz", "session"))
        self.assertEqual(
            result,
            {"claude": {"backend": "sdk", "provider_session_id": "sess-xyz", "provider_session_kind": "session"}},
        )

    def test_non_session_events_do_not_touch_agent_sessions(self):
        result = self._capture({"claude": "sdk"}, Event.create("claude", "message", "hello"))
        self.assertIsNone(result)

    def test_capture_backend_omitted_when_unresolved(self):
        result = self._capture({}, provider_session_event("claude", "claude", "sess-1", "session"))
        self.assertEqual(result, {"claude": {"provider_session_id": "sess-1", "provider_session_kind": "session"}})


if __name__ == "__main__":
    unittest.main()
