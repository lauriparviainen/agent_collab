# SDK session control: interrupt, tool approval, restart-safe resume

**Status:** Open. Continuity shipped (#47); `interrupt`, `tool_gate`, and
`resume` are false for every backend. Design resynced 2026-07-30 against 0.13.0,
which made the outer read-only Bubblewrap worker the default execution path and
so relocated where all three remaining controls have to be built. Staging was
re-cut accordingly: one shared transport stage now serves three backends.

**Created:** 2026-07-10. **Issue:**
[#20](https://github.com/lauriparviainen/agent_collab/issues/20).

**Predecessor:** [subagent-delegation-and-thread-continuity.md](../tasks_closed/subagent-delegation-and-thread-continuity.md)
(#47) built the substrate. [antigravity-read-only-bubblewrap-sandbox.md](../tasks_closed/antigravity-read-only-bubblewrap-sandbox.md)
(#43) built the worker boundary.

## Purpose and scope

Turn the captured provider session identities and the shipped conversation
adapters into three provider-tested controls:

- **interrupt** — end an in-flight SDK turn through a verified provider abort
  path;
- **tool_gate** — surface an SDK tool request through daemon, REST, MCP, CLI,
  and TUI, then return one explicit approve/deny decision to the waiting SDK;
- **resume** — reopen a captured provider session *across a daemon reload*
  through an explicit user action.

**The bar for flipping a capability.** Never flip a flag because an SDK exposes
a suggestive method or returns a session ID. A backend advertises a capability
only when agent-collab owns the complete lifecycle on *every execution path that
backend can take* (see the two-path decision below), hermetic tests cover it,
and a credentialed provider smoke test passes. Two backends flipping while two
stay false is a valid, shippable outcome — the session reducer already ANDs
conservatively.

**Out of scope:** CLI backends (they stay all-false pending a separate
bidirectional CLI protocol task), any "approve all" path, and overloading
`start` with a prior session ID.

## Staged delivery

Stage 1 is a prerequisite for the default execution path of three backends and
is new since the resync — what used to be three per-provider stages is now one
shared transport stage plus thin per-provider mappings.

### Standing duties for every stage

- **Re-verify before you implement.** Appendix A records provider facts against
  a specific pin. Before a provider stage, re-verify its claims against the
  *installed* pin; update the appendix and its verified date in the same change.
  An appendix nobody refreshes is the failure mode this rule exists to prevent.
- **Both paths or no flag.** See the two-path decision. A capability that works
  only under `read-only` or only under `sandbox = "none"` stays false.
- **Record negatives.** A provider that cannot support a control gets the
  finding written into Appendix A and the flag left false. That is a completed
  stage, not an abandoned one.
- Issue, label, changelog, and task-document conventions live in
  [.claude/skills/github-issues/SKILL.md](../../.claude/skills/github-issues/SKILL.md).

### Stage 1 — shared control plane (protocol v2 + daemon surface)

Everything backend-agnostic, in one hermetic stage:

- **Protocol v2:** the `interrupt` / `approval_request` / `approval_decision`
  frames, the out-of-band writer, and the `on_approval` injection point in
  `SupervisedWorkerSession`. `validate_envelope` already requires exact version
  equality on both sides, so bumping `PROTOCOL_VERSION` is itself the
  fail-closed gate against daemon/worker skew — which is real, because a running
  daemon can outlive a package upgrade while workers launch from the installed
  package. The `hello` frame additionally advertises the worker's supported
  control frames so a start that needs gating fails with a clear error before
  any turn, not mid-callback.
- **The defaulted seams:** `AgentRunner` grows an interrupt-request method the
  way #47 grew `conversation_active()`/`close()` (CLI and mock runners
  untouched), and the `WorkerBackend` protocol grows interrupt/approval hooks.
- **The daemon surface:** the approval registry, the `awaiting_approval`
  status, and the approval decision operation across REST/MCP/CLI/TUI — shipped
  with no producer yet, so provider stages only wire a producer and flip a
  flag. (Shipping the decision surface *after* the first gated provider would
  leave approvals with no answer path but deny-timeout.)

No backend flips a capability; no provider SDK is touched.

*Done when:* the frames and the decision operation round-trip under the
hermetic worker harness; a v2 daemon with a worker that requests nothing new
behaves byte-for-byte as v1 does today; any protocol-version-skewed pairing
fails at the `hello` handshake rather than running ungated.

### Stage 2 — Claude SDK interrupt + tool gating

The only backend with both APIs documented. Implement the worker mapping and the
in-process adapter mapping together; the decision surface exists from stage 1,
so this stage wires the provider and flips the flag.

*Done when:* `claude_sdk.interrupt` and/or `claude_sdk.tool_gate` are true with
hermetic plus credentialed coverage on both execution paths, or a negative
finding is recorded in Appendix A.

### Stage 3 — Codex and Antigravity controls

Confirm the Codex app-server's turn-cancellation and approval surfaces, and
whether an Antigravity conversation is still usable after `ChatResponse.cancel()`
(if it is not, interrupt degrades to a reset and the flag stays false). Flip each
capability independently. `xai_sdk.interrupt` is expected to stay false; record
the negative result rather than leaving it undecided.

### Stage 4 — restart-safe resume and public surfaces

Land explicit resume and the turn-level interrupt operation across
REST/MCP/CLI/TUI once persistence and authorization semantics are stable. For
Antigravity this also means promoting the trajectory `save_dir`
to a durable agent-collab-owned root — see *Durable trajectory root* under the
resume design.

*Done when:* explicit resume reloads config and policy, validates a sanitized
fingerprint, and never silently starts fresh; every new operation is reachable
and identical across all four surfaces.

Each stage must be independently shippable and must not make existing CLI or
message-first SDK workflows less reliable.

## Shipped substrate — do not rebuild

### Continuity (#47)

`AgentRunner` (`agent_collab/runners.py`) has `conversation_active() -> bool` and
idempotent `async close()`, both defaulted so CLI and mock runners are
unaffected. `Referee.run` creates runners once per session, reuses them for every
sequential, parallel, and directed turn, and closes them in a bounded,
`asyncio.shield`-ed `finally` (`_close_runners_bounded`) that adopts an
uncooperative close as a background reaper. When `conversation_active()` is true
the referee sends a delta continuation prompt — role note, new events since the
agent's watermark, directed question — with no guardrails, task, or window
re-send; watermarks use prompt-snapshot semantics. Each SDK backend holds a
conversation adapter (`active()` / `run()` / `note_session_id()` / `reset()` /
`close()`, serialized internally) that reconnects through native resume on an
abnormal turn end or fails the turn structurally — never a silent fresh provider
session.

All four SDK backends declare `BackendCapabilities(continuity=True)` and report
`settings_summary["conversation"] = "persistent"`. Full design and rationale:
the closed #47 document.

### The worker boundary (#43, 0.13.0)

Since 0.13.0 an omitted start `sandbox` field resolves to `read-only`. For
`claude_sdk`, `codex_sdk`, and `antigravity_sdk` that means **the daemon does
not import the provider SDK at all**: the SDK runs in a Bubblewrap worker launched as
`python -m agent_collab.sandbox.sdk_worker` from the installed package.

- [agent_collab/sandbox/sdk_worker.py](../../agent_collab/sandbox/sdk_worker.py)
  — generic worker entrypoint; lazy registry of the three backend `worker.py`
  classes.
- [agent_collab/sandbox/worker_codec.py](../../agent_collab/sandbox/worker_codec.py)
  — protocol `agent-collab-sdk-worker` v1: length-prefixed UTF-8 JSON frames,
  `DAEMON_TO_WORKER = {open, run, cancel, reset, close}`, `WORKER_TO_DAEMON =
  {hello, ready, event, result, cancelled, reset_result, closed, error}`, plus
  frame/run size caps, a backpressure deadline, and secret scrubbing of error
  text (constants live in the module).
- [agent_collab/sandbox/worker_session.py](../../agent_collab/sandbox/worker_session.py)
  — daemon-side `SupervisedWorkerSession`.
- Each SDK runner branches per turn: `_run_turn_worker` under
  `SandboxPolicy.READ_ONLY`, `_run_turn_in_process` otherwise. **Both paths are
  production.**
- `xai_sdk` never takes the worker path: audited `no_local_effects`, reports
  `not_applicable_no_local_effects` on a read-only start, runs in the daemon with
  `state_roots=()`.

Four properties of that transport decide the designs below:

1. **The daemon's `run()` holds the session lock for the whole turn.** It takes
   `self._lock`, writes `run`, then loops on `recv` until `result`. No other
   daemon task can send a frame mid-turn without taking the lock (deadlock) or
   bypassing it.
2. **The worker already polls its control socket mid-run.**
   `_recv_while_running` selects on the channel with a 50 ms timeout while the
   run task is in flight. The transport can already carry mid-turn control;
   only the daemon side and the frame vocabulary are missing.
3. **`cancel` means "die", and the daemon never sends it.** The worker's
   `cancel` handler cancels the run task, emits `cancelled`, and exits so no
   tree is left mid-turn; `cancel_active()` SIGKILLs the process group *before*
   any await, because under sticky cancellation an await before the kill can
   orphan Bubblewrap descendants. This is the teardown contract and must not be
   weakened.
4. **Continuity is gated on a captured provider id.** `conversation_active()`
   is true only when the worker is live *and* a provider-session event set
   `_worker_provider_active`. A turn that ends without a captured id soft-drops
   the worker so hidden client context cannot join the next full task.

### What is persisted today

`_maybe_capture_provider_session` (`agent_collab/daemon.py`) writes, per agent,
into `SessionState.agent_sessions`:

```json
{"backend": "claude_sdk", "provider_session_id": "...", "provider_session_kind": "session"}
```

Identity is accepted only from a selected agent whose `event.source` matches its
configured provider type, and it is **capture only** — nothing resumes it. The
`backend_version`, `resume_fingerprint`, and `last_turn_status` fields proposed
under the resume design do not exist yet.

## Capability semantics and decisions

Keep these definitions strict and backend-specific.

**`resume`** — agent-collab can continue a captured provider session (1) during
a later turn in the same live session, which is what `continuity` already
covers, *and* (2) after the daemon reloads the persisted session, through an
explicit user resume action rather than automatic crash recovery. The resumed
turn must retain provider identity, backend, workdir, model, permission/sandbox
posture, and compatible static configuration. An expired or rejected provider
session produces a structured error and never silently starts a fresh one.
Transcript-in-prompt continuity is not native resume. `continuity` is
deliberately the narrower half; flipping `resume` on in-session continuity alone
would dilute the definition.

**`interrupt`** — agent-collab can end an in-flight provider turn through a
documented provider abort path, reach one deterministic terminal outcome within
a bounded timeout, and leave no provider work believed to be running. Cancelling
only the local asyncio consumer is insufficient when the request may continue
remotely, keep billing, or leave a reusable session in an unknown state.
Completion racing interruption must have one deterministic, idempotent outcome.

**`tool_gate`** — an SDK tool request can pause the turn, become a
session-scoped approval request, accept exactly one authorized approve/deny
decision, return it through the SDK callback, and continue or reject the call
without restarting the turn. Preconfigured permission modes and automatic
provider approval are policy, not a tool gate. An SDK that exposes tool
notifications only *after* execution does not support `tool_gate`.

### Decision (2026-07-30): a capability needs both execution paths

`BackendCapabilities` is a session-independent constant per `(agent_type, backend_id)`,
projected into `describe_options` (`options.py`, the `static` block) with no
session or sandbox context — but behavior now forks on the resolved sandbox
policy. Therefore a backend flips `interrupt`, `tool_gate`, or `resume` only when
**both** its worker path and its in-process path implement it.

*Rejected alternative:* making capability projection policy-aware. It changes a
documented static surface, forces start-time policy resolution into
`describe_options`, and returns a capability answer that depends on a field the
caller has not sent yet. Implementing both paths is cheaper — the in-process path
is a direct adapter call and is normally the simpler of the two once the worker
protocol exists.

### Aggregation

Capabilities remain facts declared by each concrete backend; the reducer
(`summarize_session_capabilities`) is already written and tested against the
all-false reality and needs no change when inputs flip. Session `resume` requires
every selected non-mock backend to support resume *and* every required provider
session ID to be captured; session `interrupt` requires every active backend turn
to support reliable interrupt; session `continuity` requires every selected
backend to have it. Tool approval is reported per agent/backend — never imply a
workflow-wide gate when one agent supports it.
`agent_collab_describe_options`, start settings, session status, and the TUI must
project the same facts.

## Design: interrupt

Not yet implemented. Per-provider abort paths and their open questions are in
Appendix A.

Keep `cancel` exactly as it is. Add a **separate** `interrupt` frame with
different semantics:

| | `cancel` (shipped) | `interrupt` (proposed) |
|---|---|---|
| daemon action | SIGKILL process group first | write frame, wait bounded |
| worker action | cancel run task, exit process | ask the SDK to abort the turn |
| worker afterwards | gone | alive, conversation retained |
| run outcome | transport torn down | `result` with an `interrupted` outcome |
| fallback | — | `cancel` after the deadline |

Why the split matters: interrupt then becomes useful *during* a session rather
than only at its end. An interactive session that can stop a runaway turn, keep
the provider thread, and steer with `post_message` on the next delta prompt is a
materially better product than one whose only stop is burning the session down.

**Mechanics.** The blocker is transport property 1 — `run()` holds the lock for
the whole turn. Do not widen or drop it. Write the `interrupt` frame out of band:
`StreamWriter.write()` appends the whole encoded frame synchronously before any
await, so a frame written from a second task cannot interleave inside another
frame's bytes. Add a small dedicated write lock for clarity and shared
backpressure; keep `_lock` as the run/state lock. No new worker→daemon frame
type is needed: the acknowledgement is the turn's own `result` frame — carrying
an `interrupted` outcome when the abort won, or the completed outcome when the
provider finished first — on the same validated `run_id`/`sequence` stream.
Interrupting an unknown or already-finished `run_id` is a no-op, not an error.

**Runner seam.** The stop path (and stage 4's turn-level operation) reaches the
backend through the defaulted `AgentRunner` interrupt-request method from stage
1: worker-backed runners write the out-of-band frame, in-process runners call
the adapter's held client, and the default returns not-supported — so CLI and
mock runners are untouched and `SessionManager` stays free of SDK types.

**Stop-path integration.** `SessionManager.stop_session` and the referee ask the
active turn to interrupt first, wait a short bounded acknowledgement, then fall
back to local task cancellation and the shipped runner-close / `cancel_active`
teardown. Record sanitized detail:

```json
{"requested": true, "provider_acknowledged": true, "fallback_cancelled": false}
```

Do not report `stopped` until cleanup finishes or the fallback deadline expires.
A provider completion that wins the race stays `done`; a stop that wins stays
`stopped`. Exactly one terminal transition is persisted.

**Turn-level interrupt** (stop this turn, keep the session alive) runs on the
same machinery and is the more valuable half, but it is a distinct public
surface: it belongs in stage 4 with the other surfaces, not smuggled into `stop`.

## Design: tool gating

Not yet implemented. The `can_use_tool` callback fires **inside the worker**, in
the middle of `backend.run()`. The clean shape needs no second writer —
decisions are written by the run loop that already holds the lock.

**Worker side.** When the SDK invokes the permission callback, the worker backend
mints a request id, puts an `approval_request` frame on the same sequenced run
stream, and awaits a future in a `pending_approvals` map. The serve loop is
already polling the control socket at 50 ms (transport property 2), so it
receives the `approval_decision` frame and resolves the future. The callback
returns the SDK's approve/deny result type and the turn continues — no restart,
no new client.

**Daemon side.** `SupervisedWorkerSession.run()` gains an optional injected
`on_approval` callback, invoked from inside the receive loop when an
`approval_request` arrives. Because that loop already holds `_lock`, it writes
the decision itself — one writer, one lock, no deadlock, and the turn's natural
backpressure applies. `on_approval` is what parks in the daemon's approval
registry.

**In-process path.** Under `sandbox = "none"` the SDK callback runs in the
daemon and calls the same approval registry directly — same event, same status,
same operation, no frames.

**Status and settle.** `awaiting_approval` is a live, non-terminal status
distinct from `awaiting_input`, reusing the machinery `wait_result` already
relies on: `post_message` enqueues onto `managed.input_queue` before returning,
`_process_pending_inputs` / `_await_interactive_input` call `queue.task_done()`
in `finally`, so the queue's unfinished-task count is a clean settled signal. An
ordinary `post_message` must never satisfy an approval.

**The event.** A first-class event, not prose:

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

**Requirements.**

- Expose an explicit approval response operation over REST, MCP, CLI, and TUI.
- Bind every response to session ID, request ID, active agent turn, and — on the
  worker path — the worker `instance` id, so a decision cannot be applied to a
  relaunched worker.
- Accept one decision; duplicates are idempotent or rejected with a structured
  conflict.
- **Deny by default** on timeout, stop, daemon shutdown, worker loss, protocol
  error, version skew, and callback failure. A worker that never receives a
  decision must not execute the tool; a daemon that loses the worker must not
  report an approval as delivered.
- An interrupt or stop that lands while an approval is pending denies the
  approval, releases the callback, and yields one terminal outcome.
- Sanitize tool input at the source. The codec's secret scrubbing covers error
  text, not tool arguments.

## Design: restart-safe resume

Not yet implemented.

**On the worker path, resume is a variant of establishment, not a call into a
live object** — the worker does not exist when the user asks to resume. The
daemon relaunches it and includes a `resume` block in the `open` payload it
already builds per backend (`<backend>/sandbox.py:
worker_open_payload_for_agent`). The worker validates and reopens strictly
against the provider, or fails `open` — which the daemon already surfaces as a
structured `outer_sandbox_backend_incompatible` failure and never retries as a
fresh start. That keeps the provider decision inside the backend and keeps
`SessionManager` free of SDK types.

**In-process**, resume goes through the adapter's existing reconnect code; only
the persistence and validation layer above it is new.

### Persistence

Expand `agent_sessions.<agent_id>` from the three shipped keys, without storing
credentials or raw SDK objects:

```json
{
  "backend": "codex_sdk",
  "provider_session_id": "...",
  "provider_session_kind": "thread",
  "backend_version": "...",
  "resume_fingerprint": "...",
  "last_turn_status": "completed"
}
```

The fingerprint covers what must not drift silently: provider type, canonical
backend, model, workdir, permission/sandbox posture (including whether the
session ran worker-backed or in-process), and backend-owned static configuration.
It must not contain secrets.

On daemon restart: keep the current behavior that an in-flight session becomes
`interrupted` and never auto-resume a paid or side-effecting operation; reload
the exact workdir config and backend enablement policy; require an explicit
resume request; reject it if the agent/backend disappeared, is disabled, has
incompatible settings, or does not advertise restart-safe resume; append to the
existing event cursor and transcript so the audit trail stays continuous.

Add the shared operation only after this validation is defined — for example
`POST /sessions/{id}/resume`, `agent_collab_resume`, and
`agent-collab resume SESSION_ID`.

### Durable trajectory root (Antigravity only)

A provider session id is not always the whole of what resume needs. Antigravity
reopens against **local** artifacts too: `SessionContinuationMode.RESUME` needs
the same `save_dir` back, and both paths that supply it destroy it at session
end. Sandboxed, the outer sandbox sets `ANTIGRAVITY_SAVE_DIR`
(`backends/antigravity_sdk/sandbox.py`) to `<daemon runtime base>/<random
hex>/trajectory`, freshly randomized per `describe()`, declared
`Persistence.SESSION` + `CreationPolicy.CREATE_PRIVATE_DIRECTORY`, and removed by
`cleanup_created_session_private_roots`. Unsandboxed, it falls back to a
process-lifetime `tempfile.TemporaryDirectory` (`backend.py`).

Stage 4 must therefore give this backend a host-persistent, agent-collab-owned
trajectory root keyed to the agent-collab session, which pulls in three
consequences no other backend has:

1. it becomes a writable exception the outer read-only sandbox mounts rather than
   a directory it discards, so it needs the same ownership and overlap validation
   `_select_session_state_base` already applies;
2. it becomes reportable state: the install-readiness `state dir` column (0.13.0)
   prints `—` for this backend today precisely because nothing here is
   host-persistent, and unlike the provider homes that column reports for
   `claude_cli`/`codex_cli`, this directory is agent-collab's to create;
3. provider chat trajectories then outlive the session on disk, so retention must
   cover them the way it covers transcripts — reopen must not become an
   unbounded, unswept chat archive.

`xai_sdk` is unaffected (remote handle, `state_roots=()`). Claude and Codex
reopen against provider-side state plus their own provider home, which the
sandbox already treats as a writable exception.

## Agent-facing surface and token cost

The primary consumer of these controls is a delegating MCP agent driving the
documented loop (`start` → `wait_events` digest → `wait_result`). Design the
surfaces so that loop gains capability without gaining shape or cost:

- **Park-state parity.** `awaiting_approval` behaves exactly like
  `awaiting_input` for `wait_events` and `wait_result`: same settle machinery,
  same park-and-return. An agent learns one new status value, not a new polling
  pattern. `agent_collab/mcp-guidance.md` and the delegate-flow guidance add
  `awaiting_approval` to the stop states in the same change that ships the
  status.
- **Decide from the park payload alone.** The `wait_result`/status response for
  a parked approval carries a `pending_approval` block (request id, agent id,
  tool name, bounded summary, decision options) — everything needed to decide.
  The common flow is exactly two calls: the wait that parked and the decision
  operation. No `read_events` round trip.
- **Bounded summaries.** The approval `summary` is truncated at the source to a
  small fixed budget; full tool input is available only through the raw event
  view on demand. #50/#51 showed collection-loop payload size is the dominant
  delegate cost — approval events must not regress it, and the digest view
  carries the summary, never the full input.
- **Three operations, total.** One approval decision operation (approve/deny as
  a parameter), one turn-level interrupt, one resume — each mirrored across
  REST, MCP, CLI, and TUI. No per-provider or per-capability operation
  families, and no new required fields in the hot loop: capability flags ride
  the existing `describe_options`/settings payloads unchanged.
- **Resume preserves the agent's cursors.** Resume keeps the session id and
  appends to the existing event cursor and transcript, so a delegating agent
  re-attaches with its stored cursor instead of re-reading the session.
- **Interrupt is the cheap correction path.** Stopping a runaway turn and
  steering with `post_message` rides the shipped delta-continuation prompts.
  The alternative — stop, restart, re-send the full task — costs a fresh
  provider context plus a full session re-read for the supervising agent. This
  is the economic argument for the steering interrupt over kill-only stop.

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
- Never weaken the kill-first teardown contract (transport property 3) to make a
  graceful path simpler.

## Testing

### Hermetic

Transport coverage belongs with the shipped protocol tests
(`tests/sandbox/test_worker_codec.py`, `tests/sandbox/test_worker_session.py`):
an interrupt frame written while `run()` holds the lock is delivered and does not
interleave; an interrupt for an unknown or finished `run_id` is a no-op;
acknowledgement, deadline expiry, and `cancel` fallback each produce exactly one
terminal outcome; an `approval_request` parks the turn and one decision resumes
it; a lost, oversized, or duplicate decision denies; an interrupt or stop
landing while an approval is pending denies it and yields one terminal outcome;
any daemon/worker protocol-version skew fails at the `hello` handshake rather
than running ungated.

Per-backend coverage belongs under `tests/backends/<backend_id>/` with fake SDK
modules shaped to the verified installed version: interrupt calls the provider
exactly once and handles completion races; interrupt fallback cancels and closes
resources when the provider hangs; a persisted identity is restored after a
simulated daemon restart on **both** the worker `open`-payload path and the
in-process adapter path; incompatible fingerprints and rejected/expired sessions
fail without creating a fresh provider session; tool requests enter
`awaiting_approval`, emit sanitized correlated events, park `wait_result` with
the `pending_approval` block, and resume on approve or deny; duplicate, stale,
cross-session, cross-worker-instance, and post-stop
responses are rejected; approval timeout and daemon shutdown deny and release the
callback; mixed workflows aggregate flags honestly; session-index round trips
preserve only the sanitized resume descriptor; REST, direct MCP,
stdio-via-REST, CLI, and TUI share the same behavior.

The #47 fake-conversation tests already cover the adapter these controls extend.
Keep `AGENT_COLLAB_HOME` isolated everywhere. No hermetic test may import a real
optional SDK, read native credentials, or make a model call. Tests needing a real
Bubblewrap belong in `conditional_tests/`, already gated on the host having it.

### Credentialed

Opt-in, low-cost, per capability, under
`integration_tests/backends/<backend_id>/test_live.py` — and run on **both**
execution paths for a worker-backed backend, since the two-path decision makes
both production:

- interrupt a harmless long-running response, verify provider/client cleanup,
  then verify the conversation is still usable if the backend claims it;
- gate a harmless tool, deny once, then approve once, verifying no execution
  occurs before approval;
- explicit resume after a simulated reload continues the captured session.

Skip when the installed SDK version, account, or provider does not support the
feature. A skipped provider keeps the production capability false.

## Acceptance criteria

- Capability flags are true only for pairs with implemented, tested behavior on
  every execution path that pair can take.
- Explicit resume after daemon restart reloads config/policy, validates a
  sanitized fingerprint, and never silently starts fresh.
- Stop invokes verified provider cancellation when interrupt is advertised, and
  has a bounded fallback that still kills first when it must.
- Tool approval pauses before execution, is visible through all session surfaces,
  accepts one correlated response, and defaults to deny on failure — including
  worker loss and protocol-version skew.
- Session IDs, controller state, callbacks, and approval events expose no
  credentials or raw provider objects.
- Mixed workflows and persisted sessions report conservative, accurate
  capabilities.
- The MCP delegate loop absorbs approvals with one new status and one operation;
  digest and status payload budgets do not regress.
- CLI backends remain all-false unless a separate bidirectional CLI protocol task
  lands.
- Hermetic tests pass, and every enabled capability has a credentialed test on
  each supported execution path.

## Open questions

1. Does the installed Codex app-server expose a turn-cancellation method at all?
   If not, `codex_sdk.interrupt` stays false. (Stage 3)
2. Is an Antigravity conversation still usable for a following turn after
   `ChatResponse.cancel()`? If not, interrupt degrades to a reset and the flag
   stays false. (Stage 3)
3. Antigravity's unknown/expired-id rejection has never been exercised against a
   live provider — only the documented `RESUME` contract backs it. (Stage 4)
4. What retention policy governs durable trajectory roots once they outlive the
   session? (Stage 4)
5. Do any providers gate tool-approval callbacks behind account or plan
   entitlements that a credentialed test would silently skip? (Stages 2–3)

---

## Appendix A: verified provider facts

**Re-verify the claims in a backend's subsection against the installed pin
before implementing that provider, and update the subsection and its date in the
same change.** Claims are tagged with the control they serve. Facts proven for
shipped continuity are reduced to one line plus the test that now pins them;
the tests, not this document, are their guarantee.

### claude_sdk — `claude-agent-sdk` 0.2.126, bundled CLI 2.1.218 (verified 2026-07-24)

- *[continuity — shipped]* One connected `ClaudeSDKClient` (`connect` / `query` /
  `receive_response` / `interrupt` / `disconnect`) accepts sequential turns on
  one provider session with provider-held memory; every message carries the same
  `session_id`. Pinned by
  `integration_tests/backends/claude_sdk/test_live.py::test_provider_memory_across_interactive_turns`.
- *[resume]* After `disconnect()`, `ClaudeAgentOptions(resume=<sid>,
  fork_session=False)` reconnects the exact captured id with memory intact.
- *[resume]* An unknown id fails `connect()` with `ProcessError` (CLI exit 1,
  "No conversation found with session ID") — never a silent fresh session.
- *[resume]* A session materializes incrementally during its first turn: a
  fixture that abandoned turn 1 right after the init `session_id` message (no
  `ResultMessage` observed) still resumed the exact id with the delivered
  prompt's context. Resumability begins at the first delivered user message, not
  the first terminal result.
- *[interrupt]* Client `interrupt()` exists and is the intended abort path — its
  completion semantics under agent-collab's bound are unverified.
- *[interrupt]* Cancelling the local consumer does **not** stop provider work:
  the detached reader and CLI subprocess run until `disconnect()`, whose
  subprocess close is internally bounded (~20 s worst-case terminate/kill
  escalation).
- *[tool_gate]* The persistent client is the precondition for `can_use_tool`;
  the gate cannot exist on a one-shot `query()`. Request id, tool input shape,
  and result types are unverified.
- *[all]* The client is loop-scoped but usable across tasks in one loop (its
  reader is detached via `spawn_detached` -> `loop.create_task`); an `atexit`
  child killer reaps orphaned CLI subprocesses. `disconnect()` is idempotent,
  closes the receive stream the consumer owns, and must not race an active
  `receive_response()`. A cancelled `connect()` unwinds via the SDK's own
  failure-path `disconnect()`.

### codex_sdk — `openai-codex` 0.1.0b3 + `openai-codex-cli-bin` 0.137.0a4; configured local CLI 0.144.4 (verified 2026-07-23)

- *[continuity — shipped]* One open `AsyncCodex` owns an `AsyncThread` whose
  public `run()` accepts repeated collected turns with provider-held memory.
  Pinned by
  `integration_tests/backends/codex_sdk/test_live.py::test_provider_memory_across_interactive_turns`.
  The public 0.144.4 wheel has the same relevant APIs.
- *[resume]* `AsyncCodex.thread_resume(thread_id, ...)` reopens a materialized
  thread after the first client closes; a one-turn fixture resumed the exact id
  and read its persisted turn.
- *[resume]* A no-model `thread_start` alone does not materialize the thread
  (`includeTurns` is rejected before the first user message), so one lowest-cost
  turn is the minimum reconnect fixture. Starting a new thread with the same
  transcript is not thread resume; a rejected or expired `thread_resume` fails
  structurally and never falls back to `thread_start`.
- *[interrupt]* `AsyncThread.run()` waits through `asyncio.to_thread` on a
  synchronous notification queue; cancelling that waiter does **not** interrupt
  the provider worker, and cleanup requires `AsyncCodex.close()` to terminate the
  app-server transport. No turn-cancellation API has been located — see open
  question 1.
- *[tool_gate]* Command/file-change approval notifications and response methods
  on the app-server are unverified.
- *[all]* Note the two-pin ambiguity above: the bundled CLI binary and the
  configured local CLI differ. Re-verification must state which one it exercised.

### xai_sdk — `xai-sdk` 1.17.0 (verified 2026-07-24)

- *[continuity — shipped]* Continuity is the public stored-response API, not the
  `Chat` object: `store_messages=True` persists each completion under its
  `response.id`, and `previous_response_id=<stored-id>` makes the server prepend
  history without changing the new chat's local messages. Pinned by
  `integration_tests/backends/xai_sdk/test_live.py::test_provider_memory_across_stored_response_chain`.
- *[resume]* A normal `Chat` owns only a mutable local `messages` collection —
  reusing it is local history replay, not provider continuity. `conversation_id`
  only labels OpenTelemetry spans.
- *[resume]* An unknown stored id fails with gRPC `NOT_FOUND`, so continuation
  failure is structural. The adapter deletes captured stored completions
  best-effort on final close, which restart-safe resume would have to reconcile
  with retention.
- *[interrupt]* **Expected permanently false.** `sample()` is one unary gRPC call
  with no documented server-side abort; local cancellation does not stop remote
  work. `Chat` and collected `Response` have no close method, and
  `AsyncClient.close()` exposes no close-vs-request coordination contract, so the
  adapter shields an in-flight sample and retains ownership until it settles.
- *[all]* The shared environment now runs protobuf 7.35+ behind
  `backends/xai_sdk/compat.py`, which defeats this version's import-time
  protobuf-major gate. Re-check on any `xai-sdk` bump whether upstream accepts
  protobuf 7 so the shim can retire.

### antigravity_sdk — `google-antigravity` 0.1.8, Python 3.14.4 (verified 2026-07-24)

- *[continuity — shipped]* One entered `Agent` owns one stateful
  `Conversation`/localharness connection; `chat()` sends on that connection,
  accumulates step history, and accepts sequential calls with provider-held
  memory. Pinned by
  `integration_tests/backends/antigravity_sdk/test_live.py::test_provider_memory_across_interactive_turns`
  (Vertex `us-central1`, `gemini-2.5-flash` — a CLI-style
  `gemini-3.5-flash-low` target was rejected before inference as not a Vertex
  publisher model for the project).
- *[resume]* `Agent.conversation_id` is `None` before start, available after at
  least one exchanged message, and is the local connection's runtime-assigned
  main trajectory id.
- *[resume]* The strict reopen API is `LocalAgentConfig(conversation_id=<id>,
  session_continuation_mode=SessionContinuationMode.RESUME)` then `async with
  Agent(config)`. `RESUME` is documented to fail when the session does not exist.
  Never use `CREATE_OR_RESUME`; always check the observed resumed id against the
  requested one; never fall back to a fresh `Agent`. A no-model fixture confirmed
  the enum and id handling and that malformed ids fail Pydantic validation
  distinctly — a live rejection is still unexercised (open question 3).
- *[resume]* `save_dir` maps to the localharness trajectory `storage_directory`.
  Letting each `Agent` synthesize a new temporary directory breaks reopen — see
  *Durable trajectory root*.
- *[interrupt]* `Agent.chat()` returns a lazy `ChatResponse`; cancelling a local
  `resolve()` consumer does not invoke provider cancellation, while
  `ChatResponse.cancel()` delegates to `Conversation.cancel()` (local
  `halt_request`, then `AntigravityCancelledError` on the receive path). Used
  today only as best-effort abnormal-turn cleanup; whether the conversation
  survives it is open question 2.
- *[all]* `Agent.__aexit__()` disconnects: processor tasks and reader cancelled,
  WebSocket close bounded to 0.5 s, stdin closed, native process waited up to
  180 s before terminate/kill escalation. Disconnect is not safe to race with
  active response iteration, so the adapter serializes run/reset/close under one
  lock while the referee's outer bounded close adopts a slow close as a reaper.
