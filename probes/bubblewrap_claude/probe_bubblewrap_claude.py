#!/usr/bin/env python3
"""Probe Claude CLI inside an outer Bubblewrap read-only workspace boundary."""

from __future__ import annotations

import argparse
import json
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

if test "$(cat probe-input.txt)" = "bubblewrap claude probe"; then
    printf 'workspace_read=ok\\n'
else
    printf 'workspace_read=failed\\n'
fi

try_write workspace_write "$PROBE_WORKSPACE/workspace-write-marker"
try_write protected_host_write "$PROBE_PROTECTED_HOME/protected-write-marker"
try_write home_write "$PROBE_HOME_MARKER"
try_write provider_state_write "$PROBE_PROVIDER_STATE_MARKER"
try_write scratch_write "$TMPDIR/scratch-write-marker"
"""

HOME_MARKER_NAME = ".agent-collab-bwrap-probe-home-marker"
MARKER_NAME = ".agent-collab-bwrap-probe-write-marker"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="run the action script directly in Bubblewrap without Claude or authentication",
    )
    parser.add_argument("--model", help="optional Claude model id; defaults to the CLI default")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="maximum probe runtime in seconds (default: 300)",
    )
    parser.add_argument("--claude", default="claude", help="Claude CLI command (default: claude)")
    parser.add_argument("--bwrap", default="bwrap", help="Bubblewrap command (default: bwrap)")
    parser.add_argument(
        "--source-claude-config",
        type=Path,
        help="Claude state root (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    parser.add_argument(
        "--state-mode",
        choices=("writable", "read-only"),
        default="writable",
        help=(
            "mount the real Claude state root persistently writable, or leave it "
            "read-only as a negative control (default: writable)"
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


def require_claude_auth(config_dir: Path) -> None:
    credential_file = config_dir / ".credentials.json"
    credential_env = any(
        os.environ.get(name)
        for name in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
    )
    if not credential_file.is_file() and not credential_env:
        raise RuntimeError(
            "Claude credentials not found in the configured state root or supported environment"
        )


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
        "provider_state": root / "sandbox home" / ".claude",
        "scratch": root / "scratch",
    }
    for name in ("workspace", "protected_home", "provider_state", "scratch"):
        make_directory(paths[name])

    (paths["workspace"] / "probe-input.txt").write_text(
        "bubblewrap claude probe\n", encoding="utf-8"
    )
    action = paths["workspace"] / "probe-actions.sh"
    action.write_text(ACTION_SCRIPT, encoding="utf-8")
    action.chmod(0o755)
    return paths


def bubblewrap_prefix(
    bwrap: str,
    paths: dict[str, Path],
    *,
    effective_home: Path,
    home_marker: Path,
    provider_state: Path,
    provider_state_marker: Path,
    state_mode: str,
    set_claude_config_dir: bool,
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
    if set_claude_config_dir:
        prefix.extend(["--setenv", "CLAUDE_CONFIG_DIR", str(provider_state)])
    else:
        prefix.extend(["--unsetenv", "CLAUDE_CONFIG_DIR"])
    if state_mode == "writable":
        prefix.extend(["--bind", str(provider_state), str(provider_state)])
    prefix.extend(
        [
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
            str(effective_home),
            "--setenv",
            "TMPDIR",
            str(paths["scratch"]),
            "--setenv",
            "CLAUDE_CODE_TMPDIR",
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
            "DISABLE_AUTOUPDATER",
            "1",
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
            "--chdir",
            workspace,
            "--",
        ]
    )
    return prefix


def claude_command(claude: str, paths: dict[str, Path], model: str | None) -> list[str]:
    prompt = (
        filesystem_preamble(paths["workspace"], paths["scratch"])
        + "\nFor this controlled enforcement probe, run ./probe-actions.sh exactly once "
        "using the Bash tool; the script deliberately verifies that forbidden writes fail. "
        "Do not simulate it or replace it with other commands. "
        "Then report the script's stdout."
    )
    transient_settings = json.dumps(
        {"sandbox": {"enabled": False}},
        separators=(",", ":"),
    )
    command = [
        claude,
        "--print",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        transient_settings,
        "--tools",
        "Bash",
    ]
    if model:
        command.extend(["--model", model])
    command.extend(["--", prompt])
    return command


def verify(
    paths: dict[str, Path],
    home_marker: Path,
    provider_state_marker: Path,
    state_mode: str,
) -> bool:
    state_check = (
        ("provider state write allowed", provider_state_marker.is_file())
        if state_mode == "writable"
        else ("provider state write blocked", not provider_state_marker.exists())
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
        (
            "read-only home write blocked",
            not home_marker.exists(),
        ),
        state_check,
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
        claude = None if args.preflight_only else require_command(args.claude, "Claude")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="agent collab bwrap claude probe ") as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            paths = create_fixture(root)

            if args.preflight_only:
                effective_home = paths["sandbox_home"]
                provider_state = paths["provider_state"]
                set_claude_config_dir = True
                state_access = "temporary structural control"
                inner = ["/bin/sh", "./probe-actions.sh"]
                label = "Bubblewrap structural control"
            else:
                source_config = args.source_claude_config
                configured_source = os.environ.get("CLAUDE_CONFIG_DIR")
                if source_config is None:
                    source_config = Path(configured_source or str(Path.home() / ".claude"))
                set_claude_config_dir = (
                    args.source_claude_config is not None or configured_source is not None
                )
                provider_state = source_config.expanduser().resolve()
                if not provider_state.is_dir():
                    raise RuntimeError(f"Claude state root not found: {provider_state}")
                require_claude_auth(provider_state)
                effective_home = Path.home().resolve()
                state_access = (
                    "real auth/config/state, persistent writable"
                    if args.state_mode == "writable"
                    else "real auth/config/state, read-only negative control"
                )
                assert claude is not None
                inner = claude_command(claude, paths, args.model)
                label = "Claude CLI Bubblewrap probe"

            home_marker = effective_home / HOME_MARKER_NAME
            provider_state_marker = provider_state / MARKER_NAME
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
                    home_marker=home_marker,
                    provider_state=provider_state,
                    provider_state_marker=provider_state_marker,
                    state_mode=args.state_mode,
                    set_claude_config_dir=set_claude_config_dir,
                )
                + inner
            )
            print(f"▶ Running {label}", flush=True)
            print(f"  workspace      {paths['workspace']} (read-only)")
            print(f"  cwd            {paths['workspace']}")
            print(f"  home           {effective_home} (read-only)")
            print(f"  provider state {provider_state} ({state_access})")
            print(f"  scratch        {paths['scratch']} (ephemeral)")
            sys.stdout.flush()
            try:
                completed = subprocess.run(
                    command,
                    cwd=paths["workspace"],
                    check=False,
                    timeout=args.timeout,
                )
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
