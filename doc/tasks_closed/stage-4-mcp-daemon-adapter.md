# Stage 4: MCP daemon adapter

## Purpose

Change the MCP server from a session owner into a thin adapter over the daemon.

This lets Codex, Claude, or another MCP-capable agent join the same live collaboration that a human watches through the CLI.

## Current MCP behavior

The current prototype MCP server starts an `agent-collab` subprocess per session and polls that subprocess's logs.

That works for a prototype but has limitations:

- The MCP process owns session startup.
- Human transcript output is not visible on MCP stdout because stdout is protocol traffic.
- Sessions are only visible to that MCP process.

## Target MCP behavior

The MCP server talks to the daemon HTTP API.

```text
Codex/Claude MCP client
        |
        v
agent_collab.mcp_server
        |
        v
agent-collab daemon
        |
        v
live sessions and logs
```

## Tools

Keep existing tools:

```text
agent_collab_start
agent_collab_status
agent_collab_read_events
agent_collab_read_transcript
agent_collab_stop
```

Add:

```text
agent_collab_list_sessions
agent_collab_wait_events
```

`agent_collab_wait_events` is the key tool for agent-side watching:

```json
{
  "session_id": "abc",
  "cursor": 10,
  "timeout_ms": 30000
}
```

Response:

```json
{
  "cursor": 14,
  "events": [...]
}
```

If no events arrive before the timeout:

```json
{
  "cursor": 10,
  "events": []
}
```

## MCP server configuration

Codex config can still use stdio MCP:

```toml
[mcp_servers.agent_collab]
command = "python3"
args = ["-m", "agent_collab.mcp_server"]
cwd = "/home/user/projects/agent_collab"
env = { PYTHONPATH = "/home/user/projects/agent_collab", AGENT_COLLAB_SERVER = "http://127.0.0.1:8765" }
startup_timeout_sec = 10
tool_timeout_sec = 60
enabled = true
```

The daemon must already be running, or the MCP server can optionally auto-start it in a later stage.

## Error handling

The MCP adapter should return clear errors for:

- Daemon unreachable.
- Unknown session id.
- Invalid cursor.
- Session failed.
- Tool timeout too low for requested `wait_events` timeout.

## Tests

Add tests for:

- MCP `tools/list` includes the new tools.
- MCP start/status/read calls map to daemon client methods.
- `wait_events` maps to the daemon wait endpoint.
- Daemon-unreachable errors are returned as tool content, not server crashes.

## Acceptance criteria

- An MCP client can start a session.
- A CLI user can watch the same session id.
- An MCP client can poll or long-poll events by cursor.
- The MCP server no longer owns live referee execution.
