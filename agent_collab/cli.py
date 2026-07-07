from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict

from .config import DEFAULT_WORKFLOW
from .referee import RefereeConfig, run_sync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-collab", description="Watch Claude Code and Codex collaborate in a supervised terminal loop.")
    parser.add_argument("task", nargs="?", help="Task to send to the collaboration loop.")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="Workflow name from agent-collab config.")
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900, help="Per-agent turn timeout in seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running Claude or Codex.")
    parser.add_argument("--mock", action="store_true", help="Use simulated Claude/Codex runners.")
    parser.add_argument("--verbose", action="store_true", help="Print compact unknown stream events.")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--workdir", type=Path, default=Path("."), help="Project root used as cwd for agent subprocesses.")
    parser.add_argument("--log-dir", type=Path, help="Session log directory. Defaults to the global AGENT_COLLAB_HOME data/sessions directory.")
    parser.add_argument("--session-id", help=argparse.SUPPRESS)
    parser.add_argument("--mcp-server", action="store_true", help="Run the stdio MCP server instead of the CLI loop.")
    return parser


def build_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-collab watch", description="Watch an agent-collab JSONL session log.")
    parser.add_argument("session_or_path", nargs="?", help="Session id or path to a session JSONL log.")
    parser.add_argument("--server-url", help="Daemon URL for watching a daemon-owned session id.")
    parser.add_argument("--workdir", type=Path, help="Project root used to resolve SESSION_ID logs.")
    parser.add_argument("--log-dir", type=Path, help="Session log directory. Defaults to the global AGENT_COLLAB_HOME data/sessions directory.")
    parser.add_argument("--session-id", help="Session id to resolve under the session log directory.")
    parser.add_argument("--cursor", type=int, default=0, help="Start after this zero-based JSONL line offset.")
    parser.add_argument("--no-follow", action="store_true", help="Print current events and exit instead of following.")
    parser.add_argument("--wait-ms", type=int, default=30000, help="Daemon long-poll timeout while following.")
    parser.add_argument("--no-color", action="store_true")
    return parser


def build_serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-collab serve", description="Run the local agent-collab daemon.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workdir", type=Path, default=Path("."), help=argparse.SUPPRESS)
    parser.add_argument("--session-log-dir", type=Path, help=argparse.SUPPRESS)
    return parser


def build_start_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-collab start", description="Start a daemon-owned collaboration session.")
    parser.add_argument("task")
    parser.add_argument("--server-url")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--workdir", type=Path, default=Path("."))
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex-options", help="JSON object for typed Codex start options.")
    parser.add_argument("--claude-options", help="JSON object for typed Claude start options.")
    parser.add_argument("--watch", action="store_true", help="Start the session and immediately watch its transcript.")
    parser.add_argument("--watch-wait-ms", type=int, default=30000, help="Long-poll timeout while watching.")
    parser.add_argument("--no-color", action="store_true")
    return parser


def build_daemon_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-collab daemon", description="Manage the global background server.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    start = subparsers.add_parser("start", help="Start the global background server.")
    _add_daemon_default_workdir(start)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)

    subparsers.add_parser("status", help="Show daemon status.")

    stop = subparsers.add_parser("stop", help="Stop the daemon.")

    restart = subparsers.add_parser("restart", help="Restart the daemon.")
    _add_daemon_default_workdir(restart)
    restart.add_argument("--host", default="127.0.0.1")
    restart.add_argument("--port", type=int, default=8765)

    logs = subparsers.add_parser("logs", help="Print daemon logs.")
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--stderr", action="store_true", help="Read daemon.stderr.log instead of daemon.log.")
    return parser


def _add_daemon_default_workdir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Default workdir for sessions that do not pass one explicitly. Never affects daemon runtime paths.",
    )


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
    parser.add_argument("--wait", action="store_true", help="Long-poll until events are available or timeout elapses.")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--json", action="store_true", help="Print raw JSON response instead of transcript lines.")
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
            watch_jsonl(path, follow=not args.no_follow, start_cursor=args.cursor, color=not args.no_color)
        else:
            session_id = args.session_id or args.session_or_path or _latest_daemon_session_id(args.server_url)
            _watch_daemon_session(
                session_id,
                server_url=args.server_url,
                cursor=args.cursor,
                follow=not args.no_follow,
                wait_ms=args.wait_ms,
                color=not args.no_color,
            )
    except KeyboardInterrupt:
        print("\nERROR   interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
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


def _watch_daemon_session(session_id: str, server_url, cursor: int, follow: bool, wait_ms: int, color: bool) -> None:
    from .events import Event
    from .terminal import print_event

    client = _client(server_url)
    current = max(0, int(cursor))
    while True:
        result = (
            client.wait_events(session_id, current, wait_ms)
            if follow
            else client.read_events(session_id, current)
        )
        for payload in result.get("events", []):
            print_event(Event(**payload), color=color)
        current = int(result.get("cursor", current))
        if not follow:
            return
        state = client.get_session(session_id)
        if state.get("status") in {"done", "failed", "stopped"} and not result.get("events"):
            return


def _latest_daemon_session_id(server_url) -> str:
    sessions = _client(server_url).list_sessions().get("sessions", [])
    if not sessions:
        raise ValueError("no daemon sessions found")
    latest = max(sessions, key=lambda item: (item.get("updated_at") or item.get("created_at") or "", item.get("session_id") or ""))
    session_id = latest.get("session_id")
    if not session_id:
        raise ValueError("latest daemon session did not include a session_id")
    return str(session_id)


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
        codex_options = _json_object_arg(args.codex_options, "--codex-options")
        claude_options = _json_object_arg(args.claude_options, "--claude-options")
        result = _client(args.server_url).start_session(
            {
                "task": args.task,
                "workflow": args.workflow,
                "workdir": str(args.workdir.expanduser().resolve()),
                "max_turns": args.max_turns,
                "timeout": args.timeout,
                "mock": args.mock,
                "dry_run": args.dry_run,
                "codex_options": codex_options,
                "claude_options": claude_options,
            }
        )
        _print_session(result)
        if args.watch:
            print("")
            _watch_daemon_session(
                result["session_id"],
                server_url=args.server_url,
                cursor=0,
                follow=True,
                wait_ms=args.watch_wait_ms,
                color=not args.no_color,
            )
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 0


def _main_daemon(argv) -> int:
    from .daemon_supervisor import DaemonSupervisorError, daemon_status, start_daemon, stop_daemon, tail_daemon_log

    parser = build_daemon_parser()
    args = parser.parse_args(argv)
    default_workdir = args.workdir.expanduser().resolve() if getattr(args, "workdir", None) else None
    try:
        if args.action == "start":
            state = start_daemon(host=args.host, port=args.port, default_workdir=default_workdir)
            print(f"started agent-collab daemon pid {state['pid']}")
            print(f"server_url: {state['server_url']}")
            print(f"mcp_url: {state['mcp_url']}")
            print(f"data_dir: {state['data_dir']}")
            return 0
        if args.action == "status":
            status = daemon_status()
            print(status.message)
            if status.state:
                for key in ("server_url", "mcp_url", "data_dir", "daemon_log_path", "daemon_stderr_path"):
                    if key in status.state:
                        print(f"{key}: {status.state[key]}")
            return 0 if status.running else 1
        if args.action == "stop":
            print(stop_daemon().message)
            return 0
        if args.action == "restart":
            stop_daemon()
            state = start_daemon(host=args.host, port=args.port, default_workdir=default_workdir)
            print(f"restarted agent-collab daemon pid {state['pid']}")
            print(f"server_url: {state['server_url']}")
            print(f"mcp_url: {state['mcp_url']}")
            return 0
        if args.action == "logs":
            text = tail_daemon_log(tail=args.tail, stderr=args.stderr)
            if text:
                print(text)
            return 0
    except DaemonSupervisorError as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 1


def _main_list(argv) -> int:
    parser = build_client_parser("agent-collab list", "List daemon sessions.")
    args = parser.parse_args(argv)
    try:
        sessions = _client(args.server_url).list_sessions().get("sessions", [])
        print(f"{'SESSION_ID':<32} {'STATUS':<8} WORKDIR")
        for session in sessions:
            print(f"{session.get('session_id', ''):<32} {session.get('status', ''):<8} {session.get('workdir', '')}")
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 0


def _main_status(argv) -> int:
    parser = build_session_parser("agent-collab status", "Show daemon session status.")
    args = parser.parse_args(argv)
    try:
        _print_session(_client(args.server_url).get_session(args.session_id))
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 0


def _main_events(argv) -> int:
    from .events import Event
    from .terminal import print_event

    parser = build_events_parser()
    args = parser.parse_args(argv)
    try:
        client = _client(args.server_url)
        result = (
            client.wait_events(args.session_id, args.cursor, args.timeout_ms)
            if args.wait
            else client.read_events(args.session_id, args.cursor)
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for payload in result.get("events", []):
                print_event(Event(**payload), color=not args.no_color)
            print(f"cursor: {result.get('cursor', args.cursor)}")
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 0


def _main_stop(argv) -> int:
    parser = build_session_parser("agent-collab stop", "Stop a daemon session.")
    args = parser.parse_args(argv)
    try:
        _print_session(_client(args.server_url).stop_session(args.session_id))
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 0


def _print_session(session) -> None:
    for key in ("session_id", "status", "workdir", "jsonl_path", "markdown_path"):
        if key in session:
            print(f"{key}: {session[key]}")


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
    subcommands = {
        "watch": _main_watch,
        "serve": _main_serve,
        "daemon": _main_daemon,
        "start": _main_start,
        "list": _main_list,
        "status": _main_status,
        "events": _main_events,
        "stop": _main_stop,
    }
    if argv and argv[0] in subcommands:
        return subcommands[argv[0]](argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mcp_server:
        from .mcp_server import serve

        serve()
        return 0
    if not args.task:
        parser.error("task is required unless --mcp-server is used")

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
        print("\nERROR   interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
