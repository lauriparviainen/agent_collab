"""Hermetic coverage for the xAI SDK no_local_effects outer-sandbox adapter."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_collab import backends
from agent_collab.backends.xai_sdk.sandbox import (
    AUDITED_CHAT_CREATE_KEYS,
    AUDITED_PACKAGE_SERIES,
    XaiSdkSandboxAdapter,
    audit_installed_package_version,
    audit_production_source,
    production_chat_kwargs,
    surface_is_audited,
)
from agent_collab.config import AgentConfig
from agent_collab.sandbox.plan import SandboxOperatorConfig, resolve_session_plan
from agent_collab.sandbox.policy import resolve_sandbox_policy
from agent_collab.sandbox.specs import (
    NoLocalEffectsSandboxAdapter,
    SandboxContext,
    SandboxEnforcement,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
    UnsupportedSandboxAdapter,
)


class XaiSdkSandboxAdapterTests(unittest.TestCase):
    def test_registry_exposes_no_local_effects_without_common_xai_branch(self) -> None:
        adapter = backends.get_backend("xai", "sdk").sandbox_adapter
        self.assertIsInstance(adapter, XaiSdkSandboxAdapter)
        for path in Path("agent_collab/sandbox").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("xai_sdk", text)
            self.assertNotIn("XaiSdk", text)

    def test_describe_declares_no_local_effects_without_state_or_native_bypass(self) -> None:
        self.assertTrue(surface_is_audited())
        adapter = XaiSdkSandboxAdapter()
        spec = adapter.describe(SandboxContext(Path("/w"), Path("/w"), {}))
        self.assertIs(spec.support, SandboxSupport.NO_LOCAL_EFFECTS)
        self.assertEqual(spec.policies, frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}))
        self.assertEqual(spec.state_roots, ())
        self.assertEqual(spec.provider_visible_paths, ())
        self.assertEqual(dict(spec.environment.set_values), {})
        self.assertEqual(spec.native_profile.command, None)
        self.assertEqual(dict(spec.native_profile.sdk_options), {})
        self.assertEqual(spec.native_profile.summary.get("shape"), "no_local_effects")
        self.assertEqual(spec.native_profile.summary.get("local_tools"), "none")
        self.assertEqual(spec.native_profile.summary.get("local_state_root"), "none")
        self.assertTrue(any("xAI remote" in item for item in spec.external_services))
        self.assertEqual(
            {item.name for item in spec.compatibility},
            {
                "audited_xai_sdk_version",
                "audited_production_surface",
                "audited_import_identity",
            },
        )

    def test_read_only_session_is_not_applicable_without_bubblewrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir(mode=0o700)
            session = resolve_session_plan(
                policy=resolve_sandbox_policy("read-only", "none"),
                workspace_path=workspace,
                agents={
                    "xai": (None, {}, XaiSdkSandboxAdapter()),
                },
                operator=SandboxOperatorConfig(agent_collab_home=Path(raw) / "home"),
            )
        self.assertEqual(session.engine, "not_applicable")
        self.assertEqual(session.establishment, "not_applicable")
        plan = session.agents["xai"]
        self.assertIs(plan.support, SandboxSupport.NO_LOCAL_EFFECTS)
        self.assertIs(plan.enforcement, SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS)
        self.assertEqual(plan.operations, ())
        self.assertIsNone(plan.scratch_anchor)
        prompt = plan.render_prompt("USER", None)
        self.assertIn("no local file", prompt)
        self.assertNotIn("$TMPDIR", prompt)
        self.assertNotIn("OS-enforced", prompt)
        self.assertTrue(prompt.endswith("\nUSER"))
        settings = plan.settings(full=True)
        self.assertEqual(settings["support"], "no_local_effects")
        self.assertEqual(settings["enforcement"], "not_applicable_no_local_effects")
        self.assertEqual(settings["writable_exceptions"], [])

    def test_all_no_local_effects_members_skip_bubblewrap_engine(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir(mode=0o700)
            session = resolve_session_plan(
                policy=resolve_sandbox_policy("read-only", "none"),
                workspace_path=workspace,
                agents={
                    "xai": (None, {}, XaiSdkSandboxAdapter()),
                    "mock": (None, {}, NoLocalEffectsSandboxAdapter()),
                },
                operator=SandboxOperatorConfig(agent_collab_home=Path(raw) / "home"),
            )
        self.assertEqual(session.engine, "not_applicable")
        self.assertEqual(session.establishment, "not_applicable")
        for plan in session.agents.values():
            self.assertIs(plan.enforcement, SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS)

    def test_mixed_session_still_requires_os_enforcement_for_other_member(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir(mode=0o700)
            # Unsupported second member fails the whole start for read-only.
            with self.assertRaises(SandboxFailure) as raised:
                resolve_session_plan(
                    policy=resolve_sandbox_policy("read-only", "none"),
                    workspace_path=workspace,
                    agents={
                        "xai": (None, {}, XaiSdkSandboxAdapter()),
                        "other": (None, {}, UnsupportedSandboxAdapter()),
                    },
                    operator=SandboxOperatorConfig(agent_collab_home=Path(raw) / "home"),
                    audit=False,
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_unsupported")

    def test_mixed_with_direct_process_reports_mixed_engine(self) -> None:
        from agent_collab.sandbox.specs import BackendSandboxSpec

        class _Direct:
            def describe(self, context: SandboxContext) -> BackendSandboxSpec:
                del context
                return BackendSandboxSpec(
                    support=SandboxSupport.DIRECT_PROCESS,
                    policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
                    state_roots=(),
                )

            def prepare_inner(self, plan, command):  # type: ignore[no-untyped-def]
                del plan
                return tuple(command)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            # Direct process without state still needs mount/git work; disable
            # alias audit and use a git-free workspace.
            session = resolve_session_plan(
                policy=resolve_sandbox_policy("read-only", "none"),
                workspace_path=workspace,
                agents={
                    "xai": (None, {}, XaiSdkSandboxAdapter()),
                    "cli": (None, {}, _Direct()),
                },
                operator=SandboxOperatorConfig(agent_collab_home=root / "home"),
                audit=False,
            )
        self.assertEqual(session.engine, "mixed")
        self.assertEqual(session.establishment, "required")
        self.assertIs(
            session.agents["xai"].enforcement,
            SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS,
        )
        self.assertIs(session.agents["cli"].enforcement, SandboxEnforcement.OS_ENFORCED)

    def test_production_chat_kwargs_are_exactly_the_audited_set(self) -> None:
        kwargs = production_chat_kwargs(
            {"model": "grok-4.5", "thinking_level": "medium"},
            previous_response_id="resp_1",
        )
        self.assertEqual(
            kwargs,
            {
                "model": "grok-4.5",
                "store_messages": True,
                "reasoning_effort": "medium",
                "previous_response_id": "resp_1",
            },
        )
        self.assertTrue(set(kwargs).issubset(AUDITED_CHAT_CREATE_KEYS))
        self.assertNotIn("tools", kwargs)

    def test_source_audit_rejects_tools_import_and_forbidden_create_keys(self) -> None:
        clean = Path("agent_collab/backends/xai_sdk/backend.py").read_text(encoding="utf-8")
        audit_production_source(clean)
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\nfrom xai_sdk import tools\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        audited_fragment = "chat.create(\n                **production_chat_kwargs("
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(
                clean.replace(
                    audited_fragment,
                    "chat.create(\n                tools=[],\n                **production_chat_kwargs(",
                )
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        # Intermediate mutable kwargs must not keep the capability.
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(
                clean.replace(
                    audited_fragment,
                    "chat_kwargs = production_chat_kwargs(self._chat_kwargs)\n"
                    "                chat_kwargs.update(tools=[])\n"
                    "                chat = self._client.chat.create(**chat_kwargs)\n"
                    "                # ",
                )
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(
                clean.replace(
                    audited_fragment,
                    "chat.create(\n                **other_kwargs)\n"
                    "            # production_chat_kwargs(\n                # ",
                )
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_version_outside_audited_series_revokes_capability(self) -> None:
        with self.assertRaises(SandboxFailure) as raised:
            audit_installed_package_version("1.18.0")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with mock.patch(
            "agent_collab.backends.xai_sdk.sandbox.installed_xai_sdk_version",
            return_value="1.18.0",
        ):
            self.assertFalse(surface_is_audited())
            spec = XaiSdkSandboxAdapter().describe(SandboxContext(Path("/w"), Path("/w"), {}))
            self.assertIs(spec.support, SandboxSupport.UNSUPPORTED)
            self.assertNotIn(SandboxPolicy.READ_ONLY, spec.policies)

    def test_import_identity_rejects_sys_path_shadow_module(self) -> None:
        from agent_collab.backends.xai_sdk.sandbox import audit_import_identity
        from types import SimpleNamespace

        dist = mock.Mock()
        dist.locate_file.side_effect = lambda rel: Path("/opt/site-packages") / rel
        shadow_spec = SimpleNamespace(
            origin=str(Path("/tmp/shadow-xai_sdk/__init__.py")),
            name="xai_sdk",
        )
        with (
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.installed_xai_sdk_version",
                return_value="1.17.0",
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.metadata.distribution",
                return_value=dist,
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.importlib_util.find_spec",
                return_value=shadow_spec,
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.compat.import_xai_sdk",
                side_effect=AssertionError("must not import shadow before reject"),
            ),
        ):
            with self.assertRaises(SandboxFailure) as raised:
                audit_import_identity()
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        self.assertIn("shadow", str(raised.exception).lower())

    def test_import_identity_revokes_on_any_import_failure_when_installed(self) -> None:
        from agent_collab.backends.xai_sdk.sandbox import audit_import_identity
        from types import SimpleNamespace

        dist = mock.Mock()
        dist.locate_file.side_effect = lambda rel: Path("/opt/site-packages") / rel
        matching_spec = SimpleNamespace(
            origin=str(Path("/opt/site-packages/xai_sdk/__init__.py")),
            name="xai_sdk",
        )
        for exc in (
            RuntimeError("shadow wrote then failed"),
            ImportError("shadow wrote then raised ImportError"),
            ModuleNotFoundError("xai_sdk.submodule missing after side effect"),
        ):
            with self.subTest(error=type(exc).__name__):
                with (
                    mock.patch(
                        "agent_collab.backends.xai_sdk.sandbox.installed_xai_sdk_version",
                        return_value="1.17.0",
                    ),
                    mock.patch(
                        "agent_collab.backends.xai_sdk.sandbox.metadata.distribution",
                        return_value=dist,
                    ),
                    mock.patch(
                        "agent_collab.backends.xai_sdk.sandbox.importlib_util.find_spec",
                        return_value=matching_spec,
                    ),
                    mock.patch(
                        "agent_collab.backends.xai_sdk.compat.import_xai_sdk",
                        side_effect=exc,
                    ),
                ):
                    with self.assertRaises(SandboxFailure) as raised:
                        audit_import_identity()
                self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
                self.assertIn("identity audit failed", str(raised.exception))

    def test_import_identity_skips_when_package_metadata_and_module_absent(self) -> None:
        from agent_collab.backends.xai_sdk.sandbox import audit_import_identity

        with (
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.installed_xai_sdk_version",
                return_value=None,
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.importlib_util.find_spec",
                return_value=None,
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.compat.import_xai_sdk",
                side_effect=AssertionError("must not import when absent"),
            ),
        ):
            audit_import_identity()

    def test_import_identity_revokes_resolvable_module_without_distribution(self) -> None:
        from agent_collab.backends.xai_sdk.sandbox import audit_import_identity
        from types import SimpleNamespace

        shadow_spec = SimpleNamespace(
            origin=str(Path("/tmp/shadow-xai_sdk/__init__.py")),
            name="xai_sdk",
        )
        with (
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.installed_xai_sdk_version",
                return_value=None,
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.sandbox.importlib_util.find_spec",
                return_value=shadow_spec,
            ),
            mock.patch(
                "agent_collab.backends.xai_sdk.compat.import_xai_sdk",
                side_effect=AssertionError("must not import shadow"),
            ),
        ):
            with self.assertRaises(SandboxFailure) as raised:
                audit_import_identity()
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        self.assertIn("without installed", str(raised.exception).lower())

    def test_metadata_errors_other_than_not_found_revoke_capability(self) -> None:
        from importlib import metadata as importlib_metadata

        from agent_collab.backends.xai_sdk.sandbox import installed_xai_sdk_version

        with mock.patch(
            "agent_collab.backends.xai_sdk.sandbox.metadata.version",
            side_effect=RuntimeError("corrupt dist-info"),
        ):
            with self.assertRaises(SandboxFailure) as raised:
                installed_xai_sdk_version()
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

        with mock.patch(
            "agent_collab.backends.xai_sdk.sandbox.metadata.version",
            side_effect=importlib_metadata.PackageNotFoundError("xai-sdk"),
        ):
            self.assertIsNone(installed_xai_sdk_version())

    def test_source_audit_rejects_create_alias_and_local_effect_markers(self) -> None:
        clean = Path("agent_collab/backends/xai_sdk/backend.py").read_text(encoding="utf-8")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\ncreate = self._client.chat.create\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\nopen('/tmp/x', 'w').write('x')\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\nfrom pathlib import Path\nPath('x').touch()\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\nimport tempfile\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\nimport sqlite3\nsqlite3.connect('/tmp/x')\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(
                clean + '\n__import__("subprocess").run(["touch", "workspace-file"])\n'
            )
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")
        with self.assertRaises(SandboxFailure) as raised:
            audit_production_source(clean + "\nfrom .local_trace import record\n")
        self.assertEqual(raised.exception.code, "outer_sandbox_backend_incompatible")

    def test_none_policy_leaves_prompt_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir(mode=0o700)
            session = resolve_session_plan(
                policy=resolve_sandbox_policy("none", "none"),
                workspace_path=workspace,
                agents={"xai": (None, {}, XaiSdkSandboxAdapter())},
                operator=SandboxOperatorConfig(agent_collab_home=Path(raw) / "home"),
            )
        plan = session.agents["xai"]
        self.assertIs(plan.enforcement, SandboxEnforcement.DISABLED)
        self.assertEqual(plan.render_prompt("USER\n", None), "USER\n")
        self.assertEqual(session.engine, "none")

    def test_runner_prepends_no_local_effects_prompt_under_read_only(self) -> None:
        from agent_collab.backends.xai_sdk.backend import XaiSdkBackend
        from agent_collab.sandbox.plan import ResolvedSandboxPlan
        from agent_collab.sandbox.specs import ResolvedSandboxPolicy, SandboxPolicySource

        class _Conversation:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def active(self) -> bool:
                return bool(self.prompts)

            async def run(self, prompt: str) -> object:
                self.prompts.append(prompt)
                return type(
                    "R",
                    (),
                    {
                        "id": "resp_1",
                        "content": "ok",
                        "finish_reason": "STOP",
                        "tool_calls": None,
                    },
                )()

            def note_session_id(self, response_id: str) -> None:
                del response_id

            async def reset(self) -> None:
                return None

            async def close(self) -> None:
                return None

        conversation = _Conversation()
        runner = XaiSdkBackend(conversation_factory=lambda *_a, **_k: conversation).create_runner(
            AgentConfig(id="xai", type="xai", backend="sdk"),
            False,
            {"model": "grok-4.5"},
        )
        adapter = XaiSdkSandboxAdapter()
        context = SandboxContext(Path("/w"), Path("/w"), {})
        runner.sandbox_plan = ResolvedSandboxPlan(
            policy=ResolvedSandboxPolicy(
                SandboxPolicy.READ_ONLY,
                SandboxPolicy.READ_ONLY,
                SandboxPolicySource.REQUEST,
            ),
            support=SandboxSupport.NO_LOCAL_EFFECTS,
            enforcement=SandboxEnforcement.NOT_APPLICABLE_NO_LOCAL_EFFECTS,
            context=context,
            spec=adapter.describe(context),
            adapter=adapter,
        )

        async def _run() -> None:
            async def emit(_event: object) -> None:
                return None

            await runner.run_turn("TASK", Path("/w"), emit)

        import asyncio

        asyncio.run(_run())
        self.assertEqual(len(conversation.prompts), 1)
        self.assertIn("no local file", conversation.prompts[0])
        self.assertIn("TASK", conversation.prompts[0])
        self.assertNotIn("$TMPDIR", conversation.prompts[0])

    def test_release_deny_probe_construction_transport_close_no_workspace_mutation(
        self,
    ) -> None:
        """Version-pinned subprocess evidence gate (not a per-start preflight)."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            home = root / "home"
            tmp = root / "tmp"
            workspace.mkdir(mode=0o700)
            home.mkdir(mode=0o700)
            tmp.mkdir(mode=0o700)
            marker = workspace / "pre-existing.txt"
            marker.write_text("keep\n", encoding="utf-8")
            before = {p.relative_to(workspace) for p in workspace.rglob("*")}

            script = r"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Isolate any accidental home/tmp writes.
os.environ["HOME"] = sys.argv[1]
os.environ["TMPDIR"] = sys.argv[2]
os.environ["XAI_API_KEY"] = "deny-probe-not-a-real-key"
workspace = Path(sys.argv[3])
os.chdir(workspace)

from agent_collab.backends.xai_sdk.sandbox import (
    AUDITED_CHAT_CREATE_KEYS,
    FORBIDDEN_CHAT_CREATE_KEYS,
    production_chat_kwargs,
)
from agent_collab.backends.xai_sdk.backend import _PersistentXaiConversation


class _Chat:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.appended = []

    def append(self, message):
        self.appended.append(message)

    async def sample(self):
        return SimpleNamespace(
            id="resp_probe",
            content="probe",
            finish_reason="STOP",
            tool_calls=None,
        )


class _ChatAPI:
    def __init__(self):
        self.creates = []
        self.deleted = []

    def create(self, **kwargs):
        self.creates.append(kwargs)
        forbidden = set(kwargs) & FORBIDDEN_CHAT_CREATE_KEYS
        if forbidden:
            raise AssertionError(f"forbidden keys: {forbidden}")
        unknown = set(kwargs) - AUDITED_CHAT_CREATE_KEYS
        if unknown:
            raise AssertionError(f"unaudited keys: {unknown}")
        return _Chat(kwargs)

    async def delete_stored_completion(self, response_id):
        self.deleted.append(response_id)


class _Client:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = _ChatAPI()
        self.closed = False

    async def close(self):
        self.closed = True


def user(text):
    return ("user", text)


async def main():
    conversation = _PersistentXaiConversation(
        _Client,
        user,
        {"model": "grok-4.5", "reasoning_effort": "low"},
        {"api_key": "deny-probe-not-a-real-key"},
    )
    response = await conversation.run("probe prompt")
    assert response.id == "resp_probe"
    await conversation.close()
    # Shared builder stays within the audited set.
    built = production_chat_kwargs(
        {"model": "grok-4.5", "reasoning_effort": "low"},
        previous_response_id="resp_probe",
    )
    assert set(built).issubset(AUDITED_CHAT_CREATE_KEYS)
    assert "tools" not in built
    # No child processes from this fixture path.
    assert os.getpid() > 0


async def real_client_construct_close():
    # Optional: when the audited package is importable, construct and close the
    # real AsyncClient without sampling (no paid model call).
    try:
        from agent_collab.backends.xai_sdk.compat import import_xai_sdk
        import_xai_sdk()
        from xai_sdk import AsyncClient
    except Exception:
        return
    client = AsyncClient(api_key="deny-probe-not-a-real-key")
    await client.close()


asyncio.run(main())
asyncio.run(real_client_construct_close())
# Fail if workspace / private home / tmp gained unexpected children.
extra = [p for p in Path(".").rglob("*") if p.name != "pre-existing.txt"]
if extra:
    raise SystemExit(f"workspace mutated: {extra}")
home_extra = [p for p in Path(sys.argv[1]).rglob("*")]
tmp_extra = [p for p in Path(sys.argv[2]).rglob("*")]
if home_extra:
    raise SystemExit(f"home mutated: {home_extra}")
if tmp_extra:
    raise SystemExit(f"tmp mutated: {tmp_extra}")
"""
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["TMPDIR"] = str(tmp)
            env["XDG_CACHE_HOME"] = str(tmp / "cache")
            env["XDG_CONFIG_HOME"] = str(tmp / "config")
            env["PYTHONPATH"] = os.pathsep.join(
                [str(Path.cwd()), env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)
            home_before = {p.relative_to(home) for p in home.rglob("*")}
            tmp_before = {p.relative_to(tmp) for p in tmp.rglob("*")}
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(home),
                    str(tmp),
                    str(workspace),
                ],
                cwd=str(Path.cwd()),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
            after = {p.relative_to(workspace) for p in workspace.rglob("*")}
            self.assertEqual(after, before)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            home_after = {p.relative_to(home) for p in home.rglob("*")}
            tmp_after = {p.relative_to(tmp) for p in tmp.rglob("*")}
            self.assertEqual(home_after, home_before)
            self.assertEqual(tmp_after, tmp_before)

    def test_describe_options_static_outer_sandbox_advertises_no_local_effects(self) -> None:
        from agent_collab.options import describe_options
        from agent_collab.config import builtin_config

        with tempfile.TemporaryDirectory() as raw:
            workdir = Path(raw)
            payload = describe_options(builtin_config(), workdir)
        outer = payload["backends"]["xai_sdk"]["static"]["outer_sandbox"]
        self.assertEqual(outer["support"], "no_local_effects")
        self.assertIn("read-only", outer["policies"])
        self.assertIn("none", outer["policies"])
        self.assertEqual(outer["provider_native_profile"].get("shape"), "no_local_effects")


class XaiSdkSandboxSeriesPinTests(unittest.TestCase):
    def test_audited_series_matches_compat_gate(self) -> None:
        from agent_collab.backends.xai_sdk import compat as compat_mod

        self.assertEqual(AUDITED_PACKAGE_SERIES, compat_mod.VERIFIED_XAI_SDK_SERIES)


if __name__ == "__main__":
    unittest.main()
