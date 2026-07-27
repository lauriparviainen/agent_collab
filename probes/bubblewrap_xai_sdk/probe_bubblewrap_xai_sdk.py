#!/usr/bin/env python3
"""Probe the xAI SDK backend's model-only execution surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="inspect the production request shape without an API call",
    )
    parser.add_argument(
        "--sdk-python",
        default=os.environ.get("AGENT_COLLAB_XAI_SDK_PYTHON", sys.executable),
        help="Python interpreter containing xai-sdk (default: current Python)",
    )
    parser.add_argument("--model", default="grok-4.5")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--bwrap", default="bwrap")
    parser.add_argument("--_worker", choices=("structural", "live"), help=argparse.SUPPRESS)
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
        "scratch": root / "scratch",
    }
    for path in paths.values():
        make_directory(path)
    (paths["workspace"] / "probe-input.txt").write_text(
        "bubblewrap xai sdk probe\n", encoding="utf-8"
    )
    return paths


def probe_environment(
    paths: dict[str, Path], host_mnt_ns: int, *, include_key: bool
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
        "XAI_SDK_DISABLE_TRACING": "1",
        "PROBE_WORKSPACE": str(paths["workspace"]),
        "PROBE_PROTECTED_HOST": str(paths["protected_host"]),
        "PROBE_HOME_MARKER": str(paths["sandbox_home"] / ".agent-collab-bwrap-xai-sdk-home-marker"),
        "PROBE_EVIDENCE": str(scratch / "evidence.json"),
        "PROBE_HOST_MNT_NS": str(host_mnt_ns),
    }
    if include_key:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is unavailable for the credentialed comparison")
        env["XAI_API_KEY"] = api_key
    return env


def bubblewrap_prefix(bwrap: str, paths: dict[str, Path], env: dict[str, str]) -> list[str]:
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
        "--ro-bind",
        str(paths["protected_host"]),
        str(paths["protected_host"]),
        "--ro-bind",
        str(paths["workspace"]),
        str(paths["workspace"]),
    ]
    for key, value in env.items():
        prefix.extend(["--setenv", key, value])
    prefix.extend(["--chdir", str(paths["workspace"]), "--"])
    return prefix


def run_command(command: list[str], cwd: Path, timeout: int) -> int:
    process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def structural_worker(args: argparse.Namespace, paths: dict[str, Path]) -> int:
    subprocess_events: list[str] = []

    def audit(event: str, _arguments: tuple[Any, ...]) -> None:
        if event in {"subprocess.Popen", "os.system"}:
            subprocess_events.append(event)

    sys.addaudithook(audit)

    from agent_collab.backends.xai_sdk.backend import _map_sdk_options
    from agent_collab.backends.xai_sdk.compat import import_xai_sdk

    xai_sdk = import_xai_sdk()
    AsyncClient = xai_sdk.AsyncClient
    from xai_sdk.chat import user

    mapped = _map_sdk_options({"model": args.model, "thinking_level": "low"})
    client = AsyncClient(
        api_key="structural-fixture-key",
        api_host="127.0.0.1:1",
        use_insecure_channel=True,
    )
    try:
        chat = client.chat.create(**mapped, store_messages=True)
        chat.append(user("structural request only"))
        request = chat._make_request(1)
        evidence = {
            "worker_separate_from_host": os.stat("/proc/self/ns/mnt").st_ino
            != int(os.environ["PROBE_HOST_MNT_NS"]),
            "cwd_matches_workspace": Path.cwd().resolve() == paths["workspace"].resolve(),
            "tracked_input_readable": (paths["workspace"] / "probe-input.txt")
            .read_text(encoding="utf-8")
            .strip()
            == "bubblewrap xai sdk probe",
            "mapped_fields": sorted(mapped),
            "request_tool_count": len(request.tools),
            "subprocess_events": subprocess_events,
        }
    finally:
        await client.close()
    Path(os.environ["PROBE_EVIDENCE"]).write_text(
        json.dumps(evidence, sort_keys=True), encoding="utf-8"
    )
    return 0


async def live_worker(args: argparse.Namespace, paths: dict[str, Path]) -> int:
    from agent_collab.backends.xai_sdk import XaiSdkBackend
    from agent_collab.config import AgentConfig

    agent = AgentConfig(id="xai_sdk_probe", type="xai", backend="sdk")
    backend = XaiSdkBackend()
    events = []
    try:
        runner = backend.create_runner(
            agent,
            False,
            {"model": args.model, "thinking_level": "low"},
        )
        try:

            async def emit(event):
                events.append(event)

            outcome = await runner.run_turn(
                "Reply with the single word ready. Do not call any tool.",
                paths["workspace"],
                emit,
            )
        finally:
            await runner.close()
    except Exception:
        outcome = None
    evidence = {
        "worker_separate_from_host": os.stat("/proc/self/ns/mnt").st_ino
        != int(os.environ["PROBE_HOST_MNT_NS"]),
        "cwd_matches_workspace": Path.cwd().resolve() == paths["workspace"].resolve(),
        "completed": outcome is not None and outcome.outcome == "completed",
        "message_count": sum(event.type == "message" for event in events),
        "local_tool_event_count": sum(
            event.type in {"tool_call", "command", "file_change"} for event in events
        ),
    }
    Path(os.environ["PROBE_EVIDENCE"]).write_text(
        json.dumps(evidence, sort_keys=True), encoding="utf-8"
    )
    return 0 if evidence["completed"] else 1


def worker_main(args: argparse.Namespace) -> int:
    if args._fixture is None or args._host_mnt_ns is None:
        return 2
    root = args._fixture.resolve()
    paths = {
        "workspace": root / "workspace with spaces",
        "protected_host": root / "protected host",
        "sandbox_home": root / "sandbox home",
        "scratch": root / "scratch",
    }
    if args._worker == "structural":
        return asyncio.run(structural_worker(args, paths))
    return asyncio.run(live_worker(args, paths))


def verify(paths: dict[str, Path], *, preflight_only: bool) -> bool:
    evidence = load_json(paths["scratch"] / "evidence.json")
    checks = [
        (
            "worker entered a separate mount namespace",
            evidence.get("worker_separate_from_host") is True,
        ),
        ("worker cwd matches workspace", evidence.get("cwd_matches_workspace") is True),
        (
            "workspace remained unchanged",
            not (paths["workspace"] / "workspace-write-marker").exists(),
        ),
        (
            "protected host remained unchanged",
            not (paths["protected_host"] / "protected-write-marker").exists(),
        ),
        (
            "general home remained unchanged",
            not (paths["sandbox_home"] / ".agent-collab-bwrap-xai-sdk-home-marker").exists(),
        ),
    ]
    if preflight_only:
        checks.extend(
            [
                (
                    "tracked input readable",
                    evidence.get("tracked_input_readable") is True,
                ),
                (
                    "production-mapped request has no tools",
                    evidence.get("request_tool_count") == 0,
                ),
                (
                    "production maps only model/reasoning",
                    evidence.get("mapped_fields") == ["model", "reasoning_effort"],
                ),
                (
                    "request construction spawned no child",
                    evidence.get("subprocess_events") == [],
                ),
            ]
        )
    else:
        checks.extend(
            [
                ("remote model request completed", evidence.get("completed") is True),
                ("assistant message returned", evidence.get("message_count", 0) > 0),
                (
                    "no local tool events exposed",
                    evidence.get("local_tool_event_count") == 0,
                ),
            ]
        )
    width = max(len(label) for label, _ in checks)
    print("\nHost verification")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _, passed in checks)


def main() -> int:
    args = parse_args()
    if args._worker:
        return worker_main(args)
    if args.timeout <= 0:
        print("Error: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        sdk_python = require_command(args.sdk_python, "SDK Python", resolve_symlinks=False)
        bwrap = require_command(args.bwrap, "Bubblewrap")
        host_mnt_ns = os.stat("/proc/self/ns/mnt").st_ino
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="agent collab bwrap xai sdk probe ") as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            paths = create_fixture(root)
            env = probe_environment(
                paths,
                host_mnt_ns,
                include_key=not args.preflight_only,
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
            command = bubblewrap_prefix(bwrap, paths, env) + inner
            print("▶ xAI SDK Bubblewrap evidence probe")
            print(f"  mode       {worker_mode}")
            print(f"  workspace  {paths['workspace']} (read-only)")
            print("  home       private temporary (read-only)")
            print("  state root none")
            print(
                "  credentials XAI_API_KEY passed without inspection"
                if not args.preflight_only
                else "  credentials unused"
            )
            sys.stdout.flush()
            try:
                returncode = run_command(command, paths["workspace"], args.timeout)
            except subprocess.TimeoutExpired:
                print(
                    f"Error: probe timed out after {args.timeout}s; process group killed",
                    file=sys.stderr,
                )
                return 1
            verified = verify(paths, preflight_only=args.preflight_only)
            if returncode != 0:
                print(
                    f"Error: inner command exited with status {returncode}",
                    file=sys.stderr,
                )
                return 1
            if not verified:
                print("Error: one or more host assertions failed", file=sys.stderr)
                return 1
            print("✓ xAI SDK evidence probe expectations passed")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
