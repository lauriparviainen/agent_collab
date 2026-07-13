from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, Optional

from .cli_output import error, fail, info, ok, print_kv, step, warn
from .config import DEFAULT_WORKFLOW
from .referee import RefereeConfig, run_sync


PUBLIC_COMMANDS = (
    ("tui", "Open the interactive session TUI."),
    ("daemon", "Manage the global daemon and login autostart."),
    ("start", "Start a daemon-owned collaboration session."),
    ("options", "Show live backend health and start-eligible workflows (asks the daemon)."),
    ("list", "List daemon-owned sessions."),
    ("status", "Show one daemon-owned session."),
    ("events", "Read events for a daemon-owned session."),
    ("watch", "Watch a live session or stored JSONL transcript."),
    ("stop", "Stop a daemon-owned session."),
    ("sessions", "Manage stored sessions, including pruning old terminal sessions."),
    ("config", "Show the merged config files for a workdir, or create the user config."),
    ("mcp", "Run the stdio MCP adapter (direct Streamable HTTP is preferred)."),
    ("serve", "Run the daemon in the foreground for debugging."),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab",
        usage="agent-collab [COMMAND] ... | agent-collab [OPTIONS] TASK",
        description="Run and supervise bounded collaboration workflows across configured AI agents.",
        epilog=_root_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", help="Task to send to the collaboration loop.")
    parser.add_argument(
        "--workflow", default=DEFAULT_WORKFLOW, help="Workflow name from agent-collab config."
    )
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument(
        "--timeout", type=int, default=900, help="Per-agent turn timeout in seconds."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without running configured agents."
    )
    parser.add_argument("--mock", action="store_true", help="Use simulated agent runners.")
    parser.add_argument(
        "--verbose", action="store_true", help="Print compact unknown stream events."
    )
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path("."),
        help="Project root used as cwd for agent subprocesses.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Session log directory. Defaults to the global AGENT_COLLAB_HOME data/sessions directory.",
    )
    parser.add_argument("--session-id", help=argparse.SUPPRESS)
    return parser


def _root_help_epilog() -> str:
    width = max(len(name) for name, _description in PUBLIC_COMMANDS)
    commands = "\n".join(
        f"  {name:<{width}}  {description}" for name, description in PUBLIC_COMMANDS
    )
    return (
        "commands:\n"
        f"{commands}\n\n"
        "Run 'agent-collab COMMAND --help' for command-specific options.\n"
        "Without COMMAND, agent-collab runs TASK as a one-shot configured workflow.\n"
        "A bare unknown word is treated as a mistyped command, not a task; describe\n"
        "tasks in a sentence, or pass any option first to force a one-word task."
    )


def build_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab watch", description="Watch an agent-collab JSONL session log."
    )
    parser.add_argument(
        "session_or_path", nargs="?", help="Session id or path to a session JSONL log."
    )
    parser.add_argument("--server-url", help="Daemon URL for watching a daemon-owned session id.")
    parser.add_argument(
        "--workdir", type=Path, help="Project root used to resolve SESSION_ID logs."
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Session log directory. Defaults to the global AGENT_COLLAB_HOME data/sessions directory.",
    )
    parser.add_argument(
        "--session-id", help="Session id to resolve under the session log directory."
    )
    parser.add_argument(
        "--cursor", type=int, default=0, help="Start after this zero-based JSONL line offset."
    )
    parser.add_argument(
        "--no-follow",
        action="store_true",
        help="Print current events and exit instead of following.",
    )
    parser.add_argument(
        "--wait-ms", type=int, default=30000, help="Daemon long-poll timeout while following."
    )
    parser.add_argument("--no-color", action="store_true")
    return parser


def build_tui_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab tui", description="Open the interactive daemon session TUI."
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        help="Daemon session id to open. Defaults to the latest updated session.",
    )
    parser.add_argument("--server-url", help="Daemon URL for the TUI.")
    return parser


def build_serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab serve", description="Run the local agent-collab daemon."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workdir", type=Path, default=Path("."), help=argparse.SUPPRESS)
    parser.add_argument("--session-log-dir", type=Path, help=argparse.SUPPRESS)
    return parser


def build_mcp_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="agent-collab mcp",
        description=(
            "Run the stdio MCP adapter for clients that do not connect directly "
            "to the daemon's preferred Streamable HTTP endpoint."
        ),
    )


def build_start_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab start", description="Start a daemon-owned collaboration session."
    )
    parser.add_argument("task")
    parser.add_argument("--server-url")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--workdir", type=Path, default=Path("."))
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--backend",
        help="Execution backend for every selected agent (e.g. 'cli', 'sdk'). "
        "Overrides per-agent config; valid only when every selected agent's type registers it.",
    )
    parser.add_argument(
        "--backend-options",
        help='JSON object keyed by backend name, e.g. {"claude_cli":{"model":"opus"}}.',
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Start the session and immediately watch its transcript.",
    )
    parser.add_argument(
        "--watch-wait-ms", type=int, default=30000, help="Long-poll timeout while watching."
    )
    parser.add_argument("--no-color", action="store_true")
    return parser


def build_options_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab options",
        description="Show what would work right now: per-backend health and start-eligible "
        "workflows for a workdir, from the running daemon's discovery snapshot. "
        "For the configuration itself and where each value comes from, use "
        "'agent-collab config show'.",
    )
    parser.add_argument("--server-url")
    parser.add_argument("--workdir", type=Path, default=Path("."))
    parser.add_argument(
        "--fresh", action="store_true", help="Bypass the backend health cache for this snapshot."
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the complete discovery response as JSON."
    )
    return parser


def build_daemon_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab daemon", description="Manage the global background server."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    start = subparsers.add_parser("start", help="Start the global background server.")
    _add_daemon_default_workdir(start)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)

    subparsers.add_parser("status", help="Show daemon status.")

    subparsers.add_parser("stop", help="Stop the daemon.")

    restart = subparsers.add_parser("restart", help="Restart the daemon.")
    _add_daemon_default_workdir(restart)
    restart.add_argument("--host", default="127.0.0.1")
    restart.add_argument("--port", type=int, default=8765)

    logs = subparsers.add_parser("logs", help="Print daemon logs.")
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument(
        "--stderr", action="store_true", help="Read daemon.stderr.log instead of daemon.log."
    )

    run = subparsers.add_parser("run", help=argparse.SUPPRESS)
    _add_daemon_default_workdir(run)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)

    autostart = subparsers.add_parser(
        "autostart", help="Manage automatic daemon startup for the current user."
    )
    autostart_actions = autostart.add_subparsers(dest="autostart_action", required=True)
    enable = autostart_actions.add_parser(
        "enable", help="Install, enable, and start the user service."
    )
    _add_daemon_default_workdir(enable)
    enable.add_argument("--host", default="127.0.0.1")
    enable.add_argument("--port", type=int, default=8765)
    autostart_actions.add_parser("status", help="Show registration and service health.")
    autostart_actions.add_parser(
        "disable", help="Stop and unregister the user service without deleting data."
    )
    return parser


def _add_daemon_default_workdir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Default workdir for sessions that do not pass one explicitly. Never affects daemon runtime paths.",
    )


def build_sessions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-collab sessions",
        description="Manage stored daemon sessions.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    prune = subparsers.add_parser(
        "prune",
        help="Preview or delete old terminal sessions and their managed transcripts.",
        description=(
            "Preview or delete old terminal sessions. Without --apply this only "
            "previews, using the configured retention (sessions.retention_days, "
            "30 days by default). Only terminal sessions (done, failed, stopped, "
            "interrupted) are ever eligible; transcripts outside the managed "
            "session directory are preserved."
        ),
    )
    prune.add_argument("--server-url")
    mode = prune.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview candidates without deleting anything (the default behavior).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Delete the selected sessions and their managed transcripts.",
    )
    prune.add_argument(
        "--older-than",
        metavar="DURATION",
        help="Override configured retention for this run; whole-number h, d, or w (e.g. 7d).",
    )
    prune.add_argument(
        "--keep",
        type=int,
        default=0,
        metavar="N",
        help="Always keep the newest N terminal sessions regardless of age.",
    )
    prune.add_argument("--json", action="store_true", help="Print the full typed response as JSON.")
    return parser


def build_client_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--server-url")
    return parser


def build_session_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = build_client_parser(prog, description)
    parser.add_argument("session_id")
    return parser


def build_events_parser() -> argparse.ArgumentParser:
    parser = build_session_parser("agent-collab events", "Read daemon session events.")
    parser.add_argument("--cursor", type=int, default=0)
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Long-poll until events are available or timeout elapses.",
    )
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--json", action="store_true", help="Print raw JSON response instead of transcript lines."
    )
    parser.add_argument("--no-color", action="store_true")
    return parser


def _main_watch(argv) -> int:
    from .watch import resolve_jsonl_path, watch_jsonl

    parser = build_watch_parser()
    args = parser.parse_args(argv)
    try:
        if _watch_should_use_file(args):
            path = resolve_jsonl_path(
                args.session_or_path,
                workdir=args.workdir,
                session_id=args.session_id,
                log_dir=args.log_dir,
            )
            watch_jsonl(
                path, follow=not args.no_follow, start_cursor=args.cursor, color=not args.no_color
            )
        else:
            session_id = (
                args.session_id
                or args.session_or_path
                or _latest_daemon_session_id(args.server_url)
            )
            _watch_daemon_session(
                session_id,
                server_url=args.server_url,
                cursor=args.cursor,
                follow=not args.no_follow,
                wait_ms=args.wait_ms,
                color=not args.no_color,
            )
    except KeyboardInterrupt:
        print(file=sys.stderr)
        error("interrupted")
        return 130
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_tui(argv) -> int:
    parser = build_tui_parser()
    args = parser.parse_args(argv)
    from .tui import run_tui

    try:
        return run_tui(session_id=args.session_id, server_url=args.server_url)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        error(str(exc))
        return 1


def _main_mcp(argv) -> int:
    parser = build_mcp_parser()
    parser.parse_args(argv)
    from .mcp_server import serve

    serve()
    return 0


def _watch_should_use_file(args) -> bool:
    if args.log_dir is not None or args.workdir is not None:
        return True
    if args.session_id is not None:
        return args.server_url is None
    if args.session_or_path is None:
        return False
    path = Path(args.session_or_path).expanduser()
    return path.exists() or path.is_absolute() or len(path.parts) > 1 or path.suffix == ".jsonl"


def _watch_daemon_session(
    session_id: str, server_url, cursor: int, follow: bool, wait_ms: int, color: bool
) -> None:
    from .events import Event
    from .terminal import print_event

    client = _client(server_url)
    current = max(0, int(cursor))
    while True:
        batch = (
            client.wait_events(session_id, current, wait_ms)
            if follow
            else client.read_events(session_id, current)
        )
        for event in batch.events:
            print_event(Event(**event.to_dict()), color=color)
        current = int(batch.cursor)
        if not follow:
            return
        if batch.terminal:
            return
        if batch.terminal is None:
            # Compatibility with an older daemon that omits the additive
            # outcome view from event batches.
            state = client.get_session(session_id)
            if state.status in {"done", "failed", "stopped", "interrupted"} and not batch.events:
                return


def _latest_daemon_session_id(server_url) -> str:
    sessions = _client(server_url).list_sessions().sessions
    if not sessions:
        raise ValueError("no daemon sessions found")
    latest = max(
        sessions, key=lambda item: (item.updated_at or item.created_at or "", item.session_id or "")
    )
    if not latest.session_id:
        raise ValueError("latest daemon session did not include a session_id")
    return str(latest.session_id)


def _main_serve(argv) -> int:
    from .server_http import run_server

    parser = build_serve_parser()
    args = parser.parse_args(argv)
    try:
        run_server(
            args.host,
            args.port,
            default_workdir=args.workdir,
            session_log_dir=args.session_log_dir,
        )
    except KeyboardInterrupt:
        return 130
    return 0


def _client(server_url=None):
    from .client import AgentCollabClient

    return AgentCollabClient(server_url)


def _main_start(argv) -> int:
    parser = build_start_parser()
    args = parser.parse_args(argv)
    try:
        backend_options = _json_object_arg(args.backend_options, "--backend-options")
        payload = {
            "task": args.task,
            "workflow": args.workflow,
            "workdir": str(args.workdir.expanduser().resolve()),
            "max_turns": args.max_turns,
            "timeout": args.timeout,
            "mock": args.mock,
            "dry_run": args.dry_run,
            "backend_options": backend_options,
        }
        if args.backend:
            payload["backend"] = args.backend
        result = _client(args.server_url).start_session(payload)
        _print_session(result)
        if args.watch:
            print("")
            _watch_daemon_session(
                result.session_id,
                server_url=args.server_url,
                cursor=0,
                follow=True,
                wait_ms=args.watch_wait_ms,
                color=not args.no_color,
            )
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_options(argv) -> int:
    parser = build_options_parser()
    args = parser.parse_args(argv)
    try:
        payload = {
            "workdir": str(args.workdir.expanduser().resolve()),
            "health_refresh": "fresh" if args.fresh else "cached",
        }
        result = _client(args.server_url).describe_options(payload)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        discovery = result.get("discovery") or {}
        print(f"workdir: {discovery.get('workdir', payload['workdir'])}")
        print(
            f"health: {discovery.get('health_request', payload['health_refresh'])} (advisory; start revalidates)"
        )
        for name, item in sorted((result.get("canonical_backends") or {}).items()):
            probe = item.get("probe") or {}
            assessment = item.get("assessment") or {}
            policy = item.get("policy") or {}
            health = probe.get("health") or {}
            print(
                f"backend {name}: enabled={str(policy.get('enabled', True)).lower()} "
                f"readiness={assessment.get('state', 'unknown')} health={health.get('status', probe.get('status', 'unknown'))} "
                f"start_probe={policy.get('start_probe_policy', 'unknown')}"
            )
        for workflow in result.get("workflows") or []:
            selected = ", ".join(workflow.get("selected_canonical_backends") or []) or "(mock)"
            print(
                f"workflow {workflow.get('id')}: {selected} eligible={str(workflow.get('start_eligible')).lower()}"
            )
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_daemon(argv) -> int:
    from .daemon_autostart import (
        AutostartError,
        autostart_status,
        disable_autostart,
        enable_autostart,
        restart_systemd_daemon,
        start_systemd_daemon,
        stop_systemd_daemon,
        systemd_owns_daemon,
    )
    from .daemon_supervisor import (
        DaemonSupervisorError,
        daemon_status,
        run_managed_daemon,
        start_daemon,
        stop_daemon,
        tail_daemon_log,
    )

    parser = build_daemon_parser()
    args = parser.parse_args(argv)
    default_workdir = (
        args.workdir.expanduser().resolve() if getattr(args, "workdir", None) else None
    )
    try:
        if args.action == "run":
            try:
                run_managed_daemon(
                    host=args.host,
                    port=args.port,
                    default_workdir=default_workdir,
                )
            except KeyboardInterrupt:
                pass
            return 0
        if args.action == "autostart":
            if args.autostart_action == "enable":
                status = enable_autostart(
                    host=args.host,
                    port=args.port,
                    default_workdir=default_workdir,
                )
                _print_autostart_status(status)
                return 0
            if args.autostart_action == "status":
                status = autostart_status()
                _print_autostart_status(status)
                return 0 if status.enabled and status.active and status.healthy else 1
            if args.autostart_action == "disable":
                status = disable_autostart()
                _print_autostart_status(status)
                return 0
        systemd_managed = systemd_owns_daemon()
        if args.action == "start":
            if systemd_managed:
                step("Starting daemon (systemd)")
                status = start_systemd_daemon()
                _print_live_daemon()
                _print_autostart_status(status)
                return 0
            step("Starting daemon")
            state = start_daemon(host=args.host, port=args.port, default_workdir=default_workdir)
            ok("Daemon running")
            _print_daemon_state(state)
            return 0
        if args.action == "status":
            if systemd_managed:
                status = autostart_status()
                _print_live_daemon()
                _print_autostart_status(status)
                return 0 if status.active and status.healthy else 1
            status = daemon_status()
            if status.running:
                ok("Daemon running")
                _print_daemon_state(status.state, live=True)
                _warn_on_version_skew(status.state)
                return 0
            fail("Daemon not running")
            if status.message != "global agent-collab daemon is not running":
                info(status.message)
            return 1
        if args.action == "stop":
            if systemd_managed:
                step("Stopping daemon (systemd)")
                status = stop_systemd_daemon()
                _print_autostart_status(status)
                return 0
            result = stop_daemon()
            if "stopped" in result.message or "killed" in result.message:
                version = result.state.get("version") or _installed_version()
                ok(f"Daemon stopped (pid {result.state.get('pid', 'unknown')}, was {version})")
            else:
                info(result.message)
            return 0
        if args.action == "restart":
            if systemd_managed:
                step("Restarting daemon (systemd)")
                status = restart_systemd_daemon()
                _print_live_daemon()
                _print_autostart_status(status)
                return 0
            step("Restarting daemon")
            stop_daemon()
            state = start_daemon(host=args.host, port=args.port, default_workdir=default_workdir)
            ok("Daemon restarted")
            _print_daemon_state(state)
            return 0
        if args.action == "logs":
            text = tail_daemon_log(tail=args.tail, stderr=args.stderr)
            if text:
                print(text)
            return 0
    except (AutostartError, DaemonSupervisorError) as exc:
        error(str(exc))
        return 1
    except Exception as exc:
        error(str(exc))
        return 1
    return 1


def _installed_version() -> str:
    from . import __version__

    return __version__


def _print_live_daemon() -> None:
    """Render the running daemon's state block; used by the systemd branches."""

    from .daemon_supervisor import daemon_status

    live = daemon_status()
    if live.running:
        ok("Daemon running")
        _print_daemon_state(live.state, live=True)
        _warn_on_version_skew(live.state)
    else:
        fail("Daemon not running")


def _print_daemon_state(state: Dict[str, Any], live: bool = False) -> None:
    from .daemon_supervisor import count_running_sessions

    pairs = [
        ("version", state.get("version") or _installed_version()),
        ("pid", state.get("pid")),
    ]
    if live:
        pairs.append(("uptime", _format_uptime(state.get("started_at"))))
        sessions = count_running_sessions(state)
        if sessions is not None:
            plural = "s" if sessions != 1 else ""
            pairs.append(("sessions", f"{sessions} active session{plural}"))
    pairs.extend(
        (key, state.get(key)) for key in ("server_url", "mcp_url", "data_dir", "daemon_log_path")
    )
    print_kv(pairs)


def _warn_on_version_skew(state: Dict[str, Any]) -> None:
    daemon_version = state.get("version")
    installed = _installed_version()
    if daemon_version and daemon_version != installed:
        warn(
            f"daemon runs {daemon_version} but {installed} is installed; "
            "apply the upgrade with: agent-collab daemon restart"
        )


def _format_uptime(started_at: Any) -> Optional[str]:
    from datetime import datetime, timezone

    try:
        started = datetime.fromisoformat(str(started_at))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 0:
        return None
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _print_autostart_status(status) -> None:
    if status.installed and status.enabled and status.active and status.healthy:
        ok("Daemon autostart enabled and healthy")
    elif not status.installed:
        info("Daemon autostart not installed")
    else:
        fail("Daemon autostart is not healthy")
    print_kv(
        [
            ("version", _installed_version()),
            ("unit", status.unit_path),
            ("installed", str(status.installed).lower()),
            ("enabled", str(status.enabled).lower()),
            ("active", str(status.active).lower()),
            ("healthy", str(status.healthy).lower()),
            ("definition_current", str(status.definition_current).lower()),
            ("detail", status.detail or None),
        ]
    )


def _main_sessions(argv) -> int:
    from .api_schema import PruneSessionsRequestModel

    parser = build_sessions_parser()
    args = parser.parse_args(argv)
    try:
        raw: Dict[str, Any] = {"apply": bool(args.apply), "keep": args.keep}
        if args.older_than is not None:
            raw["older_than"] = args.older_than
        # Validate locally through the shared wire DTO so a bad duration or
        # keep count fails with the same message the daemon would give.
        payload = PruneSessionsRequestModel.from_dict(raw).to_dict()
        result = _client(args.server_url).prune_sessions(payload)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        _print_prune_result(result)
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _print_prune_result(result) -> None:
    # ``result`` is a PruneResultModel from the typed client.
    mode = "apply" if result.apply else "preview"
    print(f"mode: {mode}")
    print(f"cutoff: {result.cutoff}")
    print(f"keep: {result.keep}")
    print(f"candidates: {result.candidates}  pruned: {result.pruned}  failed: {result.failed}")
    label = "bytes reclaimed" if result.apply else "bytes to reclaim"
    print(f"{label}: {result.bytes_reclaimed}")
    if result.unparseable_records:
        print(f"unparseable index records (kept): {result.unparseable_records}")
    for detail in result.sessions:
        effective = f" ended {detail.effective_at}" if detail.effective_at else ""
        print(f"{detail.disposition:<21} {detail.session_id} [{detail.status}]{effective}")
        for path in detail.removed_files:
            verb = "removed" if result.apply else "would remove"
            print(f"  {verb}: {path}")
        for entry in detail.preserved_files:
            print(f"  preserved: {entry.get('path', '')} ({entry.get('reason', '')})")
        if detail.error:
            print(f"  error: {detail.error}")
    if not result.apply:
        if result.candidates:
            print(f"preview only; rerun with --apply to delete {result.candidates} session(s)")
        else:
            print("nothing to prune")


def _main_config(argv) -> int:
    from .config import DEFAULT_CONFIG_PATH, load_config, render_user_config
    from .config_migrations import CURRENT_CONFIG_SCHEMA
    from .paths import AgentCollabHome

    parser = argparse.ArgumentParser(
        prog="agent-collab config",
        description="Inspect the effective merged configuration (built-in defaults, user "
        "config, safe project config) from local files — works without the daemon — "
        "or initialize the user config. For live backend health use "
        "'agent-collab options'.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    show = subparsers.add_parser("show", help="Print the effective merged config for a workdir.")
    show.add_argument(
        "--workdir", type=Path, default=Path("."), help="Project root whose config to resolve."
    )
    init = subparsers.add_parser(
        "init", help="Create a user config with explicit backend enablement policy."
    )
    init.add_argument("--force", action="store_true", help="Replace an existing user config.")
    args = parser.parse_args(argv)

    try:
        home = AgentCollabHome.resolve()
        if args.action == "init":
            import secrets

            from .paths import atomic_write_private_text

            if home.config_path.exists() and not args.force:
                raise ValueError(
                    f"user config already exists: {home.config_path} (pass --force to replace it)"
                )
            home.root.mkdir(parents=True, exist_ok=True)
            atomic_write_private_text(
                home.config_path, render_user_config(token=secrets.token_urlsafe(32))
            )
            print(f"created user config: {home.config_path}")
            print(
                "note: this file now holds the daemon bearer token; keep it owner-only "
                "and never commit or share it"
            )
            return 0
        workdir = args.workdir.expanduser().resolve()
        config = load_config(workdir, home=home)
        print(f"schema_version: {CURRENT_CONFIG_SCHEMA}")
        print(f"workdir: {workdir}")
        print(f"built_in_config: {DEFAULT_CONFIG_PATH}")
        print(f"user_config: {home.config_path}{'' if home.config_path.exists() else ' (missing)'}")
        loaded = [str(path) for path in config.loaded_paths]
        print(f"loaded_paths: {', '.join(loaded) if loaded else '(built-in defaults only)'}")
        print(
            f"sessions: retention_days={config.sessions.retention_days} "
            f"cleanup_interval_hours={config.sessions.cleanup_interval_hours}"
        )
        roots = config.workdir.restrict_workdir_roots
        rendered_roots = ", ".join(str(path) for path in roots) if roots else "(unrestricted)"
        print("restrict_workdir_roots: " + rendered_roots)
        for warning in config.warnings:
            print(f"warning {warning['path']}: {warning['message']}")
        for agent_id, agent in sorted(config.agents.items()):
            command = " ".join([agent.command or ""] + list(agent.args)).strip()
            enabled = "" if agent.enabled else " (disabled)"
            print(
                f"agent {agent_id}: type={agent.type} backend={agent.backend or 'cli'} "
                f"command={command!r}{enabled}"
            )
            details = []
            if agent.name:
                details.append(f"name={agent.name!r}")
            if agent.cwd:
                details.append(f"cwd={agent.cwd!r}")
            if agent.timeout is not None:
                details.append(f"timeout={agent.timeout}")
            if agent.env:
                # Env values may carry credentials; show only the key names.
                details.append(f"env_keys={','.join(sorted(agent.env))}")
            if details:
                print(f"  {' '.join(details)}")
            for name, value in sorted(agent.backend_config.items()):
                print(f"  backend {agent.backend or 'cli'} config {name} = {value!r}")
            for option, value in sorted(agent.options.items()):
                print(f"  backend {agent.backend or 'cli'} option {option} = {value!r}")
        from .backends import registered_backend_names
        from .config import backend_policy

        for name in registered_backend_names():
            policy = backend_policy(config, name)
            print(f"backend {name}: enabled={str(policy.enabled).lower()} source={policy.source}")
        for workflow_id, workflow in sorted(config.workflows.items()):
            print(f"workflow {workflow_id}: {' -> '.join(workflow.sequence)}")
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_list(argv) -> int:
    parser = build_client_parser("agent-collab list", "List daemon sessions.")
    args = parser.parse_args(argv)
    try:
        sessions = _client(args.server_url).list_sessions().sessions
        print(f"{'SESSION_ID':<24} {'STATUS':<11} {'WORKFLOW':<14} {'WORKDIR':<40} AGENTS")
        for session in sessions:
            print(
                f"{session.session_id:<24} {session.status:<11} "
                f"{session.workflow:<14} {session.workdir:<40} "
                f"{_format_agents_summary(session.settings)}"
            )
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_status(argv) -> int:
    parser = build_session_parser("agent-collab status", "Show daemon session status.")
    args = parser.parse_args(argv)
    try:
        _print_session(_client(args.server_url).get_session(args.session_id))
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_events(argv) -> int:
    from .events import Event
    from .terminal import print_event

    parser = build_events_parser()
    args = parser.parse_args(argv)
    try:
        client = _client(args.server_url)
        batch = (
            client.wait_events(args.session_id, args.cursor, args.timeout_ms)
            if args.wait
            else client.read_events(args.session_id, args.cursor)
        )
        if args.json:
            print(json.dumps(batch.to_dict(), indent=2))
        else:
            for event in batch.events:
                print_event(Event(**event.to_dict()), color=not args.no_color)
            print(f"cursor: {batch.cursor}")
            if batch.status:
                print(f"status: {batch.status}")
            has_boundary = any(
                isinstance(event.raw, dict) and "turn_outcome" in event.raw
                for event in batch.events
            )
            if batch.failure and not has_boundary:
                print(f"failure: {batch.failure.get('code')} — {batch.failure.get('message')}")
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _main_stop(argv) -> int:
    parser = build_session_parser("agent-collab stop", "Stop a daemon session.")
    args = parser.parse_args(argv)
    try:
        _print_session(_client(args.server_url).stop_session(args.session_id))
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _print_session(session) -> None:
    # ``session`` is a SessionStateModel from the typed client.
    for key in ("session_id", "status", "workflow", "workdir", "jsonl_path", "markdown_path"):
        print(f"{key}: {getattr(session, key)}")
    if session.failure:
        failure = session.failure
        turn = f" {failure.get('turn_id')}" if failure.get("turn_id") else ""
        print(f"failure{turn}: {failure.get('code')} — {failure.get('message')}")
    settings = session.settings
    if not settings:
        return
    sequence = (settings.get("workflow") or {}).get("sequence")
    if sequence:
        print(f"sequence: {' -> '.join(sequence)}")
    for agent_id, agent in (settings.get("agents") or {}).items():
        details = [
            f"{key}={value}" for key, value in agent.items() if key not in {"command_preview"}
        ]
        print(f"agent {agent_id}: {' '.join(details)}")
        preview = agent.get("command_preview")
        if preview:
            print(f"  command_preview: {' '.join(str(part) for part in preview)}")


def _format_agents_summary(settings) -> str:
    if not isinstance(settings, dict):
        return ""
    parts = []
    for agent_id, agent in (settings.get("agents") or {}).items():
        details = [str(agent[key]) for key in ("model", "thinking_level") if key in agent]
        parts.append(f"{agent_id}({'/'.join(details)})" if details else agent_id)
    return ", ".join(parts)


def _json_object_arg(value: str, label: str) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
    subcommands = _command_handlers()
    if argv and argv[0] in subcommands:
        return subcommands[argv[0]](argv[1:])
    if argv and _looks_like_command(argv[0]):
        # A bare word that is not a known command is almost always a typo or
        # a script-only command (install, uninstall, build) — never silently
        # run it as a collaboration task against the current directory.
        error(
            f"unknown command {argv[0]!r}; expected one of: "
            + ", ".join(sorted(subcommands))
            + ". install/uninstall live in ./agent_collab.sh; developer commands in "
            "./agent_collab_dev.sh. To run a one-shot task, describe it in a "
            'sentence, e.g. agent-collab "Review my diff" — or pass any option '
            f"first for a deliberate one-word task: agent-collab --max-turns 3 {argv[0]}"
        )
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.task:
        parser.error("task is required")

    config = RefereeConfig(
        workflow=args.workflow,
        max_turns=args.max_turns,
        timeout=args.timeout,
        dry_run=args.dry_run,
        mock=args.mock,
        verbose=args.verbose,
        color=not args.no_color,
        workdir=args.workdir,
        log_dir=args.log_dir,
        session_id=args.session_id,
    )
    try:
        run_sync(args.task, config)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        error("interrupted")
        return 130
    except Exception as exc:
        error(str(exc))
        return 1
    return 0


def _looks_like_command(token: str) -> bool:
    """True for a bare single word: no option prefix, no whitespace.

    Real one-shot tasks are sentences; a lone word in command position is
    treated as an (unknown) command so typos fail loudly instead of launching
    agents. A deliberate one-word task can still be run by passing any option
    before it. Empty tokens fall through to the parser's task-required error.
    """

    return bool(token) and not token.startswith("-") and not any(char.isspace() for char in token)


def _command_handlers():
    return {
        "watch": _main_watch,
        "tui": _main_tui,
        "serve": _main_serve,
        "daemon": _main_daemon,
        "start": _main_start,
        "options": _main_options,
        "list": _main_list,
        "status": _main_status,
        "events": _main_events,
        "stop": _main_stop,
        "sessions": _main_sessions,
        "config": _main_config,
        "mcp": _main_mcp,
    }


if __name__ == "__main__":
    raise SystemExit(main())
