#!/usr/bin/env python3
"""Credential-free real-macOS LaunchAgent lifecycle acceptance smoke."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys
import tempfile

from agent_collab.daemon_lifecycle import lifecycle_transaction, registration_transaction
from agent_collab.paths import GlobalDataPaths


REPO_ROOT = Path(__file__).resolve().parent.parent
LABEL = "io.github.lauriparviainen.agent-collab"


class PreflightContractError(RuntimeError):
    """The native launchctl contract was available but did not match the parser."""


def _run(argv: list[str], *, env: dict[str, str], check: bool = True):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise RuntimeError(f"{' '.join(argv)} failed: {detail}")
    return result


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _registration_paths() -> tuple[Path, Path, Path]:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    definition = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return (
        definition,
        definition.with_name(definition.name + ".agent-collab-recovery"),
        definition.with_name(definition.name + ".agent-collab-recovery-fallback"),
    )


def _loaded_pid(env: dict[str, str]) -> int | None:
    target = f"gui/{os.getuid()}/{LABEL}"
    result = _run(["launchctl", "print", target], env=env, check=False)
    if result.returncode != 0:
        return None
    match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", result.stdout or "")
    return int(match.group(1)) if match else None


def _target_loaded(env: dict[str, str]) -> bool:
    target = f"gui/{os.getuid()}/{LABEL}"
    result = _run(["launchctl", "print", target], env=env, check=False)
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    lowered = detail.lower()
    if any(
        value in lowered for value in ("could not find service", "not found", "no such process")
    ):
        return False
    raise RuntimeError(f"cannot prove the production LaunchAgent target absent: {detail}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _preflight(env: dict[str, str]) -> tuple[bool, str]:
    candidates = _registration_paths()
    collisions = [str(path) for path in candidates if os.path.lexists(path)]
    target = f"gui/{os.getuid()}/{LABEL}"
    loaded = _run(["launchctl", "print", target], env=env, check=False)
    if loaded.returncode == 0:
        collisions.append(f"loaded job {target}")
    disabled = _run(["launchctl", "print-disabled", f"gui/{os.getuid()}"], env=env, check=False)
    if disabled.returncode != 0:
        return False, "the graphical launchd domain is unavailable"
    block = re.search(r"(?ms)disabled services\s*=\s*\{\s*(.*?)^\s*\}\s*$", disabled.stdout or "")
    if block is None:
        raise PreflightContractError("launchctl disabled-override output is malformed")
    override_value = None
    for line in block.group(1).splitlines():
        value = line.strip()
        if not value:
            continue
        entry = re.fullmatch(r'(?:(?:"([^"]+)")|([^"\s]+))\s*=>\s*(\S+)', value)
        if entry is None:
            if re.match(rf'^"?{re.escape(LABEL)}"?(?:\s|=>|$)', value):
                raise PreflightContractError("launchctl disabled-override output is ambiguous")
            continue
        label = entry.group(1) or entry.group(2)
        if label != LABEL:
            continue
        state = entry.group(3).lower()
        if state not in {"true", "false", "disabled", "enabled"}:
            raise PreflightContractError("launchctl disabled-override output is ambiguous")
        if override_value is not None:
            raise PreflightContractError("launchctl returned duplicate disabled overrides")
        override_value = state
    if override_value in {"true", "disabled"}:
        collisions.append(f"persisted disabled override for {LABEL}")
    if collisions:
        return False, "production registration is not empty: " + ", ".join(collisions)
    return True, ""


def _reset_disabled_override(env: dict[str, str]) -> None:
    """Clear the smoke override only while the production identity is still empty."""

    paths = GlobalDataPaths.resolve(env)
    operation = "macOS launchd smoke override cleanup"
    with lifecycle_transaction(operation, paths):
        with registration_transaction("launchd", operation):
            residuals = [str(path) for path in _registration_paths() if os.path.lexists(path)]
            if residuals or _target_loaded(env):
                raise RuntimeError(
                    "production LaunchAgent identity changed before override cleanup"
                )
            _run(["launchctl", "enable", f"gui/{os.getuid()}/{LABEL}"], env=env)


def main() -> int:
    if sys.platform != "darwin":
        print("SKIP: macOS LaunchAgent smoke requires Darwin")
        return 0
    base_env = os.environ.copy()
    try:
        clean, detail = _preflight(base_env)
    except PreflightContractError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if not clean:
        print(f"SKIP: {detail}")
        return 0

    with tempfile.TemporaryDirectory(prefix="agent-collab-launchd-smoke-") as tmp:
        root = Path(tmp)
        venv = root / "venv"
        home = root / "home"
        python = venv / "bin" / "python"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", str(REPO_ROOT)], check=True)
        env = base_env | {
            "AGENT_COLLAB_HOME": str(home),
            "PATH": str(venv / "bin") + os.pathsep + base_env.get("PATH", ""),
        }
        cli = [str(python), "-m", "agent_collab.cli", "daemon"]
        port = _unused_port()
        cleanup_armed = True
        try:
            _run(cli + ["autostart", "enable", "--port", str(port)], env=env)
            status = _run(cli + ["autostart", "status"], env=env)
            if "manager" not in status.stdout or "launchd" not in status.stdout:
                raise RuntimeError("autostart status did not report launchd ownership")
            _run(cli + ["stop"], env=env)
            _run(cli + ["start"], env=env)
            _run(cli + ["restart"], env=env)
            managed_pid = _loaded_pid(env)
            if managed_pid is None:
                raise RuntimeError("launchd restart did not expose a managed PID")
            _run(cli + ["autostart", "disable"], env=env)
            _reset_disabled_override(env)
            cleanup_armed = False
            if not (home / "config.toml").exists():
                raise RuntimeError("disable removed the preserved daemon configuration")
            if not (home / "data" / "daemon" / "daemon.log").exists():
                raise RuntimeError("disable removed the preserved daemon log")
            residuals = [str(path) for path in _registration_paths() if os.path.lexists(path)]
            if residuals:
                raise RuntimeError("disable left LaunchAgent artifacts: " + ", ".join(residuals))
            if _loaded_pid(env) is not None:
                raise RuntimeError("disable left the LaunchAgent target loaded")
            if _pid_alive(managed_pid):
                raise RuntimeError(f"disable left managed pid {managed_pid} alive")
            print("macOS LaunchAgent lifecycle smoke passed")
            return 0
        finally:
            if cleanup_armed:
                # This ordinary command reacquires lifecycle then registration
                # locks and refuses a concurrent identity change. Never replace
                # it with a shell check-then-rm cleanup.
                cleanup = _run(cli + ["autostart", "disable"], env=env, check=False)
                if cleanup.returncode != 0:
                    print(
                        "WARNING: identity-bound LaunchAgent cleanup failed: "
                        + (cleanup.stderr or cleanup.stdout).strip(),
                        file=sys.stderr,
                    )
                else:
                    try:
                        _reset_disabled_override(env)
                    except Exception as exc:
                        print(
                            "WARNING: could not reset the smoke-test disabled override: "
                            + str(exc),
                            file=sys.stderr,
                        )


if __name__ == "__main__":
    raise SystemExit(main())
