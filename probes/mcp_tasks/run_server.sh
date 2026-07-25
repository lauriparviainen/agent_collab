#!/usr/bin/env bash
# Start the tracked manual MCP Tasks compatibility server.
#
#   ./run_server.sh legacy [port] [log-file]
#   ./run_server.sh extension [port] [log-file]
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mode="${1:?usage: ./run_server.sh legacy|extension [port]}"
port="${2:-48623}"
log_file="${3:-$script_dir/probe-${mode}-${port}.log}"

exec python3 "$script_dir/probe_mcp_tasks.py" \
  "$mode" "$port" --log-file "$log_file"
