# Evaluate SSE as a collection mechanism

**Status:** Open — evaluation not started.

**Created:** 2026-07-24.

**Issue:** [#49](https://github.com/lauriparviainen/agent_collab/issues/49)

Extracted from the collection-primitive re-evaluation in
`subagent-delegation-and-thread-continuity` (#47), which keeps the remaining
candidates (compact event projections, MCP channels, client auto-backgrounding).
This document owns the SSE question alone.

## Question

Can Server-Sent Events reduce the cost of collecting a session outcome — fewer
polls, live progress without a poll loop, or a wake path that does not occupy a
tool call — for the MCP clients agent-collab actually targets? The answer must
be a decision (adopt, adopt narrowly, or reject with reasons), not a protocol
survey.

The trigger is the live cost of today's collection loop: an MCP client that
kills tool calls near 60 s turns one long `wait_result` block into a chatty
heartbeat re-poll loop, and the `wait_events` streaming alternative carries far
more payload than a settled answer justifies (measured in #47). SSE is one of
four candidate escapes; it is the only one that changes the transport, so it is
evaluated separately.

## What is and is not deprecated

Keep this distinction precise; the evaluation is worthless if it conflates the
two.

What MCP deprecated is the 2024-11-05 two-endpoint **HTTP+SSE transport**: the
client opens an SSE endpoint, receives an `endpoint` event naming a separate
POST target, then uses those split channels.

SSE framing itself remains part of modern Streamable HTTP:

- every client message is a `POST` to one MCP endpoint; a request response may
  be either one `application/json` object or a `text/event-stream` stream, and
  a conforming Streamable HTTP client must accept both;
- a client may additionally open `GET` on that same endpoint for
  server-initiated requests and notifications; the server may answer `405` when
  it does not offer that optional stream; and
- SSE event ids plus `Last-Event-ID` can provide resumability and redelivery.

Therefore agent-collab's current `POST /mcp` JSON path plus `GET /mcp -> 405`
is already valid Streamable HTTP, not an obsolete transport. Adopting SSE here
means adding response streaming and/or the optional GET stream to a transport
that is already conformant — not migrating off a dead one.

## What protocol support does not prove

Codex's VS Code extension and desktop app use the shared Codex MCP host and
configuration, and its documented remote transport is Streamable HTTP (plus
local stdio), not a separately configured legacy SSE transport. That means
POST-scoped SSE is part of the advertised protocol. It does **not** prove that
either Codex surface opens the optional GET stream, exposes unsolicited
notifications to the active thread, or wakes the model outside an in-flight
tool call. Claude Code has the same unresolved product-level questions.

Two properties decide whether SSE is worth anything here, and neither follows
from the specification:

1. **Does POST-scoped SSE change the client's timeout behavior?** If the client
   kills a long tool call at ~60 s regardless of whether the response body is
   streaming, SSE buys nothing for `wait_result`: the call still occupies one
   tool call and still dies. If interim SSE events reset the client's
   inactivity timer, a single streamed `wait_result` could replace the whole
   heartbeat loop — the strongest possible outcome.
2. **Is a server-initiated notification model-visible?** GET push is useful only
   if the client consumes it *and* surfaces it to the model as something it can
   react to. A notification the client drops on the floor, or logs without
   waking the thread, is not a collection mechanism.

Do not build toward either behavior from protocol possibility alone.

## Test matrix

Test each cell separately per client; a result on one client is not evidence
about another. Clients: Claude Code (CLI and the VS Code extension separately —
they differ in how they host MCP), the Codex VS Code extension, and the Codex
desktop app.

| # | Probe | Records |
| --- | --- | --- |
| a | Long-running POST tool call answered as `application/json` vs `text/event-stream` | Does the client accept the stream; when does it consider the call complete |
| b | Interim SSE events (progress notifications) during a long POST response | Whether they reset the client's kill timer, and whether the model sees them |
| c | `GET /mcp` offered instead of `405` | Whether the client opens it, whether it reconnects after a drop, whether it sends `Last-Event-ID` |
| d | Server-initiated notification on the GET stream at settlement | Whether it reaches the model at all, whether it wakes an idle thread, or only lands when the model next calls a tool |
| e | UI responsiveness while waiting | Whether the user can interject during an in-flight streamed call |

Instrument at the HTTP boundary (request/response framing, headers, timings),
not by inference from model behavior. Record client versions with every result;
these are product behaviors that change between releases, so every finding is
dated evidence, not a permanent fact.

## Decision criteria

Adopt POST-scoped SSE for `wait_result` only if (b) shows a real timeout
extension on at least one target client, and the implementation stays behind
content negotiation so JSON-only clients are unaffected.

Adopt the optional GET stream only if (d) shows a model-visible wake path on at
least one target client. A stream the client opens but never surfaces is worse
than none: it adds a connection, reconnect handling, and redelivery state for
no behavioral gain.

Reject with reasons — recorded here and in the issue — if neither holds. That
is a valid, closable outcome, and it makes the remaining #47 candidates (MCP
channels, client auto-backgrounding) the live paths.

## Scope notes

- Transport work touches `agent_collab/server_http.py` and the MCP adapters
  only; session semantics, `wait_result` settlement, and the answer ledger do
  not change.
- Any adoption must keep the current JSON path working unchanged: content
  negotiation, never a flag day.
- Resumability (`Last-Event-ID`) is in scope only as an observation in (c). It
  is not a goal of this task; redelivery only matters once a stream is proven
  useful.
- The static-token remote deployment is the reason this matters at all: a
  shell-less client connected over Streamable HTTP has no CLI fallback, so its
  only collection mechanism is MCP calls.

## Open questions

- Whether a streamed `wait_result` should emit protocol-level progress
  notifications, heartbeat comments (`:` lines), or both — bounded by what (b)
  shows actually resets client timers.
- Whether a proven GET wake path would replace the heartbeat loop entirely or
  only shorten it (the model still has to call a tool to read the result).
