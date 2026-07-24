# Evaluate a non-blocking wake path: SSE framing and MCP channels

**Status:** Open — root cause measured, and every probe below now answered
against both target clients with an instrumented MCP server. POST-scoped SSE
is confirmed to work; nothing implemented yet. **The measurements are
single-agent, single-run and must be independently re-verified before any
implementation starts — see "Verify before implementing" below.**

**Created:** 2026-07-24. **Probes run:** 2026-07-25.

**Issue:** [#49](https://github.com/lauriparviainen/agent_collab/issues/49)

Extracted from the collection-primitive re-evaluation in
`subagent-delegation-and-thread-continuity` (#47), now closed — its
`timeout_ms=0` instant peek shipped in 0.12.0.

## Question

Can outcome collection be made non-blocking — so the calling agent is never
frozen inside a tool call while a delegated session runs?

The requirement is absolute, not a cost trade-off: an agent that is
unresponsive to its own user for the length of a delegated session is not
acceptable, whichever tool it calls.

This document owns **every** candidate answer, because they are alternative
means to one end and picking between them is a single decision:

- **POST-scoped SSE framing** — defuse the client's first-byte timer so a long
  call survives to the point where the client backgrounds it.
- **The optional `GET /mcp` stream** — server-initiated notifications on the
  transport MCP already defines.
- **MCP channels** — Claude Code's purpose-built push path into a running
  session, reaching the model with no in-flight tool call at all.
- **Client auto-backgrounding** — not a mechanism we build but the payoff the
  first two are chasing.

Evaluating any one of these without the others in view is how a worse mechanism
gets adopted.

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
   headers plus an opening event or comment), then keep the stream alive until
   the result is ready. The first-byte timer is satisfied by the stream opening
   rather than by the settled answer — confirmed below, and the keep-alive
   turns out to have to be `notifications/progress` specifically. This needs
   nothing from the user, which is why it is the product answer rather than a
   workaround.

With either in place the call survives past two minutes, the client backgrounds
it, and the calling agent is free while the delegated session runs — the
requirement met without a poll loop at all.

## What the probes returned (measured 2026-07-25)

Measured against a purpose-built Streamable HTTP MCP server (~230 lines of
Python, logging every request with headers and monotonic timestamps) exposing
four tools that differ only in how they answer: `slow_json` blocks and replies
`application/json`; `slow_sse` opens `text/event-stream` immediately and emits
SSE comments plus `notifications/progress` every 5 s; `slow_sse_comments`
emits comments only; `slow_sse_quiet` sends stream headers and then nothing.
Clients: Claude Code 2.1.219 (`claude -p`, Haiku 4.5) and Codex CLI 0.145.0
(`codex exec`, gpt-5.6-sol), each connected over `http` to the probe server.

### Claude Code 2.1.219

| Call | Configuration | Result |
| --- | --- | --- |
| `slow_json(120)` | none | client error "The operation timed out." at ~70 s |
| `slow_sse(120)` | none | **completed**, result delivered at 120 s |
| `slow_sse_quiet(90)` | none | **completed** — stream headers alone, zero events, survive the first-byte timer |
| `slow_sse_comments(60)` | `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=20000` | aborted: "sent no response or progress for 30s" |
| `slow_sse(60)` | same | **completed** — only `notifications/progress` resets the idle window |
| `slow_sse(200)` | `CLAUDE_AUTO_BACKGROUND_TASKS=1` | at 120 s the tool result was replaced by "still running after 120s … moved to the background as task `<id>`"; `task_type: mcp_task`; the model kept working |
| `slow_sse(90)` / `slow_sse(30)` | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` = 30000 / 5000 | backgrounded at 30 s / 5 s respectively |
| idle session, `GET /mcp` answered `200 text/event-stream` | none | client opens the GET stream immediately after `initialize`; a pushed `notifications/tools/list_changed` made it re-issue `tools/list`; neither that nor `notifications/message` produced a model turn |

The whole chain works end to end: SSE headers satisfy the first-byte timer →
`notifications/progress` holds the idle window open → at the backgrounding
threshold the client hands the model a task id and frees it → the result
arrives later as a task notification. Probes (a), (b), (c) and (d) are
all confirmed positive. Two details matter for implementation: the client
sends `params._meta.progressToken` on every `tools/call` (so the keep-alive
has a token to carry), and the backgrounded task "does not survive exiting
this session".

### Codex CLI 0.145.0

| Behaviour | Result |
| --- | --- |
| `Accept` on every POST | `text/event-stream, application/json` — content negotiation is available |
| SSE response to `tools/call` | **accepted and parsed**; verified at 25 s, 90 s and 200 s |
| SSE comments interleaved with events | tolerated |
| `params._meta.progressToken` | sent on every `tools/call` |
| `notifications/progress` from the server | consumed, then only written to the tracing log (`logging_client_handler.rs`) — never reaches the model or the user |
| `GET /mcp` | never opened, in any run |
| Long call vs. the turn | blocks for the full duration; no backgrounding of any kind |
| Default per-tool timeout | **300 s**, not the documented 60 s. A 90 s plain-JSON call with nothing configured completed normally; a 330 s streamed call died with "timed out awaiting tools/call after 300s". Source agrees: `tool_timeout_sec.unwrap_or(DEFAULT_TOOL_TIMEOUT)` with `DEFAULT_TOOL_TIMEOUT = 300 s` (`codex-mcp/src/rmcp_client.rs`, tag `rust-v0.145.0`) |
| `tool_timeout_sec` semantics | hard wall clock. With `tool_timeout_sec=30`, a `slow_sse(60)` sending a keep-alive every 5 s still died at 30 s; at the 300 s default, 60 keep-alives bought nothing. The clock is paused only while an elicitation dialog is open (`active_time_timeout` in `rmcp-client/src/rmcp_client.rs`) |
| On timeout | the client drops the connection; no `notifications/cancelled` is sent |

So probe (e) is answered against the design, not just against a version:
streaming is *accepted* by Codex but buys nothing there beyond surviving up to
a wall clock it cannot extend, and its progress is invisible.

### One defect found on the way

Codex's `Auto` approval mode treats a tool with no `annotations` as
destructive (`destructive_hint.unwrap_or(true)` in `core/src/mcp_tool_call.rs`),
so it requires approval for every call. **agent-collab declares no tool
annotations at all** — `agent_collab/mcp_tools.py` has none. The probe
reproduced the consequence: under `codex exec` (approval policy `never`) an
un-annotated tool call is refused before it reaches the wire, with the
misleading text `user cancelled MCP tool call`; adding
`readOnlyHint`/`destructiveHint` made the identical call succeed. Read-only
tools like `agent_collab_status`, `wait_events` and `wait_result` should
declare `readOnlyHint: true`. This is independent of the transport question
and worth its own issue.

## The third mechanism: MCP channels

Claude Code documents a channel as an MCP server that **pushes events into a
running session**, so the model reacts to things that happen while nobody is at
the terminal. Exposing "session settled" as a channel event would reach the
model with no in-flight tool call at all — strictly better than any timer fix,
if it were generally available. It is not, and the constraints are what decide
this option (documentation read 2026-07-24; re-verify, this is a moving
target):

- It is a **research preview**. The `--channels` flag syntax and the protocol
  contract may change, and the flag is not even listed in `claude --help`.
- It requires Anthropic authentication through claude.ai or a Console API key,
  and is unavailable on Amazon Bedrock, Google Cloud's Agent Platform, and
  Microsoft Foundry.
- A channel is installed as a **plugin** and opted in per session with
  `claude --channels plugin:<name>`; being present in `.mcp.json` is explicitly
  not enough to push messages.
- During the preview, `--channels` accepts only plugins from an
  Anthropic-maintained allowlist, or an organization's own list. Team and
  Enterprise orgs must set `channelsEnabled` before any channel delivers.
- It is Claude Code only. Codex has no equivalent.

So the honest framing is not "channels versus SSE" but *what each one can be
relied on for*. Channels is the only mechanism that removes the tool call
entirely, and it is also the only one gated behind a preview flag, a plugin
install, a vendor account, and an org policy toggle — for a tool whose premise
is working across vendors. The evaluation must state plainly whether a wake
path that exists for one client, under an admin-gated preview, is worth a
protocol surface agent-collab has to maintain and document.

Two questions decide it, and both are cheap to answer before any design work:
whether a channel server can be a *plain MCP server* the user already has
connected, or must be a separately installed plugin (the docs read as the
latter); and whether the allowlist admits a third-party server like ours at
all. If channels cannot carry an ordinary MCP server's events without a
bespoke plugin on an Anthropic-controlled allowlist, it is not a mechanism
agent-collab can offer — record that and move on.

## Codex is a first-class target, and it changes the design

The transport must be designed for Codex as well as Claude Code, not verified
on Codex afterwards. What Codex documents (read 2026-07-24; local CLI 0.145.0):

- Remote MCP servers are **Streamable HTTP** servers, configured with a `url`
  under `[mcp_servers.<id>]`, with bearer-token, OAuth, or ChatGPT session
  auth. That is the same transport agent-collab already serves, so POST-scoped
  SSE is within the protocol Codex speaks.
- `tool_timeout_sec` — "Timeout (seconds) for the server to run a tool",
  documented default 60 — and `startup_timeout_sec`, documented default 10.
  **Both published defaults are wrong for 0.145.0**: the source applies 300 s
  and 30 s respectively, and a 330 s call does die at 300 s on the wire.
  Measurement, not the docs, is the input here.
- Nothing in the Codex documentation describes SSE response handling, progress
  notifications, or moving a long tool call to a background task.

Two consequences for the design, now measured rather than assumed:

1. **There is no 60 s cliff on Codex at all.** Its effective default per-tool
   wall clock is 300 s, and a 90 s plain-JSON call completes untouched. The
   binding constraint on Codex is a hard 300 s wall clock that stream activity
   cannot extend — so a bound in the ~45 s region is defensive rather than
   necessary there, while remaining exactly right for Claude Code's
   unconfigured 60 s first-byte timer.
2. **The design must not assume client backgrounding.** Confirmed: Codex has
   no equivalent, so a long streamed call there occupies the turn for its
   whole duration, and its progress notifications never leave the tracing log.
   SSE buys Codex survivability up to the wall clock and nothing else. The
   transport work must therefore degrade honestly: the same streamed response
   is correct on both clients, but only Claude Code turns it into
   responsiveness. Guidance must state which client delivers which, instead of
   promising responsiveness uniformly.

Probe (e) was a design input, not a validation step, and it came back
negative: Codex treats `tool_timeout_sec` as a hard wall clock, pausing it
only for an open elicitation dialog.

## Live observations from the #47 close-out session (2026-07-24)

Three facts from running the shipped watch-then-harvest pattern live (Claude
Code 2.1.219 as the VSCode native extension, agent-collab as an `http` MCP
server, two dual-review sessions), recorded here because each changes what the
mechanisms above are actually worth:

1. **Steering-in works as designed.** A user message sent mid-turn was
   delivered to the calling model attached to the next tool result, three
   times, each within one 30 s poll bound. The guidance claim "the block bound
   is your steering latency" is now empirically confirmed, not just designed.
2. **Narrating-out does not render mid-turn — at least on the VSCode
   extension surface.** Text the calling model emitted *between* tool calls
   (acknowledgments, progress narration) was silently dropped by the UI; the
   user saw only their own message followed by an unbroken run of tool-call
   frames, and concluded the agent was unresponsive even though it had
   answered within one poll. Only a turn's final text reliably renders.
   Consequence: the watch loop delivers steerability and progress *to the
   model*, but on such a surface the model cannot surface progress *to its
   user* until the whole delegation turn ends. This cuts both ways for the
   candidates: SSE's "visible progress" only reaches the user where the client
   renders mid-turn output, while backgrounding fixes visibility as a side
   effect — each task-notification wake starts a fresh turn whose final text
   always renders. Probe (g) below.
3. **For a `message_first` backend the digest watch degenerates to pure
   heartbeating.** In the first review round, `xai_cli` (grok-4.5) emitted no
   message or error events for ~12.5 minutes and then its entire answer at
   once; the watch loop ran ~25 bounded polls whose only information was
   "still running". Each empty poll is a full LLM turn for the caller, growing
   its context — the caller-side turn cost #47 measured is at its worst
   exactly where a long-blocking non-poll path would shine. `dual-review`
   with CLI reviewers is the shipped skill's common case, so this is the
   normal shape, not an edge case.

| # | Probe | Answer (measured 2026-07-25) |
| --- | --- | --- |
| a | Does the per-request timer treat SSE response headers / the first event as the "first response byte"? | **Yes — the headers alone.** A 90 s stream that sent nothing but headers completed; the same duration as JSON dies at ~70 s |
| b | With the timer defused, does auto-backgrounding fire for an HTTP MCP server, and does the result arrive as a task notification? | **Yes.** At the threshold the tool result becomes "moved to the background as task `<id>`" with `task_type: mcp_task`, and the model keeps working |
| c | Do periodic SSE events reset the idle window, or does only a protocol-level `notifications/progress` count? | **Only `notifications/progress`.** Comment-only keep-alives were aborted with "sent no response or progress" |
| d | Does lowering `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` background the call sooner, and how low is usable? | **Yes; 5 s works.** Verified at 30000 and 5000 ms. Default remains 120 s |
| e | Codex: does it accept a `text/event-stream` response to a POST tool call, does stream activity reset `tool_timeout_sec`, does any Codex surface background a long call? | **Accepts SSE; does not reset (hard wall clock, default 300 s); never backgrounds.** Progress notifications go to the tracing log only |
| f | Does the optional `GET /mcp` stream get opened by any target client, and is a server-initiated notification model-visible? | **Claude Code opens it and consumes it** (a pushed `tools/list_changed` triggered a fresh `tools/list`), **but no notification woke the model**. Codex never opens it |
| g | Per client surface: does assistant text emitted between tool calls render to the user mid-turn? | **Still open** — needs interactive surfaces, not `-p`. Lower stakes now: backgrounding sidesteps it, since each task notification starts a fresh turn whose final text renders |

Instrumented at the HTTP boundary with client versions recorded; these are
product behaviors that change between releases, so re-run the probe server on
client upgrades rather than trusting this table forever.

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

## Verify before implementing

Everything above is one agent's single run, on one machine, driving
`claude -p` and `codex exec` — the two surfaces that automate cleanly, and
neither of which is what users actually sit in front of. Every claim is a
statement about third-party client behaviour at one version, which is exactly
the kind of thing that is both easy to mis-measure and liable to change. **A
second agent must reproduce the probes before any of this becomes code.**

The harness is checked in to `tmp/wake-path-probe/` (untracked scratch space,
so rebuild it from the description above if it is gone): the instrumented MCP
server, one runner per client, and a README mapping each probe letter to its
command and to what was observed. Re-running the whole set costs about twenty
minutes of wall clock and a handful of cheap model turns.

Ranked by what the design breaks on if the finding is wrong:

1. **The keep-alive contract — (a) and (c).** That stream headers alone defuse
   the first-byte timer, and that *only* `notifications/progress` resets the
   idle window, together fix the shape of the streaming response. If comments
   turn out to be sufficient, or progress notifications insufficient on some
   surface, the keep-alive is a different mechanism.
2. **Backgrounding on the real Claude Code surfaces — (b) and (d).** The
   measurement was taken in `-p` with `CLAUDE_AUTO_BACKGROUND_TASKS=1`, which
   the documentation describes as a separate opt-in path from interactive
   backgrounding. The VSCode extension and the terminal UI were **not**
   tested, and they are where the requirement actually has to hold. If
   interactive sessions do not background an MCP call, the entire Claude Code
   payoff evaporates and only survivability remains.
3. **The Codex ceiling — (e).** 300 s was read from the 0.145.0 source and
   confirmed on the wire, but it is an undocumented constant that upstream has
   already moved once. Re-check it against whichever Codex version is actually
   pinned, and on the Codex VS Code extension and desktop app, neither of
   which was exercised here.
4. **The untested surfaces — (f) and (g).** (f) was answered for Claude Code
   only, and (g) not at all.

Two method notes for whoever repeats this, both learned the hard way:

- **Trust the HTTP log, not the model's narration.** The probe server logs
  every request with headers and timestamps precisely so the client's account
  of what happened can be checked against what crossed the wire.
- **Stop the model from delegating.** A subagent's MCP calls are never
  backgrounded, so a model that quietly hands the call to a subagent makes
  probe (b) look negative. The runners disable delegation for this reason.

Finally, the probe server is not agent-collab: it is a threaded stdlib server
using chunked framing, while `server_http.py` writes raw HTTP over asyncio
streams. Once the streaming path exists, re-run the same probes against the
real daemon — byte-level framing differences are exactly what clients trip
over, and a long-lived stream must not block other requests on the server.

## Decision

The criteria are met. (a) and (b) both hold on Claude Code, and Codex accepts
the same response, so **adopt POST-scoped SSE for `wait_result` and
`wait_events`**, content-negotiated, with the JSON path untouched. The
optional GET stream and channels are both declined, for measured reasons.

### What to build

1. **Negotiate on `Accept`.** Both clients advertise `text/event-stream` on
   every POST (Claude Code as `application/json, text/event-stream`, Codex the
   other way round). Stream only when the header asks for it; anything else
   gets today's single JSON object, byte for byte.
2. **Stream only `tools/call` responses.** Keep `initialize` and `tools/list`
   on the JSON path: [openai/codex#26983][codex-26983] is open against Codex's
   Streamable HTTP client failing to parse an SSE `initialize` response, and
   there is no reason to take that risk for a response that is instant anyway.
3. **Frame it as:** `200` with `Content-Type: text/event-stream` and
   `Transfer-Encoding: chunked`, headers flushed immediately — that alone
   defuses the first-byte timer — then the settled JSON-RPC response as a
   single `event: message` frame, then end the stream. Chunked framing was the
   configuration verified against both clients.
4. **Keep-alive must be `notifications/progress`,** carrying the
   `params._meta.progressToken` the client sent with the call; both clients
   send one. SSE comments are tolerated by both but do not hold Claude Code's
   idle window open, so they are decoration, not a keep-alive. Cadence ~15–20 s:
   the default HTTP idle window is 5 minutes, but a per-server `timeout` floors
   it and `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` can lower it (a 20 s setting was
   enforced as 30 s in testing), so a cheap sub-30 s tick keeps every
   configuration safe. Where no `progressToken` is supplied, fall back to
   comments and treat the idle window as the binding limit.
5. **No per-client branching.** The server cannot know whether the caller will
   background the call, and does not need to: the identical response is correct
   for a client that backgrounds at 120 s and for one that blocks for 300 s.
6. **Handle the hang-up.** Both clients abandon a call by dropping the
   connection — Codex sends no `notifications/cancelled` — so the streaming
   path must treat a write failure as "the caller gave up", leave the session
   running, and leave the answer in the ledger for a later `wait_result`.

### What each client actually gets

- **Claude Code** gets the whole requirement: the call survives, the client
  backgrounds it at 120 s (or as low as 5 s with
  `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`), the model is free while the delegated
  session runs, and the result arrives as a task notification. Once this ships,
  the ~45 s poll bound stops being the recommended shape for Claude Code —
  a long single wait is strictly better, and the caller-side turn cost the #47
  close-out measured (~25 empty polls against a `message_first` backend) goes
  to zero. Caveat to document: a backgrounded task does not survive exiting the
  session.
- **Codex** gets survivability and nothing else: the turn still blocks for the
  full call, and progress never reaches the model or the user. The useful
  bound there is the 300 s default wall clock — keep the server-side wait
  under it, and document `tool_timeout_sec` for anyone who wants longer. Codex
  users keep the bounded watch loop as the way to stay steerable.

Guidance must say this plainly per client rather than promising responsiveness
uniformly.

### What not to build

- **The optional `GET /mcp` stream.** Probe (f) split: Claude Code opens it and
  genuinely consumes it, but neither a logging notification nor
  `tools/list_changed` woke an idle model, and Codex never opens it at all. A
  stream one client consumes but never surfaces costs reconnect and redelivery
  state for no behavioural gain. Keep answering `405`.
- **Channels.** The verdict in the section above is unchanged, and the ground
  has shifted under it: backgrounding now supplies, for the one client channels
  would have served, the wake path channels were wanted for — without a preview
  flag, a plugin install, a vendor account or an org policy toggle. Revisit
  only if the gating questions turn favourable *and* something remains that
  backgrounding cannot do.
- **Option 1 as the supported answer.** Per-server `timeout` configuration
  stays worth documenting for users on clients we cannot detect, but it is no
  longer the fallback plan: the transport fix needs nothing from the user.

[codex-26983]: https://github.com/openai/codex/issues/26983

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
