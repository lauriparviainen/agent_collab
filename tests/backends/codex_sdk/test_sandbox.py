"""Hermetic coverage for the Codex SDK outer-sandbox adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from agent_collab.backends.codex_sdk.backend import CodexSdkRunner
from agent_collab.backends.codex_sdk.sandbox import CodexSdkSandboxAdapter
from agent_collab.config import AgentConfig
from agent_collab.sandbox.specs import SandboxContext, SandboxPolicy, SandboxSupport


class CodexSdkSandboxAdapterTests(unittest.TestCase):
    def test_describes_sdk_worker_support(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "codex-home"
            state.mkdir(mode=0o700)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            adapter = CodexSdkSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {"CODEX_HOME": str(state), "HOME": str(root)},
                )
            )
            self.assertIs(spec.support, SandboxSupport.SDK_WORKER)
            self.assertIn(SandboxPolicy.READ_ONLY, spec.policies)
            self.assertEqual(spec.state_roots[0].destination, state)
            self.assertEqual(
                dict(spec.native_profile.sdk_options).get("sandbox"),
                "danger-full-access",
            )
            payload = adapter.worker_open_payload(
                options={"model": "gpt-5.6-luna", "sandbox": "read-only"},
                workspace=workspace,
                cwd=workspace / "sub",
                agent_env={},
                codex_bin=None,
                verbose=False,
            )
            self.assertEqual(payload["backend"], "codex_sdk")
            self.assertEqual(payload["options"]["sandbox"], "danger-full-access")
            self.assertEqual(payload["native"]["sandbox"], "danger-full-access")
            self.assertEqual(payload["cwd"], str(workspace / "sub"))


class CodexSdkWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_close_force_tears_down_worker_when_cancelled(self) -> None:
        runner = CodexSdkRunner(
            AgentConfig(id="codex", type="codex", backend="sdk"),
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


if __name__ == "__main__":
    unittest.main()
