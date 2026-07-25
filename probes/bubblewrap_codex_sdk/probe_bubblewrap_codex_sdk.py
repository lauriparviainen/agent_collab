#!/usr/bin/env python3
"""Probe Codex SDK execution ownership and Bubblewrap feasibility."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


ACTION_SOURCE = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def try_write(path: str, text: str) -> bool:
    try:
        Path(path).write_text(text + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def checks() -> dict[str, object]:
    workspace = Path(os.environ["PROBE_WORKSPACE"])
    current_ns = os.stat("/proc/self/ns/mnt").st_ino
    return {
        "cwd_match": Path.cwd().resolve() == workspace.resolve(),
        "workspace_read": (workspace / "probe-input.txt").read_text(
            encoding="utf-8"
        ).strip()
        == "bubblewrap codex sdk probe",
        "workspace_write": try_write(
            str(workspace / "workspace-write-marker"), "workspace"
        ),
        "protected_host_write": try_write(
            str(Path(os.environ["PROBE_PROTECTED_HOST"]) / "protected-write-marker"),
            "protected",
        ),
        "home_write": try_write(os.environ["PROBE_HOME_MARKER"], "home"),
        "provider_state_write": try_write(
            os.environ["PROBE_PROVIDER_STATE_MARKER"], "provider-state"
        ),
        "scratch_write": try_write(
            str(Path(os.environ["TMPDIR"]) / "scratch-write-marker"), "scratch"
        ),
        "same_namespace_as_worker": current_ns
        == int(os.environ["PROBE_WORKER_MNT_NS"]),
        "different_namespace_from_host": current_ns
        != int(os.environ["PROBE_HOST_MNT_NS"]),
    }


def main() -> int:
    os.environ.setdefault(
        "PROBE_WORKER_MNT_NS", str(os.stat("/proc/self/ns/mnt").st_ino)
    )
    if len(sys.argv) == 2 and sys.argv[1] == "--child":
        Path(os.environ["PROBE_CHILD_RESULTS"]).write_text(
            json.dumps(checks(), sort_keys=True), encoding="utf-8"
        )
        return 0

    invoked = Path(os.environ["PROBE_ACTION_INVOKED"])
    duplicate = Path(os.environ["PROBE_ACTION_DUPLICATE"])
    if invoked.exists():
        duplicate.touch()
        return 71
    invoked.touch()

    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child"],
        cwd=Path.cwd(),
        check=False,
        timeout=30,
    )
    child_results_path = Path(os.environ["PROBE_CHILD_RESULTS"])
    child_results = (
        json.loads(child_results_path.read_text(encoding="utf-8"))
        if child.returncode == 0 and child_results_path.is_file()
        else {}
    )
    Path(os.environ["PROBE_ACTION_RESULTS"]).write_text(
        json.dumps(
            {
                "direct": checks(),
                "child": child_results,
                "child_returncode": child.returncode,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0 if child.returncode == 0 else 72


if __name__ == "__main__":
    raise SystemExit(main())
"""


RUNTIME_WRAPPER_SOURCE = r"""#!/usr/bin/python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


current = os.stat("/proc/self/ns/mnt").st_ino
Path(os.environ["PROBE_RUNTIME_EVIDENCE"]).write_text(
    json.dumps(
        {
            "pid": os.getpid(),
            "same_namespace_as_worker": current
            == int(os.environ["PROBE_WORKER_MNT_NS"]),
            "different_namespace_from_host": current
            != int(os.environ["PROBE_HOST_MNT_NS"]),
            "cwd_matches_workspace": Path.cwd().resolve()
            == Path(os.environ["PROBE_WORKSPACE"]).resolve(),
            "environment_matches_worker": (
                Path(os.environ["TMPDIR"]).resolve()
                == Path(os.environ["PROBE_RUNTIME_EVIDENCE"]).resolve().parent
                and Path(os.environ["HOME"]).resolve()
                == Path(os.environ["PROBE_HOME_MARKER"]).resolve().parent
                and Path(os.environ["CODEX_HOME"]).resolve()
                == Path(os.environ["PROBE_PROVIDER_STATE_MARKER"]).resolve().parent
            ),
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
runtime = os.environ["PROBE_REAL_RUNTIME"]
os.execv(runtime, [runtime, *sys.argv[1:]])
"""


HOME_MARKER_NAME = ".agent-collab-bwrap-codex-sdk-home-marker"
STATE_MARKER_NAME = ".agent-collab-bwrap-codex-sdk-state-marker"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="inspect production mapping and run filesystem controls without a model call",
    )
    parser.add_argument(
        "--execution-mode",
        choices=("wrapped-worker", "current-architecture"),
        default="wrapped-worker",
        help="wrap the whole SDK worker or exercise the current unsandboxed runner",
    )
    parser.add_argument(
        "--state-mode",
        choices=("writable", "read-only"),
        default="writable",
        help="mount the complete CODEX_HOME writable or leave it read-only",
    )
    parser.add_argument(
        "--sdk-python",
        default=os.environ.get("AGENT_COLLAB_CODEX_SDK_PYTHON", sys.executable),
        help="Python interpreter containing openai-codex",
    )
    parser.add_argument(
        "--codex-runtime",
        default=os.environ.get("AGENT_COLLAB_CODEX_SDK_RUNTIME", "codex"),
        help="Codex app-server runtime (default: codex on PATH)",
    )
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--bwrap", default="bwrap")
    parser.add_argument(
        "--source-codex-home",
        type=Path,
        help="Codex state root (default: $CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--_worker",
        choices=("structural", "live"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--_fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_host-mnt-ns", help=argparse.SUPPRESS)
    return parser.parse_args()


def require_command(value: str, label: str, *, resolve_symlinks: bool = True) -> str:
    resolved = shutil.which(value)
    if resolved is None and Path(value).is_file():
        resolved = value
    if resolved is None:
        raise RuntimeError(f"{label} command not found: {value}")
    path = Path(resolved).absolute()
    return str(path.resolve() if resolve_symlinks else path)


def make_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def create_fixture(root: Path) -> dict[str, Path]:
    paths = {
        "workspace": root / "workspace with spaces",
        "protected_host": root / "protected host",
        "sandbox_home": root / "sandbox home",
        "private_provider_state": root / "sandbox home" / ".codex",
        "scratch": root / "scratch",
    }
    for path in paths.values():
        make_directory(path)
    (paths["workspace"] / "probe-input.txt").write_text(
        "bubblewrap codex sdk probe\n", encoding="utf-8"
    )
    action = paths["workspace"] / "probe_actions.py"
    action.write_text(ACTION_SOURCE, encoding="utf-8")
    action.chmod(0o755)
    wrapper = paths["scratch"] / "codex-runtime-wrapper"
    wrapper.write_text(RUNTIME_WRAPPER_SOURCE, encoding="utf-8")
    wrapper.chmod(0o755)
    paths["runtime_wrapper"] = wrapper
    return paths


def package_version(sdk_python: str, distribution: str) -> str:
    script = f"import importlib.metadata as m; print(m.version({distribution!r}))"
    completed = subprocess.run(
        [sdk_python, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def runtime_version(runtime: str) -> str:
    try:
        completed = subprocess.run(
            [runtime, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    first = (completed.stdout or completed.stderr).strip().splitlines()
    return first[0] if completed.returncode == 0 and first else "unavailable"


def require_codex_auth(provider_state: Path) -> None:
    credential_file = provider_state / "auth.json"
    credential_env = any(
        os.environ.get(name) for name in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY")
    )
    if not credential_file.is_file() and not credential_env:
        raise RuntimeError(
            "Codex credentials are unavailable in the selected state root or environment"
        )


def probe_environment(
    paths: dict[str, Path],
    *,
    host_mnt_ns: int,
    effective_home: Path,
    provider_state: Path,
    runtime: str,
) -> dict[str, str]:
    scratch = paths["scratch"]
    return {
        "HOME": str(effective_home),
        "CODEX_HOME": str(provider_state),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
        "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
        "XDG_DATA_HOME": str(scratch / "xdg-data"),
        "XDG_STATE_HOME": str(scratch / "xdg-state"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                (
                    str(Path(__file__).resolve().parents[2]),
                    os.environ.get("PYTHONPATH"),
                ),
            )
        ),
        "PROBE_WORKSPACE": str(paths["workspace"]),
        "PROBE_PROTECTED_HOST": str(paths["protected_host"]),
        "PROBE_HOME_MARKER": str(effective_home / HOME_MARKER_NAME),
        "PROBE_PROVIDER_STATE_MARKER": str(provider_state / STATE_MARKER_NAME),
        "PROBE_ACTION_RESULTS": str(scratch / "action-results.json"),
        "PROBE_CHILD_RESULTS": str(scratch / "child-results.json"),
        "PROBE_ACTION_INVOKED": str(scratch / "action-invoked"),
        "PROBE_ACTION_DUPLICATE": str(scratch / "action-duplicate"),
        "PROBE_RUNTIME_EVIDENCE": str(scratch / "runtime-evidence.json"),
        "PROBE_STRUCTURAL_EVIDENCE": str(scratch / "structural-evidence.json"),
        "PROBE_LIVE_EVIDENCE": str(scratch / "live-evidence.json"),
        "PROBE_HOST_MNT_NS": str(host_mnt_ns),
        "PROBE_REAL_RUNTIME": runtime,
        "PROBE_RUNTIME_WRAPPER": str(paths["runtime_wrapper"]),
    }


def bubblewrap_prefix(
    bwrap: str,
    paths: dict[str, Path],
    env: dict[str, str],
    provider_state: Path,
    state_mode: str,
) -> list[str]:
    prefix = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        str(paths["scratch"]),
        str(paths["scratch"]),
    ]
    if state_mode == "writable":
        prefix.extend(["--bind", str(provider_state), str(provider_state)])
    prefix.extend(
        [
            "--ro-bind",
            str(paths["protected_host"]),
            str(paths["protected_host"]),
            "--ro-bind",
            str(paths["workspace"]),
            str(paths["workspace"]),
        ]
    )
    for key, value in env.items():
        prefix.extend(["--setenv", key, value])
    prefix.extend(["--chdir", str(paths["workspace"]), "--"])
    return prefix


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def boundary_ok(
    result: dict[str, Any],
    *,
    wrapped: bool,
    state_mode: str,
) -> bool:
    expected_state = state_mode == "writable" or not wrapped
    return (
        result.get("cwd_match") is True
        and result.get("workspace_read") is True
        and result.get("workspace_write") is (not wrapped)
        and result.get("protected_host_write") is (not wrapped)
        and result.get("home_write") is (not wrapped)
        and result.get("provider_state_write") is expected_state
        and result.get("scratch_write") is True
        and result.get("same_namespace_as_worker") is True
        and result.get("different_namespace_from_host") is wrapped
    )


def verify_action(
    paths: dict[str, Path],
    *,
    home_marker: Path,
    provider_state_marker: Path,
    wrapped: bool,
    state_mode: str,
) -> bool:
    payload = load_json(paths["scratch"] / "action-results.json")
    direct = payload.get("direct") if isinstance(payload.get("direct"), dict) else {}
    child = payload.get("child") if isinstance(payload.get("child"), dict) else {}
    expected_state = state_mode == "writable" or not wrapped
    checks = [
        (
            "action script ran exactly once",
            (paths["scratch"] / "action-invoked").is_file()
            and not (paths["scratch"] / "action-duplicate").exists(),
        ),
        (
            "direct action boundary",
            boundary_ok(direct, wrapped=wrapped, state_mode=state_mode),
        ),
        (
            "child inherited boundary",
            payload.get("child_returncode") == 0
            and boundary_ok(child, wrapped=wrapped, state_mode=state_mode),
        ),
        (
            f"workspace write {'reached host' if not wrapped else 'blocked'}",
            (paths["workspace"] / "workspace-write-marker").is_file() is (not wrapped),
        ),
        (
            f"protected-host write {'reached host' if not wrapped else 'blocked'}",
            (paths["protected_host"] / "protected-write-marker").is_file() is (not wrapped),
        ),
        (
            f"general-home write {'reached host' if not wrapped else 'blocked'}",
            home_marker.is_file() is (not wrapped),
        ),
        (
            f"provider-state write {'allowed' if expected_state else 'blocked'}",
            provider_state_marker.is_file() is expected_state,
        ),
        ("scratch write allowed", (paths["scratch"] / "scratch-write-marker").is_file()),
    ]
    width = max(len(label) for label, _ in checks)
    print("\nHost verification")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def verify_runtime(paths: dict[str, Path], *, wrapped: bool) -> bool:
    evidence = load_json(paths["scratch"] / "runtime-evidence.json")
    checks = [
        ("app-server inherited worker namespace", evidence.get("same_namespace_as_worker") is True),
        ("app-server inherited workspace cwd", evidence.get("cwd_matches_workspace") is True),
        (
            "app-server inherited worker environment",
            evidence.get("environment_matches_worker") is True,
        ),
        (
            "app-server namespace relationship",
            evidence.get("different_namespace_from_host") is wrapped,
        ),
    ]
    width = max(len(label) for label, _ in checks)
    print("\nRuntime ownership")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def verify_structural(paths: dict[str, Path]) -> bool:
    evidence = load_json(paths["scratch"] / "structural-evidence.json")
    checks = [
        ("production cwd mapped exactly", evidence.get("cwd_matches_workspace") is True),
        ("configured app-server runtime used", evidence.get("configured_runtime") is True),
        ("native full-access sandbox mapped", evidence.get("sandbox") == "full_access"),
        ("reasoning effort mapped", evidence.get("effort") == "low"),
        (
            "no per-thread config supplied",
            evidence.get("thread_keys") == ["cwd", "model", "sandbox"],
        ),
        ("SDK default is auto-review", evidence.get("approval_default") == "auto_review"),
        ("no caller-side tool executor exists", evidence.get("approval_handler_only") is True),
    ]
    width = max(len(label) for label, _ in checks)
    print("\nProduction request structure")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def report_failure_stage(paths: dict[str, Path]) -> None:
    evidence = load_json(paths["scratch"] / "live-evidence.json")
    print("\nSanitized failure stage")
    print(
        "  app-server started before failure   "
        + ("PASS" if (paths["scratch"] / "runtime-evidence.json").is_file() else "FAIL")
    )
    print(
        "  failed before shell dispatch         "
        + ("PASS" if evidence.get("saw_command") is False else "FAIL")
    )
    categories = evidence.get("error_categories")
    if isinstance(categories, list) and categories:
        print(f"  provider error categories            {', '.join(categories)}")


def wait_for_recorded_runtime_exit(paths: dict[str, Path]) -> bool:
    evidence = load_json(paths["scratch"] / "runtime-evidence.json")
    pid = evidence.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(0.05)
    return not Path(f"/proc/{pid}").exists()


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> int:
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def fixture_paths(root: Path) -> dict[str, Path]:
    return {
        "workspace": root / "workspace with spaces",
        "protected_host": root / "protected host",
        "sandbox_home": root / "sandbox home",
        "private_provider_state": root / "sandbox home" / ".codex",
        "scratch": root / "scratch",
        "runtime_wrapper": root / "scratch" / "codex-runtime-wrapper",
    }


async def structural_worker(args: argparse.Namespace, paths: dict[str, Path]) -> int:
    import openai_codex

    from agent_collab.backends.codex_sdk.backend import _default_conversation
    from agent_collab.config import AgentConfig

    agent = AgentConfig(
        id="codex_sdk_probe",
        type="codex",
        backend="sdk",
        command=os.environ["PROBE_RUNTIME_WRAPPER"],
    )
    conversation = _default_conversation(
        agent,
        {
            "model": args.model,
            "sandbox": "danger-full-access",
            "thinking_level": "low",
        },
        paths["workspace"],
    )
    client_config = conversation._client_config
    thread_kwargs = conversation._thread_kwargs
    run_kwargs = conversation._run_kwargs
    signature = inspect.signature(openai_codex.AsyncCodex.thread_start)
    approval = signature.parameters["approval_mode"].default
    evidence = {
        "cwd_matches_workspace": Path(thread_kwargs["cwd"]).resolve()
        == paths["workspace"].resolve(),
        "configured_runtime": client_config.codex_bin == os.environ["PROBE_RUNTIME_WRAPPER"],
        "sandbox": getattr(thread_kwargs["sandbox"], "name", None),
        "effort": getattr(run_kwargs["effort"], "name", None),
        "thread_keys": sorted(thread_kwargs),
        "approval_default": getattr(approval, "value", None),
        "approval_handler_only": not any(
            "tool" in parameter.lower() or "callback" in parameter.lower()
            for parameter in signature.parameters
        ),
    }
    Path(os.environ["PROBE_STRUCTURAL_EVIDENCE"]).write_text(
        json.dumps(evidence, sort_keys=True), encoding="utf-8"
    )
    os.environ["PROBE_WORKER_MNT_NS"] = str(os.stat("/proc/self/ns/mnt").st_ino)
    completed = subprocess.run(
        [sys.executable, str(paths["workspace"] / "probe_actions.py")],
        cwd=paths["workspace"],
        check=False,
        timeout=30,
    )
    return completed.returncode


async def live_worker(args: argparse.Namespace, paths: dict[str, Path]) -> int:
    from agent_collab.backends.codex_sdk import CodexSdkBackend
    from agent_collab.config import AgentConfig

    os.environ["PROBE_WORKER_MNT_NS"] = str(os.stat("/proc/self/ns/mnt").st_ino)
    agent = AgentConfig(
        id="codex_sdk_probe",
        type="codex",
        backend="sdk",
        command=os.environ["PROBE_RUNTIME_WRAPPER"],
    )
    backend = CodexSdkBackend()
    runner = backend.create_runner(
        agent,
        False,
        {
            "model": args.model,
            "sandbox": "danger-full-access",
            "thinking_level": "low",
        },
    )
    events = []

    async def emit(event):
        events.append(event)

    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(paths['workspace'] / 'probe_actions.py'))}"
    )
    prompt = (
        "For this controlled enforcement probe, use the shell tool to execute "
        f"exactly this command once: {command}. Do not run another command and "
        "do not simulate it. Then reply with done."
    )
    try:
        outcome = await runner.run_turn(prompt, paths["workspace"], emit)
    except Exception:
        outcome = None
    finally:
        try:
            await runner.close()
        except Exception:
            pass
    saw_command = any(event.type == "command" for event in events)
    error_text = "\n".join(event.text.lower() for event in events if event.type == "error")
    categories = [
        label
        for label, token in (
            ("read-only-filesystem", "read-only file system"),
            ("erofs", "erofs"),
            ("sqlite-read-only", "readonly database"),
            ("sqlite-code-8", "error code 8"),
            ("permission-denied", "permission denied"),
        )
        if token in error_text
    ]
    Path(os.environ["PROBE_LIVE_EVIDENCE"]).write_text(
        json.dumps(
            {
                "completed": outcome is not None and outcome.outcome == "completed",
                "saw_command": saw_command,
                "error_categories": categories,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0 if outcome is not None and outcome.outcome == "completed" and saw_command else 1


def worker_main(args: argparse.Namespace) -> int:
    if args._fixture is None or args._host_mnt_ns is None:
        return 2
    paths = fixture_paths(args._fixture.resolve())
    os.environ["PROBE_HOST_MNT_NS"] = args._host_mnt_ns
    try:
        if args._worker == "structural":
            return asyncio.run(structural_worker(args, paths))
        return asyncio.run(live_worker(args, paths))
    except Exception as exc:
        print(f"Error: SDK worker failed ({type(exc).__name__})", file=sys.stderr)
        return 1


def main() -> int:
    args = parse_args()
    if args._worker:
        return worker_main(args)
    if args.timeout <= 0:
        print("Error: --timeout must be positive", file=sys.stderr)
        return 2
    if args.preflight_only and args.execution_mode != "wrapped-worker":
        print(
            "Error: --preflight-only applies only to --execution-mode wrapped-worker",
            file=sys.stderr,
        )
        return 2

    try:
        sdk_python = require_command(args.sdk_python, "SDK Python", resolve_symlinks=False)
        runtime = require_command(args.codex_runtime, "Codex runtime")
        bwrap = (
            require_command(args.bwrap, "Bubblewrap")
            if args.execution_mode == "wrapped-worker"
            else None
        )
        sdk_version = package_version(sdk_python, "openai-codex")
        pinned_version = package_version(sdk_python, "openai-codex-cli-bin")
        selected_runtime_version = runtime_version(runtime)
        host_mnt_ns = os.stat("/proc/self/ns/mnt").st_ino
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="agent collab bwrap codex sdk probe ") as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            paths = create_fixture(root)
            if args.preflight_only:
                effective_home = paths["sandbox_home"]
                provider_state = paths["private_provider_state"]
            else:
                effective_home = Path.home().resolve()
                provider_state = (
                    (
                        args.source_codex_home
                        or Path(
                            os.environ.get(
                                "CODEX_HOME",
                                str(effective_home / ".codex"),
                            )
                        )
                    )
                    .expanduser()
                    .resolve()
                )
                if not provider_state.is_dir():
                    raise RuntimeError("selected CODEX_HOME is unavailable")
                require_codex_auth(provider_state)

            home_marker = effective_home / HOME_MARKER_NAME
            provider_state_marker = provider_state / STATE_MARKER_NAME
            marker_paths = (home_marker, provider_state_marker)
            if any(path.exists() for path in marker_paths):
                raise RuntimeError("refusing to overwrite an existing guarded marker")

            env = probe_environment(
                paths,
                host_mnt_ns=host_mnt_ns,
                effective_home=effective_home,
                provider_state=provider_state,
                runtime=runtime,
            )
            worker_mode = "structural" if args.preflight_only else "live"
            inner = [
                sdk_python,
                str(Path(__file__).resolve()),
                "--_worker",
                worker_mode,
                "--_fixture",
                str(root),
                "--_host-mnt-ns",
                str(host_mnt_ns),
                "--model",
                args.model,
            ]
            if args.execution_mode == "wrapped-worker":
                assert bwrap is not None
                command = (
                    bubblewrap_prefix(
                        bwrap,
                        paths,
                        env,
                        provider_state,
                        args.state_mode,
                    )
                    + inner
                )
                command_env = None
                wrapped = True
            else:
                command = inner
                command_env = {**os.environ, **env}
                wrapped = False

            print("▶ Codex SDK Bubblewrap feasibility probe")
            print(f"  execution mode {args.execution_mode}")
            print(f"  worker mode    {worker_mode}")
            print(f"  SDK version    {sdk_version}")
            print(f"  pinned runtime {pinned_version}")
            print(f"  selected       {selected_runtime_version}")
            print("  workspace      private path containing spaces")
            print(f"  provider state complete root ({args.state_mode})")
            print(
                "  credentials    unused" if args.preflight_only else "  credentials    inherited"
            )
            sys.stdout.flush()

            try:
                try:
                    returncode = run_command(
                        command,
                        cwd=paths["workspace"],
                        timeout=args.timeout,
                        env=command_env,
                    )
                    action_verified = verify_action(
                        paths,
                        home_marker=home_marker,
                        provider_state_marker=provider_state_marker,
                        wrapped=wrapped,
                        state_mode=args.state_mode,
                    )
                    structural_verified = verify_structural(paths) if args.preflight_only else True
                    runtime_verified = (
                        True if args.preflight_only else verify_runtime(paths, wrapped=wrapped)
                    )
                except subprocess.TimeoutExpired:
                    runtime_reaped = wait_for_recorded_runtime_exit(paths)
                    print(
                        f"Error: probe timed out after {args.timeout}s; process group killed",
                        file=sys.stderr,
                    )
                    print(
                        "  Codex app-server descendant reaped   "
                        + ("PASS" if runtime_reaped else "FAIL"),
                        file=sys.stderr,
                    )
                    return 1
            finally:
                for path in marker_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        print(
                            "! Warning: could not remove a guarded marker",
                            file=sys.stderr,
                        )

            if returncode != 0:
                if not args.preflight_only:
                    report_failure_stage(paths)
                print(f"Error: worker exited with status {returncode}", file=sys.stderr)
                return 1
            if not action_verified or not structural_verified or not runtime_verified:
                if not args.preflight_only:
                    report_failure_stage(paths)
                print("Error: one or more host assertions failed", file=sys.stderr)
                return 1
            print("✓ Codex SDK probe expectations passed")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
