#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$script_dir"
# shellcheck source=scripts/agent_collab_lib.sh
source "$repo_root/scripts/agent_collab_lib.sh"

print_help() {
  cat <<'EOF'
agent_collab_dev.sh - developer commands for the agent-collab checkout

Usage:
  ./agent_collab_dev.sh help
  ./agent_collab_dev.sh build [--check] [--workdir DIR]
  ./agent_collab_dev.sh test
  ./agent_collab_dev.sh integration-test [PROVIDER_BACKEND] [--strict]
  ./agent_collab_dev.sh smoke

build validates the effective config and regenerates the daemon REST API
artifacts under doc/daemon_api_doc; build --check fails if they differ
without writing. test runs Ruff and the hermetic unittest suite.
integration-test makes credentialed model calls. smoke runs a mock session
end to end.

End-user commands (install, uninstall, daemon, sessions) live in
./agent_collab.sh.
EOF
}

case "${1:-help}" in
  help|-h|--help)
    print_help
    ;;
  build)
    shift
    cd "$repo_root"
    exec "$python_bin" -m agent_collab.project_build "$@"
    ;;
  test)
    shift
    cd "$repo_root"
    if ! "$python_bin" -m ruff --version >/dev/null 2>&1; then
      printf "Ruff is not installed in this environment. Install the dev extra:\n  %s -m pip install -e '.[dev]'\n" \
        "$python_bin" >&2
      exit 2
    fi
    "$python_bin" -m ruff check .
    "$python_bin" -m ruff format --check .
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
    printf 'unknown developer command: %s\n' "$1" >&2
    print_help >&2
    exit 2
    ;;
esac
