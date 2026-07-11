# Local server architecture

## Shape

`agent-collab` uses a joinable session model rather than a one-shot process model: a local server owns sessions, and clients attach to them.

The implementation has both a foreground local server and a global background daemon lifecycle. `agent-collab serve` is the foreground debugging path; `agent-collab daemon start` starts the same server model in the background as the single global daemon.

The shape is:

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

## Components

- `agent_collab.events`: normalized event model and stream parsers.
- `agent_collab.runners`: runner primitives (subprocess, dry-run, mock) and the registry-backed `configured_runner`.
- `agent_collab.backends`: backend registry keyed by `(agent_type, backend_id)`, capabilities, live health probes, the `cli` subprocess backends, and the first-class Claude/Codex/Antigravity/xAI `sdk` backends (lazy-imported). An agent's provider (`type`) is separate from its execution mechanism (`backend`); the resolved per-agent backend map is computed once at start validation and threaded into execution.
- `agent_collab.referee`: bounded turn loop.
- `agent_collab.logging`: JSONL and Markdown session logs.
- `agent_collab.cli`: one-shot runner plus foreground server/client commands.
- `agent_collab.server_http`: local HTTP server for session control and event reads.
- `agent_collab.daemon`: in-memory `SessionManager` that owns live sessions.
- `agent_collab.client`: HTTP client used by CLI commands.
- `agent_collab.mcp_tools`: shared MCP tool schemas and dispatch.
- `agent_collab.mcp_server`: stdio MCP adapter that connects to the local server.

The MCP process does not own live referee execution. MCP clients can connect directly to `agent-collab serve` at `/mcp`, and the stdio adapter remains available for clients that launch servers as subprocesses.

## Ownership model

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

Agent availability comes from `agent-collab` config rather than being
hardcoded to exactly one Claude runner and one Codex runner. See
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
wait_events(session_id, cursor, timeout_ms, tool_output="summary")
```

The server returns as soon as new events exist or the timeout expires.

## Local API

The local server API binds to `127.0.0.1` by default. The shared DTOs and
`ROUTES` registry in
[`agent_collab/api_schema.py`](../agent_collab/api_schema.py) are the single
source of truth for the complete REST surface; the server dispatch table and
typed client both consume that registry. The generated human-readable and
OpenAPI outputs live under [`doc/daemon_api_doc/`](daemon_api_doc/http-api.md)
and are refreshed by `./agent_collab.sh setup`.

`POST /sessions` and `/options` require an explicit non-blank `workdir`; the
`workdir` selects project config and the session subprocess cwd.

Event and transcript reads use `tool_output=summary` by default. Tool events are
projected to one line containing their absolute cursor index, tool/argument
digest, and result size; storage remains full fidelity. A caller can retrieve a
specific payload with `read_events(cursor=EVENT_ID, limit=1,
tool_output="full")`.

MCP endpoint:

```text
POST /mcp
GET  /mcp  -> 405 until SSE is implemented
```

`/mcp` implements the Streamable HTTP JSON POST path for `initialize`, `tools/list`, and `tools/call`. It accepts MCP notifications and client responses with HTTP `202`, validates non-local `Origin` headers on all `/mcp` methods, validates supported `MCP-Protocol-Version` headers, and returns `405 Method Not Allowed` for `GET /mcp` because SSE is not implemented yet.

All HTTP routes except `GET /health` require the permanent bearer token stored
as `[daemon].token` in the user config; the daemon generates and persists it on
first start and reuses it afterwards, so it stays valid across daemon restarts
(see [Runtime layout](runtime-layout.md) for the file semantics and rotation).
`GET /health` is intentionally an open liveness probe and exposes only status,
session count, and API version. The supervisor verifies readiness against
authenticated `GET /sessions`, so a missing token or an unrelated listener
cannot satisfy startup. The config and daemon state files are owner-only, and
the daemon directory is owner-only.

This is a loopback, cross-user safety measure, not a sandbox against other
processes running as the same OS user: same-user processes can read the
owner-owned config file. Non-loopback clients must supply `AGENT_COLLAB_TOKEN`;
the client does not send the local config token to a remote server URL.

Optional later session event stream:

```text
GET /sessions/{session_id}/events/stream?cursor=N
```

That endpoint can use Server-Sent Events for CLI watch mode, but cursor-based long polling should remain the compatibility baseline.

## CLI shape

One-shot mode remains for convenience:

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

`watch` also supports direct file watching:

```bash
agent-collab watch ~/.agent-collab/data/sessions/SESSION.jsonl
```

This makes it useful even without a running server.

## MCP shape

Recommended local MCP shape (the adapter reads the config token automatically):

```text
MCP client
  -> stdio agent_collab.mcp_server
  -> authenticated local HTTP
  -> SessionManager
```

Direct MCP shape (configure the client with the permanent `[daemon].token`
value, or `AGENT_COLLAB_TOKEN` for remote servers):

```text
MCP client
  -> authenticated http://127.0.0.1:8765/mcp
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

Before agents pass non-default model, reasoning, permission, or sandbox settings,
they should call `agent_collab_describe_options` and then start sessions with the
backend-qualified `backend_options` map. Invalid options are rejected before
execution with field-level feedback. `agent_collab_guidance` serves full
Markdown usage guidance from `doc/mcp-guidance.md`.

## Safety model

The server keeps the existing guardrails:

- Claude and Codex are called only as subprocesses by `agent-collab`.
- Available agents are registered explicitly in `agent-collab` config.
- Agent prompts tell agents not to spawn Claude, Codex, `agent-collab`, or other agents.
- `--workdir` controls the project root used as subprocess `cwd`.
- Command paths remain configurable.
- Timeouts and max turns are enforced by the referee.

Server-level controls in place:

- Localhost bind by default.
- A mandatory permanent bearer token on every route except
  `GET /health` (see [Local API](#local-api)).
- Per-session stop support.
- No automatic broad shell permissions.

A possible later control is an explicit allowlist for workdir roots if the
daemon becomes shared or long-running.

## Dependency choice

The core uses the Python standard library:

- `asyncio` for session tasks.
- A small custom server on stdlib primitives for local HTTP.
- `urllib.request` for CLI client calls.

If the stdlib server becomes awkward, the planned escape hatch is one focused
dependency (`aiohttp` for async HTTP server and client), not a larger stack.

Backend dependencies follow a lazy rule. The provider SDKs (`claude-agent-sdk`,
`openai-codex`, `google-antigravity`, `xai-sdk`) install with the project (Python ≥ 3.10),
but every SDK import is lazy — done only inside a backend's `probe()`/runner — so
a missing wheel degrades to an *unavailable* backend rather than an import error,
and the default `cli` path never needs any SDK to import, register, or run.
