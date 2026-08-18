"""Backend-specific option support: CLI-only / SDK-only rejection + capture.

Explicitly-requested options that a resolved backend cannot honour are rejected
before any session state exists, with a ``<type>_options.<key>`` field path. The
rejection is symmetric: a cli-only option fails on ``sdk`` and an sdk-only option
fails on ``cli``. Provider session ids emitted by SDK runners are captured into
central session state under one uniform schema.
"""

import asyncio
import unittest
from unittest import mock

from agent_collab import backends as backend_registry
from agent_collab.backends.base import BackendCapabilities
from agent_collab.backends.common.sdk import provider_session_event
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.daemon import (
    SessionManager,
    SessionState,
    StartSessionRequest,
    _ManagedSession,
)
from agent_collab.events import Event
from agent_collab.options import (
    StartOptionsError,
    validate_start_backends,
)
from agent_collab.outcomes import TurnOutcome, TurnOutcomeRecord


_REAL_CAPABILITIES_FOR = backend_registry.capabilities_for


def _capabilities_for_with_resume(agent_type, backend_id):
    caps = _REAL_CAPABILITIES_FOR(agent_type, backend_id)
    return BackendCapabilities(
        resume=True,
        interrupt=caps.interrupt,
        tool_gate=caps.tool_gate,
        continuity=caps.continuity,
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
        from agent_collab.config import builtin_config
        from agent_collab.options import build_session_settings, validate_start_options

        agent = AgentConfig(
            id=agent_type,
            type=agent_type,
            command=agent_type,
            args=args,
            default_options=dict(builtin_config().backends[f"{agent_type}_sdk"].default_options),
            backend="sdk",
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

    async def run_turn(self, prompt, workdir, emit):
        await emit(provider_session_event("claude", "claude", "sess-xyz", "session"))
        await emit(Event.create("claude", "message", "hi"))
        return TurnOutcome("completed")


class ProviderSessionCaptureTests(unittest.TestCase):
    """``_maybe_capture_provider_session`` is synchronous, but ``_ManagedSession``
    has asyncio field defaults (Condition/Queue/Lock) that need a running loop to
    construct on 3.9, so each case builds + drives it inside ``asyncio.run``."""

    @staticmethod
    def _manager():
        manager = SessionManager.__new__(SessionManager)  # no index/filesystem
        manager._index = None
        manager._notify_tasks = set()
        return manager

    @staticmethod
    def _managed(resolved_backends, *, mock=False, dry_run=False, agents=None):
        if agents is None:
            agents = {"claude": AgentConfig(id="claude", type="claude", backend="sdk")}
        config = CollaborationConfig(
            agents=agents,
            workflows={"solo": WorkflowConfig(id="solo", sequence=list(agents))},
        )
        settings_agents = {}
        for agent_id, agent in agents.items():
            entry = {"type": agent.type}
            backend_id = resolved_backends.get(agent_id) or agent.backend
            if agent.type != "mock" and isinstance(backend_id, str) and backend_id:
                entry["backend"] = backend_id
            settings_agents[agent_id] = entry
        request = StartSessionRequest(
            task="t",
            resolved_backends=resolved_backends,
            collab_config=config,
            mock=mock,
            dry_run=dry_run,
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
            mock=mock,
            dry_run=dry_run,
            settings={"agents": settings_agents},
            capabilities={"resumable": False, "interruptible": False, "continuity": False},
        )
        return _ManagedSession(
            request=request, state=state, events=[], condition=asyncio.Condition()
        )

    def _capture(self, resolved_backends, event, **managed_kwargs):
        async def run():
            manager = self._manager()
            managed = self._managed(resolved_backends, **managed_kwargs)
            manager._maybe_capture_provider_session(managed, event)
            return managed.state.agent_sessions

        return asyncio.run(run())

    def _capture_state(self, resolved_backends, event, **managed_kwargs):
        async def run():
            manager = self._manager()
            managed = self._managed(resolved_backends, **managed_kwargs)
            manager._maybe_capture_provider_session(managed, event)
            return managed.state

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

    def test_capture_projects_sdk_continuity_without_claiming_resumable(self):
        state = self._capture_state(
            {"claude": "sdk"},
            provider_session_event("claude", "claude", "sess-xyz", "session"),
        )
        self.assertEqual(
            state.capabilities,
            {"resumable": False, "interruptible": True, "continuity": True},
        )

    def test_capture_of_every_agent_with_resume_stub_becomes_resumable(self):
        agents = {
            "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
            "codex": AgentConfig(id="codex", type="codex", backend="sdk"),
        }
        resolved = {"claude": "sdk", "codex": "sdk"}

        async def run():
            manager = self._manager()
            managed = self._managed(resolved, agents=agents)
            with mock.patch(
                "agent_collab.backends.capabilities_for",
                side_effect=_capabilities_for_with_resume,
            ):
                manager._maybe_capture_provider_session(
                    managed,
                    provider_session_event("claude", "claude", "sess-c", "session"),
                )
                after_one = dict(managed.state.capabilities)
                manager._maybe_capture_provider_session(
                    managed,
                    provider_session_event("codex", "codex", "sess-x", "thread"),
                )
                return after_one, dict(managed.state.capabilities)

        after_one, after_both = asyncio.run(run())
        self.assertFalse(after_one["resumable"])
        self.assertTrue(after_one["continuity"])
        self.assertEqual(
            after_both,
            {"resumable": False, "interruptible": False, "continuity": True},
        )

    def test_mock_and_dry_run_cannot_become_resumable(self):
        event = provider_session_event("claude", "claude", "sess-xyz", "session")
        with mock.patch(
            "agent_collab.backends.capabilities_for",
            side_effect=_capabilities_for_with_resume,
        ):
            mocked = self._capture_state({"claude": "sdk"}, event, mock=True)
            dry = self._capture_state({"claude": "sdk"}, event, dry_run=True)
        self.assertTrue(mocked.agent_sessions)
        self.assertTrue(dry.agent_sessions)
        self.assertFalse(mocked.capabilities["resumable"])
        self.assertFalse(dry.capabilities["resumable"])

    def test_mock_type_agent_is_excluded_from_resumable(self):
        agents = {
            "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
            "mocker": AgentConfig(id="mocker", type="mock"),
        }

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk"}, agents=agents)
            with mock.patch(
                "agent_collab.backends.capabilities_for",
                side_effect=_capabilities_for_with_resume,
            ):
                manager._maybe_capture_provider_session(
                    managed,
                    provider_session_event("claude", "claude", "sess-c", "session"),
                )
            return managed.state.capabilities

        summary = asyncio.run(run())
        self.assertFalse(summary["resumable"])
        self.assertTrue(summary["continuity"])

    def test_turn_commit_refreshes_projection_from_boundary_capture(self):
        record = TurnOutcomeRecord.from_outcome(
            turn_id="turn-1",
            stage_index=1,
            agent_id="claude",
            backend="claude_sdk",
            outcome=TurnOutcome("completed"),
        )
        boundary = provider_session_event("claude", "claude", "sess-xyz", "session")

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk"})
            with mock.patch(
                "agent_collab.backends.capabilities_for",
                side_effect=_capabilities_for_with_resume,
            ):
                await manager._record_turn_outcome(managed, record, boundary)
            return managed.state

        state = asyncio.run(run())
        self.assertEqual(
            state.agent_sessions["claude"]["provider_session_id"],
            "sess-xyz",
        )
        self.assertEqual(
            state.capabilities,
            {"resumable": False, "interruptible": True, "continuity": True},
        )

    def test_full_eligible_descriptor_projects_resumable(self):
        from agent_collab.resume import compute_resume_fingerprint

        fingerprint = compute_resume_fingerprint(agent_type="claude", backend_id="sdk", workdir=".")

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk"})
            managed.state.workflow_phase = {
                "completed_stages": 0,
                "parked_in_input_loop": False,
            }
            managed.state.agent_sessions = {
                "claude": {
                    "backend": "sdk",
                    "provider_session_id": "sess-c",
                    "provider_session_kind": "session",
                    "last_turn_status": "completed",
                    "prompt_event_cursor": 2,
                    "resume_fingerprint": fingerprint,
                    "interrupt_acknowledged": False,
                    "quarantined": False,
                }
            }
            with mock.patch(
                "agent_collab.backends.capabilities_for",
                side_effect=_capabilities_for_with_resume,
            ):
                manager._refresh_session_capabilities(managed.state)
            return managed.state.capabilities

        summary = asyncio.run(run())
        self.assertEqual(
            summary,
            {"resumable": True, "interruptible": True, "continuity": True},
        )

    def test_mid_workflow_unstarted_peer_projects_resumable(self):
        from agent_collab.resume import compute_resume_fingerprint

        fingerprint = compute_resume_fingerprint(agent_type="claude", backend_id="sdk", workdir=".")
        agents = {
            "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
            "codex": AgentConfig(id="codex", type="codex", backend="sdk"),
        }

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk", "codex": "sdk"}, agents=agents)
            managed.state.workflow_phase = {
                "completed_stages": 1,
                "parked_in_input_loop": False,
            }
            managed.state.settings["workflow"] = {"sequence": ["claude", "codex"]}
            managed.state.agent_sessions = {
                "claude": {
                    "backend": "sdk",
                    "provider_session_id": "sess-c",
                    "provider_session_kind": "session",
                    "last_turn_status": "completed",
                    "prompt_event_cursor": 2,
                    "resume_fingerprint": fingerprint,
                    "interrupt_acknowledged": False,
                    "quarantined": False,
                }
            }
            with mock.patch(
                "agent_collab.backends.capabilities_for",
                side_effect=_capabilities_for_with_resume,
            ):
                manager._refresh_session_capabilities(managed.state)
            return managed.state.capabilities

        summary = asyncio.run(run())
        self.assertEqual(
            summary,
            {"resumable": True, "interruptible": False, "continuity": True},
        )

    def test_quarantined_agent_makes_session_not_resumable(self):
        from agent_collab.resume import compute_resume_fingerprint

        fingerprint = compute_resume_fingerprint(agent_type="claude", backend_id="sdk", workdir=".")

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk"})
            managed.state.workflow_phase = {
                "completed_stages": 0,
                "parked_in_input_loop": False,
            }
            managed.state.agent_sessions = {
                "claude": {
                    "backend": "sdk",
                    "provider_session_id": "sess-c",
                    "provider_session_kind": "session",
                    "last_turn_status": "resume_rejected",
                    "prompt_event_cursor": 2,
                    "resume_fingerprint": fingerprint,
                    "quarantined": True,
                }
            }
            with mock.patch(
                "agent_collab.backends.capabilities_for",
                side_effect=_capabilities_for_with_resume,
            ):
                manager._refresh_session_capabilities(managed.state)
            return managed.state.capabilities

        summary = asyncio.run(run())
        self.assertFalse(summary["resumable"])

    def test_mid_turn_capture_preserves_already_persisted_descriptor_fields(self):
        extra = {
            "prompt_event_cursor": 12,
            "last_turn_status": "in_flight",
            "resume_fingerprint": {"model": "sonnet"},
            "backend_version": "1.2.3",
            "interrupt_acknowledged": True,
            "quarantined": False,
            "phase_stage_index": 1,
        }

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk"})
            managed.state.agent_sessions = {
                "claude": {
                    "backend": "sdk",
                    "provider_session_id": "old",
                    "provider_session_kind": "session",
                    **extra,
                }
            }
            manager._maybe_capture_provider_session(
                managed,
                provider_session_event("claude", "claude", "sess-xyz", "session"),
            )
            return managed.state.agent_sessions["claude"]

        entry = asyncio.run(run())
        self.assertEqual(entry["backend"], "sdk")
        self.assertEqual(entry["provider_session_id"], "sess-xyz")
        self.assertEqual(entry["provider_session_kind"], "session")
        self.assertEqual(entry["prompt_event_cursor"], 12)
        self.assertEqual(entry["last_turn_status"], "in_flight")
        self.assertEqual(entry["resume_fingerprint"], {"model": "sonnet"})
        self.assertEqual(entry["backend_version"], "1.2.3")
        self.assertTrue(entry["interrupt_acknowledged"])
        self.assertFalse(entry["quarantined"])
        self.assertEqual(entry["phase_stage_index"], 1)

    def test_turn_commit_refreshes_projection_without_new_capture(self):
        record = TurnOutcomeRecord.from_outcome(
            turn_id="turn-1",
            stage_index=1,
            agent_id="claude",
            backend="claude_sdk",
            outcome=TurnOutcome("completed"),
        )

        async def run():
            manager = self._manager()
            managed = self._managed({"claude": "sdk"})
            managed.state.capabilities = {
                "resumable": True,
                "interruptible": True,
                "continuity": False,
            }
            await manager._record_turn_outcome(
                managed, record, Event.create("claude", "status", "turn done")
            )
            return managed.state.capabilities

        summary = asyncio.run(run())
        self.assertEqual(
            summary,
            {"resumable": False, "interruptible": True, "continuity": True},
        )


class SessionCapabilityStartTests(unittest.TestCase):
    def test_start_time_empty_capture_keeps_resumable_false_when_resume_stubbed(self):
        manager = SessionManager.__new__(SessionManager)
        config = CollaborationConfig(
            agents={
                "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
                "codex": AgentConfig(id="codex", type="codex", backend="sdk"),
            },
            workflows={"pair": WorkflowConfig(id="pair", sequence=["claude", "codex"])},
        )
        with mock.patch(
            "agent_collab.backends.capabilities_for",
            side_effect=_capabilities_for_with_resume,
        ):
            summary = manager._session_capabilities(config, {"claude": "sdk", "codex": "sdk"})
        self.assertEqual(
            summary,
            {"resumable": False, "interruptible": False, "continuity": True},
        )

    def test_start_time_continuity_is_true_only_for_all_sdk_selection(self):
        manager = SessionManager.__new__(SessionManager)
        sdk = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", backend="sdk")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        cli = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", backend="cli")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        mixed = CollaborationConfig(
            agents={
                "claude": AgentConfig(id="claude", type="claude", backend="sdk"),
                "codex": AgentConfig(id="codex", type="codex", backend="cli"),
            },
            workflows={"pair": WorkflowConfig(id="pair", sequence=["claude", "codex"])},
        )
        self.assertTrue(manager._session_capabilities(sdk, {"claude": "sdk"})["continuity"])
        self.assertFalse(manager._session_capabilities(cli, {"claude": "cli"})["continuity"])
        self.assertFalse(
            manager._session_capabilities(mixed, {"claude": "sdk", "codex": "cli"})["continuity"]
        )


if __name__ == "__main__":
    unittest.main()
