#!/usr/bin/env bash
# Ask Claude Code to invoke the tracked MCP Tasks probe tool manually.
set -eu

port="${PROBE_PORT:-48623}"
seconds="${1:-2}"
model="${PROBE_MODEL:-claude-haiku-4-5-20251001}"
config="{\"mcpServers\":{\"tasksprobe\":{\"type\":\"http\",\"url\":\"http://127.0.0.1:${port}/mcp\"}}}"

start=$(date +%s)
set +e
output=$(timeout 180 claude -p \
  --strict-mcp-config \
  --mcp-config "$config" \
  --allowedTools "mcp__tasksprobe__delayed_echo,ToolSearch" \
  --disallowedTools "Task,Agent,Bash" \
  --model "$model" \
  --output-format json \
  "Call mcp__tasksprobe__delayed_echo yourself with seconds=${seconds} and message=\"hello from Claude\". Do not delegate. Report its exact result or exact error. Do nothing else." \
  2>&1)
status=$?
set -e

echo "##### claude MCP Tasks probe: rc=${status} elapsed=$(( $(date +%s) - start ))s"
printf '%s\n' "$output" | python3 -c '
import json
import sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print(raw[:2000])
else:
    print("is_error:", data.get("is_error"))
    print("subtype:", data.get("subtype"))
    print("result:", str(data.get("result"))[:1800])
'

exit "$status"
