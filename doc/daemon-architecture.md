# Local server architecture

## Goal

Move `agent-collab` from a one-shot process model to a joinable session model.

The implementation has both a foreground local server and a global background daemon lifecycle. `agent-collab serve` remains the foreground debugging path; `agent-collab daemon start` starts the same server model in the background as the single global daemon.

The desired shape is:

```text
agent-collab serve
  owns sessions, subprocess lifetimes, logs, and event delivery

agent-collab daemon start
  starts the same server model in the background (one global daemon)
  writes runtime state under ~/.agent-collab/data/ (AGENT_COLLAB_HOME)

CLI client
  starts sessions, watches sessions, and prints human-readable transcripts

MCP interface
  exposes session control and event polling tools to Codex, Claude, or other agents
```

This keeps the human terminal UI and the agent tool API separate while letting both observe the same collaboration through a shared `session_id`.

## Current state

The prototype has:

- `agent_collab.events`: normalized event model and stream parsers.
- `agent_collab.runners`: runner primitives (subprocess, dry-run, mock) and the registry-backed `configured_runner`.
- `agent_collab.backends`: backend registry keyed by `(agent_type, backend_id)`, capabilities, live health probes, the `cli` subprocess backend, and the extras-gated Antigravity `sdk` backend. An agent's provider (`type`) is separate from its execution mechanism (`backend`); the resolved per-agent backend map is computed once at start validation and threaded into execution.
- `agent_collab.referee`: bounded turn loop.
- `agent_collab.logging`: JSONL and Markdown session logs.
- `agent_collab.cli`: one-shot runner plus foreground server/client commands.
- `agent_collab.server_http`: local HTTP server for session control and event reads.
- `agent_collab.daemon`: in-memory `SessionManager` that owns live sessions.
- `agent_collab.client`: HTTP client used by CLI commands.
- `agent_collab.mcp_tools`: shared MCP tool schemas and dispatch.
- `agent_collab.mcp_server`: stdio MCP adapter that connects to the local server.

The current MCP process no longer owns live referee execution. MCP clients can connect directly to `agent-collab serve` at `/mcp`, and the stdio adapter remains available for clients that launch servers as subprocesses.

## Target state

The foreground server or global daemon owns live collaboration sessions. CLI and MCP connect to whichever local server is running. Each session carries its own `workdir`; the daemon's location never decides which project a session works on.

```text
                 starts/watches
Human terminal -----------------> CLI client
                                      |
                                      | HTTP/local API
                                      v
                               agent-collab serve/daemon
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
3. The server loads config from the session `workdir`, validates typed start options, and builds the effective settings confirmation (workflow sequence, per-agent options, prompt-free command previews) before creating session state. Session logs go to the global `~/.agent-collab/data/sessions/`.
4. The server runs the existing `Referee` in a background task.
5. Each emitted event is:
   - appended to in-memory session history,
   - sent to live watchers,
   - written to JSONL,
   - written to Markdown.
6. Clients watch by reading event history from a cursor and then waiting for new events.
7. Session status becomes `done`, `failed`, or `stopped`. Interactive sessions may pause in non-terminal `awaiting_input` before a terminal status. Session state is persisted to the global `session-index.json` on every change; after a daemon restart, sessions that were `running` or `awaiting_input` are reported as `interrupted`.

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
POST /options
GET  /sessions
GET  /sessions/{session_id}
GET  /sessions/{session_id}/events?cursor=N
GET  /sessions/{session_id}/events/wait?cursor=N&timeout_ms=30000
GET  /sessions/{session_id}/transcript
POST /sessions/{session_id}/stop
```

MCP endpoint:

```text
POST /mcp
GET  /mcp  -> 405 until SSE is implemented
```

`/mcp` implements the Streamable HTTP JSON POST path for `initialize`, `tools/list`, and `tools/call`. It accepts MCP notifications and client responses with HTTP `202`, validates non-local `Origin` headers on all `/mcp` methods, validates supported `MCP-Protocol-Version` headers, and returns `405 Method Not Allowed` for `GET /mcp` because SSE is not implemented yet.

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
agent-collab daemon start
agent-collab daemon status
agent-collab daemon logs --tail 100
agent-collab daemon stop
agent-collab start --mock --workdir /repo "task"
agent-collab watch SESSION_ID
agent-collab status SESSION_ID
agent-collab stop SESSION_ID
agent-collab config show --workdir /repo
```

`watch` should also support direct file watching:

```bash
agent-collab watch ~/.agent-collab/data/sessions/SESSION.jsonl
```

This makes it useful even without a running server.

## MCP shape

Preferred MCP shape:

```text
MCP client
  -> http://127.0.0.1:8765/mcp
  -> SessionManager
```

Compatibility MCP shape:

```text
MCP client
  -> stdio agent_collab.mcp_server
  -> local HTTP server
  -> SessionManager
```

Tools:

```text
agent_collab_guidance
agent_collab_describe_options
agent_collab_start
agent_collab_list_sessions
agent_collab_status
agent_collab_read_events
agent_collab_wait_events
agent_collab_read_transcript
agent_collab_stop
```

MCP code should not run the referee loop directly and should not own a separate session registry.

Before agents pass non-default model, reasoning, permission, or sandbox settings, they should call `agent_collab_describe_options` and then start sessions with typed `codex_options` and `claude_options`. Invalid options are rejected before subprocess launch with field-level feedback. `agent_collab_guidance` serves full Markdown usage guidance from `doc/mcp-guidance.md`. Start payloads use `task`, `workdir`, and `workflow`.

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

Backend dependencies follow the same rule. The base install and the default
`cli` backend stay standard-library only; a provider SDK backend is an optional,
extras-gated dependency (`antigravity-sdk = ["google-antigravity>=0.1,<1"]`) and
all SDK imports are lazy, so the SDK is never required to import, register, or
run the `cli` path.
