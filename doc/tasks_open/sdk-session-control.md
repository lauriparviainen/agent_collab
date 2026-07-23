# SDK session control: interrupt, tool approval, restart-safe resume

**Status:** Open — design refreshed 2026-07-23 against the shipped continuity
substrate (issue #47). Interrupt, tool gating, and restart-safe resume remain
open, each buildable per backend on top of the conversation adapters.

**Created:** 2026-07-10.

**Issue:** [#20](https://github.com/lauriparviainen/agent_collab/issues/20)

> **Design refresh (2026-07-23).** Issue #47 (subagent delegation and thread
> continuity) delivered the substrate this task once described as future work:
> the `conversation_active()` + `close()` runner contract, the referee's
> delta-prompt continuation and bounded runner-close lifecycle, the per-backend
> conversation adapter, and a new `continuity` capability distinct from `resume`.
> This document has been rewritten to that reality. The older
> `ActiveTurnController` / streaming-runner sketch is retired; the smaller
> contract below is the one in the code. What remains for #20 is **interrupt**,
> **tool gating**, and **restart-safe resume** with their public surfaces — each
> stands on the shipped adapters without re-touching the referee. The
> version-sensitive SDK facts must still be re-verified against the installed
> pin before implementing a provider.

## Purpose

Turn the SDK backends' captured provider session identities and the shipped
in-session conversation adapters into the remaining provider-tested controls:

- interrupt an active SDK turn through a verified provider cancellation path;
- surface SDK tool approval requests through the daemon, REST, MCP, CLI, and
  TUI, then return an explicit approve or deny decision to the waiting SDK;
- resume a captured provider session *across a daemon reload* through an
  explicit user action, extending the in-session continuity #47 shipped.

Do not flip a capability merely because an SDK exposes a suggestive method or
returns a session ID. A backend advertises `interrupt`, `tool_gate`, or `resume`
only after agent-collab owns the complete lifecycle and the behavior is covered
by hermetic tests plus a credentialed provider smoke test.

## What #47 already shipped (the substrate)

The continuity groundwork (backend-neutral) and per-backend continuity stages of
#47 land the pieces #20's later stages reuse. Do not rebuild them.

### Runner contract (`agent_collab/runners.py`)

`AgentRunner` grew two defaulted methods so CLI and mock runners stay untouched:

- `conversation_active() -> bool` (default `False`): the runner holds
  provider-side context the next `run_turn` will continue.
- `async close() -> None` (default no-op, idempotent): release any client or
  subprocess held across turns.

### Runner lifecycle (`agent_collab/referee.py`)

Runners are created once per session (`Referee.run` calls `self._runners()`
once) and reused for every sequential, parallel, and directed turn — a stateful
runner needs no scheduling change, only a cleanup hook. `Referee.run` closes
every runner in a bounded, `asyncio.shield`-ed `finally`
(`_close_runners_bounded`), reusing the existing bounded-cancel /
reaper-adoption pattern, so cleanup runs on normal exit, failure, and stop
cancellation alike. The daemon needs no new hook: stop cancels the session task
and the finally runs; daemon exit is covered by the SDK's `atexit` child killer.

Close-vs-turn coordination: the referee's bounded cancel can adopt a
non-cooperative `run_turn` as a reaper that outlives the turn, so `close()` must
be concurrency-safe against an in-flight or adopted turn — the conversation
adapter serializes `close()` against `run()` internally. A hanging or failing
`close()` is adopted as a background reaper; it never hangs teardown and never
alters an already-committed session outcome.

### Delta continuation prompts (`agent_collab/referee.py`)

Per-agent watermarks with prompt-snapshot semantics: an agent's watermark
advances to the transcript length captured when its prompt was *built*, never to
completion-time length (which in a parallel stage or during a mid-turn post
would silently skip peer events the provider never saw). At the two sequential
call sites (the stage loop and `_process_input_item`), when
`runner.conversation_active()` is true the referee sends a continuation prompt —
a role note, `NEW EVENTS SINCE YOUR LAST TURN:`, the delta
(`transcript[watermark:]` minus the agent's own events and the provider-session
bookkeeping id), plus the directed question when present — with no guardrails,
task, or full-window re-send. Turn 1, CLI/mock runners, and parallel stages keep
the prior prompt byte-for-byte. No silent cap: the watermark advances only over
events actually included.

### The `continuity` capability

`BackendCapabilities` grew `continuity: bool = False` (in `to_dict()` and the
per-agent `settings.agents.<id>.capabilities` view). The session reducer
(`summarize_session_capabilities`) reports session `continuity: true` only when
*every* selected agent's backend has it — conservative, like `resumable` /
`interruptible`, but with no captured-id precondition because continuity is an
in-session fact, not restart-safe resume. `resume`, `interrupt`, and `tool_gate`
stay false everywhere until this task lands them.

### Per-backend conversation adapter (the injectable seam)

Each SDK backend replaces its per-turn provider context with a per-session
conversation adapter behind an injectable seam:

- `active() -> bool` — the adapter holds a live provider thread;
- `run(prompt) -> ...` — run one turn on the held thread;
- `note_session_id(...)` — record the captured provider id;
- `reset()` — drop the live client but **keep** the captured id for reconnect;
- `close()` — drop everything, idempotently.

On an abnormal turn end the adapter resets and reconnects via the provider's
native resume when the verified API supports it, else fails the turn
structurally — never a silent fresh provider session. A backend flips
`BackendCapabilities(continuity=True)` and adds
`settings_summary["conversation"] = "persistent"` only when both the hermetic
fake-conversation suite and a credentialed provider-memory integration test
pass. A backend whose SDK cannot prove native continuity keeps the capability
false with the finding recorded — a valid, closable outcome. This adapter is the
handle #20's `interrupt()` and restart-safe resume build on.

## Verified enablers (installed pins — re-verify on bump)

- `SessionStateModel.settings` and `capabilities` are documented-opaque dicts,
  so new capability keys and compact settings views are additive; the REST API
  major stays 2.
- `post_message` enqueues onto `managed.input_queue` before returning;
  `_process_pending_inputs` / `_await_interactive_input` call `queue.task_done()`
  in `finally`; status stays `awaiting_input` during directed turns — so the
  queue's unfinished-task count is a clean "settled" signal (used by
  `wait_result`). #20's proposed `awaiting_approval` status can reuse that
  wait/settle mechanism.
- **Claude SDK — implementation-time `claude-agent-sdk` 0.2.126 (bundled CLI
  2.1.218):** one connected `ClaudeSDKClient` (`connect` / `query` /
  `receive_response` / `interrupt` / `disconnect`) accepts sequential
  `query()`/`receive_response()` turns on one provider session. A credentialed
  haiku fixture completed two turns on one client: turn 2 received no codeword
  in its prompt and recalled turn 1's codeword, and every message across both
  turns carried the same `session_id`. After `disconnect()`,
  `ClaudeAgentOptions(resume=<sid>, fork_session=False)` reconnected the exact
  captured id with memory intact (a third fixture turn); resuming an unknown id
  fails `connect()` with a `ProcessError` (CLI exit 1, "No conversation found
  with session ID") — never a silent fresh session. A session is materialized
  incrementally during its first turn: a fixture that abandoned turn 1 right
  after the init `session_id` message (no `ResultMessage` ever observed) and
  disconnected still resumed the exact id with the delivered prompt's context
  intact — resumability begins at the first delivered user message, not at the
  first terminal result. The client is
  loop-scoped but usable across asyncio tasks within one loop: its reader task
  is detached (`spawn_detached` -> `loop.create_task`), so a client connected in
  one turn's task survives into the next; an `atexit` child killer reaps
  orphaned CLI subprocesses on ungraceful exit. Cancelling the local consumer
  does not stop provider work — the detached reader and CLI subprocess keep
  running until `disconnect()` (whose subprocess close is internally bounded,
  ~20 s worst-case terminate/kill escalation). A cancelled `connect()` unwinds
  itself via the SDK's own failure-path `disconnect()`; `disconnect()` is
  idempotent. `disconnect()` closes the receive stream side the consumer owns,
  so it must not race an active `receive_response()` — the conversation adapter
  serializes run/reset/close internally. The persistent client is the
  precondition for the `can_use_tool` callback — `tool_gate` cannot exist on a
  one-shot `query()`. `claude_sdk.continuity` is true; `resume`, `interrupt`,
  and `tool_gate` remain false.
- **Codex SDK — implementation-time `openai-codex` 0.1.0b3 with
  `openai-codex-cli-bin` 0.137.0a4; configured local CLI 0.144.4:** one open
  `AsyncCodex` owns an `AsyncThread` whose public `run()` accepts repeated
  collected turns. A credentialed `luna`/low provider-memory fixture completed
  two turns on one thread; turn 2 received no codeword in its prompt and recalled
  turn 1's codeword. The public `openai-codex` 0.144.4 wheel was also inspected
  and has the same relevant `AsyncCodex` / `AsyncThread` APIs.
  `AsyncCodex.thread_resume(thread_id, ...)` reopens a materialized thread after
  the first client closes: a one-turn `luna`/low fixture resumed the exact id and
  read its persisted turn. A no-model `thread_start` alone does not materialize
  the thread (`includeTurns` is rejected before the first user message), so one
  lowest-cost turn is the minimum reconnect fixture.
  `AsyncThread.run()` waits through `asyncio.to_thread` on the synchronous
  notification queue. Cancelling its asyncio waiter does not itself interrupt
  the provider worker; cleanup requires `AsyncCodex.close()` to terminate the
  app-server transport. The adapter therefore serializes run/reset/close, resets
  after abnormal turns while retaining the captured id, and never treats local
  cancellation as provider interruption. `codex_sdk.continuity` is true;
  `resume`, `interrupt`, and `tool_gate` remain false.

## Capability semantics

Keep these definitions strict and backend-specific. `continuity` (shipped by
#47) is deliberately narrower than `resume`: continuity is provider-thread
continuation *within one live session*; `resume` additionally requires
restart-safe explicit resume across daemon reloads. Flipping `resume` on
in-session continuity alone would dilute the definition.

### `resume`

`resume = true` means agent-collab can continue a captured provider session:

1. during a later turn in the same live agent-collab session (this half is what
   `continuity` covers); and
2. after the daemon reloads the persisted session, through an explicit user
   resume action rather than automatic crash recovery.

The resumed turn must retain the provider identity, backend, workdir, model,
permissions, and compatible static configuration. An expired or rejected
provider session produces a structured error; it must never silently start a
fresh provider session under the same identity.

Transcript-in-prompt continuity alone is not native resume and does not satisfy
this capability.

### `interrupt`

`interrupt = true` means the daemon stop path invokes a documented SDK abort or
cancellation mechanism (asking the conversation adapter for a provider-side abort
before local cancellation), closes the active response stream/client, and
reaches a known terminal result within a bounded timeout.

Cancelling only the local asyncio consumer is insufficient when the provider
request may continue remotely, keep billing, or leave a reusable session in an
unknown state. Completion racing with interruption must have one deterministic,
idempotent outcome.

### `tool_gate`

`tool_gate = true` means an SDK tool request can pause the turn, become a
session-scoped approval request, accept exactly one authorized approve/deny
decision, return that decision through the SDK callback, and continue or reject
the tool call without restarting the turn.

Preconfigured permission modes and automatic provider approval are policy, not
an agent-collab tool gate.

## Source facts to re-verify

The SDKs are version-sensitive. Before implementing a provider, inspect the
installed pinned version and capture a no-model or lowest-cost fixture for the
exact API used. The Claude persistent-client facts above are verified on
0.2.126; the continuity stages of #47 record each backend's verified thread
facts as they land.

### Claude SDK

For interrupt and tool gating, verify on the installed `claude-agent-sdk`:

- a reliable client `interrupt()`/cancel path and its completion semantics;
- a `can_use_tool` or equivalent async permission callback, its request ID,
  tool name/input shape, and approve/deny result types;
- cleanup behavior when the callback is waiting and the session is stopped.

Continuity already moves this backend onto the persistent `ClaudeSDKClient`; do
not regress to the one-shot `query()` helper.

### Codex SDK

Continuity was verified on the implementation-time pin as recorded above. For
the remaining #20 capabilities, re-verify the installed
`openai-codex`/app-server shapes for:

- restoring the persisted thread descriptor after an agent-collab daemon
  restart and validating its fingerprint before calling the already-proven
  `thread_resume` API (the public restart-safe resume half);
- turn cancellation and acknowledgement;
- command/file-change approval notifications and response methods.

Starting a new thread with the same transcript is not thread resume. The adapter
fails a rejected/expired `thread_resume` structurally and never falls back to
`thread_start`.

### Antigravity SDK

Verify the installed `google-antigravity` API for:

- reopening an `Agent` from `conversation_id` after a reload;
- cancelling an unresolved `ChatResponse` and confirming termination;
- intercepting local `BuiltinTools` or tool callbacks before execution.

The presence of `Agent.conversation_id` is identity evidence, not proof that
these controls exist. Known blocker: the bundled native runtime requires
glibc >= 2.36; a host below that probes `unavailable`, so credentialed proof
waits for a compatible host.

## Runner / adapter contract

The provider mapping lives inside each backend's conversation adapter, reached
through the shipped runner seam (`conversation_active()` / `close()` plus the
adapter's `active()` / `run()` / `note_session_id()` / `reset()` / `close()`).
For interrupt and tool gating, extend the adapter — not the daemon — with:

- one active turn handle per agent turn, registered and removed under the
  referee/daemon session lock;
- an idempotent provider-side interrupt reachable from the stop path;
- correlated tool requests and decisions;
- bounded cleanup when the SDK or callback misbehaves.

`SessionManager` must not import or branch on Claude/Codex/Antigravity SDK
types. Each backend owns the provider mapping.

## Session and persistence model

Expand `agent_sessions.<agent_id>` without storing credentials or raw SDK
objects:

```json
{
  "backend": "sdk",
  "provider_session_id": "...",
  "provider_session_kind": "thread",
  "backend_version": "...",
  "resume_fingerprint": "...",
  "last_turn_status": "completed"
}
```

The resume fingerprint should cover the fields that must not drift silently,
including provider type, canonical backend, model, workdir, permission/sandbox
posture, and backend-owned static configuration. It must not contain secrets.

On daemon restart:

- retain the current behavior that an in-flight session becomes
  `interrupted`; never auto-resume a paid or side-effecting operation;
- reload the exact workdir config and backend enablement policy;
- require an explicit resume request;
- reject resume if the agent/backend disappeared, is disabled, has incompatible
  settings, or does not advertise restart-safe resume;
- append to the existing event cursor and transcript so the audit trail stays
  continuous.

Add a shared resume operation only after this validation is defined, for
example `POST /sessions/{id}/resume`, `agent_collab_resume`, and
`agent-collab resume SESSION_ID`. Do not overload `start` with ambiguous prior
session IDs.

## Tool approval protocol

Add a first-class event and session status instead of encoding approval as
ordinary prose:

```json
{
  "source": "tool",
  "type": "approval_request",
  "raw": {
    "request_id": "opaque-session-scoped-id",
    "agent_id": "claude",
    "tool_name": "Bash",
    "summary": "python -m unittest discover -s tests",
    "decision_options": ["approve", "deny"]
  }
}
```

Requirements:

- add `awaiting_approval` as a live, non-terminal status distinct from
  `awaiting_input` (it can reuse the `wait_result` settle mechanism);
- expose an explicit approval response operation over REST, MCP, CLI, and TUI;
- bind every response to session ID, request ID, and active agent turn;
- accept only one decision; duplicate responses are idempotent or rejected with
  a structured conflict;
- default to deny on timeout, stop, daemon shutdown, or callback failure;
- never expose raw credentials, environment values, or unrestricted provider
  objects in the approval event;
- never infer approval from an ordinary `post_message`;
- do not add an "approve all" path as part of this task.

If an SDK exposes tool notifications only after execution, it does not support
`tool_gate` and must keep the capability false.

## Interrupt protocol

Update `SessionManager.stop_session` and the referee so stop first asks the
active conversation adapter to interrupt, waits for a short bounded
acknowledgement, and then falls back to local task cancellation and resource
cleanup (the runner-close lifecycle #47 shipped).

Record sanitized outcome detail:

```json
{
  "requested": true,
  "provider_acknowledged": true,
  "fallback_cancelled": false
}
```

Do not report `stopped` until controller cleanup finishes or the fallback
deadline expires. A provider completion that wins the race remains `done`; a
stop that wins remains `stopped`. Exactly one terminal transition is persisted.

## Capability aggregation and discovery

Capabilities remain facts declared by each concrete backend. It is valid to land
`claude_sdk.interrupt = true` while the other SDKs remain false.

Keep the existing session reducer conservative:

- session resume requires every selected non-mock backend to support resume and
  every required provider session ID to be captured;
- session interrupt requires every currently active backend turn to support
  reliable interrupt;
- session continuity (shipped) requires every selected backend to support it;
- tool approval is reported per agent/backend; do not imply a workflow-wide gate
  when only one agent supports it.

`agent_collab_describe_options`, start settings, session status, and the TUI
must all project the same backend capability facts.

## Safety and failure rules

- Never automatically retry a provider operation after an uncertain interrupt.
- Never silently replace native resume/continuity with a new provider session.
- Never auto-approve a tool because the same command was approved in another
  session or turn.
- Do not persist callbacks, clients, access tokens, tool results containing
  secrets, or raw SDK objects.
- Treat provider session IDs as opaque diagnostics; do not parse policy from
  their format.
- Reload home backend enablement before resume. A newly disabled backend cannot
  be resumed.
- Preserve cursor monotonicity and one terminal state under stop/completion,
  approval/stop, and daemon-restart races.

## Testing

### Hermetic tests

For every backend that flips a capability, use fake SDK modules shaped to the
verified installed version and cover:

- interrupt calls the provider exactly once, acknowledges within the bound, and
  handles completion races;
- interrupt fallback cancels and closes resources when the provider hangs;
- a persisted identity is restored after a simulated daemon restart;
- incompatible resume fingerprints and rejected/expired sessions fail without
  creating a fresh provider session;
- tool requests enter `awaiting_approval`, emit sanitized correlated events, and
  resume on approve or deny;
- duplicate, stale, cross-session, and post-stop approval responses are
  rejected;
- approval timeout and daemon shutdown deny and release the SDK callback;
- mixed workflows aggregate capability flags honestly;
- session-index round trips preserve only the sanitized resume descriptor;
- REST, direct MCP, stdio-via-REST, CLI, and TUI share the same behavior.

The continuity fake-conversation tests (two turns reuse the exact captured
provider session; abnormal end -> a single bounded `reset()`; `close()`
idempotent; `conversation_active()` transitions) ship with #47 and cover the
adapter these controls extend.

Keep `AGENT_COLLAB_HOME` isolated in every test. No hermetic test may import a
real optional SDK, read native credentials, or make a model call.

### Credentialed integration tests

Add opt-in, low-cost tests separately for each provider capability:

- interrupt a harmless long-running response and verify provider/client
  cleanup;
- gate a harmless tool, deny once, then approve once, verifying no execution
  occurs before approval;
- explicit resume after a simulated reload continues the captured session.

Skip when the installed SDK version, account, or provider does not support the
feature. A skipped provider keeps the corresponding production capability false.

## Staged delivery

The shared control/state contract for continuity is done (#47). What remains:

1. **Claude SDK interrupt + tool gating.** Confirm the installed `interrupt()`
   and `can_use_tool` APIs on the persistent client the continuity stage
   already adopted; implement only what that version proves.
2. **Codex SDK interrupt + approval.** Confirm cancellation and app-server
   approval APIs; flip each capability independently.
3. **Antigravity SDK controls.** Confirm response cancellation and
   pre-execution tool interception on a compatible host; unsupported
   capabilities remain false.
4. **Restart-safe resume and public surfaces.** Land explicit resume and
   approval operations across REST/MCP/CLI/TUI once persistence and
   authorization semantics are stable. This is the half of `resume` that
   in-session `continuity` does not cover.

Each stage must be independently shippable and must not make existing CLI or
message-first SDK workflows less reliable.

## Acceptance criteria

- Capability flags are true only for provider/backend pairs with implemented,
  tested behavior.
- Explicit resume after daemon restart reloads config/policy, validates a
  sanitized fingerprint, and never silently starts fresh.
- Stop invokes verified provider cancellation when interrupt is advertised and
  has a bounded cleanup fallback.
- Tool approval pauses before execution, is visible through all session
  surfaces, accepts one correlated approve/deny response, and defaults to deny
  on failure.
- Session IDs, controller state, callbacks, and approval events expose no
  credentials or raw provider objects.
- Mixed workflows and persisted sessions report conservative, accurate
  capabilities.
- Existing CLI backends remain `resume=false`, `interrupt=false`,
  `tool_gate=false`, and `continuity=false` unless a separate bidirectional CLI
  protocol task lands.
- Hermetic tests pass, and every enabled production capability has a separate
  credentialed integration test.
