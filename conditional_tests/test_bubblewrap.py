from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import unittest

from agent_collab.sandbox.plan import SandboxOperatorConfig, resolve_session_plan
from agent_collab.sandbox.policy import resolve_sandbox_policy
from agent_collab.sandbox.specs import (
    BackendSandboxSpec,
    CreationPolicy,
    EnvironmentSpec,
    NativeSandboxProfile,
    PathAccess,
    Persistence,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
    StateRootSpec,
)
from agent_collab.sandbox.supervisor import SandboxSupervisor


ACTION = r"""
import json, os, pathlib, subprocess, sys
workspace = pathlib.Path(os.environ["BOUNDARY_WORKSPACE"])
state = pathlib.Path(os.environ["BOUNDARY_STATE"])
root = pathlib.Path(os.environ["BOUNDARY_ROOT"])
results = {}
for name, target, operation in (
    ("workspace_create", workspace / "forbidden", "write"),
    ("workspace_overwrite", workspace / "tracked", "write"),
    ("workspace_delete", workspace / "tracked", "unlink"),
    ("workspace_rename", workspace / "tracked", "rename"),
    ("workspace_chmod", workspace / "tracked", "chmod"),
    ("workspace_timestamp", workspace / "tracked", "utime"),
    ("state", state / "allowed", "write"),
    ("tmp", pathlib.Path(os.environ["TMPDIR"]) / "allowed", "write"),
    ("unapproved_host", root / "host-forbidden", "write"),
    ("workspace_symlink_host", workspace / "host-link", "write"),
    ("workspace_symlink_state", workspace / "state-link", "write"),
):
    try:
        if operation == "write":
            target.write_text(name, encoding="utf-8")
        elif operation == "unlink":
            target.unlink()
        elif operation == "rename":
            target.rename(workspace / "renamed")
        elif operation == "chmod":
            target.chmod(0o600)
        else:
            os.utime(target, None)
        results[name] = "allowed"
    except OSError:
        results[name] = "blocked"
try:
    os.link(workspace / "tracked", state / "hardlink")
    results["hardlink"] = "allowed"
except OSError as exc:
    results["hardlink"] = "cross-device" if exc.errno == 18 else "blocked"
child = subprocess.run(
    [sys.executable, "-c",
     "import os,pathlib,subprocess,sys;"
     "p=pathlib.Path(os.environ['BOUNDARY_WORKSPACE'])/'child-forbidden';"
     "\ntry: p.write_text('x'); direct='allowed'"
     "\nexcept OSError: direct='blocked'"
     "\ngrand=subprocess.run([sys.executable,'-c',"
     "\"import os,pathlib; p=pathlib.Path(os.environ['BOUNDARY_WORKSPACE'])/'grand-forbidden';"
     "\\ntry: p.write_text('x'); print('allowed')"
     "\\nexcept OSError: print('blocked')\"],check=True,text=True,stdout=subprocess.PIPE)"
     "\nprint(direct+','+grand.stdout.strip())"],
    check=True, text=True, stdout=subprocess.PIPE,
)
results["child_and_grandchild"] = child.stdout.strip()
results["prompt_has_policy"] = sys.argv[1].startswith("FILESYSTEM POLICY\n")
print(json.dumps(results, sort_keys=True))
"""

DESCENDANT_ACTION = r"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


@dataclass(frozen=True)
class _Adapter:
    root: Path
    workspace: Path
    state: Path

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        del context
        return BackendSandboxSpec(
            support=SandboxSupport.DIRECT_PROCESS,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=(
                StateRootSpec(
                    "Fixture state",
                    self.state,
                    PathAccess.WRITABLE,
                    Persistence.HOST,
                    CreationPolicy.MUST_EXIST,
                ),
            ),
            environment=EnvironmentSpec(
                set_values={
                    "BOUNDARY_STATE": str(self.state),
                    "BOUNDARY_WORKSPACE": str(self.workspace),
                    "BOUNDARY_ROOT": str(self.root),
                }
            ),
            native_profile=NativeSandboxProfile(summary={"fixture": "permissive_after_ack"}),
        )

    def prepare_inner(self, plan, command):
        del plan
        return tuple(command)


class BubblewrapBoundaryTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    async def test_workspace_and_child_are_read_only_while_state_and_scratch_write(self):
        if platform.system() != "Linux":
            self.skipTest("Bubblewrap boundary tests require Linux")
        if shutil.which("bwrap") is None:
            self.skipTest("Bubblewrap boundary tests require bwrap on PATH")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            self.skipTest("XDG_RUNTIME_DIR is unavailable for the safe scratch anchor")
        runtime_path = Path(runtime)
        try:
            runtime_stat = runtime_path.stat()
        except OSError:
            self.skipTest("XDG_RUNTIME_DIR is not accessible")
        if runtime_stat.st_uid != os.getuid() or runtime_stat.st_mode & 0o077:
            self.skipTest("XDG_RUNTIME_DIR is not an owner-private daemon runtime")

        with tempfile.TemporaryDirectory(
            prefix="agent-collab-boundary-",
            dir=runtime_path,
        ) as raw:
            root = Path(raw).resolve()
            root.chmod(0o700)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            tracked = workspace / "tracked"
            tracked.write_text("original", encoding="utf-8")
            host_target = root / "host-target"
            host_target.write_text("host-original", encoding="utf-8")
            (workspace / "host-link").symlink_to(host_target)
            (workspace / "state-link").symlink_to(state / "through-link")
            adapter = _Adapter(root, workspace, state)
            policy = resolve_sandbox_policy("read-only", "none")
            plan = resolve_session_plan(
                policy=policy,
                workspace_path=workspace,
                agents={"fixture": (None, {}, adapter)},
                operator=SandboxOperatorConfig(
                    scratch_root=runtime_path / "agent-collab" / "sandbox-tests",
                ),
            ).agents["fixture"]
            process = await SandboxSupervisor().launch_cli(
                plan,
                (str(Path(sys.executable).resolve()), "-I", "-S", "-c", ACTION),
                "run the structural action",
                stream_limit=1024 * 1024,
            )
            scratch = process._scratch
            stdout_task = asyncio.create_task(process.stdout.read())
            stderr_task = asyncio.create_task(process.stderr.read())
            code = await process.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            self.assertEqual(code, 0, stderr.decode("utf-8", errors="replace"))
            result = json.loads(stdout.decode("utf-8"))
            self.assertEqual(
                result,
                {
                    "child_and_grandchild": "blocked,blocked",
                    "hardlink": "cross-device",
                    "prompt_has_policy": True,
                    "state": "allowed",
                    "tmp": "allowed",
                    "unapproved_host": "blocked",
                    "workspace_chmod": "blocked",
                    "workspace_create": "blocked",
                    "workspace_delete": "blocked",
                    "workspace_overwrite": "blocked",
                    "workspace_rename": "blocked",
                    "workspace_symlink_host": "blocked",
                    "workspace_symlink_state": "allowed",
                    "workspace_timestamp": "blocked",
                },
            )
            self.assertFalse((workspace / "forbidden").exists())
            self.assertFalse((workspace / "child-forbidden").exists())
            self.assertFalse((workspace / "grand-forbidden").exists())
            self.assertEqual(tracked.read_text(encoding="utf-8"), "original")
            self.assertEqual(host_target.read_text(encoding="utf-8"), "host-original")
            self.assertTrue((state / "allowed").is_file())
            self.assertTrue((state / "through-link").is_file())
            self.assertFalse((state / "hardlink").exists())
            self.assertFalse(scratch.exists())

    async def test_preflight_proves_recursive_read_only_nested_mounts(self):
        if platform.system() != "Linux":
            self.skipTest("Bubblewrap boundary tests require Linux")
        if shutil.which("bwrap") is None:
            self.skipTest("Bubblewrap boundary tests require bwrap on PATH")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            self.skipTest("XDG_RUNTIME_DIR is unavailable for the safe scratch anchor")
        runtime_path = Path(runtime)
        if runtime_path.stat().st_uid != os.getuid() or runtime_path.stat().st_mode & 0o077:
            self.skipTest("XDG_RUNTIME_DIR is not an owner-private daemon runtime")

        with tempfile.TemporaryDirectory(
            prefix="agent-collab-preflight-",
            dir=runtime_path,
        ) as raw:
            root = Path(raw).resolve()
            root.chmod(0o700)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            plan = resolve_session_plan(
                policy=resolve_sandbox_policy("read-only", "none"),
                workspace_path=workspace,
                agents={"fixture": (None, {}, _Adapter(root, workspace, state))},
                operator=SandboxOperatorConfig(
                    scratch_root=runtime_path / "agent-collab" / "sandbox-tests",
                ),
            ).agents["fixture"]

            await SandboxSupervisor().preflight(plan)

    async def test_termination_reaps_descendant_and_cleans_scratch(self):
        if platform.system() != "Linux":
            self.skipTest("Bubblewrap boundary tests require Linux")
        if shutil.which("bwrap") is None:
            self.skipTest("Bubblewrap boundary tests require bwrap on PATH")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            self.skipTest("XDG_RUNTIME_DIR is unavailable for the safe scratch anchor")
        runtime_path = Path(runtime)
        if runtime_path.stat().st_uid != os.getuid() or runtime_path.stat().st_mode & 0o077:
            self.skipTest("XDG_RUNTIME_DIR is not an owner-private daemon runtime")

        with tempfile.TemporaryDirectory(
            prefix="agent-collab-reaping-",
            dir=runtime_path,
        ) as raw:
            root = Path(raw).resolve()
            root.chmod(0o700)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            plan = resolve_session_plan(
                policy=resolve_sandbox_policy("read-only", "none"),
                workspace_path=workspace,
                agents={"fixture": (None, {}, _Adapter(root, workspace, state))},
                operator=SandboxOperatorConfig(
                    scratch_root=runtime_path / "agent-collab" / "sandbox-tests",
                ),
            ).agents["fixture"]
            process = await SandboxSupervisor().launch_cli(
                plan,
                (str(Path(sys.executable).resolve()), "-I", "-S", "-c", DESCENDANT_ACTION),
                "start descendant fixture",
                stream_limit=1024 * 1024,
            )
            scratch = process._scratch
            child_pid = int((await asyncio.wait_for(process.stdout.readline(), 2)).decode().strip())
            process.terminate()
            code = await asyncio.wait_for(process.wait(), 5)
            self.assertNotEqual(code, 0)
            for _ in range(100):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
            self.assertFalse(scratch.exists())

    async def test_each_launch_rejects_hardlink_planted_after_plan_resolution(self):
        if platform.system() != "Linux":
            self.skipTest("Bubblewrap boundary tests require Linux")
        if shutil.which("bwrap") is None:
            self.skipTest("Bubblewrap boundary tests require bwrap on PATH")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            self.skipTest("XDG_RUNTIME_DIR is unavailable for the safe scratch anchor")
        runtime_path = Path(runtime)
        if runtime_path.stat().st_uid != os.getuid() or runtime_path.stat().st_mode & 0o077:
            self.skipTest("XDG_RUNTIME_DIR is not an owner-private daemon runtime")

        with tempfile.TemporaryDirectory(
            prefix="agent-collab-reaudit-",
            dir=runtime_path,
        ) as raw:
            root = Path(raw).resolve()
            root.chmod(0o700)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            tracked = workspace / "tracked"
            tracked.write_text("protected", encoding="utf-8")
            plan = resolve_session_plan(
                policy=resolve_sandbox_policy("read-only", "none"),
                workspace_path=workspace,
                agents={"fixture": (None, {}, _Adapter(root, workspace, state))},
                operator=SandboxOperatorConfig(
                    scratch_root=runtime_path / "agent-collab" / "sandbox-tests",
                ),
            ).agents["fixture"]

            os.link(tracked, state / "planted-after-plan")
            with self.assertRaises(SandboxFailure) as raised:
                await SandboxSupervisor().launch_cli(
                    plan,
                    (str(Path(sys.executable).resolve()), "-I", "-S", "-c", "pass"),
                    "provider must not run",
                    stream_limit=1024 * 1024,
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")
