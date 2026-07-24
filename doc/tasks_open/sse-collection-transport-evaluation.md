# Evaluate SSE as a collection mechanism

**Status:** Open — root cause identified and measured; transport fix not
implemented.

**Created:** 2026-07-24.

**Issue:** [#49](https://github.com/lauriparviainen/agent_collab/issues/49)

Extracted from the collection-primitive re-evaluation in
`subagent-delegation-and-thread-continuity` (#47).

## Question

Can Server-Sent Events make outcome collection non-blocking — so the calling
agent is never frozen inside a tool call while a delegated session runs?

The requirement is absolute, not a cost trade-off: an agent that is
unresponsive to its own user for the length of a delegated session is not
acceptable, whichever tool it calls.

## Root cause, measured

`agent_collab_wait_result` and `agent_collab_wait_events` both block
server-side and send nothing until they have something to say. Over the
Streamable HTTP transport that is exactly the pattern one documented client
timer kills.

Claude Code documents, for HTTP, SSE, and claude.ai connector servers only, a
**per-request timer covering each request through to the server's first
response byte**. It is 60 seconds unless the per-server `timeout` field or
`MCP_TOOL_TIMEOUT` is set to 60 s or more, in which case it rises to that
value. Stdio and WebSocket servers have no per-request timer.

Measured on 2026-07-24, Claude Code 2.1.219, agent-collab connected as an
`http` MCP server with no `timeout` field configured:

| Call | Result |
| --- | --- |
| `wait_events(timeout_ms=120000)` on a parked session with no new events | client error "The operation timed out" after ~60–70 s |
| `wait_events(timeout_ms=45000)`, same session | normal heartbeat response, empty `events` |

So the cliff is the documented 60 s first-byte timer, and it is a property of
the transport, not of which tool is called: `wait_result` hits it identically,
and `wait_events` only looks safe because its default bound (30 s) happens to
sit under the timer. This is the same failure recorded live in #47 on
2026-07-24 (`timeout_ms` of 120000 and 600000 dying client-side).

## Why that timer is the whole problem

Claude Code v2.1.212+ moves a main-conversation MCP tool call that is **still
running after two minutes** into a background task: Claude receives the task id
immediately and keeps working, and the result arrives as a task notification
when the call settles. That is precisely the non-blocking collection the
requirement asks for, already implemented on the client side.

It never fires for agent-collab because the 60 s first-byte timer kills the
call at 60 s — before the two-minute backgrounding threshold is reached. The
two facts interlock: **defusing the first-byte timer is the precondition for
client auto-backgrounding to ever trigger.** #47 recorded that backgrounding
"did not trigger" in the live session; this is why.

Related documented limits that shape any fix:

- **Wall-clock limit** per tool call: the per-server `timeout` field (ms) in
  the MCP server entry, overriding `MCP_TOOL_TIMEOUT`; an unset
  `MCP_TOOL_TIMEOUT` defaults to about 28 hours. Progress notifications do not
  extend it. The limits still apply while a call runs in the background.
- **Idle timeout**: a call whose server sends no response and no progress
  notification for the idle window aborts. Default 5 minutes for HTTP, SSE,
  WebSocket, and connector servers; 30 minutes for stdio; configurable with
  `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (ms, `0` disables). A per-server
  `timeout` of at least 1000 also acts as a floor on the idle timeout
  (v2.1.203+).
- **Backgrounding controls**: `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` changes the
  two-minute threshold (`0` disables); `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
  disables it along with all background-task features. Subagent calls are never
  backgrounded; in non-interactive `-p` mode backgrounding needs
  `CLAUDE_AUTO_BACKGROUND_TASKS=1`; a call waiting on an open elicitation
  dialog is deferred.

All of the above is documented client behavior read on 2026-07-24 and version-
gated in the docs; re-verify on client upgrades rather than treating it as
permanent.

## Two ways past the first byte

1. **Client configuration.** Add `"timeout": 600000` to the agent-collab entry
   in the MCP server config. Per the documented rules this raises the
   per-request timer to 600 s, sets the wall-clock cap to 600 s, and floors the
   idle timeout at 600 s. Zero code, but it is per-user, per-client setup that
   every consumer must repeat, and it does nothing for clients without an
   equivalent knob.
2. **SSE response framing — the transport fix.** Answer a long-running tool
   call with `text/event-stream` and emit the first byte immediately (stream
   headers plus an opening event or comment), then keep the stream alive with
   periodic events until the result is ready. The first-byte timer is satisfied
   by the stream opening rather than by the settled answer, and periodic events
   are what the idle window measures. This needs nothing from the user, which
   is why it is the product answer rather than a workaround.

With either in place the call survives past two minutes, the client backgrounds
it, and the calling agent is free while the delegated session runs — the
requirement met without a poll loop at all.

## Codex is a first-class target, and it changes the design

The transport must be designed for Codex as well as Claude Code, not verified
on Codex afterwards. What Codex documents (read 2026-07-24; local CLI 0.145.0):

- Remote MCP servers are **Streamable HTTP** servers, configured with a `url`
  under `[mcp_servers.<id>]`, with bearer-token, OAuth, or ChatGPT session
  auth. That is the same transport agent-collab already serves, so POST-scoped
  SSE is within the protocol Codex speaks.
- `tool_timeout_sec` — "Timeout (seconds) for the server to run a tool",
  **default 60** — and `startup_timeout_sec`, default 10. A later upstream
  change raises the default tool timeout; verify against the pinned Codex
  version rather than assuming.
- Nothing in the Codex documentation describes SSE response handling, progress
  notifications, or moving a long tool call to a background task.

Two consequences for the design:

1. **The 60 s cliff is not Claude-Code-specific.** Codex's default per-tool
   timeout is also 60 s, so `wait_result`'s current 60000 ms default sits
   exactly on both clients' default limit. A recommended bound below it
   (~45 s) is the correct no-configuration default for both, and per-server
   configuration (`timeout` in Claude Code, `tool_timeout_sec` in Codex) is the
   documented way to raise it in each.
2. **The design must not assume client backgrounding.** Auto-backgrounding is
   documented by Claude Code only. If Codex has no equivalent, a long streamed
   call there still occupies the thread for its whole duration — SSE would buy
   survivability and visible progress, but not responsiveness. So the transport
   work must degrade honestly: SSE keeps the call alive and carries progress
   everywhere it is supported, while true non-blocking collection depends on a
   per-client wake path (Claude Code backgrounding today; a Codex equivalent,
   the optional GET stream, or channels if they prove out). Guidance must state
   which clients deliver which of the two, instead of promising responsiveness
   uniformly.

Probe (e) below is therefore a design input, not a validation step: whether
Codex's client resets `tool_timeout_sec` on stream activity or treats it as a
hard wall clock decides whether streaming is worth anything there at all.

## What must be verified, not assumed

The mechanism above is read from client documentation plus one measured
failure. Each of these is a separate empirical question:

| # | Probe | Why it matters |
| --- | --- | --- |
| a | Does the per-request timer treat SSE response headers / the first event as the "first response byte"? | The entire transport fix rests on this reading |
| b | With the timer defused (config or SSE), does auto-backgrounding actually fire for an HTTP MCP server, and does the result arrive as a task notification? | This is the responsiveness payoff; needs a client restart to test |
| c | Do periodic SSE events reset the idle window, or does only a protocol-level `notifications/progress` count? | Decides what the keep-alive must carry |
| d | Does lowering `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` background the call sooner, and how low is usable? | Decides how quickly the agent gets free |
| e | Same questions on the Codex CLI, its VS Code extension, and the Codex desktop app: does it accept a `text/event-stream` response to a POST tool call, does stream activity reset `tool_timeout_sec` or is that a hard wall clock, and does any Codex surface background a long call? | Design input, not validation — see the Codex section above |
| f | Does the optional `GET /mcp` stream get opened by any target client, and is a server-initiated notification model-visible? | Only worth building if a client both consumes it and wakes the model |

Instrument at the HTTP boundary and record client versions with every result;
these are product behaviors that change between releases.

## What is and is not deprecated

Keep this distinction precise. What MCP deprecated is the 2024-11-05
two-endpoint **HTTP+SSE transport**: the client opens an SSE endpoint, receives
an `endpoint` event naming a separate POST target, then uses those split
channels.

SSE framing itself remains part of modern Streamable HTTP:

- every client message is a `POST` to one MCP endpoint; a request response may
  be either one `application/json` object or a `text/event-stream` stream, and
  a conforming Streamable HTTP client must accept both;
- a client may additionally open `GET` on that same endpoint for
  server-initiated requests and notifications; the server may answer `405` when
  it does not offer that optional stream; and
- SSE event ids plus `Last-Event-ID` can provide resumability and redelivery.

So agent-collab's current `POST /mcp` JSON path plus `GET /mcp -> 405` is
already valid Streamable HTTP. Adding SSE here is using more of the transport we
already speak, not migrating off a dead one. Codex documents Streamable HTTP
(plus local stdio) as its remote transport, which means POST-scoped SSE is
part of the advertised protocol there too — it does not prove that any Codex
surface opens the optional GET stream or surfaces unsolicited notifications to
the model.

## Decision criteria

Adopt POST-scoped SSE for `wait_result` if (a) and (b) hold on at least one
target client, behind content negotiation that leaves the JSON path unchanged
for clients that do not ask for a stream. Design the streaming path against
Claude Code and Codex together — content negotiation, keep-alive cadence, and
progress framing must be chosen so the same response works for a client that
backgrounds long calls and for one that does not.

Adopt the optional GET stream only if (f) shows a model-visible wake path. A
stream a client opens but never surfaces costs reconnect and redelivery state
for no behavioral gain.

If neither holds, record the reasons and fall back to documenting the client
configuration in option 1 as the supported setup — a valid, closable outcome.

## Scope notes

- Transport work touches `agent_collab/server_http.py` and the MCP adapters
  only; session semantics, `wait_result` settlement, and the answer ledger do
  not change.
- The JSON path must keep working unchanged: content negotiation, never a flag
  day.
- Resumability (`Last-Event-ID`) is an observation in (f), not a goal;
  redelivery matters only once a stream is proven useful.
- The static-token remote deployment is why this matters most: a shell-less
  client connected over Streamable HTTP has no CLI fallback, so MCP calls are
  its only collection mechanism.
