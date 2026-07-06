# Stage 4.25: Foreground Streamable HTTP server

## Purpose

Make `agent-collab serve` the single foreground server process that all clients connect to.

This stage focuses on plumbing, not UI. The server should own sessions, expose the existing CLI HTTP API, and expose an MCP Streamable HTTP endpoint from the same process.

Do not daemonize yet. Running in a terminal is useful while the protocol is still changing because connection events, request logs, MCP tool calls, and session lifecycle logs are visible.

## Completion notes

Implemented:

- `POST /mcp` for Streamable HTTP JSON requests.
- `initialize`, `tools/list`, and `tools/call`.
- `202 Accepted` with no body for MCP notifications and client responses.
- Non-local `Origin` rejection for all `/mcp` methods.
- `MCP-Protocol-Version` validation for supported protocol versions.
- `GET /mcp` returns `405 Method Not Allowed`; SSE is still out of scope.
- Shared `SessionManager` use between `/mcp` and the normal session HTTP routes.

## Target shape

```text
agent-collab serve
  owns SessionManager
  exposes CLI/session HTTP API
  exposes MCP Streamable HTTP endpoint
  starts Claude/Codex subprocesses
  writes JSONL/Markdown logs
```

Clients connect to the same foreground server:

```text
human CLI watch/start/list/status
        |
        v
http://127.0.0.1:8765
        ^
        |
MCP clients over Streamable HTTP
```

## User experience

Start the server:

```bash
agent-collab serve
```

or during local development:

```bash
python3 -m agent_collab.cli serve
```

Use existing CLI clients:

```bash
agent-collab start --workdir /repo --watch "Task"
agent-collab list
agent-collab watch
agent-collab stop SESSION_ID
```

Configure MCP clients to use:

```text
http://127.0.0.1:8765/mcp
```

## HTTP shape

Keep:

```text
GET /health
```

Keep the existing session routes, or move them under `/api` only if the change stays small and all callers are updated:

```text
POST /sessions
GET  /sessions
GET  /sessions/{session_id}
GET  /sessions/{session_id}/events?cursor=N
GET  /sessions/{session_id}/events/wait?cursor=N&timeout_ms=30000
GET  /sessions/{session_id}/transcript
POST /sessions/{session_id}/stop
```

Add:

```text
POST /mcp
```

For this stage, JSON responses are enough. Do not implement SSE streaming unless JSON-only responses block client compatibility.

## MCP behavior

Implement MCP Streamable HTTP enough for tools over HTTP POST:

- `initialize`
- `tools/list`
- `tools/call`

Expose the existing tools:

```text
agent_collab_start
agent_collab_list_sessions
agent_collab_status
agent_collab_read_events
agent_collab_wait_events
agent_collab_read_transcript
agent_collab_stop
```

`/mcp` must call the in-process `SessionManager` directly.

It must not:

- spawn `agent_collab.mcp_server`,
- spawn a second daemon,
- own a separate session registry,
- block forever for a long-running collaboration.

Long-running observation remains cursor-based:

```json
{
  "session_id": "daemon-abc",
  "cursor": 10,
  "timeout_ms": 30000
}
```

## Refactor plan

1. Extract MCP tool schema and dispatch logic from `agent_collab/mcp_server.py` into a reusable module, for example `agent_collab/mcp_tools.py`.
2. Make the extracted dispatcher accept a `SessionManager`.
3. Keep the stdio MCP adapter working only if it remains simple; it can call the same extracted tool layer through the daemon client or a small adapter.
4. Add `/mcp` handling in `agent_collab/server_http.py`.
5. Ensure `/mcp` and the session HTTP routes share the same `SessionManager` instance.
6. Update `README.md` with the foreground server and Streamable HTTP MCP URL.

## Foreground server logs

`serve` should print useful operational logs while it is running:

- startup address,
- request method/path,
- MCP `initialize`,
- MCP `tools/list`,
- MCP `tools/call` tool name,
- session started,
- session done/failed/stopped,
- request errors.

Do not print every transcript event by default. Transcript output belongs in `agent-collab watch`, JSONL logs, Markdown logs, and MCP event reads.

## Security

Defaults:

- bind to `127.0.0.1`,
- do not expose `0.0.0.0`,
- allow absent `Origin` for local CLI and agent clients,
- allow localhost origins,
- reject non-local origins for `/mcp`.

Auth is out of scope unless it stays very small and optional. A stronger auth/token model belongs in Stage 5 hardening.

## Tests

Add tests for:

- `POST /mcp` `initialize`,
- `POST /mcp` `tools/list`,
- `POST /mcp` `tools/call` with `agent_collab_start`,
- MCP-started session is visible through normal session listing,
- MCP-started session can be watched through existing event endpoints,
- non-local `Origin` is rejected,
- existing CLI/session HTTP tests still pass.

Run:

```bash
python3 -m unittest discover -s tests
```

## Live smoke

Start the foreground server:

```bash
python3 -m agent_collab.cli serve
```

Verify the normal client path:

```bash
python3 -m agent_collab.cli list
python3 -m agent_collab.cli start --mock --watch --workdir . "Smoke test"
```

Verify MCP over HTTP:

```text
POST http://127.0.0.1:8765/mcp initialize
POST http://127.0.0.1:8765/mcp tools/list
POST http://127.0.0.1:8765/mcp tools/call agent_collab_start
```

Then confirm the MCP-started session appears in:

```bash
python3 -m agent_collab.cli list
python3 -m agent_collab.cli watch
```

## Out of scope

- TUI watch.
- Daemonization.
- systemd, launchd, pidfiles, background fork, service management.
- SSE streaming unless required for client compatibility.
- Remote/public deployment.
- Full auth model.

## Acceptance criteria

- `agent-collab serve` is the only required long-running process.
- Existing CLI clients still work against the foreground server.
- MCP clients can use `http://127.0.0.1:8765/mcp`.
- MCP tool calls use the same live `SessionManager` as the CLI HTTP routes.
- A session started through MCP can be listed and watched through the CLI.
- Server logs show connection/request/session lifecycle activity without dumping full transcripts.
