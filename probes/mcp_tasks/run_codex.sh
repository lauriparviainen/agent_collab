#!/usr/bin/env bash
# Ask Codex CLI to invoke the tracked MCP Tasks probe tool manually.
set -eu

port="${PROBE_PORT:-48623}"
seconds="${1:-2}"
server="{url=\"http://127.0.0.1:${port}/mcp\",tool_timeout_sec=60}"

start=$(date +%s)
set +e
output=$(timeout 180 codex exec \
  --sandbox read-only \
  --skip-git-repo-check \
  -c "mcp_servers.tasksprobe=${server}" \
  "Call delayed_echo from the tasksprobe MCP server with seconds=${seconds} and message=\"hello from Codex\". Report its exact result or exact error. Do nothing else." \
  2>&1)
status=$?
set -e

echo "##### codex MCP Tasks probe: rc=${status} elapsed=$(( $(date +%s) - start ))s"
printf '%s\n' "$output" | tail -20

exit "$status"
