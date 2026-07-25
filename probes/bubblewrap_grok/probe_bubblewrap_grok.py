#!/usr/bin/env python3
"""Probe Grok CLI inside an outer Bubblewrap read-only workspace boundary."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


ACTION_SCRIPT = """#!/bin/sh
set -u

record() {
    printf '%s\\n' "$1"
    printf '%s\\n' "$1" >> "$PROBE_ACTION_RESULTS" || exit 70
}

try_write() {
    label=$1
    target=$2
    if (umask 077; printf '%s\\n' "$label" > "$target") 2>/dev/null; then
        record "$label=allowed"
    else
        record "$label=blocked"
    fi
}

if test -e "$PROBE_ACTION_INVOKED"; then
    : > "$PROBE_ACTION_DUPLICATE"
    exit 71
fi
: > "$PROBE_ACTION_INVOKED" || exit 70
: > "$PROBE_ACTION_RESULTS" || exit 70

actual_pwd=$(pwd -P)
record "pwd=$actual_pwd"
if test "$actual_pwd" = "$PROBE_WORKSPACE"; then
    record "cwd_match=yes"
else
    record "cwd_match=no"
fi

if test "$(cat probe-input.txt)" = "bubblewrap grok probe"; then
    record "workspace_read=ok"
else
    record "workspace_read=failed"
fi

try_write workspace_write "$PROBE_WORKSPACE/workspace-write-marker"
try_write protected_host_write "$PROBE_PROTECTED_HOME/protected-write-marker"
try_write home_write "$PROBE_HOME_MARKER"
try_write provider_state_write "$PROBE_PROVIDER_STATE_MARKER"
try_write scratch_write "$TMPDIR/scratch-write-marker"
record "action_complete=yes"
"""

HOME_MARKER_NAME = ".agent-collab-bwrap-grok-probe-home-marker"
STATE_MARKER_NAME = ".agent-collab-bwrap-grok-probe-state-marker"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="run the action script directly in Bubblewrap without Grok or authentication",
    )
    parser.add_argument(
        "--state-mode",
        choices=("writable", "read-only"),
        default="writable",
        help=(
            "mount the selected Grok state root persistently writable, or leave "
            "it read-only as a negative control (default: writable)"
        ),
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        help="Grok state root (default: $GROK_HOME or ~/.grok)",
    )
    parser.add_argument(
        "--model",
        help="optional Grok model id; defaults to the CLI default",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="maximum probe runtime in seconds (default: 300)",
    )
    parser.add_argument(
        "--grok",
        default="grok",
        help="Grok CLI command (default: grok)",
    )
    parser.add_argument(
        "--bwrap",
        default="bwrap",
        help="Bubblewrap command (default: bwrap)",
    )
    return parser.parse_args()


def require_command(value: str, label: str) -> str:
    resolved = shutil.which(value)
    if resolved is None:
        raise RuntimeError(f"{label} command not found: {value}")
    return str(Path(resolved).resolve())


def make_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def filesystem_preamble(workspace: Path, scratch: Path) -> str:
    return f"""FILESYSTEM POLICY
Workspace root: {workspace}
Current directory: {workspace}
Access: OS-enforced read-only.
Use $TMPDIR ({scratch}) for temporary files and command output.
Do not attempt workspace edits; describe proposed changes in your response.
"""


def create_fixture(root: Path) -> dict[str, Path]:
    paths = {
        "workspace": root / "workspace with spaces",
        "protected_home": root / "protected host home",
        "sandbox_home": root / "sandbox home",
        "provider_state": root / "sandbox home" / ".grok",
        "scratch": root / "scratch",
    }
    for name in ("workspace", "protected_home", "sandbox_home", "provider_state", "scratch"):
        make_directory(paths[name])

    (paths["workspace"] / "probe-input.txt").write_text(
        "bubblewrap grok probe\n",
        encoding="utf-8",
    )
    action = paths["workspace"] / "probe-actions.sh"
    action.write_text(ACTION_SCRIPT, encoding="utf-8")
    action.chmod(0o755)
    return paths


def paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def resolve_real_state(args: argparse.Namespace, temp_root: Path) -> tuple[Path, Path]:
    configured_root = os.environ.get("GROK_HOME")
    state_root = args.state_root or Path(configured_root or str(Path.home() / ".grok"))
    state_root = state_root.expanduser().resolve()
    if not state_root.is_dir():
        raise RuntimeError(f"Grok state root not found: {state_root}")
    if paths_overlap(state_root, temp_root):
        raise RuntimeError("real Grok state root must not overlap the probe fixture")
    effective_home = Path.home().resolve()
    if state_root == effective_home or state_root in effective_home.parents:
        raise RuntimeError("Grok state root must not make the complete user home writable")
    return effective_home, state_root


def require_auth_evidence(state_root: Path) -> None:
    if os.environ.get("XAI_API_KEY"):
        return
    if (state_root / "auth.json").exists():
        return
    if (state_root / "config.toml").exists():
        # The config may select an environment key or an external auth provider.
        # Do not inspect it here because it may contain credential material.
        return
    raise RuntimeError(
        "Grok credential evidence unavailable: XAI_API_KEY, auth.json, and "
        "config.toml are all absent"
    )


def bubblewrap_prefix(
    bwrap: str,
    paths: dict[str, Path],
    *,
    effective_home: Path,
    provider_state: Path,
    home_marker: Path,
    provider_state_marker: Path,
    state_mode: str,
) -> list[str]:
    workspace = str(paths["workspace"])
    scratch = str(paths["scratch"])
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
        scratch,
        scratch,
    ]
    if state_mode == "writable":
        prefix.extend(["--bind", str(provider_state), str(provider_state)])
    prefix.extend(
        [
            "--ro-bind",
            str(paths["protected_home"]),
            str(paths["protected_home"]),
            # Keep this as the final filesystem bind so a nested workspace
            # cannot inherit a broader writable exception.
            "--ro-bind",
            workspace,
            workspace,
            "--setenv",
            "HOME",
            str(effective_home),
            "--setenv",
            "GROK_HOME",
            str(provider_state),
            "--setenv",
            "TMPDIR",
            scratch,
            "--setenv",
            "TMP",
            scratch,
            "--setenv",
            "TEMP",
            scratch,
            "--setenv",
            "XDG_CACHE_HOME",
            str(paths["scratch"] / "xdg-cache"),
            "--setenv",
            "XDG_CONFIG_HOME",
            str(paths["scratch"] / "xdg-config"),
            "--setenv",
            "XDG_DATA_HOME",
            str(paths["scratch"] / "xdg-data"),
            "--setenv",
            "XDG_STATE_HOME",
            str(paths["scratch"] / "xdg-state"),
            "--setenv",
            "PROBE_WORKSPACE",
            workspace,
            "--setenv",
            "PROBE_PROTECTED_HOME",
            str(paths["protected_home"]),
            "--setenv",
            "PROBE_HOME_MARKER",
            str(home_marker),
            "--setenv",
            "PROBE_PROVIDER_STATE_MARKER",
            str(provider_state_marker),
            "--setenv",
            "PROBE_ACTION_RESULTS",
            str(paths["scratch"] / "action-results"),
            "--setenv",
            "PROBE_ACTION_INVOKED",
            str(paths["scratch"] / "action-invoked"),
            "--setenv",
            "PROBE_ACTION_DUPLICATE",
            str(paths["scratch"] / "action-duplicate"),
            "--chdir",
            workspace,
            "--",
        ]
    )
    return prefix


def grok_command(grok: str, paths: dict[str, Path], model: str | None) -> list[str]:
    workspace = str(paths["workspace"])
    prompt = (
        filesystem_preamble(paths["workspace"], paths["scratch"])
        + "\nFor this controlled enforcement probe, run ./probe-actions.sh exactly once "
        "using the Bash tool. The script deliberately verifies that forbidden writes "
        "fail. Do not simulate it, replace it, or run any other command. "
        "Then report the script's stdout."
    )
    command = [
        grok,
        "--no-auto-update",
        "--output-format",
        "streaming-json",
        "--permission-mode",
        "bypassPermissions",
        "--sandbox",
        "off",
        "--cwd",
        workspace,
        "--no-plan",
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--tools",
        "Bash",
    ]
    if model:
        command.extend(["--model", model])
    command.extend(["-p", prompt])
    return command


def read_action_results(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    results: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            results[key] = value
    return results


def verify(
    paths: dict[str, Path],
    home_marker: Path,
    provider_state_marker: Path,
    state_mode: str,
) -> bool:
    results = read_action_results(paths["scratch"] / "action-results")
    expected_state = "allowed" if state_mode == "writable" else "blocked"
    state_check = (
        ("provider state write allowed", provider_state_marker.is_file())
        if state_mode == "writable"
        else ("provider state write blocked", not provider_state_marker.exists())
    )
    checks = [
        (
            "action script ran exactly once",
            (paths["scratch"] / "action-invoked").is_file()
            and not (paths["scratch"] / "action-duplicate").exists()
            and results.get("action_complete") == "yes",
        ),
        (
            "cwd matches authoritative path",
            results.get("pwd") == str(paths["workspace"]) and results.get("cwd_match") == "yes",
        ),
        ("tracked input readable", results.get("workspace_read") == "ok"),
        (
            "workspace write blocked",
            results.get("workspace_write") == "blocked"
            and not (paths["workspace"] / "workspace-write-marker").exists(),
        ),
        (
            "protected host write blocked",
            results.get("protected_host_write") == "blocked"
            and not (paths["protected_home"] / "protected-write-marker").exists(),
        ),
        (
            "general home write blocked",
            results.get("home_write") == "blocked" and not home_marker.exists(),
        ),
        (
            state_check[0],
            results.get("provider_state_write") == expected_state and state_check[1],
        ),
        (
            "scratch write allowed",
            results.get("scratch_write") == "allowed"
            and (paths["scratch"] / "scratch-write-marker").is_file(),
        ),
    ]
    width = max(len(label) for label, _passed in checks)
    print("\nHost verification")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _label, passed in checks)


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


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        print("Error: --timeout must be positive", file=sys.stderr)
        return 2

    try:
        bwrap = require_command(args.bwrap, "Bubblewrap")
        grok = None if args.preflight_only else require_command(args.grok, "Grok")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="agent collab bwrap grok probe ") as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            paths = create_fixture(root)

            if args.preflight_only:
                effective_home = paths["sandbox_home"]
                provider_state = paths["provider_state"]
                state_access = "temporary structural control"
                inner = ["/bin/sh", "./probe-actions.sh"]
                label = "Bubblewrap structural control"
            else:
                effective_home, provider_state = resolve_real_state(args, root)
                require_auth_evidence(provider_state)
                state_access = (
                    "real auth/config/state, persistent writable"
                    if args.state_mode == "writable"
                    else "real auth/config/state, read-only negative control"
                )
                assert grok is not None
                inner = grok_command(grok, paths, args.model)
                label = "Grok CLI Bubblewrap probe"

            home_marker = effective_home / HOME_MARKER_NAME
            provider_state_marker = provider_state / STATE_MARKER_NAME
            existing_markers = [
                marker for marker in (home_marker, provider_state_marker) if marker.exists()
            ]
            if existing_markers:
                raise RuntimeError(
                    "refusing to overwrite existing probe marker: "
                    + ", ".join(str(marker) for marker in existing_markers)
                )

            command = (
                bubblewrap_prefix(
                    bwrap,
                    paths,
                    effective_home=effective_home,
                    provider_state=provider_state,
                    home_marker=home_marker,
                    provider_state_marker=provider_state_marker,
                    state_mode=args.state_mode,
                )
                + inner
            )
            print(f"▶ Running {label}", flush=True)
            print(f"  workspace      {paths['workspace']} (read-only)")
            print(f"  cwd/--cwd      {paths['workspace']}")
            print(f"  home           {effective_home} (read-only)")
            print(f"  provider state {provider_state} ({state_access})")
            print(f"  scratch        {paths['scratch']} (ephemeral)")
            sys.stdout.flush()
            try:
                returncode = run_command(command, paths["workspace"], args.timeout)
                verified = verify(
                    paths,
                    home_marker,
                    provider_state_marker,
                    args.state_mode,
                )
            except subprocess.TimeoutExpired:
                print(f"Error: probe timed out after {args.timeout}s", file=sys.stderr)
                return 1
            finally:
                for marker in (home_marker, provider_state_marker):
                    try:
                        marker.unlink(missing_ok=True)
                    except OSError as exc:
                        print(
                            f"! Warning: could not remove probe marker {marker}: {exc}",
                            file=sys.stderr,
                        )

            if returncode != 0:
                print(
                    f"Error: inner command exited with status {returncode}",
                    file=sys.stderr,
                )
                return 1
            if not verified:
                print("Error: one or more filesystem expectations failed", file=sys.stderr)
                return 1
            print("✓ Bubblewrap filesystem expectations passed")
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
