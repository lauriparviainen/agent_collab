"""Codex ``sdk`` backend tests (real-shape fakes; no live model call).

The fake object graph mirrors ``openai-codex==0.1.0b3``: a collected
``TurnResult`` owns ``ThreadItem`` root models, and the thread id lives on the
``AsyncThread`` rather than individual items.  The production-factory tests
replace the imported module with an async-context-manager fake, so the verified
call shape and resource lifetime are exercised without credentials.
"""

import asyncio
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock

from agent_collab import backends
from agent_collab.backends.base import BackendUnavailable
from agent_collab.backends.codex_sdk import (
    CodexSdkBackend,
    CodexSdkRunner,
    CodexTurnOutcome,
    _default_item_stream,
    _map_sdk_options,
    iter_codex_events,
    sandbox_member_name,
)
from agent_collab.config import AgentConfig

AGENT = AgentConfig(id="codex", type="codex", backend="sdk")


class _TurnStatus(Enum):
    completed = "completed"
    failed = "failed"


class _MessagePhase(Enum):
    commentary = "commentary"
    final_answer = "final_answer"


class _CommandStatus(Enum):
    completed = "completed"
    failed = "failed"


class _PatchStatus(Enum):
    completed = "completed"


class _Object:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ThreadItem:
    """Shape of the SDK's pydantic ``ThreadItem`` RootModel."""

    def __init__(self, root):
        self.root = root


def _agent_message(text, *, phase=_MessagePhase.final_answer, item_id="msg-1"):
    return _ThreadItem(
        _Object(type="agentMessage", id=item_id, text=text, phase=phase)
    )


def _reasoning(*, summary=None, content=None, item_id="reason-1"):
    return _ThreadItem(
        _Object(
            type="reasoning",
            id=item_id,
            summary=[] if summary is None else summary,
            content=[] if content is None else content,
        )
    )


def _command(command="pytest -q", *, status=_CommandStatus.completed, exit_code=0):
    return _ThreadItem(
        _Object(
            type="commandExecution",
            id="cmd-1",
            command=command,
            cwd="/workspace",
            status=status,
            exit_code=exit_code,
            aggregated_output="1 passed",
            duration_ms=125,
        )
    )


def _file_change():
    # PatchChangeKind is itself another generated RootModel, not an enum.
    kind = _Object(root=_Object(type="update"))
    change = _Object(path="hello.py", kind=kind, diff="@@ changed @@")
    return _ThreadItem(
        _Object(type="fileChange", id="patch-1", changes=[change], status=_PatchStatus.completed)
    )


def _turn_result(
    *,
    final_response="Done.",
    items=None,
    status=_TurnStatus.completed,
    error=None,
):
    # All public TurnResult fields are represented, even when a focused mapper
    # test only consumes a subset.
    return _Object(
        id="turn-1",
        status=status,
        error=error,
        started_at=100,
        completed_at=200,
        duration_ms=100,
        final_response=final_response,
        items=list(items or []),
        usage=None,
    )


def _stream_factory(outcomes, *, error=None):
    def factory(agent, options, workdir, prompt):
        async def stream():
            if error is not None:
                raise error
            for outcome in outcomes:
                yield outcome

        return stream()

    return factory


def _run(result=None, *, verbose=False, options=None, error=None, thread_id="thread-9"):
    outcomes = [] if result is None else [CodexTurnOutcome(thread_id, result)]
    runner = CodexSdkRunner(
        AGENT,
        verbose,
        options or {},
        item_stream=_stream_factory(outcomes, error=error),
    )

    async def collect():
        return [event async for event in runner.run("do a thing", Path("."))]

    return asyncio.run(collect())


class CodexEventMappingTests(unittest.TestCase):
    def test_final_response_is_message_first_then_verified_items_map(self):
        result = _turn_result(
            final_response="Ran the tests and edited hello.py.",
            items=[
                _command(),
                _file_change(),
                _agent_message("Ran the tests and edited hello.py."),
            ],
        )
        events = _run(result)
        content = [event for event in events if event.type in {"message", "command", "file_change"}]

        self.assertEqual(content[0].source, "codex")
        self.assertEqual(content[0].text, "Ran the tests and edited hello.py.")
        self.assertEqual([event.type for event in content], ["message", "command", "file_change"])

        command = content[1]
        self.assertEqual(command.source, "tool")
        self.assertEqual(command.raw["cwd"], "/workspace")
        self.assertEqual(command.raw["status"], "completed")
        self.assertEqual(command.raw["exit_code"], 0)
        self.assertEqual(command.raw["aggregated_output"], "1 passed")

        file_change = content[2]
        self.assertEqual(file_change.source, "tool")
        self.assertEqual(file_change.raw["status"], "completed")
        self.assertEqual(
            file_change.raw["changes"],
            [{"path": "hello.py", "kind": "update", "diff": "@@ changed @@"}],
        )

    def test_non_final_agent_message_is_preserved(self):
        result = _turn_result(
            final_response="Final answer.",
            items=[
                _agent_message("I am checking the tests.", phase=_MessagePhase.commentary),
                _agent_message("Final answer."),
            ],
        )
        messages = [event for event in _run(result) if event.type == "message"]
        self.assertEqual([event.text for event in messages], ["Final answer.", "I am checking the tests."])
        self.assertEqual(messages[1].raw["phase"], "commentary")

    def test_agent_item_is_fallback_when_final_response_is_absent(self):
        result = _turn_result(final_response=None, items=[_agent_message("Collected message.")])
        messages = [event for event in _run(result) if event.type == "message"]
        self.assertEqual([event.text for event in messages], ["Collected message."])

    def test_reasoning_uses_summary_then_content_and_is_verbose_only(self):
        summary_result = _turn_result(
            final_response=None,
            items=[_reasoning(summary=["public summary"], content=["reasoning content"])],
        )
        quiet = _run(summary_result, verbose=False)
        self.assertFalse(any((event.raw or {}).get("reasoning") for event in quiet))
        loud = _run(summary_result, verbose=True)
        reasoning = [event for event in loud if (event.raw or {}).get("reasoning")]
        self.assertEqual(reasoning[0].text, "public summary")
        self.assertEqual(reasoning[0].raw["content"], ["reasoning content"])

        content_result = _turn_result(final_response=None, items=[_reasoning(content=["content fallback"])])
        fallback = [event for event in _run(content_result, verbose=True) if (event.raw or {}).get("reasoning")]
        self.assertEqual(fallback[0].text, "content fallback")

    def test_failed_command_remains_a_command_with_real_status(self):
        result = _turn_result(
            final_response=None,
            items=[_command("false", status=_CommandStatus.failed, exit_code=1)],
        )
        events = _run(result)
        commands = [event for event in events if event.type == "command"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].text, "false")
        self.assertEqual(commands[0].raw["status"], "failed")
        self.assertFalse(any(event.type == "error" for event in events))

    def test_turn_error_maps_to_error_event(self):
        error = _Object(message="app-server crashed", additional_details="transport closed")
        result = _turn_result(final_response=None, status=_TurnStatus.failed, error=error)
        errors = [event for event in _run(result) if event.type == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].text, "app-server crashed")
        self.assertEqual(errors[0].raw["additional_details"], "transport closed")
        self.assertEqual(errors[0].raw["status"], "failed")

    def test_unknown_root_is_only_a_verbose_status(self):
        unknown = _ThreadItem(_Object(type="webSearch", id="search-1", query="docs"))
        quiet = list(iter_codex_events(unknown, verbose=False))
        loud = list(iter_codex_events(unknown, verbose=True))
        self.assertEqual(quiet, [])
        self.assertEqual(loud[0].type, "status")
        self.assertEqual(loud[0].raw["item_type"], "webSearch")

    def test_missing_runtime_and_sdk_failures_surface_as_error_events(self):
        unavailable = BackendUnavailable("codex", "sdk", "openai_codex is not importable", "hint")
        missing = _run(error=unavailable)
        self.assertTrue(any(event.type == "error" and "not importable" in event.text for event in missing))

        failed = _run(error=RuntimeError("authentication failed"))
        self.assertTrue(any(event.type == "error" and "authentication failed" in event.text for event in failed))


class CodexSessionCaptureTests(unittest.TestCase):
    def test_thread_id_comes_from_thread_outcome_regardless_of_verbose(self):
        for verbose in (False, True):
            events = _run(_turn_result(), verbose=verbose, thread_id="thread-9")
            captured = [
                event for event in events if (event.raw or {}).get("provider_session_id") == "thread-9"
            ]
            self.assertEqual(len(captured), 1, "verbose={}".format(verbose))
            raw = captured[0].raw
            self.assertEqual(raw["provider_session_kind"], "thread")
            self.assertEqual(raw["agent_id"], AGENT.id)
            self.assertEqual(captured[0].source, "codex")


class CodexOptionMappingTests(unittest.TestCase):
    def test_map_sdk_options_keeps_verified_keys_and_normalizes_effort_alias(self):
        mapped = _map_sdk_options(
            {
                "model": "gpt-5-codex",
                "sandbox": "workspace-write",
                "reasoning_effort": "high",
                "profile": "p",
                "approval_policy": "never",
            }
        )
        self.assertEqual(
            mapped,
            {"model": "gpt-5-codex", "sandbox": "workspace-write", "reasoning_effort": "high"},
        )
        self.assertEqual(_map_sdk_options({"thinking_level": "minimal"}), {"reasoning_effort": "minimal"})

    def test_sandbox_member_name_maps_cli_values_to_enum_members(self):
        self.assertEqual(sandbox_member_name("read-only"), "read_only")
        self.assertEqual(sandbox_member_name("workspace-write"), "workspace_write")
        self.assertEqual(sandbox_member_name("danger-full-access"), "full_access")
        self.assertIsNone(sandbox_member_name("nonesuch"))


class CodexProductionFactoryTests(unittest.TestCase):
    @staticmethod
    def _fake_module(state, result):
        module = ModuleType("openai_codex")

        class FakeCodexConfig:
            def __init__(self, *, codex_bin=None):
                self.codex_bin = codex_bin

        class FakeSandbox:
            read_only = object()
            workspace_write = object()
            full_access = object()

        class FakeReasoningEffort:
            minimal = object()
            low = object()
            medium = object()
            high = object()
            xhigh = object()

        class FakeThread:
            id = "thread-production"

            async def run(self, prompt, **kwargs):
                state["run"] = (prompt, kwargs)
                state["open_during_run"] = state["open"]
                return result

        class FakeAsyncCodex:
            def __init__(self, config=None):
                state["client_config"] = config

            async def __aenter__(self):
                state["entered"] += 1
                state["open"] = True
                return self

            async def __aexit__(self, exc_type, exc, tb):
                state["exited"] += 1
                state["open"] = False

            async def thread_start(self, **kwargs):
                state["thread_start"] = kwargs
                state["open_during_start"] = state["open"]
                return FakeThread()

        module.AsyncCodex = FakeAsyncCodex
        module.CodexConfig = FakeCodexConfig
        module.Sandbox = FakeSandbox
        module.generated = SimpleNamespace(v2_all=SimpleNamespace(ReasoningEffort=FakeReasoningEffort))
        return module, FakeSandbox, FakeReasoningEffort

    def test_production_factory_uses_verified_async_api_and_keeps_client_open_for_mapping(self):
        state = {"entered": 0, "exited": 0, "open": False}

        class GuardedResult:
            id = "turn-production"
            status = _TurnStatus.completed
            error = None

            @property
            def final_response(self):
                if not state["open"]:
                    raise AssertionError("client closed before final response mapping")
                return "Production-shape response."

            @property
            def items(self):
                if not state["open"]:
                    raise AssertionError("client closed before item mapping")
                return []

        module, sandbox, effort = self._fake_module(state, GuardedResult())
        configured_agent = AgentConfig(
            id="codex",
            type="codex",
            command="codex",
            backend="sdk",
        )
        runner = CodexSdkRunner(
            configured_agent,
            False,
            {
                "model": "gpt-5-codex",
                "sandbox": "workspace-write",
                "reasoning_effort": "high",
            },
            item_stream=_default_item_stream,
        )

        async def collect():
            return [event async for event in runner.run("do a thing", Path("/workspace"))]

        with mock.patch.dict(sys.modules, {"openai_codex": module}), mock.patch(
            "agent_collab.backends.codex_sdk.shutil.which",
            return_value="/opt/codex/bin/codex",
        ):
            events = asyncio.run(collect())

        self.assertEqual(state["entered"], 1)
        self.assertEqual(state["exited"], 1)
        self.assertTrue(state["open_during_start"])
        self.assertTrue(state["open_during_run"])
        self.assertFalse(state["open"])
        self.assertEqual(state["client_config"].codex_bin, "/opt/codex/bin/codex")
        self.assertEqual(
            state["thread_start"],
            {"cwd": "/workspace", "model": "gpt-5-codex", "sandbox": sandbox.workspace_write},
        )
        self.assertEqual(state["run"], ("do a thing", {"effort": effort.high}))
        self.assertNotIn("working_directory", state["thread_start"])
        self.assertTrue(any(event.type == "message" for event in events))
        self.assertTrue(
            any((event.raw or {}).get("provider_session_id") == "thread-production" for event in events)
        )

    def test_early_consumer_close_unwinds_async_codex_context(self):
        state = {"entered": 0, "exited": 0, "open": False}
        module, _, _ = self._fake_module(state, _turn_result())
        runner = CodexSdkRunner(AGENT, False, {}, item_stream=_default_item_stream)

        async def consume_one_then_close():
            events = runner.run("do a thing", Path("/workspace"))
            first = await events.__anext__()
            self.assertEqual((first.raw or {}).get("provider_session_id"), "thread-production")
            self.assertTrue(state["open"])
            await events.aclose()

        with mock.patch.dict(sys.modules, {"openai_codex": module}):
            asyncio.run(consume_one_then_close())

        self.assertEqual(state["exited"], 1)
        self.assertFalse(state["open"])

    def test_default_stream_reports_missing_or_incompatible_module(self):
        async def collect():
            return [outcome async for outcome in _default_item_stream(AGENT, {}, Path("."), "hi")]

        with mock.patch.dict(sys.modules, {"openai_codex": None}):
            with self.assertRaises(BackendUnavailable) as missing:
                asyncio.run(collect())
        self.assertIn("openai-codex", str(missing.exception))

        incompatible = ModuleType("openai_codex")
        incompatible.AsyncCodex = object
        with mock.patch.dict(sys.modules, {"openai_codex": incompatible}):
            with self.assertRaises(BackendUnavailable) as wrong_api:
                asyncio.run(collect())
        self.assertIn("thread_start", str(wrong_api.exception))


class CodexBackendSurfaceTests(unittest.TestCase):
    def test_registered_pair_and_honest_capabilities(self):
        self.assertTrue(backends.is_registered("codex", "sdk"))
        caps = backends.capabilities_for("codex", "sdk")
        self.assertEqual(caps.to_dict(), {"resume": False, "interrupt": False, "tool_gate": False})

    def test_probe_reports_unavailable_with_install_hint(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            health = CodexSdkBackend().probe()
        self.assertEqual(health.status, "unavailable")
        self.assertIn("openai-codex", health.reason)

    def test_settings_summary_has_package_and_verified_options(self):
        summary = CodexSdkBackend().settings_summary(
            AGENT,
            {"model": "gpt-5-codex", "sandbox": "read-only", "thinking_level": "high"},
        )
        self.assertEqual(summary["backend"], "sdk")
        self.assertEqual(summary["package"], "openai-codex")
        self.assertEqual(
            summary["options"],
            {"model": "gpt-5-codex", "sandbox": "read-only", "reasoning_effort": "high"},
        )


if __name__ == "__main__":
    unittest.main()
