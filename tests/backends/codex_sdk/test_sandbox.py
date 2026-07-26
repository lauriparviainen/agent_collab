"""Hermetic coverage for the Codex SDK outer-sandbox adapter."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_collab.backends.codex_sdk.sandbox import CodexSdkSandboxAdapter
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


if __name__ == "__main__":
    unittest.main()
