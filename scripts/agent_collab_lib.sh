# shellcheck shell=bash
# Shared environment setup for the agent-collab entrypoint scripts. Sourced,
# never executed, so it intentionally has no shebang and no execute bit.
#
# Sourced by agent_collab.sh and agent_collab_dev.sh, which set $repo_root
# first. Selects a Python interpreter, enforces the minimum version, and
# exposes run_cli. Shell stays a thin dispatch layer: everything beyond
# interpreter selection lives in Python modules so behavior is testable and
# portable to other operating systems.

if [[ -z "${repo_root:-}" ]]; then
  printf 'agent_collab_lib.sh requires the caller to set repo_root\n' >&2
  exit 2
fi

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
