from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_collab.sandbox.bubblewrap import (
    BootstrapHandles,
    BubblewrapInstallation,
    BubblewrapStatusParser,
    build_bubblewrap_argv,
)
from agent_collab.sandbox.plan import ResolvedSandboxPlan
from agent_collab.sandbox.paths import (
    GitProtectionRecord,
    GitProvenance,
    MountOperation,
    PinnedIdentity,
)
from agent_collab.sandbox.specs import (
    BackendSandboxSpec,
    EnvironmentSpec,
    GitRole,
    NativeSandboxProfile,
    PathAccess,
    PathOrigin,
    Persistence,
    ResolvedSandboxPolicy,
    SandboxContext,
    SandboxEnforcement,
    SandboxFailure,
    SandboxPolicy,
    SandboxPolicySource,
    SandboxSupport,
    UnsupportedSandboxAdapter,
)
from agent_collab.sandbox.supervisor import (
    SandboxSupervisor,
    _PinnedProcess,
    _allocate_scratch,
    _resolve_inner_executable,
    _wait_pins,
    _verify_recursive_read_only_control,
    _verify_coverage_plan,
)


def _frame(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


def _recv_frame(channel):
    size = struct.unpack(">I", os.read(channel.fileno(), 4))[0]
    chunks = bytearray()
    while len(chunks) < size:
        chunks.extend(os.read(channel.fileno(), size - len(chunks)))
    return json.loads(bytes(chunks))


def _write_all(channel, value):
    view = memoryview(value)
    while view:
        written = os.write(channel.fileno(), view)
        view = view[written:]


class BubblewrapStatusTests(unittest.TestCase):
    def test_streaming_status_accepts_additive_objects_and_reconciles_exit(self):
        parser = BubblewrapStatusParser()
        self.assertEqual(
            parser.feed(b'{"child-pid":41,"future":true}\n{"phase":"x"}'),
            [{"child-pid": 41, "future": True}],
        )
        parser.feed(b'\n{"exit-code":0}\n')
        parser.finish()
        parser.reconcile(0)
        self.assertEqual(parser.child_pid, 41)
        self.assertEqual(parser.exit_code, 0)

    def test_repeated_child_pid_and_premature_eof_fail_closed(self):
        parser = BubblewrapStatusParser()
        parser.feed(b'{"child-pid":1}\n')
        with self.assertRaises(SandboxFailure):
            parser.feed(b'{"child-pid":2}\n')
        with self.assertRaises(SandboxFailure):
            BubblewrapStatusParser().finish()


class BubblewrapArgvTests(unittest.TestCase):
    def test_exact_engine_prefix_environment_and_bootstrap_roles(self):
        policy = ResolvedSandboxPolicy(
            SandboxPolicy.READ_ONLY,
            SandboxPolicy.READ_ONLY,
            SandboxPolicySource.REQUEST,
        )
        plan = ResolvedSandboxPlan(
            policy=policy,
            support=SandboxSupport.DIRECT_PROCESS,
            enforcement=SandboxEnforcement.OS_ENFORCED,
            context=SandboxContext(Path("/work with spaces"), Path("/work with spaces"), {}),
            spec=BackendSandboxSpec(
                support=SandboxSupport.DIRECT_PROCESS,
                policies=frozenset({SandboxPolicy.READ_ONLY}),
                environment=EnvironmentSpec(
                    set_values={"CODEX_HOME": "/state"},
                    private_tmp_names=("CLAUDE_CODE_TMPDIR",),
                ),
                native_profile=NativeSandboxProfile(),
            ),
            adapter=UnsupportedSandboxAdapter(),
        )
        handles = BootstrapHandles(9, 10, 11, 12, 13)
        argv = build_bubblewrap_argv(
            BubblewrapInstallation(Path("/usr/bin/bwrap"), "0.6.3"),
            plan,
            Path("/scratch"),
            handles,
            ("/usr/bin/true",),
        )
        self.assertEqual(
            argv[:16],
            (
                "/usr/bin/bwrap",
                "--json-status-fd",
                "9",
                "--die-with-parent",
                "--new-session",
                "--unshare-user",
                "--unshare-pid",
                "--cap-drop",
                "ALL",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
            ),
        )
        self.assertNotIn("--unshare-user-try", argv)
        self.assertNotIn("--ro-bind-try", argv)
        self.assertEqual(argv[-2:], ("--", "/usr/bin/true"))
        self.assertLess(argv.index("--unsetenv"), argv.index("--setenv"))
        self.assertIn("/work with spaces", argv)
        claude_tmp_index = argv.index("CLAUDE_CODE_TMPDIR")
        self.assertEqual(argv[claude_tmp_index + 1], "/scratch/tmp")


class SandboxLaunchInputTests(unittest.TestCase):
    def test_missing_scratch_anchor_is_a_stable_sandbox_failure(self):
        plan = mock.Mock()
        plan.scratch_anchor = Path("/agent-collab-tests/missing-scratch-anchor")

        with self.assertRaises(SandboxFailure) as raised:
            _allocate_scratch(plan)

        self.assertEqual(raised.exception.code, "outer_sandbox_scratch_anchor_invalid")
        self.assertEqual(raised.exception.phase, "launch")

    def test_missing_absolute_provider_is_a_stable_sandbox_failure(self):
        with self.assertRaises(SandboxFailure) as raised:
            _resolve_inner_executable(
                ("/agent-collab-tests/missing-provider",),
                {},
            )

        self.assertEqual(raised.exception.code, "outer_sandbox_inner_command_invalid")
        self.assertEqual(raised.exception.phase, "launch")


class PinnedProcessWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_requires_every_pidfd_to_signal_exit(self):
        first_read, first_write = os.pipe2(os.O_CLOEXEC)
        second_read, second_write = os.pipe2(os.O_CLOEXEC)
        proc_fd = os.open("/proc/self", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        pins = (
            _PinnedProcess(os.getpid(), os.dup(proc_fd), 0, first_read),
            _PinnedProcess(os.getpid(), os.dup(proc_fd), 0, second_read),
        )
        os.close(proc_fd)
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, os.write, first_write, b"x")
        loop.call_later(0.10, os.write, second_write, b"x")
        started = loop.time()
        try:
            await _wait_pins(pins, 1.0)
            self.assertGreaterEqual(loop.time() - started, 0.08)
        finally:
            for pin in pins:
                pin.close()
            os.close(first_write)
            os.close(second_write)


class RecursiveReadOnlyControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_establishment_timeout_has_a_stable_sandbox_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = mock.Mock()
            plan.policy.effective = SandboxPolicy.READ_ONLY
            plan.scratch_anchor = Path(raw)
            plan.render_prompt.return_value = "prompt"
            plan.prepare_inner.return_value = (sys.executable, "-c", "pass")
            plan.context.inherited_environment = dict(os.environ)
            supervisor = SandboxSupervisor(
                BubblewrapInstallation(Path("/usr/bin/bwrap"), "fixture")
            )
            with mock.patch.object(
                supervisor,
                "_launch",
                mock.AsyncMock(side_effect=asyncio.TimeoutError()),
            ):
                with self.assertRaises(SandboxFailure) as raised:
                    await supervisor.launch_cli(
                        plan,
                        (sys.executable, "-c", "pass"),
                        "prompt",
                        stream_limit=1024,
                    )

        self.assertEqual(raised.exception.code, "outer_sandbox_bootstrap_failed")
        self.assertEqual(raised.exception.phase, "establishment")

    async def test_writable_nested_mount_has_a_distinct_failure_code(self):
        class Process:
            def __init__(self):
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()
                self.returncode = None

            async def wait(self):
                self.returncode = 42
                return self.returncode

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as raw:
            installation = BubblewrapInstallation(Path("/usr/bin/bwrap"), "fixture")
            with mock.patch(
                "agent_collab.sandbox.supervisor.asyncio.create_subprocess_exec",
                return_value=Process(),
            ):
                with self.assertRaises(SandboxFailure) as raised:
                    await _verify_recursive_read_only_control(installation, Path(raw))

        self.assertEqual(
            raised.exception.code,
            "outer_sandbox_recursive_read_only_unavailable",
        )
        self.assertEqual(raised.exception.phase, "preflight")


class CoverageProofTests(unittest.TestCase):
    def test_logical_git_roots_require_exact_read_only_coverage_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            state = root / "state"
            external = state / "git"
            workspace.mkdir()
            external.mkdir(parents=True)
            record = GitProtectionRecord(
                external,
                GitRole.PRIMARY_OBJECT_STORE,
                (GitProvenance(),),
                PinnedIdentity.from_stat(os.stat(external, follow_symlinks=False)),
            )
            writable = MountOperation(
                state,
                state,
                PathAccess.WRITABLE,
                Persistence.HOST,
                (PathOrigin.PROVIDER_STATE,),
                ("State",),
            )
            coverage = MountOperation(
                external,
                external,
                PathAccess.READ_ONLY,
                Persistence.HOST,
                (PathOrigin.GIT_METADATA,),
                ("Git metadata",),
                covered_paths=(external,),
                git_roles=(GitRole.PRIMARY_OBJECT_STORE,),
                git_provenance=(GitProvenance(),),
            )
            workspace_operation = MountOperation(
                workspace,
                workspace,
                PathAccess.READ_ONLY,
                Persistence.HOST,
                (PathOrigin.WORKSPACE,),
                ("Workspace",),
            )
            policy = ResolvedSandboxPolicy(
                SandboxPolicy.READ_ONLY,
                SandboxPolicy.READ_ONLY,
                SandboxPolicySource.REQUEST,
            )
            plan = ResolvedSandboxPlan(
                policy=policy,
                support=SandboxSupport.DIRECT_PROCESS,
                enforcement=SandboxEnforcement.OS_ENFORCED,
                context=SandboxContext(workspace, workspace, {}),
                spec=BackendSandboxSpec(
                    support=SandboxSupport.DIRECT_PROCESS,
                    policies=frozenset({SandboxPolicy.READ_ONLY}),
                ),
                adapter=UnsupportedSandboxAdapter(),
                operations=(writable, coverage, workspace_operation),
                git_records=(record,),
            )

            self.assertEqual(_verify_coverage_plan(plan)[0][1], (record,))
            with self.assertRaises(SandboxFailure):
                _verify_coverage_plan(
                    replace(
                        plan,
                        operations=(
                            writable,
                            replace(coverage, covered_paths=()),
                            workspace_operation,
                        ),
                    )
                )
            with self.assertRaises(SandboxFailure):
                _verify_coverage_plan(
                    replace(
                        plan,
                        operations=(coverage, writable, workspace_operation),
                    )
                )


class BootstrapGateTests(unittest.TestCase):
    def _run(self, acknowledge: bool) -> tuple[int, bool]:
        bootstrap = Path(__file__).parents[2] / "agent_collab" / "sandbox" / "bootstrap.py"
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "provider-ran"
            proof_parent, proof_child = socket.socketpair()
            provider_input = os.open("/dev/null", os.O_RDONLY)
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            descriptors = [
                proof_child.fileno(),
                provider_input,
                stdout_write,
                stderr_write,
            ]
            command = [
                sys.executable,
                "-I",
                "-S",
                str(bootstrap),
                "--protocol-version",
                "1",
                "--proof-fd",
                str(proof_child.fileno()),
                "--provider-stdin-fd",
                str(provider_input),
                "--provider-stdout-fd",
                str(stdout_write),
                "--provider-stderr-fd",
                str(stderr_write),
                "--",
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=tuple(descriptors),
            )
            proof_child.close()
            os.close(provider_input)
            os.close(stdout_write)
            os.close(stderr_write)
            nonce = "a" * 64
            _write_all(
                proof_parent, _frame({"type": "sandbox_challenge", "version": 1, "nonce": nonce})
            )
            hello = _recv_frame(proof_parent)
            self.assertEqual(hello["type"], "sandbox_hello")
            self.assertFalse(marker.exists())
            if acknowledge:
                _write_all(
                    proof_parent,
                    _frame(
                        {
                            "type": "sandbox_ack",
                            "version": 1,
                            "nonce": nonce,
                            "verified": True,
                        }
                    ),
                )
            proof_parent.close()
            code = process.wait(timeout=5)
            diagnostic = os.read(stderr_read, 1024).decode("utf-8", errors="replace")
            os.close(stdout_read)
            os.close(stderr_read)
            if acknowledge and code != 0:
                self.fail(diagnostic or "bootstrap failed without diagnostics")
            return code, marker.exists()

    def test_provider_cannot_execute_before_ack(self):
        code, ran = self._run(False)
        self.assertEqual(code, 125)
        self.assertFalse(ran)

    def test_exact_ack_releases_provider_exec(self):
        code, ran = self._run(True)
        self.assertEqual(code, 0)
        self.assertTrue(ran)
