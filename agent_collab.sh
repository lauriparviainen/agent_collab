#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$script_dir"

if [[ -d "$repo_root/agent_collab" ]]; then
  export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
fi

run_cli() {
  exec python3 -m agent_collab.cli "$@"
}

print_help() {
  cat <<'EOF'
agent_collab.sh - source checkout helper for agent-collab

Usage:
  ./agent_collab.sh help
  ./agent_collab.sh serve
  ./agent_collab.sh daemon start [--workdir DIR]
  ./agent_collab.sh daemon status [--workdir DIR]
  ./agent_collab.sh daemon logs [--workdir DIR] [--tail N]
  ./agent_collab.sh daemon stop [--workdir DIR]
  ./agent_collab.sh start --mock --watch --workdir . "Task"
  ./agent_collab.sh watch [SESSION_ID]
  ./agent_collab.sh list
  ./agent_collab.sh status SESSION_ID
  ./agent_collab.sh stop SESSION_ID
  ./agent_collab.sh test
  ./agent_collab.sh smoke

Examples:
  ./agent_collab.sh smoke
  ./agent_collab.sh serve
  ./agent_collab.sh daemon start
  ./agent_collab.sh start --mock --watch --workdir . "Smoke test"
  ./agent_collab.sh watch

Most commands pass through to:
  python3 -m agent_collab.cli

Daemon helpers use --workdir . unless you pass --workdir yourself.
EOF
}

has_workdir_arg() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --workdir|--workdir=*) return 0 ;;
    esac
  done
  return 1
}

case "${1:-help}" in
  help|-h|--help)
    print_help
    ;;
  test)
    shift
    cd "$repo_root"
    exec python3 -m unittest discover -s tests "$@"
    ;;
  smoke)
    shift
    if (($#)); then
      run_cli --mock "$@"
    else
      run_cli --mock --workdir . "Smoke test"
    fi
    ;;
  daemon)
    if [[ "${2:-}" =~ ^(start|status|logs|stop)$ ]]; then
      action="$2"
      shift 2
      if has_workdir_arg "$@"; then
        run_cli daemon "$action" "$@"
      else
        run_cli daemon "$action" --workdir . "$@"
      fi
    else
      run_cli "$@"
    fi
    ;;
  *)
    run_cli "$@"
    ;;
esac
