#!/usr/bin/env python3
"""Probe Codex CLI inside an outer Bubblewrap read-only filesystem boundary."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ACTION_SCRIPT = """#!/bin/sh
set -u

try_write() {
    label=$1
    target=$2
    if (umask 077; printf '%s\\n' "$label" > "$target") 2>/dev/null; then
        printf '%s=allowed\\n' "$label"
    else
        printf '%s=blocked\\n' "$label"
    fi
}

printf 'pwd=%s\\n' "$(pwd -P)"
if test "$(pwd -P)" = "$PROBE_WORKSPACE"; then
    printf 'cwd_match=yes\\n'
else
    printf 'cwd_match=no\\n'
fi

if test "$(cat probe-input.txt)" = "bubblewrap codex probe"; then
    printf 'workspace_read=ok\\n'
else
    printf 'workspace_read=failed\\n'
fi

try_write workspace_write "$PROBE_WORKSPACE/workspace-write-marker"
try_write protected_host_write "$PROBE_PROTECTED_HOME/protected-write-marker"
try_write home_write "$HOME/home-write-marker"
try_write scratch_write "$TMPDIR/scratch-write-marker"
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="run the action script directly in Bubblewrap without Codex or authentication",
    )
    parser.add_argument("--model", help="optional Codex model id; defaults to the CLI default")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="maximum probe runtime in seconds (default: 300)",
    )
    parser.add_argument("--codex", default="codex", help="Codex CLI command (default: codex)")
    parser.add_argument("--bwrap", default="bwrap", help="Bubblewrap command (default: bwrap)")
    parser.add_argument(
        "--source-codex-home",
        type=Path,
        help="source containing auth.json (default: $CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--home-mode",
        choices=("read-only", "staged"),
        default="read-only",
        help=(
            "read auth directly from a read-only CODEX_HOME, or copy auth into "
            "a writable ephemeral home (default: read-only)"
        ),
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


def stage_codex_auth(source_home: Path, staged_codex_home: Path) -> None:
    source = source_home / "auth.json"
    if not source.is_file():
        raise RuntimeError(f"Codex auth file not found: {source}")
    make_directory(staged_codex_home)
    target = staged_codex_home / "auth.json"
    shutil.copy2(source, target)
    target.chmod(0o600)


def require_codex_auth(source_home: Path) -> None:
    source = source_home / "auth.json"
    if not source.is_file():
        raise RuntimeError(f"Codex auth file not found: {source}")


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
        "codex_home": root / "sandbox home" / ".codex",
        "scratch": root / "scratch",
    }
    for name in ("workspace", "protected_home", "sandbox_home", "scratch"):
        make_directory(paths[name])

    (paths["workspace"] / "probe-input.txt").write_text(
        "bubblewrap codex probe\n", encoding="utf-8"
    )
    action = paths["workspace"] / "probe-actions.sh"
    action.write_text(ACTION_SCRIPT, encoding="utf-8")
    action.chmod(0o755)
    return paths


def bubblewrap_prefix(
    bwrap: str,
    paths: dict[str, Path],
    *,
    home_mode: str,
    effective_codex_home: Path,
) -> list[str]:
    workspace = str(paths["workspace"])
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
    ]
    if home_mode == "staged":
        # The staged home is a private temporary subtree. Read-only mode leaves
        # both HOME and the real CODEX_HOME under the root read-only mount.
        prefix.extend(
            [
                "--bind",
                str(paths["sandbox_home"]),
                str(paths["sandbox_home"]),
            ]
        )
    prefix.extend(
        [
            # Scratch is the only always-writable host-backed mount. It lives
            # under the probe's private temporary root and is discarded.
            "--bind",
            str(paths["scratch"]),
            str(paths["scratch"]),
            "--ro-bind",
            workspace,
            workspace,
            "--ro-bind",
            str(paths["protected_home"]),
            str(paths["protected_home"]),
            "--setenv",
            "HOME",
            str(paths["sandbox_home"]),
            "--setenv",
            "CODEX_HOME",
            str(effective_codex_home),
            "--setenv",
            "TMPDIR",
            str(paths["scratch"]),
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
            "--chdir",
            workspace,
            "--",
        ]
    )
    return prefix


def codex_command(codex: str, paths: dict[str, Path], model: str | None) -> list[str]:
    workspace = str(paths["workspace"])
    prompt = (
        filesystem_preamble(paths["workspace"], paths["scratch"])
        + "\nFor this controlled enforcement probe, run ./probe-actions.sh exactly once "
        "using the shell tool; the script deliberately verifies that forbidden writes fail. "
        "Do not simulate it or replace it with other commands. "
        "Then report the script's stdout."
    )
    command = [
        codex,
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        workspace,
    ]
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    return command


def verify(paths: dict[str, Path], home_mode: str) -> bool:
    home_marker = paths["sandbox_home"] / "home-write-marker"
    home_check = (
        ("ephemeral home write allowed", home_marker.is_file())
        if home_mode == "staged"
        else ("read-only home write blocked", not home_marker.exists())
    )
    checks = [
        (
            "workspace write blocked",
            not (paths["workspace"] / "workspace-write-marker").exists(),
        ),
        (
            "protected host write blocked",
            not (paths["protected_home"] / "protected-write-marker").exists(),
        ),
        home_check,
        (
            "scratch write allowed",
            (paths["scratch"] / "scratch-write-marker").is_file(),
        ),
    ]
    width = max(len(label) for label, _passed in checks)
    print("\nHost verification")
    for label, passed in checks:
        print(f"  {label:<{width}}   {'PASS' if passed else 'FAIL'}")
    return all(passed for _label, passed in checks)


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        print("Error: --timeout must be positive", file=sys.stderr)
        return 2

    try:
        bwrap = require_command(args.bwrap, "Bubblewrap")
        codex = None if args.preflight_only else require_command(args.codex, "Codex")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="agent collab bwrap codex probe ") as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            paths = create_fixture(root)

            source_home = args.source_codex_home
            if source_home is None:
                source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            source_home = source_home.expanduser().resolve()

            if args.preflight_only:
                inner = ["/bin/sh", "./probe-actions.sh"]
                label = "Bubblewrap structural control"
                effective_codex_home = paths["codex_home"]
                codex_access = "unused by structural control"
            else:
                require_codex_auth(source_home)
                if args.home_mode == "staged":
                    stage_codex_auth(source_home, paths["codex_home"])
                    effective_codex_home = paths["codex_home"]
                    codex_access = "staged, writable, ephemeral"
                else:
                    effective_codex_home = source_home
                    codex_access = "real auth/config, read-only"
                assert codex is not None
                inner = codex_command(codex, paths, args.model)
                label = "Codex CLI Bubblewrap probe"

            command = (
                bubblewrap_prefix(
                    bwrap,
                    paths,
                    home_mode=args.home_mode,
                    effective_codex_home=effective_codex_home,
                )
                + inner
            )
            print(f"▶ Running {label}", flush=True)
            print(f"  workspace  {paths['workspace']}")
            print(f"  cwd        {paths['workspace']}")
            home_access = "writable, ephemeral" if args.home_mode == "staged" else "read-only"
            print(f"  home       {paths['sandbox_home']} ({home_access})")
            print(f"  codex home {effective_codex_home} ({codex_access})")
            print(f"  scratch    {paths['scratch']} (ephemeral)")
            sys.stdout.flush()
            try:
                completed = subprocess.run(
                    command,
                    cwd=paths["workspace"],
                    check=False,
                    timeout=args.timeout,
                )
            except subprocess.TimeoutExpired:
                print(f"Error: probe timed out after {args.timeout}s", file=sys.stderr)
                return 1

            verified = verify(paths, args.home_mode)
            if completed.returncode != 0:
                print(
                    f"Error: inner command exited with status {completed.returncode}",
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
