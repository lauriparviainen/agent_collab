import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_collab import backends
from agent_collab.backend_contract import BackendOptionError
from agent_collab.backends.base import (
    CREDENTIALS_OK,
    CREDENTIALS_UNKNOWN,
    HEALTH_UNAVAILABLE,
    BackendHealth,
)
from agent_collab.backends.common.health import xai_api_key_credentials
from agent_collab.backends.xai_sdk import XaiSdkBackend
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.options import StartOptionsError, describe_options, validate_start_backends


def _agent(options=None):
    return AgentConfig(id="xai", type="xai", backend="sdk", options=options or {})


def _config():
    return CollaborationConfig(
        agents={"xai": _agent()},
        workflows={"solo-xai": WorkflowConfig(id="solo-xai", sequence=["xai"])},
    )


async def _collect(runner):
    return [event async for event in runner.run("fixture prompt", Path("/tmp/fixture"))]


class XaiSdkBackendTests(unittest.TestCase):
    def test_cancellation_closes_turn_stream_even_when_close_fails(self):
        async def scenario(close_error):
            entered = asyncio.Event()

            class BlockingStream:
                def __init__(self):
                    self.closed = False

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    entered.set()
                    await asyncio.Event().wait()

                async def aclose(self):
                    self.closed = True
                    if close_error:
                        raise RuntimeError("close failed")

            stream = BlockingStream()
            runner = XaiSdkBackend(turn_stream=lambda *_args: stream).create_runner(
                _agent(), False, {"model": "grok-4.5"}
            )
            consumer = asyncio.create_task(_collect(runner))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assertTrue(stream.closed)

        for close_error in (False, True):
            with self.subTest(close_error=close_error):
                asyncio.run(scenario(close_error))

    def test_registration_schema_and_capability_contract(self):
        backend = backends.get_backend("xai", "sdk")
        self.assertIsInstance(backend, XaiSdkBackend)
        self.assertEqual(backends.backend_name("xai", "sdk"), "xai_sdk")
        self.assertEqual(
            set(backend.option_schema(_agent())),
            {"model", "thinking_level", "reasoning_effort"},
        )
        self.assertEqual(
            backend.capabilities.to_dict(),
            {"resume": False, "interrupt": False, "tool_gate": False},
        )
        self.assertEqual(backend.event_fidelity, "message_only")
        self.assertEqual(backend.provider_session_id_kind, "response")

    def test_reasoning_alias_agreement_and_conflict(self):
        backend = XaiSdkBackend()
        self.assertEqual(
            backend.normalize_options(
                _agent(), {"model": "grok-4.5", "reasoning_effort": "medium"}
            ),
            {"model": "grok-4.5", "thinking_level": "medium"},
        )
        with self.assertRaises(BackendOptionError) as ctx:
            backend.normalize_options(
                _agent(),
                {
                    "model": "grok-4.5",
                    "thinking_level": "low",
                    "reasoning_effort": "high",
                },
            )
        self.assertEqual(ctx.exception.field, "reasoning_effort")

    def test_none_reasoning_is_supported_by_verified_sdk_contract(self):
        self.assertEqual(
            XaiSdkBackend().normalize_options(
                _agent(), {"model": "grok-4.5", "thinking_level": "none"}
            ),
            {"model": "grok-4.5", "thinking_level": "none"},
        )

    def test_model_is_required_before_session_creation(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(_config(), "solo-xai")
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "backend_options.xai_sdk.model")
        self.assertEqual(detail["message"], "is required")

        payload = describe_options(_config(), health=lambda backend: BackendHealth(status="ok"))
        self.assertEqual(
            payload["backend_options"]["properties"]["xai_sdk"]["required"],
            ["model"],
        )

    def test_cli_only_option_rejection_uses_backend_qualified_path(self):
        for field in ("permission_mode", "sandbox"):
            with self.subTest(field=field), self.assertRaises(StartOptionsError) as ctx:
                validate_start_backends(
                    _config(),
                    "solo-xai",
                    backend_options={"xai_sdk": {field: "plan"}},
                )
            self.assertEqual(
                ctx.exception.to_dict()["details"][0]["path"],
                f"backend_options.xai_sdk.{field}",
            )

    def test_describe_options_exposes_dynamic_xai_sdk_contract(self):
        payload = describe_options(_config(), health=lambda backend: BackendHealth(status="ok"))
        entry = payload["canonical_backends"]["xai_sdk"]
        self.assertEqual(entry["static"]["event_fidelity"], "message_only")
        self.assertEqual(entry["static"]["provider_session_id_kind"], "response")
        properties = payload["backend_options"]["properties"]["xai_sdk"]["properties"]
        self.assertNotIn("permission_mode", properties)
        self.assertIn("reasoning_effort", properties)

    def test_fake_response_maps_message_identity_and_closes_stream(self):
        closed = []

        async def fake_stream(agent, options, workdir, prompt):
            self.assertEqual(options, {"model": "grok-4.5", "thinking_level": "low"})
            self.assertEqual(prompt, "fixture prompt")
            try:
                yield SimpleNamespace(content="fixture response", id="resp-123")
            finally:
                closed.append(True)

        backend = XaiSdkBackend(turn_stream=fake_stream)
        options = backend.normalize_options(
            _agent(), {"model": "grok-4.5", "thinking_level": "low"}
        )
        events = asyncio.run(_collect(backend.create_runner(_agent(), True, options)))
        self.assertTrue(closed)
        messages = [event for event in events if event.type == "message"]
        self.assertEqual(
            [(event.source, event.text) for event in messages], [("xai", "fixture response")]
        )
        identity = next(event for event in events if (event.raw or {}).get("provider_session_id"))
        self.assertEqual(identity.raw["provider_session_id"], "resp-123")
        self.assertEqual(identity.raw["provider_session_kind"], "response")
        self.assertEqual(identity.raw["agent_id"], "xai")
        self.assertFalse(
            any(event.type in {"tool_call", "command", "file_change"} for event in events)
        )

    def test_sdk_exception_maps_to_transcript_error(self):
        async def failing_stream(agent, options, workdir, prompt):
            raise RuntimeError("fixture failure")
            yield  # pragma: no cover

        runner = XaiSdkBackend(turn_stream=failing_stream).create_runner(_agent(), False, {})
        events = asyncio.run(_collect(runner))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].source, events[0].type), ("error", "error"))
        self.assertIn("xai sdk error", events[0].text)

    def test_probe_surfaces_missing_dependency_without_importing_sdk(self):
        unavailable = BackendHealth(
            status=HEALTH_UNAVAILABLE,
            reason="xai_sdk is not importable",
            credentials=CREDENTIALS_UNKNOWN,
        )
        with mock.patch(
            "agent_collab.backends.xai_sdk.backend.probe_sdk_backend",
            return_value=unavailable,
        ) as probe:
            health = XaiSdkBackend().probe()
        self.assertEqual(health.status, HEALTH_UNAVAILABLE)
        probe.assert_called_once()

    def test_api_key_credential_probe_is_ok_or_unknown_without_exposing_value(self):
        self.assertEqual(xai_api_key_credentials({}), CREDENTIALS_UNKNOWN)
        self.assertEqual(
            xai_api_key_credentials({"XAI_API_KEY": "fixture-secret"}),
            CREDENTIALS_OK,
        )

    def test_settings_summary_reports_verified_distribution_version(self):
        with mock.patch(
            # settings_summary delegates to the shared sdk_settings_summary helper,
            # so the version lookup must be patched where it is resolved.
            "agent_collab.backends.common.sdk.package_version",
            return_value="1.17.0",
        ):
            summary = XaiSdkBackend().settings_summary(
                _agent(), {"model": "grok-4.5", "thinking_level": "low"}
            )
        self.assertEqual(summary["version"], "1.17.0")
        self.assertEqual(
            summary["options"],
            {"model": "grok-4.5", "reasoning_effort": "low"},
        )


if __name__ == "__main__":
    unittest.main()
