import asyncio
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
from agent_collab.backends.xai_sdk.backend import (
    _PersistentXaiConversation,
    _default_conversation,
    _finish_reason,
)
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
    events = []

    async def emit(event):
        events.append(event)

    await runner.run_turn("fixture prompt", Path("/tmp/fixture"), emit)
    return events


async def _outcome(runner):
    async def emit(_event):
        return None

    return await runner.run_turn("fixture prompt", Path("/tmp/fixture"), emit)


class _FakeConversation:
    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.error = error
        self.prompts = []
        self.session_ids = []
        self.reset_count = 0
        self.close_count = 0
        self.closed = False

    def active(self):
        return not self.closed and bool(self.session_ids)

    async def run(self, prompt):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)

    def note_session_id(self, response_id):
        self.session_ids.append(response_id)

    async def reset(self):
        self.reset_count += 1

    async def close(self):
        if not self.closed:
            self.closed = True
            self.close_count += 1


def _conversation_factory(conversation):
    return lambda _agent, _options, _workdir: conversation


def _runner_for(*responses, error=None, verbose=False):
    conversation = _FakeConversation(responses, error=error)
    runner = XaiSdkBackend(conversation_factory=_conversation_factory(conversation)).create_runner(
        _agent(), verbose, {"model": "grok-4.5"}
    )
    return runner, conversation


class XaiSdkBackendTests(unittest.TestCase):
    def test_cancellation_performs_exactly_one_reset(self):
        async def scenario():
            entered = asyncio.Event()

            class BlockingConversation(_FakeConversation):
                async def run(self, prompt):
                    self.prompts.append(prompt)
                    entered.set()
                    await asyncio.Event().wait()

            conversation = BlockingConversation()
            runner = XaiSdkBackend(
                conversation_factory=_conversation_factory(conversation)
            ).create_runner(
                _agent(),
                False,
                {"model": "grok-4.5"},
            )
            consumer = asyncio.create_task(_collect(runner))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assertEqual(conversation.reset_count, 1)

        asyncio.run(scenario())

    def test_registration_schema_and_capability_contract(self):
        backend = backends.get_backend("xai", "sdk")
        self.assertIsInstance(backend, XaiSdkBackend)
        self.assertEqual(backends.backend_name("xai", "sdk"), "xai_sdk")
        self.assertEqual(
            set(backend.option_schema(_agent())),
            {"model", "thinking_level", "reasoning_effort"},
        )
        model = backend.option_schema(_agent())["model"]
        self.assertEqual(model.suggested, ("grok-4.5",))
        self.assertIsNone(model.allowed)
        self.assertEqual(
            backend.capabilities.to_dict(),
            {"resume": False, "interrupt": False, "tool_gate": False, "continuity": True},
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
            payload["backends"]["xai_sdk"]["static"]["option_schema"]["required"],
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
        entry = payload["backends"]["xai_sdk"]
        self.assertEqual(entry["static"]["event_fidelity"], "message_only")
        self.assertEqual(entry["static"]["provider_session_id_kind"], "response")
        properties = entry["static"]["option_schema"]["properties"]
        self.assertNotIn("permission_mode", properties)
        self.assertIn("reasoning_effort", properties)
        self.assertEqual(properties["model"]["suggested"], ["grok-4.5"])
        self.assertNotIn("allowed", properties["model"])

    def test_fake_response_maps_message_identity_and_closes_stream(self):
        closed = []

        response = SimpleNamespace(content="fixture response", id="resp-123", finish_reason="STOP")
        conversation = _FakeConversation([response])
        backend = XaiSdkBackend(conversation_factory=_conversation_factory(conversation))
        options = backend.normalize_options(
            _agent(), {"model": "grok-4.5", "thinking_level": "low"}
        )
        runner = backend.create_runner(_agent(), True, options)
        events = asyncio.run(_collect(runner))
        self.assertEqual(conversation.prompts, ["fixture prompt"])
        self.assertEqual(conversation.session_ids, ["resp-123"])
        asyncio.run(runner.close())
        closed.append(conversation.closed)
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

    def test_production_stream_uses_agent_scoped_api_key(self):
        captured = {}
        module = ModuleType("xai_sdk")
        chat_module = ModuleType("xai_sdk.chat")

        class FakeChat:
            def append(self, message):
                captured["message"] = message

            async def sample(self):
                return SimpleNamespace(
                    content="fixture response",
                    id="resp-agent-env",
                    finish_reason="STOP",
                )

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.chat = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                captured["chat_kwargs"] = kwargs
                return FakeChat()

            async def __aenter__(self):
                return self

            async def close(self):
                captured["closed"] = True

        module.AsyncClient = FakeAsyncClient
        chat_module.user = lambda prompt: ("user", prompt)
        agent = AgentConfig(
            id="xai",
            type="xai",
            backend="sdk",
            env={"XAI_API_KEY": "agent-scoped-key"},
        )
        runner = XaiSdkBackend(conversation_factory=_default_conversation).create_runner(
            agent,
            False,
            {"model": "grok-4.5", "thinking_level": "high"},
        )
        with mock.patch.dict(
            sys.modules,
            {"xai_sdk": module, "xai_sdk.chat": chat_module},
        ):
            events = asyncio.run(_collect(runner))
            asyncio.run(runner.close())

        self.assertEqual(captured["client_kwargs"], {"api_key": "agent-scoped-key"})
        self.assertEqual(
            captured["chat_kwargs"],
            {
                "model": "grok-4.5",
                "reasoning_effort": "high",
                "store_messages": True,
            },
        )
        self.assertEqual(captured["message"], ("user", "fixture prompt"))
        self.assertTrue(captured["closed"])
        self.assertTrue(any(event.type == "message" for event in events))

    def test_finish_reason_and_content_control_outcome(self):
        completed, _ = _runner_for(SimpleNamespace(content="done", id="r1", finish_reason="STOP"))
        limited, limited_conversation = _runner_for(
            SimpleNamespace(content="partial", id="r2", finish_reason="MAX_TOKENS")
        )
        empty, empty_conversation = _runner_for(
            SimpleNamespace(content="", id="r3", finish_reason="STOP")
        )

        self.assertEqual(asyncio.run(_outcome(completed)).outcome, "completed")
        self.assertEqual(asyncio.run(_outcome(limited)).code, "provider_output_incomplete")
        self.assertEqual(asyncio.run(_outcome(empty)).code, "provider_empty_response")
        self.assertEqual(limited_conversation.reset_count, 1)
        self.assertEqual(empty_conversation.reset_count, 1)

    def test_verified_sdk_finish_reason_names_are_normalized(self):
        self.assertEqual(
            _finish_reason(SimpleNamespace(finish_reason="REASON_STOP")),
            "STOP",
        )
        self.assertEqual(
            _finish_reason(SimpleNamespace(finish_reason="REASON_MAX_LEN")),
            "LENGTH",
        )
        self.assertEqual(
            _finish_reason(SimpleNamespace(finish_reason="REASON_MAX_CONTEXT")),
            "LENGTH",
        )

    def test_sdk_exception_maps_to_transcript_error(self):
        runner, conversation = _runner_for(error=RuntimeError("fixture failure"))
        events = asyncio.run(_collect(runner))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].source, events[0].type), ("error", "error"))
        self.assertIn("xai sdk error", events[0].text)
        self.assertEqual(conversation.reset_count, 1)

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
        self.assertEqual(summary["conversation"], "persistent")


class PersistentXaiConversationTests(unittest.TestCase):
    def _sdk(self, responses, *, sample_error=None, delete_error=None):
        state = SimpleNamespace(
            responses=list(responses),
            clients=[],
            creates=[],
            messages=[],
            deleted=[],
            close_count=0,
        )

        class FakeRequest:
            def __init__(self, kwargs):
                self.kwargs = kwargs
                self.message = None

            def append(self, message):
                self.message = message
                state.messages.append(message)

            async def sample(self):
                if sample_error is not None and self.kwargs.get("previous_response_id"):
                    raise sample_error
                return state.responses.pop(0)

        class FakeChatApi:
            def create(self, **kwargs):
                state.creates.append(kwargs)
                return FakeRequest(kwargs)

            async def delete_stored_completion(self, response_id):
                state.deleted.append(response_id)
                if delete_error is not None:
                    raise delete_error

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.chat = FakeChatApi()
                state.clients.append(self)

            async def close(self):
                state.close_count += 1

        conversation = _PersistentXaiConversation(
            FakeClient,
            lambda prompt: ("user", prompt),
            {"model": "grok-4.5", "reasoning_effort": "low"},
            {"api_key": "fixture-key"},
        )
        return conversation, state

    def test_two_turns_reuse_client_and_send_no_local_history(self):
        async def scenario():
            conversation, state = self._sdk(
                [
                    SimpleNamespace(id="response-1"),
                    SimpleNamespace(id="response-2"),
                ]
            )
            self.assertFalse(conversation.active())
            await conversation.run("turn one secret")
            self.assertTrue(conversation.active())
            await conversation.run("turn two only")
            self.assertTrue(conversation.active())

            self.assertEqual(len(state.clients), 1)
            self.assertEqual(
                state.creates,
                [
                    {
                        "model": "grok-4.5",
                        "reasoning_effort": "low",
                        "store_messages": True,
                    },
                    {
                        "model": "grok-4.5",
                        "reasoning_effort": "low",
                        "store_messages": True,
                        "previous_response_id": "response-1",
                    },
                ],
            )
            self.assertEqual(
                state.messages,
                [("user", "turn one secret"), ("user", "turn two only")],
            )
            self.assertNotIn("turn one secret", state.messages[1][1])

            await conversation.close()
            self.assertFalse(conversation.active())
            self.assertEqual(state.deleted, ["response-2", "response-1"])
            self.assertEqual(state.close_count, 1)
            await conversation.close()
            self.assertEqual(state.close_count, 1)

        asyncio.run(scenario())

    def test_reset_closes_once_and_reconnects_strictly_with_latest_identity(self):
        async def scenario():
            conversation, state = self._sdk(
                [
                    SimpleNamespace(id="response-1"),
                    SimpleNamespace(id="response-2"),
                ]
            )
            await conversation.run("first")
            await conversation.reset()
            self.assertTrue(conversation.active())
            self.assertEqual(state.close_count, 1)
            await conversation.run("second")
            self.assertEqual(len(state.clients), 2)
            self.assertEqual(
                state.creates[1]["previous_response_id"],
                "response-1",
            )
            await conversation.close()

        asyncio.run(scenario())

    def test_rejected_reconnect_never_retries_without_identity(self):
        async def scenario():
            conversation, state = self._sdk(
                [SimpleNamespace(id="response-1")],
                sample_error=LookupError("not found"),
            )
            await conversation.run("first")
            await conversation.reset()
            with self.assertRaisesRegex(LookupError, "not found"):
                await conversation.run("second")
            self.assertEqual(len(state.creates), 2)
            self.assertEqual(
                state.creates[1]["previous_response_id"],
                "response-1",
            )
            self.assertFalse(
                any(
                    call.get("store_messages") and "previous_response_id" not in call
                    for call in state.creates[1:]
                )
            )
            await conversation.close()

        asyncio.run(scenario())

    def test_unsupported_reconnect_fails_without_fresh_fallback(self):
        async def scenario():
            creates = []

            class FakeChatApi:
                def create(self, **kwargs):
                    creates.append(kwargs)
                    if "previous_response_id" in kwargs:
                        raise TypeError("unsupported previous_response_id")
                    return SimpleNamespace(
                        append=lambda _message: None,
                        sample=lambda: asyncio.sleep(
                            0,
                            result=SimpleNamespace(id="response-1"),
                        ),
                    )

                async def delete_stored_completion(self, _response_id):
                    return None

            class FakeClient:
                def __init__(self, **_kwargs):
                    self.chat = FakeChatApi()

                async def close(self):
                    return None

            conversation = _PersistentXaiConversation(
                FakeClient,
                lambda prompt: prompt,
                {"model": "grok-4.5"},
                {},
            )
            await conversation.run("first")
            await conversation.reset()
            with self.assertRaisesRegex(TypeError, "unsupported"):
                await conversation.run("second")
            self.assertEqual(len(creates), 2)
            self.assertIn("previous_response_id", creates[1])
            await conversation.close()

        asyncio.run(scenario())

    def test_close_serializes_against_repeated_cancellation_ignoring_sample(self):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            closed = asyncio.Event()

            class FakeRequest:
                def append(self, _message):
                    return None

                async def sample(self):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(id="response-1")

            class FakeChatApi:
                def create(self, **_kwargs):
                    return FakeRequest()

                async def delete_stored_completion(self, _response_id):
                    return None

            class FakeClient:
                def __init__(self, **_kwargs):
                    self.chat = FakeChatApi()

                async def close(self):
                    closed.set()

            conversation = _PersistentXaiConversation(
                FakeClient,
                lambda prompt: prompt,
                {"model": "grok-4.5"},
                {},
            )
            run_task = asyncio.create_task(conversation.run("prompt"))
            await entered.wait()
            run_task.cancel()
            await asyncio.sleep(0)
            run_task.cancel()
            close_task = asyncio.create_task(conversation.close())
            await asyncio.sleep(0)
            self.assertFalse(closed.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await run_task
            await close_task
            self.assertTrue(closed.is_set())
            self.assertFalse(conversation.active())

        asyncio.run(scenario())

    def test_cleanup_error_still_closes_and_is_idempotent(self):
        async def scenario():
            conversation, state = self._sdk(
                [SimpleNamespace(id="response-1")],
                delete_error=RuntimeError("delete failed"),
            )
            await conversation.run("prompt")
            with self.assertRaisesRegex(RuntimeError, "delete failed"):
                await conversation.close()
            self.assertEqual(state.close_count, 1)
            self.assertFalse(conversation.active())
            await conversation.close()
            self.assertEqual(state.close_count, 1)

        asyncio.run(scenario())

    def test_cancelled_sample_captures_completed_response_before_reset(self):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            state = SimpleNamespace(creates=[], close_count=0)

            class FakeRequest:
                def append(self, _message):
                    return None

                async def sample(self):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(id="response-after-cancel")

            class FakeChatApi:
                def create(self, **kwargs):
                    state.creates.append(kwargs)
                    return FakeRequest()

                async def delete_stored_completion(self, _response_id):
                    return None

            class FakeClient:
                def __init__(self, **_kwargs):
                    self.chat = FakeChatApi()

                async def close(self):
                    state.close_count += 1

            conversation = _PersistentXaiConversation(
                FakeClient,
                lambda prompt: prompt,
                {"model": "grok-4.5"},
                {},
            )
            task = asyncio.create_task(conversation.run("first"))
            await entered.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await conversation.reset()
            next_task = asyncio.create_task(conversation.run("second"))
            await entered.wait()
            release.set()
            await next_task
            self.assertEqual(
                state.creates[1]["previous_response_id"],
                "response-after-cancel",
            )
            await conversation.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
