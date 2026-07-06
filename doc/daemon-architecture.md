# Foreground server architecture

## Goal

Move `agent-collab` from a one-shot process model to a joinable session model.

The implementation is currently a foreground local server, not a background service. Keep it that way until the protocol and client shape settle.

The desired shape is:

```text
agent-collab serve
  owns sessions, subprocess lifetimes, logs, and event delivery

CLI client
  starts sessions, watches sessions, and prints human-readable transcripts

MCP interface
  exposes session control and event polling tools to Codex, Claude, or other agents
```

This keeps the human terminal UI and the agent tool API separate while letting both observe the same collaboration through a shared `session_id`.

## Current state

The prototype has:

- `agent_collab.events`: normalized event model and stream parsers.
- `agent_collab.runners`: Claude, Codex, dry-run, and mock runners.
- `agent_collab.referee`: bounded turn loop.
- `agent_collab.logging`: JSONL and Markdown session logs.
- `agent_collab.cli`: one-shot runner plus foreground server/client commands.
- `agent_collab.server_http`: local HTTP server for session control and event reads.
- `agent_collab.daemon`: in-memory `SessionManager` that owns live sessions.
- `agent_collab.client`: HTTP client used by CLI commands.
- `agent_collab.mcp_server`: stdio MCP adapter that connects to the local server.

The current MCP process no longer owns live referee execution. It is a transitional stdio adapter for MCP clients that launch servers as subprocesses.

The next target is to expose MCP Streamable HTTP directly from `agent-collab serve`, at `/mcp`, so the foreground server is the only long-running process.

## Target state

The foreground server owns live collaboration sessions. Both CLI and MCP connect to it.

```text
                 starts/watches
Human terminal -----------------> CLI client
                                      |
                                      | HTTP/local API
                                      v
                               agent-collab serve
                                      |
                    starts Claude/Codex subprocesses
                                      |
                                      v
                              JSONL/Markdown logs
                                      ^
                                      |
MCP client / Codex ---- MCP tools ----+
```

Agent availability should come from an `agent-collab` config file instead of
being hardcoded to exactly one Claude runner and one Codex runner. See
[Agent configuration](agent-configuration.md).

## Session lifecycle

1. A client asks the server to start a session.
2. The server creates a `session_id`.
3. The server creates log paths under `WORKDIR/.agent-collab/sessions/`.
4. The server runs the existing `Referee` in a background task.
5. Each emitted event is:
   - appended to in-memory session history,
   - sent to live watchers,
   - written to JSONL,
   - written to Markdown.
6. Clients watch by reading event history from a cursor and then waiting for new events.
7. Session status becomes `done`, `failed`, or `stopped`.

## Event cursor model

Every session exposes a monotonic integer cursor:

```text
cursor 0 -> before the first event
cursor 1 -> after event 0
cursor N -> after event N - 1
```

Clients call:

```text
read_events(session_id, cursor)
```

and receive:

```json
{
  "cursor": 11,
  "events": [...]
}
```

For near-streaming behavior, clients call:

```text
wait_events(session_id, cursor, timeout_ms)
```

The server returns as soon as new events exist or the timeout expires.

## Local API

The local server API binds to `127.0.0.1` by default.

Suggested endpoints:

```text
POST /sessions
GET  /sessions
GET  /sessions/{session_id}
GET  /sessions/{session_id}/events?cursor=N
GET  /sessions/{session_id}/events/wait?cursor=N&timeout_ms=30000
GET  /sessions/{session_id}/transcript
POST /sessions/{session_id}/stop
```

Planned MCP endpoint:

```text
POST /mcp
```

`/mcp` should implement MCP Streamable HTTP enough for `initialize`, `tools/list`, and `tools/call`. JSON responses are enough for the first implementation. SSE can be added later if a client requires it.

Optional later session event stream:

```text
GET /sessions/{session_id}/events/stream?cursor=N
```

That endpoint can use Server-Sent Events for CLI watch mode, but cursor-based long polling should remain the compatibility baseline.

## CLI shape

Keep one-shot mode for convenience:

```bash
agent-collab --mock --workdir /repo "task"
```

Client/server commands:

```bash
agent-collab serve
agent-collab start --mock --workdir /repo "task"
agent-collab watch SESSION_ID
agent-collab status SESSION_ID
agent-collab stop SESSION_ID
```

`watch` should also support direct file watching:

```bash
agent-collab watch /repo/.agent-collab/sessions/SESSION.jsonl
```

This makes it useful even without a running server.

## MCP shape

Current MCP shape:

```text
MCP client
  -> stdio agent_collab.mcp_server
  -> local HTTP server
  -> SessionManager
```

Target MCP shape:

```text
MCP client
  -> http://127.0.0.1:8765/mcp
  -> SessionManager
```

Tools:

```text
agent_collab_start
agent_collab_list_sessions
agent_collab_status
agent_collab_read_events
agent_collab_wait_events
agent_collab_read_transcript
agent_collab_stop
```

MCP code should not run the referee loop directly and should not own a separate session registry.

## Safety model

The server keeps the existing guardrails:

- Claude and Codex are called only as subprocesses by `agent-collab`.
- Available agents are registered explicitly in `agent-collab` config.
- Agent prompts tell agents not to spawn Claude, Codex, `agent-collab`, or other agents.
- `--workdir` controls the project root used as subprocess `cwd`.
- Command paths remain configurable.
- Timeouts and max turns are enforced by the referee.

Add server-level controls:

- Localhost bind by default.
- Optional auth token for HTTP access.
- Per-session stop support.
- Explicit allowlist for workdir roots if this becomes shared or long-running.
- No automatic broad shell permissions.

## Dependency choice

Start with the Python standard library if possible:

- `asyncio` for session tasks.
- `http.server` or a small custom server for local HTTP.
- `urllib.request` for CLI client calls.

If the stdlib server becomes awkward, add one focused dependency:

- `aiohttp` for async HTTP server and client.

Avoid adding a larger stack until the API shape is proven.
