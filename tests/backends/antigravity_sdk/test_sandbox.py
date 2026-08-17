"""Hermetic coverage for the Antigravity SDK outer-sandbox adapter."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from agent_collab.backends.antigravity_sdk.sandbox import (
    AntigravitySdkSandboxAdapter,
    durable_trajectory_dir,
)
from agent_collab.config import AgentConfig
from agent_collab.outcomes import TurnOutcome
from agent_collab.sandbox.specs import (
    Persistence,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
)


class AntigravitySdkSandboxAdapterTests(unittest.TestCase):
    def test_describes_sdk_worker_support(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            adapter = AntigravitySdkSandboxAdapter()
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {"HOME": str(root), "AGENT_COLLAB_HOME": str(root / "ac-home")},
                    session_id="sess-describe",
                )
            )
            self.assertIs(spec.support, SandboxSupport.SDK_WORKER)
            self.assertIn(SandboxPolicy.READ_ONLY, spec.policies)
            self.assertIn(SandboxPolicy.NONE, spec.policies)
            labels = {item.label for item in spec.state_roots}
            self.assertIn("Antigravity SDK trajectory", labels)
            self.assertIn("Antigravity SDK app data", labels)
            self.assertIn("Antigravity SDK private home", labels)
            by_label = {item.label: item for item in spec.state_roots}
            self.assertEqual(by_label["Antigravity SDK trajectory"].persistence, Persistence.HOST)
            self.assertEqual(
                by_label["Antigravity SDK app data"].persistence.value, "session_private"
            )
            self.assertEqual(
                by_label["Antigravity SDK private home"].persistence.value, "session_private"
            )
            for item in spec.state_roots:
                self.assertEqual(item.creation.value, "create_private_directory")
            self.assertEqual(dict(spec.native_profile.sdk_options).get("policy"), "ask_user")
            self.assertEqual(
                spec.environment.set_values.get("HOME"),
                str(spec.state_roots[2].destination),
            )
            self.assertEqual(
                spec.environment.set_values.get("ANTIGRAVITY_SAVE_DIR"),
                str(spec.state_roots[0].destination),
            )
            payload = adapter.worker_open_payload_for_agent(
                agent_id="reviewer",
                options={"model": "gemini-3.1-pro-high"},
                workspace=workspace,
                cwd=workspace / "sub",
                agent_env={},
                backend_config={"vertex": True, "project": "p", "location": "us"},
                verbose=False,
                save_dir=str(root / "traj"),
                app_data_dir=str(root / "app"),
            )
            self.assertEqual(payload["backend"], "antigravity_sdk")
            self.assertEqual(payload["agent_id"], "reviewer")
            self.assertEqual(payload["native"]["policy"], "ask_user")
            self.assertEqual(payload["save_dir"], str(root / "traj"))
            self.assertEqual(payload["app_data_dir"], str(root / "app"))
            self.assertEqual(payload["cwd"], str(workspace / "sub"))
            self.assertEqual(payload["backend_config"]["project"], "p")
            with mock.patch.dict(
                os.environ,
                {
                    "ANTIGRAVITY_SAVE_DIR": str(root / "ambient-traj"),
                    "ANTIGRAVITY_APP_DATA_DIR": str(root / "ambient-app"),
                },
            ):
                with self.assertRaises(RuntimeError) as raised:
                    adapter.worker_open_payload_for_agent(
                        agent_id="reviewer",
                        options={},
                        workspace=workspace,
                        cwd=workspace,
                        agent_env={},
                        backend_config={},
                        verbose=False,
                    )
            self.assertIn("durable save_dir", str(raised.exception))
            self.assertNotIn("agent-collab-antigravity-sdk", str(raised.exception))
            self.assertNotIn("ambient-traj", str(raised.exception))
            with self.assertRaises(RuntimeError) as raised_app:
                adapter.worker_open_payload_for_agent(
                    agent_id="reviewer",
                    options={},
                    workspace=workspace,
                    cwd=workspace,
                    agent_env={},
                    backend_config={},
                    verbose=False,
                    save_dir=str(root / "traj"),
                )
            self.assertIn("app_data_dir", str(raised_app.exception))

    def test_worker_open_payload_typed_resume_wins_over_conversation_id(self) -> None:
        adapter = AntigravitySdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            payload = adapter.worker_open_payload_for_agent(
                agent_id="reviewer",
                options={},
                workspace=workspace,
                cwd=workspace,
                agent_env={},
                backend_config={},
                verbose=False,
                save_dir=str(root / "traj"),
                app_data_dir=str(root / "app"),
                conversation_id="old-id",
                resume={"provider_session_id": "conv-resume"},
            )
            self.assertEqual(payload["resume"]["provider_session_id"], "conv-resume")
            self.assertEqual(payload["conversation_id"], "conv-resume")
            with self.assertRaises(RuntimeError):
                adapter.worker_open_payload_for_agent(
                    agent_id="reviewer",
                    options={},
                    workspace=workspace,
                    cwd=workspace,
                    agent_env={},
                    backend_config={},
                    verbose=False,
                    save_dir=str(root / "traj"),
                    app_data_dir=str(root / "app"),
                    resume={"provider_session_id": ""},
                )

    def test_session_keyed_trajectory_is_stable_and_host_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            home = root / "ac-home"
            home.mkdir(mode=0o700)
            adapter = AntigravitySdkSandboxAdapter()
            env = {"HOME": str(root / "home"), "AGENT_COLLAB_HOME": str(home)}
            first = adapter.describe(SandboxContext(workspace, workspace, env, session_id="sess-a"))
            second = adapter.describe(
                SandboxContext(workspace, workspace, env, session_id="sess-a")
            )
            other = adapter.describe(SandboxContext(workspace, workspace, env, session_id="sess-b"))
            base_only = adapter.describe(SandboxContext(workspace, workspace, env))
            traj_a = first.state_roots[0].destination
            self.assertEqual(traj_a, second.state_roots[0].destination)
            self.assertNotEqual(traj_a, other.state_roots[0].destination)
            self.assertEqual(traj_a, home / "trajectories" / "sess-a")
            self.assertEqual(other.state_roots[0].destination, home / "trajectories" / "sess-b")
            self.assertEqual(base_only.state_roots[0].destination, home / "trajectories")
            self.assertEqual(first.state_roots[0].persistence, Persistence.HOST)
            self.assertNotIn("sess-a", str(base_only.state_roots[0].destination))
            self.assertFalse(any(part == "trajectory" and "hex" in part for part in traj_a.parts))
            self.assertEqual(
                durable_trajectory_dir(
                    SandboxContext(workspace, workspace, env, session_id="sess-a")
                ),
                traj_a,
            )

    def test_invalid_session_id_is_not_interpolated_into_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            home = root / "ac-home"
            home.mkdir(mode=0o700)
            spec = AntigravitySdkSandboxAdapter().describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {"HOME": str(root), "AGENT_COLLAB_HOME": str(home)},
                    session_id="../escape",
                )
            )
            self.assertNotIn("escape", spec.state_roots[0].destination.parts)
            with self.assertRaises(SandboxFailure) as raised:
                for item in spec.compatibility:
                    if item.name == "session_id_path_safety":
                        item.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_scratch_anchor_invalid")

    def test_adc_must_be_absolute_existing_file_when_set(self) -> None:
        adapter = AntigravitySdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            missing = root / "missing-adc.json"
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {
                        "HOME": str(root),
                        "GOOGLE_APPLICATION_CREDENTIALS": str(missing),
                    },
                )
            )
            with self.assertRaises(SandboxFailure) as raised:
                for item in spec.compatibility:
                    if item.name == "adc_path":
                        item.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_path_missing")

            relative = "relative-adc.json"
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {
                        "HOME": str(root),
                        "GOOGLE_APPLICATION_CREDENTIALS": relative,
                    },
                )
            )
            with self.assertRaises(SandboxFailure) as raised:
                for item in spec.compatibility:
                    if item.name == "adc_path":
                        item.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

            adc = root / "adc.json"
            adc.write_text("{}\n", encoding="utf-8")
            spec = adapter.describe(
                SandboxContext(
                    workspace,
                    workspace,
                    {
                        "HOME": str(root),
                        "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                    },
                )
            )
            for item in spec.compatibility:
                if item.name == "adc_path":
                    item.check()
            self.assertEqual(
                spec.environment.set_values.get("GOOGLE_APPLICATION_CREDENTIALS"),
                str(adc.absolute()),
            )
            self.assertTrue(
                any(
                    item.destination == adc.parent.absolute()
                    for item in spec.provider_visible_paths
                )
            )
            self.assertIn("ANTIGRAVITY_STATE_ROOT", spec.environment.set_values)

    def test_empty_agent_adc_does_not_restore_daemon_ambient(self) -> None:
        adapter = AntigravitySdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            ambient = root / "daemon-adc.json"
            ambient.write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"GOOGLE_APPLICATION_CREDENTIALS": str(ambient)},
                clear=False,
            ):
                spec = adapter.describe(
                    SandboxContext(
                        workspace,
                        workspace,
                        {
                            "HOME": str(root),
                            "GOOGLE_APPLICATION_CREDENTIALS": "",
                        },
                    )
                )
            self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", spec.environment.set_values)
            self.assertEqual(spec.provider_visible_paths, ())

    def test_session_state_anchor_rejects_workspace_overlap(self) -> None:
        adapter = AntigravitySdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            with mock.patch(
                "agent_collab.backends.antigravity_sdk.sandbox._select_session_state_base",
                return_value=None,
            ):
                spec = adapter.describe(
                    SandboxContext(workspace, workspace, {"HOME": str(workspace)})
                )
                with self.assertRaises(SandboxFailure) as raised:
                    for item in spec.compatibility:
                        if item.name == "session_state_anchor":
                            item.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_scratch_anchor_invalid")

    def test_explicit_agent_collab_home_does_not_fall_back_to_real_home(self) -> None:
        """Configured AGENT_COLLAB_HOME overlapping workspace must fail closed."""
        from agent_collab.backends.antigravity_sdk import sandbox as sandbox_mod

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            # Explicit home under workspace → rejected for overlap; must not
            # silently use the real ~/.agent-collab.
            overlapping_home = workspace / ".agent-collab"
            overlapping_home.mkdir(mode=0o700)
            base = sandbox_mod._select_session_state_base(
                SandboxContext(
                    workspace,
                    workspace,
                    {
                        "HOME": str(root / "home"),
                        "AGENT_COLLAB_HOME": str(overlapping_home),
                    },
                )
            )
            self.assertIsNone(base)
            persistent = sandbox_mod._select_persistent_trajectory_base(
                SandboxContext(
                    workspace,
                    workspace,
                    {
                        "HOME": str(root / "home"),
                        "AGENT_COLLAB_HOME": str(overlapping_home),
                    },
                )
            )
            self.assertIsNone(persistent)

    def test_protobuf_incompatible_fails_closed(self) -> None:
        adapter = AntigravitySdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            spec = adapter.describe(SandboxContext(workspace, workspace, {"HOME": str(root)}))
            with mock.patch(
                "agent_collab.backends.antigravity_sdk.sandbox.package_version",
                return_value="6.33.6",
            ):
                with self.assertRaises(SandboxFailure) as raised:
                    for item in spec.compatibility:
                        if item.name == "protobuf_runtime":
                            item.check()
            self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_native_runtime_gate_requires_compatible_glibc(self) -> None:
        adapter = AntigravitySdkSandboxAdapter()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            spec = adapter.describe(SandboxContext(workspace, workspace, {"HOME": str(root)}))

            def _run_check() -> None:
                for item in spec.compatibility:
                    if item.name == "native_runtime":
                        item.check()

            for host, label in (
                (("musl", "1.2.3"), "not_applicable"),
                (("", ""), "indeterminate"),
                (("glibc", "2.25"), "incompatible"),
            ):
                with mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.platform.libc_ver",
                    return_value=host,
                ):
                    with self.assertRaises(SandboxFailure) as raised:
                        _run_check()
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_backend_incompatible",
                    msg=label,
                )
            with mock.patch(
                "agent_collab.backends.antigravity_sdk.sandbox.platform.libc_ver",
                return_value=("glibc", "2.35"),
            ):
                _run_check()


class AntigravitySdkWorkerBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_uses_workspace_not_cwd_for_sdk_workspaces(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        captured: dict = {}

        def fake_default_conversation(agent, options, workdir, **kwargs):
            captured["workdir"] = workdir
            captured["kwargs"] = kwargs
            captured["extra_workspaces"] = kwargs.get("extra_workspaces")

            class _Conv:
                def active(self):
                    return False

                async def run(self, prompt):
                    raise RuntimeError("unused")

                def note_session_id(self, conversation_id):
                    return None

                async def reset(self):
                    return None

                async def close(self):
                    return None

            return _Conv()

        with mock.patch(
            "agent_collab.backends.antigravity_sdk.worker._default_conversation",
            side_effect=fake_default_conversation,
        ):
            backend = AntigravitySdkWorkerBackend()
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                workspace = root / "workspace"
                sub = workspace / "sub"
                traj = root / "traj"
                app = root / "app"
                workspace.mkdir()
                sub.mkdir()
                traj.mkdir()
                app.mkdir()
                await backend.open(
                    {
                        "workspace": str(workspace),
                        "cwd": str(sub),
                        "options": {"model": "m"},
                        "backend_config": {},
                        "agent_env": {},
                        "agent_id": "reviewer",
                        "verbose": False,
                        "save_dir": str(traj),
                        "app_data_dir": str(app),
                    }
                )
        self.assertEqual(captured["workdir"], workspace.resolve())
        self.assertNotEqual(captured["kwargs"].get("allow_all_policy"), True)
        self.assertTrue(callable(captured["kwargs"].get("ask_user_handler")))
        self.assertEqual(tuple(captured.get("extra_workspaces") or ()), ())

    async def test_open_seeds_captured_conversation_id_for_resume(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        noted: list[str] = []

        def fake_default_conversation(agent, options, workdir, **kwargs):
            del agent, options, workdir, kwargs

            class _Conv:
                def active(self):
                    return True

                async def run(self, prompt):
                    raise RuntimeError("unused")

                def note_session_id(self, conversation_id):
                    noted.append(conversation_id)

                async def reset(self):
                    return None

                async def close(self):
                    return None

            return _Conv()

        with mock.patch(
            "agent_collab.backends.antigravity_sdk.worker._default_conversation",
            side_effect=fake_default_conversation,
        ):
            backend = AntigravitySdkWorkerBackend()
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                workspace = root / "workspace"
                traj = root / "traj"
                app = root / "app"
                workspace.mkdir()
                traj.mkdir()
                app.mkdir()
                await backend.open(
                    {
                        "workspace": str(workspace),
                        "cwd": str(workspace),
                        "options": {},
                        "backend_config": {},
                        "agent_env": {},
                        "agent_id": "reviewer",
                        "verbose": False,
                        "save_dir": str(traj),
                        "app_data_dir": str(app),
                        "conversation_id": "conv-resume-1",
                    }
                )
        self.assertEqual(noted, ["conv-resume-1"])

    async def test_open_with_conversation_id_does_not_recreate_missing_save_dir(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        def unused_conversation(*_args, **_kwargs):
            raise AssertionError(
                "worker open must not construct a conversation when save_dir is missing"
            )

        with (
            mock.patch(
                "agent_collab.backends.antigravity_sdk.worker._default_conversation",
                side_effect=unused_conversation,
            ),
            mock.patch(
                "agent_collab.backends.antigravity_sdk.backend._default_agent_factory",
                side_effect=unused_conversation,
            ),
        ):
            backend = AntigravitySdkWorkerBackend()
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                workspace = root / "workspace"
                traj = root / "traj"
                app = root / "app"
                workspace.mkdir()
                traj.mkdir()
                app.mkdir()
                traj.rmdir()
                with self.assertRaises(RuntimeError) as raised:
                    await backend.open(
                        {
                            "workspace": str(workspace),
                            "cwd": str(workspace),
                            "options": {},
                            "backend_config": {},
                            "agent_env": {},
                            "agent_id": "reviewer",
                            "verbose": False,
                            "save_dir": str(traj),
                            "app_data_dir": str(app),
                            "conversation_id": "conv-resume-missing",
                        }
                    )
                self.assertIn("save_dir is missing or unusable", str(raised.exception))
                self.assertFalse(traj.exists())

    async def test_open_declares_external_cwd_as_extra_workspace(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        captured: dict = {}

        def fake_default_conversation(agent, options, workdir, **kwargs):
            del agent, options
            captured["workdir"] = workdir
            captured["extra_workspaces"] = kwargs.get("extra_workspaces")

            class _Conv:
                def active(self):
                    return False

                async def run(self, prompt):
                    raise RuntimeError("unused")

                def note_session_id(self, conversation_id):
                    return None

                async def reset(self):
                    return None

                async def close(self):
                    return None

            return _Conv()

        with mock.patch(
            "agent_collab.backends.antigravity_sdk.worker._default_conversation",
            side_effect=fake_default_conversation,
        ):
            backend = AntigravitySdkWorkerBackend()
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                workspace = root / "workspace"
                external = root / "other-tree"
                traj = root / "traj"
                app = root / "app"
                workspace.mkdir()
                external.mkdir()
                traj.mkdir()
                app.mkdir()
                await backend.open(
                    {
                        "workspace": str(workspace),
                        "cwd": str(external),
                        "options": {"model": "m"},
                        "backend_config": {},
                        "agent_env": {},
                        "agent_id": "reviewer",
                        "verbose": False,
                        "save_dir": str(traj),
                        "app_data_dir": str(app),
                    }
                )
        self.assertEqual(captured["workdir"], workspace.resolve())
        self.assertEqual(
            tuple(Path(p).resolve() for p in (captured.get("extra_workspaces") or ())),
            (external.resolve(),),
        )

    async def test_exception_after_id_capture_emits_provider_session(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        class _Conversation:
            def __init__(self) -> None:
                self._conversation_id = "conv-after-chat"
                self.reset_calls = 0

            async def run(self, prompt: str):
                del prompt
                raise RuntimeError("resolve failed after chat assigned id")

            def note_session_id(self, conversation_id: str) -> None:
                self._conversation_id = conversation_id

            async def reset(self) -> None:
                self.reset_calls += 1

            async def close(self) -> None:
                return None

        backend = AntigravitySdkWorkerBackend()
        backend._conversation = _Conversation()
        backend._agent_id = "reviewer"
        streamed: list = []

        async def capture(event) -> None:
            streamed.append(event)

        residual, outcome = await backend.run("hello", run_id="r1", emit=capture)
        self.assertEqual(residual, [])
        self.assertEqual(outcome.outcome, "failed")
        session_events = [
            event
            for event in streamed
            if (getattr(event, "provider_session", None) or {}).get("provider_session_id")
            == "conv-after-chat"
            or (getattr(event, "raw", None) or {}).get("provider_session_id") == "conv-after-chat"
        ]
        self.assertEqual(len(session_events), 1)
        self.assertTrue(any(event.type == "error" for event in streamed))
        self.assertEqual(backend._conversation.reset_calls, 1)

    async def test_interrupt_without_conversation_is_noop(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        backend = AntigravitySdkWorkerBackend()
        await backend.interrupt("run-1")

    async def test_interrupt_calls_conversation_once_and_maps_cancelled_error(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        class AntigravityCancelledError(asyncio.CancelledError):
            pass

        class _Conversation:
            def __init__(self) -> None:
                self.interrupt_calls = 0
                self.reset_calls = 0
                self.started = asyncio.Event()
                self._release = asyncio.Event()
                self._conversation_id = "conv-1"

            async def run(self, prompt: str):
                del prompt
                self.started.set()
                await self._release.wait()
                raise AntigravityCancelledError()

            def note_session_id(self, conversation_id: str) -> None:
                self._conversation_id = conversation_id

            async def interrupt(self) -> bool:
                self.interrupt_calls += 1
                self._release.set()
                return True

            async def reset(self) -> None:
                self.reset_calls += 1

            async def close(self) -> None:
                return None

        conversation = _Conversation()
        backend = AntigravitySdkWorkerBackend()
        backend._conversation = conversation
        backend._agent_id = "reviewer"
        task = asyncio.create_task(backend.run("hello", run_id="r1"))
        await conversation.started.wait()
        await backend.interrupt("r1")
        residual, outcome = await task
        self.assertTrue(
            any(
                (getattr(event, "raw", None) or {}).get("provider_session_id") == "conv-1"
                for event in residual
            )
        )
        self.assertEqual(conversation.interrupt_calls, 1)
        self.assertEqual(
            (outcome.outcome, outcome.code),
            ("interrupted", "local_turn_interrupted"),
        )
        self.assertEqual(conversation.reset_calls, 0)
        self.assertIs(backend._conversation, conversation)

    async def test_interrupt_completion_race_keeps_completed_outcome(self) -> None:
        from agent_collab.backends.antigravity_sdk.backend import AntigravityTurn
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        class _Text:
            def __init__(self) -> None:
                self.step_index = 0
                self.text = "Done."

        class _Conversation:
            def __init__(self) -> None:
                self.interrupt_calls = 0
                self.reset_calls = 0
                self.started = asyncio.Event()
                self._release = asyncio.Event()

            async def run(self, prompt: str):
                del prompt
                self.started.set()
                await self._release.wait()
                return AntigravityTurn([_Text()], None, "conv-1", True)

            def note_session_id(self, conversation_id: str) -> None:
                del conversation_id

            async def interrupt(self) -> bool:
                self.interrupt_calls += 1
                self._release.set()
                return True

            async def reset(self) -> None:
                self.reset_calls += 1

            async def close(self) -> None:
                return None

        conversation = _Conversation()
        backend = AntigravitySdkWorkerBackend()
        backend._conversation = conversation
        task = asyncio.create_task(backend.run("hello", run_id="r1"))
        await conversation.started.wait()
        await backend.interrupt("r1")
        _residual, outcome = await task
        self.assertEqual(conversation.interrupt_calls, 1)
        self.assertEqual(outcome.outcome, "completed")
        self.assertIsNone(outcome.code)
        self.assertEqual(conversation.reset_calls, 0)
        self.assertIs(backend._conversation, conversation)

    async def test_host_cancelled_error_is_not_provider_interrupt(self) -> None:
        from agent_collab.backends.antigravity_sdk.worker import AntigravitySdkWorkerBackend

        class _Conversation:
            def __init__(self) -> None:
                self.reset_calls = 0
                self.started = asyncio.Event()

            async def run(self, prompt: str):
                del prompt
                self.started.set()
                raise asyncio.CancelledError()

            async def reset(self) -> None:
                self.reset_calls += 1

            async def close(self) -> None:
                return None

        conversation = _Conversation()
        backend = AntigravitySdkWorkerBackend()
        backend._conversation = conversation
        with self.assertRaises(asyncio.CancelledError):
            await backend.run("hello", run_id="r1")
        self.assertEqual(conversation.reset_calls, 0)
        self.assertIs(backend._conversation, conversation)


class AntigravitySdkPlanCleanupTests(unittest.TestCase):
    def test_multi_agent_failure_cleans_earlier_private_roots(self) -> None:
        """Agent A creates private roots; agent B fails later — A's roots must go."""
        from agent_collab.sandbox.plan import SandboxOperatorConfig, resolve_session_plan
        from agent_collab.sandbox.policy import resolve_sandbox_policy
        from agent_collab.sandbox.specs import (
            BackendSandboxSpec,
            CompatibilityCheck,
            EnvironmentSpec,
            NativeSandboxProfile,
            SandboxFailure,
            SandboxPolicy,
            SandboxSupport,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            state_base = root / "runtime" / "antigravity-sdk"
            state_base.mkdir(parents=True, mode=0o700)
            before = {p for p in state_base.iterdir()} if state_base.exists() else set()

            good = AntigravitySdkSandboxAdapter()

            # Second agent fails after first has already created private roots.
            class _FailingAdapter:
                def describe(self, context):
                    del context
                    return BackendSandboxSpec(
                        support=SandboxSupport.SDK_WORKER,
                        policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
                        state_roots=(),
                        provider_visible_paths=(),
                        environment=EnvironmentSpec(set_values={}),
                        native_profile=NativeSandboxProfile(summary={}, sdk_options={}),
                        compatibility=(
                            CompatibilityCheck(
                                "always_fail",
                                lambda: (_ for _ in ()).throw(
                                    SandboxFailure(
                                        "outer_sandbox_backend_incompatible",
                                        "forced second-agent failure",
                                    )
                                ),
                            ),
                        ),
                        external_services=(),
                    )

                def prepare_inner(self, plan, command):
                    del plan
                    return tuple(command)

            ac_home = root / "home-ac"
            ac_home.mkdir(mode=0o700)
            with (
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox._select_session_state_base",
                    return_value=state_base,
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.package_version",
                    return_value="7.35.0",
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.platform.libc_ver",
                    return_value=("glibc", "2.35"),
                ),
                mock.patch(
                    "agent_collab.sandbox.plan.resolve_scratch_anchor",
                    return_value=root / "scratch",
                ),
            ):
                with self.assertRaises(SandboxFailure):
                    resolve_session_plan(
                        policy=resolve_sandbox_policy("read-only", "none"),
                        workspace_path=workspace,
                        agents={
                            "a": (
                                None,
                                {"HOME": str(root / "home"), "AGENT_COLLAB_HOME": str(ac_home)},
                                good,
                            ),
                            "b": (None, {}, _FailingAdapter()),
                        },
                        operator=SandboxOperatorConfig(
                            scratch_root=root / "scratch",
                            agent_collab_home=ac_home,
                        ),
                        audit=False,
                        session_id="sess-multi",
                    )
            after = {p for p in state_base.iterdir()} if state_base.exists() else set()
            self.assertEqual(after, before)
            self.assertFalse((ac_home / "trajectories" / "sess-multi").exists())

    def test_in_agent_failure_removes_empty_shared_parent(self) -> None:
        """Rollback must delete children and empty random token parent."""
        from agent_collab.sandbox.plan import (
            SandboxOperatorConfig,
            remove_created_session_private_roots,
            resolve_session_plan,
        )
        from agent_collab.sandbox.policy import resolve_sandbox_policy
        from agent_collab.sandbox.specs import SandboxFailure

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            state_base = root / "runtime" / "antigravity-sdk"
            state_base.mkdir(parents=True, mode=0o700)
            parent = state_base / "tokentoken"
            child_a = parent / "trajectory"
            child_b = parent / "app-data"
            parent.mkdir(mode=0o700)
            child_a.mkdir(mode=0o700)
            child_b.mkdir(mode=0o700)
            remove_created_session_private_roots((child_a, child_b))
            self.assertFalse(child_a.exists())
            self.assertFalse(child_b.exists())
            self.assertFalse(parent.exists())

            # Provider-visible path rejection after private roots are created:
            # use a symlink ADC parent so visible-path resolve fails after creates.
            adapter = AntigravitySdkSandboxAdapter()
            home = root / "home"
            home.mkdir(mode=0o700)
            real_adc_dir = root / "adc-real"
            real_adc_dir.mkdir(mode=0o700)
            adc = real_adc_dir / "adc.json"
            adc.write_text("{}\n", encoding="utf-8")
            link_dir = root / "adc-link"
            link_dir.symlink_to(real_adc_dir, target_is_directory=True)
            (root / "home-ac").mkdir(mode=0o700)
            before = set(state_base.iterdir())
            with (
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox._select_session_state_base",
                    return_value=state_base,
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.package_version",
                    return_value="7.35.0",
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.platform.libc_ver",
                    return_value=("glibc", "2.35"),
                ),
                mock.patch(
                    "agent_collab.sandbox.plan.resolve_scratch_anchor",
                    return_value=root / "scratch",
                ),
            ):
                with self.assertRaises(SandboxFailure):
                    resolve_session_plan(
                        policy=resolve_sandbox_policy("read-only", "none"),
                        workspace_path=workspace,
                        agents={
                            "ag": (
                                None,
                                {
                                    "HOME": str(home),
                                    "AGENT_COLLAB_HOME": str(root / "home-ac"),
                                    "GOOGLE_APPLICATION_CREDENTIALS": str(link_dir / "adc.json"),
                                },
                                adapter,
                            )
                        },
                        operator=SandboxOperatorConfig(
                            scratch_root=root / "scratch",
                            agent_collab_home=root / "home-ac",
                        ),
                        audit=False,
                        session_id="sess-adc-fail",
                    )
            after = set(state_base.iterdir())
            self.assertEqual(after, before)


class AntigravitySdkRunnerWorkerPathTests(unittest.IsolatedAsyncioTestCase):
    """Hermetic coverage for the outer-sandbox worker launch path (no bwrap)."""

    @staticmethod
    def _unused_conversation_factory(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("in-process conversation factory must not run on worker path")

    async def test_failed_turn_without_id_soft_drops_and_blocks_resume(self) -> None:
        from agent_collab.backends.antigravity_sdk.backend import AntigravitySdkRunner

        agent = AgentConfig(id="ag", type="antigravity", backend="sdk")
        runner = AntigravitySdkRunner(
            agent,
            False,
            {},
            conversation_factory=self._unused_conversation_factory,
        )
        plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, scratch: prompt,
            created_session_private_roots=(),
            cleanup_created_session_private_roots=lambda: None,
        )
        runner.sandbox_plan = plan

        class _FakeSession:
            def __init__(self) -> None:
                self.killed = 0
                self.waited = 0
                self._scratch = None
                self.terminal = False

            async def run(self, prompt, emit=None):
                del prompt, emit
                return [], TurnOutcome("failed", "provider_empty_response")

            def kill(self) -> None:
                self.killed += 1

            async def wait(self) -> None:
                self.waited += 1

        fake = _FakeSession()

        async def _worker_for(workdir):
            del workdir
            runner._worker_session = fake
            return fake

        runner._worker_for = _worker_for  # type: ignore[method-assign]
        events = []

        async def emit(event) -> None:
            events.append(event)

        first = await runner.run_turn("one", Path("/workspace"), emit)
        self.assertEqual(first.outcome, "failed")
        self.assertIsNone(runner._worker_session)
        self.assertTrue(runner._worker_resume_blocked)
        self.assertEqual(fake.killed, 1)

        second = await runner.run_turn("two", Path("/workspace"), emit)
        self.assertEqual(second.outcome, "failed")
        self.assertEqual(second.code, "provider_transport_failed")
        self.assertFalse(runner._worker_resume_blocked)

    async def test_cancel_terminates_worker_and_marks_terminal(self) -> None:
        import asyncio

        from agent_collab.backends.antigravity_sdk.backend import AntigravitySdkRunner

        agent = AgentConfig(id="ag", type="antigravity", backend="sdk")
        runner = AntigravitySdkRunner(
            agent,
            False,
            {},
            conversation_factory=self._unused_conversation_factory,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, scratch: prompt,
            created_session_private_roots=(),
            cleanup_created_session_private_roots=lambda: None,
        )

        class _FakeSession:
            def __init__(self) -> None:
                self.cancelled = 0
                self.killed = 0
                self._scratch = None
                self.terminal = False

            async def run(self, prompt, emit=None):
                del prompt, emit
                raise asyncio.CancelledError()

            def kill(self) -> None:
                self.killed += 1

            async def cancel_active(self) -> None:
                self.cancelled += 1

            async def wait(self) -> None:
                return None

        fake = _FakeSession()

        async def _worker_for(workdir):
            del workdir
            runner._worker_session = fake
            return fake

        runner._worker_for = _worker_for  # type: ignore[method-assign]

        async def emit(_event) -> None:
            return None

        with self.assertRaises(asyncio.CancelledError):
            await runner.run_turn("cancel-me", Path("/workspace"), emit)
        self.assertTrue(runner._worker_terminal)
        self.assertIsNone(runner._worker_session)

    async def test_cancel_during_soft_drop_preserves_state_and_resume_block(self) -> None:
        import asyncio

        from agent_collab.backends.antigravity_sdk.backend import AntigravitySdkRunner

        cleanups = 0

        def cleanup() -> None:
            nonlocal cleanups
            cleanups += 1

        runner = AntigravitySdkRunner(
            AgentConfig(id="ag", type="antigravity", backend="sdk"),
            False,
            {},
            conversation_factory=self._unused_conversation_factory,
        )
        runner.sandbox_plan = SimpleNamespace(
            policy=SimpleNamespace(effective=SandboxPolicy.READ_ONLY),
            render_prompt=lambda prompt, _scratch: prompt,
            created_session_private_roots=(),
            cleanup_created_session_private_roots=cleanup,
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
        self.assertTrue(runner._worker_resume_blocked)
        self.assertEqual(cleanups, 0)


class AntigravitySdkDurableRootCleanupTests(unittest.TestCase):
    def test_session_private_cleanup_leaves_host_trajectory(self) -> None:
        from agent_collab.sandbox.plan import SandboxOperatorConfig, resolve_session_plan
        from agent_collab.sandbox.policy import resolve_sandbox_policy

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            ac_home = root / "ac-home"
            ac_home.mkdir(mode=0o700)
            state_base = root / "runtime" / "antigravity-sdk"
            state_base.mkdir(parents=True, mode=0o700)
            with (
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox._select_session_state_base",
                    return_value=state_base,
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.package_version",
                    return_value="7.35.0",
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.platform.libc_ver",
                    return_value=("glibc", "2.35"),
                ),
                mock.patch(
                    "agent_collab.sandbox.plan.resolve_scratch_anchor",
                    return_value=root / "scratch",
                ),
            ):
                plan = resolve_session_plan(
                    policy=resolve_sandbox_policy("read-only", "none"),
                    workspace_path=workspace,
                    agents={
                        "ag": (
                            None,
                            {"HOME": str(root / "home"), "AGENT_COLLAB_HOME": str(ac_home)},
                            AntigravitySdkSandboxAdapter(),
                        )
                    },
                    operator=SandboxOperatorConfig(
                        scratch_root=root / "scratch",
                        agent_collab_home=ac_home,
                    ),
                    audit=False,
                    session_id="sess-keep",
                )
            trajectory = ac_home / "trajectories" / "sess-keep"
            self.assertTrue(trajectory.is_dir())
            plan.cleanup_created_session_private_roots()
            self.assertTrue(trajectory.is_dir())
            self.assertFalse((state_base / "sess-keep").exists())

    def test_resolve_without_session_id_does_not_mkdir_host_trajectory(self) -> None:
        from agent_collab.sandbox.plan import SandboxOperatorConfig, resolve_session_plan
        from agent_collab.sandbox.policy import resolve_sandbox_policy

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            ac_home = root / "ac-home"
            ac_home.mkdir(mode=0o700)
            state_base = root / "runtime" / "antigravity-sdk"
            state_base.mkdir(parents=True, mode=0o700)
            with (
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox._select_session_state_base",
                    return_value=state_base,
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.package_version",
                    return_value="7.35.0",
                ),
                mock.patch(
                    "agent_collab.backends.antigravity_sdk.sandbox.platform.libc_ver",
                    return_value=("glibc", "2.35"),
                ),
                mock.patch(
                    "agent_collab.sandbox.plan.resolve_scratch_anchor",
                    return_value=root / "scratch",
                ),
            ):
                with self.assertRaises(SandboxFailure) as raised:
                    resolve_session_plan(
                        policy=resolve_sandbox_policy("read-only", "none"),
                        workspace_path=workspace,
                        agents={
                            "ag": (
                                None,
                                {"HOME": str(root / "home"), "AGENT_COLLAB_HOME": str(ac_home)},
                                AntigravitySdkSandboxAdapter(),
                            )
                        },
                        operator=SandboxOperatorConfig(
                            scratch_root=root / "scratch",
                            agent_collab_home=ac_home,
                        ),
                        audit=False,
                    )
            self.assertIn("session_id", str(raised.exception))
            self.assertFalse((ac_home / "trajectories").exists())
            self.assertFalse((state_base / "session-private").exists())

    def test_runner_close_does_not_delete_host_trajectory(self) -> None:
        from agent_collab.backends.antigravity_sdk.backend import AntigravitySdkRunner

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            trajectory = root / "trajectories" / "sess-keep"
            state_root = root / "runtime" / "antigravity-sdk" / "sess-keep"
            app_data = state_root / "app-data"
            trajectory.mkdir(parents=True, mode=0o700)
            app_data.mkdir(parents=True, mode=0o700)
            (trajectory / "marker").write_text("keep\n", encoding="utf-8")
            runner = AntigravitySdkRunner(
                AgentConfig(id="ag", type="antigravity", backend="sdk"),
                False,
                {},
                conversation_factory=AntigravitySdkRunnerWorkerPathTests._unused_conversation_factory,
            )
            runner.sandbox_plan = SimpleNamespace(
                spec=SimpleNamespace(
                    environment=SimpleNamespace(
                        set_values={
                            "ANTIGRAVITY_SAVE_DIR": str(trajectory),
                            "ANTIGRAVITY_APP_DATA_DIR": str(app_data),
                            "ANTIGRAVITY_STATE_ROOT": str(state_root),
                        }
                    )
                )
            )
            runner._cleanup_session_state()
            self.assertTrue(trajectory.is_dir())
            self.assertTrue((trajectory / "marker").is_file())
            self.assertFalse(state_root.exists())


if __name__ == "__main__":
    unittest.main()
