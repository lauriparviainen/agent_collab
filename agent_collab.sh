#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$script_dir"

if [[ -d "$repo_root/agent_collab" ]]; then
  export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
fi

default_venv="${AGENT_COLLAB_VENV:-$HOME/.agent-collab/venv}"
if [[ -n "${AGENT_COLLAB_PYTHON:-}" ]]; then
  python_bin="$AGENT_COLLAB_PYTHON"
elif [[ -x "$default_venv/bin/python" ]]; then
  python_bin="$default_venv/bin/python"
else
  python_bin=""
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      python_bin="$(command -v "$candidate")"
      break
    fi
  done
  if [[ -z "$python_bin" ]]; then
    printf 'agent-collab requires Python >= 3.10; no Python interpreter found\n' >&2
    exit 2
  fi
fi

if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  detected_version="$("$python_bin" --version 2>&1 || true)"
  printf 'agent-collab requires Python >= 3.10; selected %s (%s)\n' \
    "$python_bin" "${detected_version:-version unavailable}" >&2
  exit 2
fi

run_cli() {
  exec "$python_bin" -m agent_collab.cli "$@"
}

print_help() {
  cat <<'EOF'
agent_collab.sh - source checkout helper for agent-collab

Usage:
  ./agent_collab.sh help
  ./agent_collab.sh serve
  ./agent_collab.sh daemon start [--workdir DIR]
  ./agent_collab.sh daemon status
  ./agent_collab.sh daemon logs [--tail N]
  ./agent_collab.sh daemon stop
  ./agent_collab.sh start --mock --watch --workdir . "Task"
  ./agent_collab.sh watch [SESSION_ID]
  ./agent_collab.sh list
  ./agent_collab.sh status SESSION_ID
  ./agent_collab.sh stop SESSION_ID
  ./agent_collab.sh test
  ./agent_collab.sh integration-test [PROVIDER] [BACKEND] [--strict]
  ./agent_collab.sh smoke

Examples:
  ./agent_collab.sh smoke
  ./agent_collab.sh serve
  ./agent_collab.sh daemon start
  ./agent_collab.sh start --mock --watch --workdir . "Smoke test"
  ./agent_collab.sh watch

Most commands pass through to:
  ~/.agent-collab/venv/bin/python -m agent_collab.cli

Override the interpreter with AGENT_COLLAB_PYTHON or the environment location
with AGENT_COLLAB_VENV.

The daemon is global: runtime state lives under ~/.agent-collab/data
(override with AGENT_COLLAB_HOME). "daemon start --workdir DIR" only sets
the default workdir for sessions that do not pass one explicitly.
EOF
}

case "${1:-help}" in
  help|-h|--help)
    print_help
    ;;
  test)
    shift
    cd "$repo_root"
    exec "$python_bin" -m unittest discover -s tests -t . "$@"
    ;;
  integration-test)
    shift
    cd "$repo_root"
    exec "$python_bin" -m integration_tests "$@"
    ;;
  smoke)
    shift
    if (($#)); then
      run_cli --mock "$@"
    else
      run_cli --mock --workdir . "Smoke test"
    fi
    ;;
  *)
    run_cli "$@"
    ;;
esac
