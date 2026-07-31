"""Hermetic coverage for the Claude SDK outer-sandbox adapter."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from agent_collab.backends.claude_sdk.backend import ClaudeSdkRunner
from agent_collab.backends.claude_sdk import sandbox as claude_sdk_sandbox
from agent_collab.backends.claude_sdk.sandbox import ClaudeSdkSandboxAdapter
from agent_collab.backends.claude_sdk.worker import ClaudeSdkWorkerBackend
from agent_collab.config import AgentConfig
from agent_collab.outcomes import TurnOutcome
from agent_collab.sandbox.specs import (
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
)


class ClaudeSdkSandboxAdapterTests(unittest.TestCase):
    def test_describes_sdk_worker_support(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "claude-config"
            state.mkdir(mode=0o700)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            adapter = ClaudeSdkSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {
                        "CLAUDE_CONFIG_DIR": str(state),
                        "CLAUDE_CODE_TMPDIR": "claude-temp",
                        "HOME": str(root),
                    },
                )
            )
            self.assertIs(spec.support, SandboxSupport.SDK_WORKER)
            self.assertIn(SandboxPolicy.READ_ONLY, spec.policies)
            self.assertEqual(spec.state_roots[0].destination, state.absolute())
            self.assertEqual(
                spec.environment.set_values["CLAUDE_CONFIG_DIR"], str(state.absolute())
            )
            self.assertEqual(spec.environment.set_values["DISABLE_AUTOUPDATER"], "1")
            self.assertIn("CLAUDE_CODE_TMPDIR", spec.environment.private_tmp_names)
            self.assertEqual(
                spec.accounting_peer_roots,
                (workspace / "claude-temp" / f"claude-{os.getuid()}",),
            )
            self.assertEqual(
                dict(spec.native_profile.sdk_options).get("permission_mode"),
                "bypassPermissions",
            )
            self.assertIs(dict(spec.native_profile.sdk_options).get("strict_mcp_config"), True)
            self.assertEqual(dict(spec.native_profile.sdk_options).get("mcp_servers"), {})
            self.assertEqual(spec.native_profile.summary.get("mcp"), "strict_empty_configuration")

            payload = adapter.worker_open_payload_for_agent(
                agent_id="reviewer",
                options={"model": "sonnet", "permission_mode": "default"},
                workspace=workspace,
                cwd=workspace / "sub",
                agent_env={},
                verbose=False,
            )
            self.assertEqual(payload["backend"], "claude_sdk")
            self.assertEqual(payload["agent_id"], "reviewer")
            self.assertEqual(payload["options"]["permission_mode"], "bypassPermissions")
            self.assertEqual(payload["native"]["permission_mode"], "bypassPermissions")
            self.assertEqual(payload["cwd"], str(workspace / "sub"))

    def test_empty_or_home_config_dir_fails_closed(self) -> None:
        adapter = ClaudeSdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            for value in ("", "   ", ".", ".."):
                with self.subTest(value=repr(value)):
                    spec = adapter.describe(
                        SandboxContext(
                            workspace,
                            workspace,
                            {"CLAUDE_CONFIG_DIR": value, "HOME": str(root)},
                        )
                    )
                    with self.assertRaises(SandboxFailure) as raised:
                        for item in spec.compatibility:
                            item.check()
                    self.assertIn(
                        raised.exception.code,
                        {
                            "outer_sandbox_backend_incompatible",
                            "outer_sandbox_writable_too_broad",
                        },
                    )
            # Explicit HOME as config dir is too broad.
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {"CLAUDE_CONFIG_DIR": str(root), "HOME": str(root)},
                )
            )
            with self.assertRaises(SandboxFailure) as raised:
                for item in spec.compatibility:
                    item.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_writable_too_broad")

    def test_default_config_dir_leaves_legacy_home_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            adapter = ClaudeSdkSandboxAdapter()
            spec = adapter.describe(SandboxContext(workspace, workspace, {"HOME": str(root)}))
            expected = (root / ".claude").absolute()
            self.assertEqual(spec.state_roots[0].destination, expected)
            self.assertNotIn("CLAUDE_CONFIG_DIR", spec.environment.set_values)
            self.assertIn("CLAUDE_CONFIG_DIR", spec.environment.unset_names)

    def test_file_and_dropin_managed_configuration_fail_closed(self) -> None:
        adapter = ClaudeSdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw)
            state = managed / "state"
            workspace = managed / "workspace"
            state.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            for relative in (
                Path("managed-settings.json"),
                Path("managed-mcp.json"),
                Path("managed-settings.d") / "10-policy.json",
            ):
                with self.subTest(relative=str(relative)):
                    for item in managed.glob("managed-*"):
                        if item.is_dir():
                            for child in item.iterdir():
                                child.unlink()
                            item.rmdir()
                        else:
                            item.unlink()
                    target = managed / relative
                    target.parent.mkdir(exist_ok=True)
                    target.write_text("{}\n", encoding="utf-8")
                    with mock.patch.object(
                        claude_sdk_sandbox,
                        "LINUX_MANAGED_CONFIG_ROOT",
                        managed,
                    ):
                        spec = adapter.describe(
                            SandboxContext(
                                workspace,
                                workspace,
                                {"CLAUDE_CONFIG_DIR": str(state), "HOME": str(managed)},
                            )
                        )
                        with self.assertRaises(SandboxFailure) as raised:
                            for item in spec.compatibility:
                                item.check()
                    self.assertEqual(
                        raised.exception.code,
                        "outer_sandbox_backend_incompatible",
                    )


class ClaudeSdkWorkerBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_dedupes_session_and_streams_via_emit(self) -> None:
        class _System:
            def __init__(self, session_id: str) -> None:
                self.subtype = "init"
                self.data = {"session_id": session_id}
                self.session_id = session_id

        class _Result:
            def __init__(self, session_id: str) -> None:
                self.subtype = "success"
                self.is_error = False
                self.session_id = session_id
                self.content = None

        class _Conversation:
            def __init__(self) -> None:
                self.noted: list[str] = []

            async def run(self, prompt: str):
                del prompt
                yield _System("sess-1")
                yield _Result("sess-1")

            def note_session_id(self, session_id: str) -> None:
                self.noted.append(session_id)

            async def reset(self) -> None:
                return None

            async def close(self) -> None:
                return None

        backend = ClaudeSdkWorkerBackend()
        backend._conversation = _Conversation()
        backend._agent_id = "reviewer"
        streamed: list = []

        async def capture(event) -> None:
            streamed.append(event)

        residual, outcome = await backend.run("hello", run_id="r1", emit=capture)
        self.assertEqual(residual, [])
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(backend._conversation.noted, ["sess-1"])
        session_events = [
            event
            for event in streamed
            if (getattr(event, "raw", None) or {}).get("provider_session_id") == "sess-1"
            or (getattr(event, "provider_session", None) or {}).get("provider_session_id")
            == "sess-1"
        ]
        self.assertEqual(len(session_events), 1)

    async def test_runner_close_force_tears_down_worker_when_cancelled(self) -> None:
        runner = ClaudeSdkRunner(
            AgentConfig(id="claude", type="claude", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )

        class _Session:
            def __init__(self) -> None:
                self.close_started = asyncio.Event()
                self.force_teardowns = 0

            async def close(self) -> None:
                self.close_started.set()
                await asyncio.Event().wait()

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        session = _Session()
        runner._worker_session = session
        task = asyncio.create_task(runner.close())
        await session.close_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.force_teardowns, 1)
        self.assertIsNone(runner._worker_session)

    async def test_completed_turn_without_session_id_soft_drops_worker(self) -> None:
        runner = ClaudeSdkRunner(
            AgentConfig(id="claude", type="claude", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self) -> None:
                self.force_teardowns = 0

            async def run(self, _prompt, emit=None):
                del emit
                return [], TurnOutcome("completed")

            async def force_teardown(self) -> None:
                self.force_teardowns += 1

        session = _Session()

        async def worker_for(_workdir):
            runner._worker_session = session
            return session

        runner._worker_for = worker_for

        async def emit(_event) -> None:
            return None

        outcome = await runner.run_turn("prompt", Path("/workspace"), emit)

        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(session.force_teardowns, 1)
        self.assertIsNone(runner._worker_session)
        self.assertFalse(runner._worker_terminal)

    async def test_cancel_during_soft_drop_preserves_relaunch_eligibility(self) -> None:
        runner = ClaudeSdkRunner(
            AgentConfig(id="claude", type="claude", backend="sdk"),
            False,
            {},
            lambda _options, _workdir: None,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
        )

        class _Session:
            terminal = False
            _scratch = None

            def __init__(self) -> None:
                self.drop_started = asyncio.Event()

            async def run(self, _prompt, emit=None):
                del emit
                return [], TurnOutcome("failed", "provider_empty_response")

            async def force_teardown(self) -> None:
                self.drop_started.set()
                await asyncio.Event().wait()

        session = _Session()

        async def worker_for(_workdir):
            runner._worker_session = session
            return session

        runner._worker_for = worker_for

        async def emit(_event) -> None:
            return None

        task = asyncio.create_task(runner.run_turn("prompt", Path("/workspace"), emit))
        await session.drop_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(runner._worker_session)
        self.assertFalse(runner._worker_terminal)


if __name__ == "__main__":
    unittest.main()
