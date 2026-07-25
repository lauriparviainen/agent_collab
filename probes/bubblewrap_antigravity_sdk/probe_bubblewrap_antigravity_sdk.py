#!/usr/bin/env python3
"""Probe Antigravity SDK execution ownership and Bubblewrap feasibility."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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
    return {
        "cwd_match": Path.cwd().resolve() == workspace.resolve(),
        "workspace_read": (workspace / "probe-input.txt").read_text(
            encoding="utf-8"
        ).strip()
        == "bubblewrap antigravity sdk probe",
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
        "same_namespace_as_worker": os.stat("/proc/self/ns/mnt").st_ino
        == int(os.environ["PROBE_WORKER_MNT_NS"]),
        "different_namespace_from_host": os.stat("/proc/self/ns/mnt").st_ino
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


HARNESS_WRAPPER_SOURCE = r"""#!/usr/bin/python3
from __future__ import annotations

import json
import os
from pathlib import Path


current = os.stat("/proc/self/ns/mnt").st_ino
Path(os.environ["PROBE_HARNESS_EVIDENCE"]).write_text(
    json.dumps(
        {
            "pid": os.getpid(),
            "same_namespace_as_worker": current
            == int(os.environ["PROBE_WORKER_MNT_NS"]),
            "different_namespace_from_host": current
            != int(os.environ["PROBE_HOST_MNT_NS"]),
            "cwd_matches_workspace": Path.cwd().resolve()
            == Path(os.environ["PROBE_WORKSPACE"]).resolve(),
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
real_harness = os.environ["PROBE_REAL_HARNESS"]
os.execv(real_harness, [real_harness])
"""


HOME_MARKER_NAME = ".agent-collab-bwrap-antigravity-sdk-home-marker"
STATE_MARKER_NAME = ".agent-collab-bwrap-antigravity-sdk-state-marker"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="run the Python filesystem and descendant controls without an SDK call",
    )
    parser.add_argument(
        "--execution-mode",
        choices=("wrapped-worker", "current-architecture"),
        default="wrapped-worker",
        help=(
            "wrap the complete standalone SDK worker, or demonstrate the current "
            "in-process backend plus its unsandboxed localharness child"
        ),
    )
    parser.add_argument(
        "--state-mode",
        choices=("writable", "read-only"),
        default="writable",
        help="mount the private provider-state root writable or leave it read-only",
    )
    parser.add_argument(
        "--sdk-python",
        default=os.environ.get("AGENT_COLLAB_ANTIGRAVITY_SDK_PYTHON", sys.executable),
        help="Python interpreter containing google-antigravity (default: current Python)",
    )
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--vertex-project", help=argparse.SUPPRESS)
    parser.add_argument(
        "--vertex-location",
        default=os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_LOCATION", "us-central1"),
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--bwrap", default="bwrap")
    parser.add_argument("--_worker", choices=("wrapped", "current"), help=argparse.SUPPRESS)
    parser.add_argument("--_fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_host-mnt-ns", help=argparse.SUPPRESS)
    parser.add_argument("--_project", help=argparse.SUPPRESS)
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
        "provider_state": root / "sandbox home" / ".gemini" / "antigravity",
        "scratch": root / "scratch",
    }
    for path in paths.values():
        make_directory(path)
    make_directory(paths["provider_state"] / "trajectory")
    make_directory(paths["provider_state"] / "app-data")

    (paths["workspace"] / "probe-input.txt").write_text(
        "bubblewrap antigravity sdk probe\n", encoding="utf-8"
    )
    action = paths["workspace"] / "probe_actions.py"
    action.write_text(ACTION_SOURCE, encoding="utf-8")
    action.chmod(0o755)

    wrapper = paths["scratch"] / "localharness-wrapper"
    wrapper.write_text(HARNESS_WRAPPER_SOURCE, encoding="utf-8")
    wrapper.chmod(0o755)
    paths["harness_wrapper"] = wrapper
    return paths


def resolve_adc_path() -> Path:
    configured = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    path = (
        Path(configured)
        if configured
        else Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    )
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("Google Application Default Credentials file is unavailable")
    return path


def resolve_project(explicit: str | None) -> str:
    for value in (
        explicit,
        os.environ.get("AGENT_COLLAB_IT_ANTIGRAVITY_PROJECT"),
        os.environ.get("GOOGLE_CLOUD_PROJECT"),
    ):
        if value:
            return value
    gcloud = shutil.which("gcloud")
    if gcloud:
        try:
            completed = subprocess.run(
                [gcloud, "config", "get-value", "project"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None:
            value = completed.stdout.strip()
            if completed.returncode == 0 and value and value != "(unset)":
                return value
    raise RuntimeError("Vertex project is unavailable")


def package_harness(sdk_python: str) -> str:
    script = (
        "from google.antigravity.connections.local.local_connection import "
        "_get_default_binary_path; print(_get_default_binary_path())"
    )
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("ANTIGRAVITY_HARNESS_PATH", None)
    completed = subprocess.run(
        [sdk_python, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError("google-antigravity is unavailable or incompatible in --sdk-python")
    path = Path(completed.stdout.strip()).resolve()
    if not path.is_file():
        raise RuntimeError("google-antigravity localharness binary was not found")
    return str(path)


def probe_environment(
    paths: dict[str, Path],
    *,
    host_mnt_ns: int,
    real_harness: str | None,
    adc_path: Path | None,
) -> dict[str, str]:
    scratch = paths["scratch"]
    env = {
        "HOME": str(paths["sandbox_home"]),
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
        "PROBE_HOME_MARKER": str(paths["sandbox_home"] / HOME_MARKER_NAME),
        "PROBE_PROVIDER_STATE_MARKER": str(paths["provider_state"] / STATE_MARKER_NAME),
        "PROBE_ACTION_RESULTS": str(scratch / "action-results.json"),
        "PROBE_CHILD_RESULTS": str(scratch / "child-results.json"),
        "PROBE_ACTION_INVOKED": str(scratch / "action-invoked"),
        "PROBE_ACTION_DUPLICATE": str(scratch / "action-duplicate"),
        "PROBE_HARNESS_EVIDENCE": str(scratch / "harness-evidence.json"),
        "PROBE_HOST_MNT_NS": str(host_mnt_ns),
        "ANTIGRAVITY_HARNESS_PATH": str(paths["harness_wrapper"]),
    }
    if real_harness is not None:
        env["PROBE_REAL_HARNESS"] = real_harness
    if adc_path is not None:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc_path)
    return env


def bubblewrap_prefix(
    bwrap: str,
    paths: dict[str, Path],
    env: dict[str, str],
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
        prefix.extend(["--bind", str(paths["provider_state"]), str(paths["provider_state"])])
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


def verify_action(paths: dict[str, Path], state_mode: str) -> bool:
    payload = load_json(paths["scratch"] / "action-results.json")
    direct = payload.get("direct") if isinstance(payload.get("direct"), dict) else {}
    child = payload.get("child") if isinstance(payload.get("child"), dict) else {}
    expected_state = state_mode == "writable"

    def boundary_ok(result: dict[str, Any]) -> bool:
        return (
            result.get("cwd_match") is True
            and result.get("workspace_read") is True
            and result.get("workspace_write") is False
            and result.get("protected_host_write") is False
            and result.get("home_write") is False
            and result.get("provider_state_write") is expected_state
            and result.get("scratch_write") is True
            and result.get("same_namespace_as_worker") is True
            and result.get("different_namespace_from_host") is True
        )

    checks = [
        (
            "action script ran exactly once",
            (paths["scratch"] / "action-invoked").is_file()
            and not (paths["scratch"] / "action-duplicate").exists(),
        ),
        ("direct action boundary", boundary_ok(direct)),
        (
            "child inherited boundary",
            payload.get("child_returncode") == 0 and boundary_ok(child),
        ),
        (
            "workspace marker absent",
            not (paths["workspace"] / "workspace-write-marker").exists(),
        ),
        (
            "protected-host marker absent",
            not (paths["protected_host"] / "protected-write-marker").exists(),
        ),
        (
            "general-home marker absent",
            not (paths["sandbox_home"] / HOME_MARKER_NAME).exists(),
        ),
        (
            f"provider-state write {'allowed' if expected_state else 'blocked'}",
            (paths["provider_state"] / STATE_MARKER_NAME).is_file() is expected_state,
        ),
        (
            "scratch write allowed",
            (paths["scratch"] / "scratch-write-marker").is_file(),
        ),
    ]
    width = max(len(label) for label, _ in checks)
    print("\nHost verification")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def verify_harness(paths: dict[str, Path], *, wrapped: bool) -> bool:
    evidence = load_json(paths["scratch"] / "harness-evidence.json")
    checks = [
        ("harness inherited worker namespace", evidence.get("same_namespace_as_worker") is True),
        ("harness inherited worker cwd", evidence.get("cwd_matches_workspace") is True),
        (
            "harness namespace relationship",
            evidence.get("different_namespace_from_host") is wrapped,
        ),
    ]
    width = max(len(label) for label, _ in checks)
    print("\nHarness ownership")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def wait_for_recorded_harness_exit(paths: dict[str, Path]) -> bool:
    evidence = load_json(paths["scratch"] / "harness-evidence.json")
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
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


async def sdk_worker(args: argparse.Namespace) -> int:
    if args._fixture is None or args._host_mnt_ns is None or not args._project:
        return 2
    root = args._fixture.resolve()
    paths = {
        "workspace": root / "workspace with spaces",
        "protected_host": root / "protected host",
        "sandbox_home": root / "sandbox home",
        "provider_state": root / "sandbox home" / ".gemini" / "antigravity",
        "scratch": root / "scratch",
    }
    worker_ns = os.stat("/proc/self/ns/mnt").st_ino
    os.environ["PROBE_WORKER_MNT_NS"] = str(worker_ns)
    os.environ["PROBE_HOST_MNT_NS"] = args._host_mnt_ns
    logging.disable(logging.CRITICAL)

    if args._worker == "wrapped":
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
        from google.antigravity.hooks import policy

        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(paths['workspace'] / 'probe_actions.py'))}"
        prompt = (
            "For this controlled enforcement probe, use run_command to execute "
            f"exactly this command once: {command}. Do not run any other command "
            "and do not simulate it. Then report the command output."
        )
        config = LocalAgentConfig(
            capabilities=CapabilitiesConfig(),
            policies=[policy.allow_all()],
            workspaces=[str(paths["workspace"])],
            model=args.model,
            vertex=True,
            project=args._project,
            location=args.vertex_location,
            save_dir=str(paths["provider_state"] / "trajectory"),
            app_data_dir=str(paths["provider_state"] / "app-data"),
        )
        try:
            async with Agent(config) as agent:
                response = await agent.chat(prompt)
                chunks = await response.resolve()
        except Exception:
            return 1
        has_text = any(
            type(chunk).__name__ == "Text"
            and isinstance(getattr(chunk, "text", None), str)
            and getattr(chunk, "text").strip()
            for chunk in chunks
        )
        return 0 if has_text else 1

    from agent_collab.backends.antigravity_sdk import AntigravitySdkBackend
    from agent_collab.config import AgentConfig

    agent = AgentConfig(
        id="antigravity_sdk_probe",
        type="antigravity",
        backend="sdk",
        backend_config={
            "vertex": True,
            "project": args._project,
            "location": args.vertex_location,
        },
    )
    backend = AntigravitySdkBackend()
    runner = backend.create_runner(agent, False, {"model": args.model})
    events = []

    async def emit(event):
        events.append(event)

    prompt = (
        "Use the create_file tool exactly once to create "
        f"{paths['workspace'] / 'current-architecture-marker'} containing "
        "the word uncontained, then reply with done. Do not use run_command."
    )
    try:
        outcome = await runner.run_turn(prompt, paths["workspace"], emit)
    finally:
        await runner.close()
    result = {
        "completed": outcome.outcome == "completed",
        "saw_file_tool": any(event.type == "file_change" for event in events),
    }
    (paths["scratch"] / "current-result.json").write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8"
    )
    return 0 if result["completed"] else 1


def current_architecture_probe(
    args: argparse.Namespace,
    paths: dict[str, Path],
    sdk_python: str,
    env: dict[str, str],
    project: str,
) -> bool:
    env = {**os.environ, **env}
    env["PROBE_WORKER_MNT_NS"] = str(os.stat("/proc/self/ns/mnt").st_ino)
    command = [
        sdk_python,
        str(Path(__file__).resolve()),
        "--_worker",
        "current",
        "--_fixture",
        str(paths["workspace"].parent),
        "--_host-mnt-ns",
        str(os.stat("/proc/self/ns/mnt").st_ino),
        "--_project",
        project,
        "--model",
        args.model,
        "--vertex-location",
        args.vertex_location,
    ]
    returncode = run_command(command, cwd=paths["workspace"], timeout=args.timeout, env=env)
    result = load_json(paths["scratch"] / "current-result.json")
    marker = paths["workspace"] / "current-architecture-marker"
    checks = [
        ("production SDK turn completed", returncode == 0 and result.get("completed") is True),
        ("production file tool observed", result.get("saw_file_tool") is True),
        ("workspace write reached host", marker.is_file()),
        ("localharness shares host namespace", verify_harness(paths, wrapped=False)),
    ]
    width = max(len(label) for label, _ in checks)
    print("\nCurrent-architecture negative control")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def main() -> int:
    args = parse_args()
    if args._worker:
        return asyncio.run(sdk_worker(args))
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
        bwrap = (
            require_command(args.bwrap, "Bubblewrap")
            if args.execution_mode == "wrapped-worker"
            else None
        )
        host_mnt_ns = os.stat("/proc/self/ns/mnt").st_ino
        real_harness = None if args.preflight_only else package_harness(sdk_python)
        adc_path = None if args.preflight_only else resolve_adc_path()
        project = None if args.preflight_only else resolve_project(args.vertex_project)
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(
            prefix="agent collab bwrap antigravity sdk probe "
        ) as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            paths = create_fixture(root)
            marker_paths = (
                paths["sandbox_home"] / HOME_MARKER_NAME,
                paths["provider_state"] / STATE_MARKER_NAME,
            )
            if any(path.exists() for path in marker_paths):
                raise RuntimeError("refusing to overwrite an existing guarded marker")
            env = probe_environment(
                paths,
                host_mnt_ns=host_mnt_ns,
                real_harness=real_harness,
                adc_path=adc_path,
            )

            print("▶ Antigravity SDK Bubblewrap feasibility probe")
            print(f"  execution mode {args.execution_mode}")
            print(f"  workspace      {paths['workspace']}")
            print(f"  provider state private temporary ({args.state_mode})")
            print("  general home   private temporary")
            print("  credentials    read-only ADC path" if adc_path else "  credentials    unused")
            sys.stdout.flush()

            try:
                if args.execution_mode == "current-architecture":
                    assert project is not None
                    verified = current_architecture_probe(args, paths, sdk_python, env, project)
                    return 0 if verified else 1

                assert bwrap is not None
                if args.preflight_only:
                    inner = [
                        sdk_python,
                        str(paths["workspace"] / "probe_actions.py"),
                    ]
                else:
                    assert project is not None
                    inner = [
                        sdk_python,
                        str(Path(__file__).resolve()),
                        "--_worker",
                        "wrapped",
                        "--_fixture",
                        str(root),
                        "--_host-mnt-ns",
                        str(host_mnt_ns),
                        "--_project",
                        project,
                        "--model",
                        args.model,
                        "--vertex-location",
                        args.vertex_location,
                    ]
                command = bubblewrap_prefix(bwrap, paths, env, args.state_mode) + inner
                returncode = run_command(command, cwd=paths["workspace"], timeout=args.timeout)
                action_verified = verify_action(paths, args.state_mode)
                harness_verified = (
                    True if args.preflight_only else verify_harness(paths, wrapped=True)
                )
            except subprocess.TimeoutExpired:
                runtime_reaped = wait_for_recorded_harness_exit(paths)
                print(
                    f"Error: probe timed out after {args.timeout}s; process group killed",
                    file=sys.stderr,
                )
                print(
                    "  SDK localharness descendant reaped   "
                    + ("PASS" if runtime_reaped else "FAIL"),
                    file=sys.stderr,
                )
                return 1
            finally:
                for path in marker_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        print(
                            f"! Warning: could not remove guarded marker: {exc}",
                            file=sys.stderr,
                        )

            if returncode != 0:
                print(
                    f"Error: inner command exited with status {returncode}",
                    file=sys.stderr,
                )
                return 1
            if not action_verified or not harness_verified:
                print("Error: one or more host assertions failed", file=sys.stderr)
                return 1
            print("✓ Antigravity SDK probe expectations passed")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
