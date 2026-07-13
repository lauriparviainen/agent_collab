#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$script_dir"
# shellcheck source=scripts/agent_collab_lib.sh
source "$repo_root/scripts/agent_collab_lib.sh"

print_help() {
  cat <<'EOF'
agent_collab.sh - agent-collab from a source checkout

Usage:
  ./agent_collab.sh help
  ./agent_collab.sh install
  ./agent_collab.sh uninstall
  ./agent_collab.sh serve
  ./agent_collab.sh daemon start [--workdir DIR]
  ./agent_collab.sh daemon status
  ./agent_collab.sh daemon restart
  ./agent_collab.sh daemon logs [--tail N]
  ./agent_collab.sh daemon stop
  ./agent_collab.sh daemon autostart enable|status|disable
  ./agent_collab.sh start --mock --watch --workdir . "Task"
  ./agent_collab.sh watch [SESSION_ID]
  ./agent_collab.sh list
  ./agent_collab.sh status SESSION_ID
  ./agent_collab.sh stop SESSION_ID

Getting started:
  ./agent_collab.sh install     # also the upgrade path: re-run after git pull
  ./agent_collab.sh daemon start

install upgrades everything in place and restarts a running daemon, which
interrupts active sessions. uninstall removes the installation but keeps
config and session data under ~/.agent-collab.

Most commands pass through to:
  ~/.agent-collab/venv/bin/python -m agent_collab.cli

Override the interpreter with AGENT_COLLAB_PYTHON or the environment location
with AGENT_COLLAB_VENV. Developer commands (build, test, integration-test,
smoke) live in ./agent_collab_dev.sh.

The daemon is global: runtime state lives under ~/.agent-collab/data
(override with AGENT_COLLAB_HOME). "daemon start --workdir DIR" only sets
the default workdir for sessions that do not pass one explicitly.
EOF
}

case "${1:-help}" in
  help|-h|--help)
    print_help
    ;;
  install)
    shift
    cd "$repo_root"
    exec "$python_bin" -m agent_collab.user_install install \
      --repo-root "$repo_root" --venv "$default_venv" "$@"
    ;;
  uninstall)
    shift
    cd "$repo_root"
    exec "$python_bin" -m agent_collab.user_install uninstall \
      --venv "$default_venv" "$@"
    ;;
  *)
    run_cli "$@"
    ;;
esac
