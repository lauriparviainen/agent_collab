"""Install or uninstall agent-collab in the durable user environment.

Both commands are switchless and narrate one progress line and one result
line per step (see ``.claude/skills/cli-scripting/SKILL.md``). Install is
also the upgrade path: it reinstalls the checkout into the venv, migrates the
user config forward with a backup, and restarts the daemon when it was
running before install. Uninstall is the exact inverse, except user config
and session data under the agent-collab home are always kept.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import __version__
from .cli_output import error, info, ok, print_kv, print_table, step, warn


class UserInstallError(RuntimeError):
    pass


DEFAULT_BIN_DIR_ENV = "AGENT_COLLAB_BIN_DIR"
EDITABLE_ENV = "AGENT_COLLAB_INSTALL_EDITABLE"
INSTALL_LOG_NAME = "install.log"
READINESS_TIMEOUT_SECONDS = 30


def install_user_command(
    *,
    repo_root: Path,
    venv: Path,
    bin_dir: Path,
    editable: bool = False,
    bootstrap_python: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> Path:
    """Create/reuse the venv, pip-install the checkout, expose the command."""

    repo_root = repo_root.expanduser().resolve()
    venv = venv.expanduser().resolve()
    bin_dir = bin_dir.expanduser().resolve()
    bootstrap_python = (bootstrap_python or Path(sys.executable)).expanduser().resolve()
    venv_python = venv / "bin" / "python"

    # Fail fast on a foreign command before any expensive or stateful step,
    # so a refused install leaves nothing half-upgraded behind.
    link = bin_dir / "agent-collab"
    entrypoint = venv / "bin" / "agent-collab"
    if os.path.lexists(link) and _link_target(link) != entrypoint:
        raise UserInstallError(
            f"a command that agent-collab did not create is at {link}; "
            f"remove it and re-run: rm {link} && ./agent_collab.sh install"
        )

    step(f"Preparing durable environment ({_display(venv)})")
    created = False
    if not venv_python.exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        _run_checked([str(bootstrap_python), "-m", "venv", str(venv)])
        created = True
    if not venv_python.exists():
        raise UserInstallError(f"venv creation did not produce an interpreter: {venv_python}")
    ok(f"{_python_version(venv_python)} ready" + (" (created venv)" if created else ""))

    step(f"Installing agent-collab {__version__} into {_display(venv)}")
    install_args = [str(venv_python), "-m", "pip", "install"]
    if editable:
        install_args.append("--editable")
    # The durable user environment installs every provider SDK so the `sdk`
    # backends work out of the box; a plain checkout install stays SDK-free
    # and selects per-provider extras instead.
    install_args.append(f"{repo_root}[all]")
    _run_logged(install_args, log_path or _default_log_path(), action="package installation")
    _remove_obsolete_entrypoints(venv)
    ok(f"Installed agent-collab {__version__}" + (" (editable)" if editable else ""))

    step("Exposing the agent-collab command")
    if not entrypoint.exists():
        raise UserInstallError(f"package installation did not create {entrypoint}")
    bin_dir.mkdir(parents=True, exist_ok=True)
    _install_link(link, entrypoint)
    ok(f"Command available: {_display(link)}")
    if not _path_contains(link.parent):
        warn(f"{link.parent} is not on PATH; add it to use agent-collab in new shells")
    return link


def _remove_obsolete_entrypoints(venv: Path) -> None:
    obsolete = venv / "bin" / "agent-collab-mcp"
    if not os.path.lexists(obsolete):
        return
    if not obsolete.is_file() and not obsolete.is_symlink():
        raise UserInstallError(f"obsolete managed entry point is not a file: {obsolete}")
    obsolete.unlink()
    info(f"Removed obsolete command: {_display(obsolete)}")


def _install_link(link: Path, target: Path) -> None:
    existing = _link_target(link)
    if existing is not None and existing == target.resolve():
        return
    if os.path.lexists(link):
        raise UserInstallError(
            f"a command that agent-collab did not create is at {link}; "
            f"remove it and re-run: rm {link} && ./agent_collab.sh install"
        )
    fd, temporary = tempfile.mkstemp(prefix=f".{link.name}.", dir=str(link.parent))
    os.close(fd)
    temporary_path = Path(temporary)
    temporary_path.unlink()
    try:
        temporary_path.symlink_to(target)
        os.replace(temporary_path, link)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _link_target(path: Path) -> Optional[Path]:
    if not path.is_symlink():
        return None
    raw = path.readlink()
    return (path.parent / raw).resolve() if not raw.is_absolute() else raw.resolve()


def _run_checked(argv: List[str]) -> None:
    try:
        subprocess.run(argv, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UserInstallError(f"command failed: {' '.join(argv)}") from exc


def _run_logged(argv: List[str], log_path: Path, *, action: str) -> None:
    """Run a verbose subprocess with output captured to the install log."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(argv)}\n")
            log.flush()
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, check=False)
    except OSError as exc:
        raise UserInstallError(f"{action} failed: {exc}") from exc
    if result.returncode != 0:
        raise UserInstallError(
            f"{action} failed (exit {result.returncode}); full log: {log_path}\n"
            + _log_tail(log_path)
        )


def _log_tail(log_path: Path, lines: int = 15) -> str:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def _default_log_path() -> Path:
    from .paths import AgentCollabHome

    return AgentCollabHome.resolve().root / INSTALL_LOG_NAME


def _python_version(python: Path) -> str:
    try:
        result = subprocess.run(
            [str(python), "--version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return "Python (version unavailable)"
    detected = (result.stdout or result.stderr).strip()
    return detected or "Python (version unavailable)"


def _display(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _path_contains(directory: Path) -> bool:
    for item in os.environ.get("PATH", "").split(os.pathsep):
        if not item:
            continue
        try:
            if Path(item).expanduser().resolve() == directory:
                return True
        except OSError:
            continue
    return False


class _WarningBridge(logging.Handler):
    """Route config-loader log warnings to the CLI `! Warning:` marker."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            warn(record.getMessage())


def _migrate_user_config() -> None:
    from .config import ConfigError, ensure_daemon_token, load_user_config
    from .config_migrations import (
        CURRENT_CONFIG_SCHEMA,
        ConfigMigrationError,
        migrate_user_config_file,
    )
    from .paths import AgentCollabHome

    step("Checking user config")
    config_path = AgentCollabHome.resolve().config_path
    bridge = _WarningBridge()
    config_logger = logging.getLogger("agent_collab.config")
    config_logger.addHandler(bridge)
    try:
        try:
            result = migrate_user_config_file(config_path)
        except (ConfigError, ConfigMigrationError) as exc:
            raise UserInstallError(
                f"user config could not be migrated: {exc}; fix {config_path} and re-run install"
            ) from exc
        if result.status == "absent":
            # Create the config now with the durable daemon token so it is
            # available for MCP client setup without first starting the daemon.
            ensure_daemon_token()
            ok(f"Created user config with a daemon token: {_display(config_path)}")
            return
        if result.permissions_fixed:
            warn(f"{result.path} was group/world-readable; tightened to owner-only (0600)")
        if result.status == "migrated":
            ok(
                f"Config migrated from schema {result.previous_version} to "
                f"{CURRENT_CONFIG_SCHEMA} (backup: {_display(result.backup_path)})"
            )
        try:
            load_user_config()
        except (ConfigError, ConfigMigrationError) as exc:
            raise UserInstallError(
                f"user config is invalid: {exc}; fix {config_path} and re-run install"
            ) from exc
        if result.status == "current":
            ok(f"Config OK (schema {CURRENT_CONFIG_SCHEMA})")
        _ensure_daemon_token(config_path)
    finally:
        config_logger.removeHandler(bridge)


def _ensure_daemon_token(config_path: Path) -> None:
    """Add the durable daemon token to an existing config when it lacks one.

    Never fails the install: a config that already carries a token is left
    untouched, and a config whose token cannot be written (permissive mode, or
    a malformed ``[daemon]`` section) is reported as a warning so the daemon can
    still mint one on first start.
    """

    from .config import ConfigError, load_daemon_token, ensure_daemon_token

    if load_daemon_token() is not None:
        return
    try:
        ensure_daemon_token()
    except ConfigError as exc:
        warn(f"daemon token not set: {exc}")
        return
    ok("Added a daemon token to the user config")


def _probe_daemon() -> Dict[str, Any]:
    """Snapshot daemon state before install; never fails the install."""

    probe: Dict[str, Any] = {"running": False, "systemd": False, "sessions": None, "state": {}}
    try:
        from .daemon_supervisor import count_running_sessions, daemon_status

        status = daemon_status()
    except Exception:
        return probe
    if not status.running:
        return probe
    probe["running"] = True
    probe["systemd"] = status.state.get("manager") == "systemd"
    probe["state"] = dict(status.state)
    probe["sessions"] = count_running_sessions(status.state)
    return probe


def _restart_daemon(probe: Dict[str, Any], venv_python: Path) -> None:
    sessions = probe.get("sessions")
    suffix = ""
    if sessions:
        plural = "s" if sessions != 1 else ""
        suffix = f", interrupting {sessions} active session{plural}"
    step(f"Restarting daemon (was running{suffix})")
    try:
        if probe.get("systemd"):
            from .daemon_autostart import restart_systemd_daemon

            restart_systemd_daemon()
        else:
            from .daemon_supervisor import start_daemon, stop_daemon

            state = probe.get("state", {})
            stop_daemon()
            raw_workdir = state.get("default_workdir")
            # The installer may run under a bootstrap system Python; the
            # daemon must run under the durable venv it was installed into.
            start_daemon(
                host=str(state.get("host") or "127.0.0.1"),
                port=_state_port(state),
                default_workdir=Path(str(raw_workdir)) if raw_workdir else None,
                interpreter=venv_python,
            )
    except Exception as exc:
        raise UserInstallError(
            f"daemon restart failed: {exc}; restart it manually: agent-collab daemon restart"
        ) from exc
    ok(f"Daemon restarted on {__version__}")


def _state_port(state: Dict[str, Any]) -> int:
    try:
        return int(state.get("port") or 8765)
    except (TypeError, ValueError):
        return 8765


def _collect_backend_readiness(venv_python: Path) -> Dict[str, Any]:
    """Collect readiness through the installed environment, never bootstrap Python."""

    try:
        result = subprocess.run(
            [str(venv_python), "-m", "agent_collab.install_readiness"],
            capture_output=True,
            text=True,
            timeout=READINESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UserInstallError("installed readiness helper could not run") from exc
    if result.returncode != 0:
        raise UserInstallError("installed readiness helper returned an error")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise UserInstallError("installed readiness helper returned invalid data") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise UserInstallError("installed readiness helper returned an invalid summary")
    return payload


def _check_backend_readiness(venv_python: Path) -> bool:
    """Print the post-install table and return whether setup warnings remain."""

    step("Checking configured backend readiness")
    try:
        payload = _collect_backend_readiness(venv_python)
        return _print_backend_readiness(payload)
    except (UserInstallError, TypeError, ValueError):
        warn(
            "Backend readiness could not be checked; once the daemon is running, retry with: "
            "agent-collab options --workdir PROJECT --fresh"
        )
        return True


def _print_backend_readiness(payload: Dict[str, Any]) -> bool:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("readiness rows must be a list")
    selected_count = int(payload.get("selected_count", len(rows)))
    attention_count = int(payload.get("attention_count", 0))
    if attention_count:
        verb = "is" if attention_count == 1 else "are"
        info(
            f"{attention_count} of {selected_count} enabled backends {verb} not set up yet; "
            "each is simply skipped until its provider is available — the rest work normally"
        )
    else:
        ok(f"All {selected_count} enabled backends have available dependencies")

    summary = [
        ("scope", payload.get("scope") or "global user config"),
        ("config", payload.get("config_source") or "unknown"),
        ("probe source", payload.get("probe_source") or "installed environment"),
    ]
    disabled_backends = payload.get("disabled_backends") or []
    if disabled_backends:
        summary.append(("disabled backends", ", ".join(str(item) for item in disabled_backends)))
    print_kv(tuple(summary))
    print()
    table_rows = []
    remediation_rows = []
    seen_remediation = set()
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("readiness row must be an object")
        backend = str(item.get("backend") or "—")
        # The default agent shares the backend's name; only personae and
        # renames add information, so the agents cell lists just those.
        extra_agents = [str(agent) for agent in item.get("agents") or [] if str(agent) != backend]
        table_rows.append(
            (
                backend,
                ", ".join(extra_agents),
                item.get("dependency") or "unknown",
                item.get("credentials") or "unknown",
                item.get("version") or "—",
            )
        )
        for remediation in item.get("remediation") or []:
            if not isinstance(remediation, dict) or not remediation.get("message"):
                continue
            entry = (backend, str(remediation["message"]))
            if entry not in seen_remediation:
                seen_remediation.add(entry)
                remediation_rows.append(entry)

    if any(row[1] for row in table_rows):
        headers = ("backend", "agents", "dependency", "credentials", "version")
        printable = table_rows
    else:
        headers = ("backend", "dependency", "credentials", "version")
        printable = [(row[0], *row[2:]) for row in table_rows]
    # No max_widths: readiness facts are short-lived diagnostics and must not
    # be truncated; the terminal wraps long remediation text if it has to.
    print_table(headers, printable)
    if remediation_rows:
        print()
        print_table(("backend", "remediation"), remediation_rows)
    if attention_count or disabled_backends:
        print()
        info(
            "A backend you will not use can be turned off (and drop off this table) with "
            "enabled = false under its [backends.<name>] section in the user config."
        )
    print()
    return attention_count > 0


def _main_install(args: argparse.Namespace) -> int:
    bin_dir = Path(os.environ.get(DEFAULT_BIN_DIR_ENV) or "~/.local/bin")
    editable = os.environ.get(EDITABLE_ENV, "").strip().lower() in {"1", "true", "yes"}
    probe = _probe_daemon()
    install_user_command(
        repo_root=args.repo_root,
        venv=args.venv,
        bin_dir=bin_dir,
        editable=editable,
    )
    _migrate_user_config()
    venv_python = args.venv.expanduser().resolve() / "bin" / "python"
    if probe["running"]:
        _restart_daemon(probe, venv_python)
    else:
        info("Daemon not running; start it with: agent-collab daemon start")
    backends_awaiting_setup = _check_backend_readiness(venv_python)
    if backends_awaiting_setup:
        ok(
            "Install complete — some backends await provider setup (see above); try: agent-collab --help"
        )
    else:
        ok("Install complete — try: agent-collab --help")
    return 0


def uninstall_user_command(*, venv: Path, bin_dir: Path) -> None:
    """Reverse install: daemon, autostart, venv, command link. Data stays."""

    venv = venv.expanduser().resolve()
    bin_dir = bin_dir.expanduser().resolve()
    from .paths import AgentCollabHome

    home_root = AgentCollabHome.resolve().root

    step("Checking daemon and autostart")
    _teardown_daemon()

    step(f"Removing environment ({_display(venv)})")
    link = bin_dir / "agent-collab"
    link_target = _link_target(link)
    if venv.exists():
        shutil.rmtree(venv)
        ok("Environment removed")
    else:
        info("Environment not present")

    step("Removing the agent-collab command")
    if link_target is not None and (link_target == venv / "bin" / "agent-collab"):
        link.unlink(missing_ok=True)
        ok(f"Removed {_display(link)}")
    elif os.path.lexists(link):
        warn(f"left {link} in place; agent-collab did not create it")
    else:
        info("Command link not present")

    info(
        f"Config and session data kept at {_display(home_root)}; "
        "delete that directory to remove everything"
    )
    ok("Uninstall complete")


def _teardown_daemon() -> None:
    """Stop the daemon and remove autostart; failure aborts the uninstall.

    Deleting the venv while a systemd unit or live daemon still references it
    would leave a broken service behind, so teardown errors are fatal here.
    """

    try:
        from .daemon_autostart import disable_autostart, managed_unit_installed
        from .daemon_supervisor import daemon_status, stop_daemon

        autostart_disabled = False
        if managed_unit_installed():
            disable_autostart()
            ok("Autostart disabled")
            autostart_disabled = True
        # A manual daemon can be running even when a (stopped) systemd unit
        # existed, so always probe again after the autostart teardown.
        status = daemon_status()
        if status.running:
            stop_daemon()
            ok("Daemon stopped")
        elif autostart_disabled:
            ok("Daemon stopped (was systemd-managed)")
        else:
            info("Daemon not running")
    except UserInstallError:
        raise
    except Exception as exc:
        raise UserInstallError(
            f"daemon teardown failed: {exc}; stop it (agent-collab daemon stop, "
            "agent-collab daemon autostart disable) and re-run uninstall"
        ) from exc


def _main_uninstall(args: argparse.Namespace) -> int:
    bin_dir = Path(os.environ.get(DEFAULT_BIN_DIR_ENV) or "~/.local/bin")
    uninstall_user_command(venv=args.venv, bin_dir=bin_dir)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="./agent_collab.sh",
        description="Install or uninstall agent-collab in the durable user environment.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="Install or upgrade; re-run after git pull.")
    install.add_argument("--repo-root", type=Path, required=True, help=argparse.SUPPRESS)
    install.add_argument("--venv", type=Path, required=True, help=argparse.SUPPRESS)
    uninstall = actions.add_parser("uninstall", help="Remove the installation; data is kept.")
    uninstall.add_argument("--venv", type=Path, required=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            return _main_install(args)
        return _main_uninstall(args)
    except UserInstallError as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
