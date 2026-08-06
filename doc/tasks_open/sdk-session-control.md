# Provider session control: interrupt, tool approval, restart-safe resume

**Status:** Open. Continuity shipped (#47); `interrupt`, `tool_gate`, and
`resume` are false for every backend. Design resynced 2026-07-30 against 0.13.0,
which made the outer read-only Bubblewrap worker the default execution path and
so relocated where the SDK controls have to be built. Resume scope was widened
the same day after re-verifying the installed provider CLIs: Claude, Codex, and
Grok expose strict headless resume-by-id surfaces, as does Antigravity.
Agent-collab's current Antigravity parser is still plain text and
captures no conversation id, but installed CLI 1.1.8 now offers structured
`json`/`stream-json`; its root identity payload must be checked before declaring
that backend blocked. Staging therefore separates the shared SDK control
transport from a provider-neutral resume lifecycle that can serve verified CLI
and SDK backends.

**Created:** 2026-07-10. **Issue:**
[#20](https://github.com/lauriparviainen/agent_collab/issues/20).

**Predecessor:** [subagent-delegation-and-thread-continuity.md](../tasks_closed/subagent-delegation-and-thread-continuity.md)
(#47) built the substrate. [antigravity-read-only-bubblewrap-sandbox.md](../tasks_closed/antigravity-read-only-bubblewrap-sandbox.md)
(#43) built the worker boundary.

## Next work — pick up here (2026-08-06)

This design has been through multi-round adversarial review (three internal
rounds plus eleven cross-vendor dual-review rounds against the shipped code on
that date); treat the body below as settled and implement against it rather
than re-deriving it. The outstanding work, in the order the next agent should
take it:

1. **Fix the shipped `codex_sdk` continuity gate first.** Its
   `conversation_active()` keys on worker liveness alone — no
   `_worker_provider_active` gate, no soft-drop — deviating from the #47
   contract the other two worker SDKs implement (see transport property 4).
   This is a live defect independent of the stages: a worker turn ending
   without a `thread_id` feeds hidden context into the next delta prompt.
   File it as a discrete issue and land the fix plus its hermetic test before
   or with stage 1.
2. **Stage 1 — shared control plane.** Protocol v2 frames (`approval_request`
   is a first-class frame emitted through the serve loop's single writer; a
   late `approval_decision` is a no-op, never fatal); the event/status
   vocabulary work (`VALID_TYPES`, `LIVE_WAIT_STATUSES`, every hard-coded
   status enum surface); the registry-keyed settle arm of `_result_settled`;
   the approval registry and decision operation with deny-by-default —
   including turn-deadline deny, the `abandoned` outcome, and deny-before-
   interrupt/close ordering on the stop path.
3. **Capability projection wiring.** `summarize_session_capabilities`'
   production call site passes no capture/eligibility set and freezes
   capabilities at start; the projection must be re-evaluated after capture,
   turn commit, and restore (see *Aggregation*). Without this every later
   capability flip projects wrongly.
4. **Stage 2 — Claude SDK interrupt + tool gating**, including the clocks
   design (re-armed remaining-budget loop, per-park and per-turn caps) and
   resolving open questions 9–10 (provider-side decision deadlines, callback
   concurrency) for this backend.
5. **Stage 3 — Codex, Antigravity, and xAI SDK controls** (open questions
   1–2; record negatives explicitly).
6. **Stage 4 — CLI continuity, restart-safe resume, public surfaces**, in its
   five increments. Mind the pieces added in review: keyed-merge identity
   capture (the shipped capture write full-replaces the descriptor), the
   session-level workflow-phase record (no stage replay on resume), the
   atomic per-session resume claim, the `interrupt_acknowledged` eligibility
   marker, the `xai_sdk` close-deletes-resume-material conflict, and the
   durable Antigravity trajectory root.

## Purpose and scope

Turn captured provider session identities and shipped provider adapters into
three provider-tested controls:

- **interrupt** — end an in-flight provider turn through a verified provider abort
  path;
- **tool_gate** — surface a provider tool request through daemon, REST, MCP, CLI,
  and TUI, then return one explicit approve/deny decision to the waiting SDK;
- **resume** — reopen a captured provider session *across a daemon reload*
  through an explicit user action.

**The bar for flipping a capability.** Never flip a flag because an SDK or CLI
exposes a suggestive method or returns a session ID. A backend advertises a
capability only when agent-collab owns the complete lifecycle on *every
execution path that backend can take* (see the execution-path decision below),
hermetic tests cover it, and a credentialed provider smoke test passes.
Backends flipping independently while others stay false is a valid, shippable
outcome — the session reducer already ANDs conservatively.

**CLI scope:** restart-safe resume and the in-session continuity it requires are
in scope for `claude_cli`, `codex_cli`, and `xai_cli`. `antigravity_cli` joins
if the new structured print stream exposes a stable root conversation id for
the exact turn; otherwise it remains gated without guessing from mutable cache
state. CLI interrupt and tool gating remain out of scope because the shipped
one-shot transports provide no verified bidirectional control channel.

**Resume policy decision (2026-07-30):** implement native resume for every
backend that can prove strict resume-by-id and preserve the required provider or
local state. Do not limit resume to SDKs or to long-lived transports. The
expected value is substantial token savings: after the first turn, the provider
retains its native thread and agent-collab sends only the delta continuation
prompt instead of making another backend re-read the task, guardrails, and
session history. A backend remains false only for a recorded technical blocker,
not because it is CLI-based.

**Also out of scope:** converting `codex_cli` to `codex app-server`, adding a
second Codex app-server backend, any "approve all" path, provider resume via
"most recent" selectors, and overloading `start` with a prior session ID.

## Staged delivery

Stage 1 is a prerequisite for the default SDK execution path of three backends
and is new since the resync — what used to be three per-provider stages is now
one shared transport stage plus thin per-provider mappings. Stage 4 owns the
provider-neutral persisted resume lifecycle and thin CLI/SDK establishment
mappings.

### Standing duties for every stage

- **Re-verify before you implement.** Appendix A records provider facts against
  a specific pin. Before a provider stage, re-verify its claims against the
  *installed* pin; update the appendix and its verified date in the same change.
  An appendix nobody refreshes is the failure mode this rule exists to prevent.
- **Every path or no flag.** See the execution-path decision. A capability that
  works only under `read-only` or only under `sandbox = "none"` stays false.
  For worker-backed SDKs this means worker plus in-process adapters; for a CLI
  backend it means the outer-sandbox and direct subprocess launch paths.
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
- **The event and status vocabulary:** `approval_request` and
  `approval_resolved` become first-class members of the event `type`
  vocabulary. Today `Event.create` coerces an unknown type to `status`
  (logging only a warning) and `worker_codec.event_from_payload` rejects it
  outright, so this
  is a deliberate, wire-visible change to the `read_events`/`wait_events`
  `types` filter contract and the digest projection — not a free-form `raw`
  payload. Likewise `awaiting_approval` must be added everywhere the status
  set is hard-coded — `LIVE_WAIT_STATUSES` (`retention.py`), both
  `project_build.py` status-enum schemas, the `api_schema` settled contract,
  and the TUI/CLI status renderings — so `wait_events` keeps blocking while
  parked (instead of returning instantly in a busy spin) and every surface can
  display the state. `_result_settled` gains a **new** registry-keyed arm
  rather than joining the `awaiting_input` one (see *Status and settle*).

No backend flips a capability; no provider SDK is touched.

*Done when:* the frames and the decision operation round-trip under the
hermetic worker harness; a v2 daemon with a worker that requests nothing new
behaves byte-for-byte as v1 does today on turn traffic and emitted events (the
status-enum and filter-vocabulary additions are the one deliberate exception);
any protocol-version-skewed pairing
fails at the `hello` handshake rather than running ungated.

### Stage 2 — Claude SDK interrupt + tool gating

The only backend with both APIs documented. Implement the worker mapping and the
in-process adapter mapping together; the decision surface exists from stage 1,
so this stage wires the provider and flips the flag.

*Done when:* `claude_sdk.interrupt` and/or `claude_sdk.tool_gate` are true with
hermetic plus credentialed coverage on both execution paths, or a negative
finding is recorded in Appendix A.

### Stage 3 — Codex, Antigravity, and xAI SDK controls

Confirm the Codex app-server's turn-cancellation and approval surfaces, and
whether an Antigravity conversation is still usable after `ChatResponse.cancel()`
(if it is not, interrupt degrades to a reset and the flag stays false). Flip each
capability independently. `xai_sdk.interrupt` is expected to stay false; record
the negative result rather than leaving it undecided.

### Stage 4 — CLI continuity, restart-safe resume, and public surfaces

Land explicit resume and the turn-level interrupt operation across
REST/MCP/CLI/TUI once each operation's persistence and authorization semantics
are stable. They share surface conventions but are independent deliverables:
turn-level interrupt never gates live continuity or explicit resume.
Implement strict provider establishment by captured id for each verified
backend:

- `claude_cli`: `claude --resume <session-id> -p ...`;
- `codex_cli`: `codex [global-options] exec resume --json
  [resume-options] <thread-id> ...`;
- `xai_cli`: `grok --resume <session-id> -p ...`;
- SDK backends: worker-open or in-process adapter resume as designed below;
- `antigravity_cli`: only after exact print-mode identity capture is solved,
  then `agy --conversation <conversation-id> -p ...`.

Stage 4 first converts `antigravity_cli` to
`agy -p --output-format stream-json ...` with a typed NDJSON parser. Capture
representative root-turn fixtures from `agy 1.1.8` before implementing the
parser; the transport migration may then ship independently of resume and does
not depend on the stream containing a root conversation id. It retires the
message-only success contract and `clean_eof_fallback`: malformed NDJSON,
invalid required records, a failed terminal result, or a missing terminal result
is a structural turn failure with no plain-text fallback. Unknown additive
non-terminal records may be ignored or exposed as bounded verbose status; they
must not be mistaken for identity or terminal success.

This migration deliberately makes `agy >= 1.1.8` the backend-wide compatibility
floor, not merely a conditional resume requirement. Every `antigravity_cli`
start probes it and an older binary fails readiness with an actionable
required/observed-version diagnostic. After the parser fixtures prove a stable
root conversation id, enable identity capture and strict resume; if they do not,
keep `continuity=false, resume=false` while retaining the structured transport.

For Antigravity SDK this also means promoting the trajectory `save_dir` to a
durable agent-collab-owned root — see *Durable trajectory root* under the resume
design.

Ship the stage in five independently testable increments: the Antigravity
structured transport and version floor; live CLI continuation by exact captured
id; the durable Antigravity trajectory root and SDK resume establishment;
persisted explicit resume plus its public operation; and the turn-level
interrupt operation once the first provider abort mapping from stages 2–3 is
verified.
A CLI may flip `continuity` after its two-turn live path passes both launch
modes; it flips `resume` only after reload, fingerprint validation, and the
explicit resume operation pass. Turn-level interrupt can ship before or after
those increments. This delivers the token-saving hot path without weakening the
stricter restart-safe definition.

*Done when:* explicit resume reloads config and policy, validates a sanitized
fingerprint, addresses providers by captured id rather than "last session," and
never silently starts fresh; every CLI with verified exact identity capture
either flips each completed capability after both subprocess launch paths pass
or records a verified negative; every SDK backend with a verified reopen path
flips `resume` only after every production path for that backend passes —
worker plus in-process for worker-backed SDKs, the in-process path alone for
`xai_sdk`, which never takes the worker path — or records a verified negative;
each public operation is identical across all
four surfaces when that operation ships, without blocking another operation.

Each stage must be independently shippable and must not make existing CLI or
message-first SDK workflows less reliable.

## Shipped substrate — do not rebuild

### Continuity (#47)

`AgentRunner` (`agent_collab/runners.py`) has `conversation_active() -> bool` and
idempotent `async close()`, both defaulted so existing CLI and mock runners are
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

The CLI runner substrate can use the same seam without retaining a subprocess.
A resume-aware CLI runner holds the last exact provider id, returns
`conversation_active()` only after that id was captured from its own turn **and
the turn reached an eligible terminal outcome**, and launches the next one-shot
command through the provider's strict resume-by-id form with a delta prompt.
After a daemon reload, the same runner is constructed from a validated persisted
descriptor. No second long-lived protocol is needed for CLI continuity or
resume.

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
4. **Continuity is gated on a captured provider id — with one shipped gap.**
   In `claude_sdk` and `antigravity_sdk`, `conversation_active()` is true only
   when the worker is live *and* a provider-session event set
   `_worker_provider_active`; a turn that ends without a captured id
   soft-drops the worker so hidden client context cannot join the next full
   task. `codex_sdk` deviates today: its `conversation_active()` keys on
   worker liveness alone, with no provider-id gate and no soft-drop, so a
   worker turn that ends without a `thread_id` can feed hidden context into
   the next delta prompt. Stage 2–4 work must not assume this property for
   `codex_sdk`; closing the gap to match the other two backends is the
   expected outcome, and its hermetic test lands with the first stage that
   relies on the gate.

### What is persisted today

`_maybe_capture_provider_session` (`agent_collab/daemon.py`) writes, per agent,
into `SessionState.agent_sessions`:

```json
{"backend": "codex_cli", "provider_session_id": "...", "provider_session_kind": "thread"}
```

Identity is accepted only from a selected agent whose `event.source` matches its
configured provider type. The Claude, Codex, and Grok CLI parsers already emit
their provider identity (`session_id`, `thread_id`, and `sessionId`
respectively); the shipped Antigravity plain-text parser does not, while its new
structured print shape is unverified. Identity is **capture only** — nothing
resumes it. The `backend_version`, `resume_fingerprint`, and `last_turn_status`
fields proposed under the resume design do not exist yet.

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

**`tool_gate`** — a provider tool request can pause the turn, become a
session-scoped approval request, accept exactly one authorized approve/deny
decision, return it through the provider callback, and continue or reject the call
without restarting the turn. Preconfigured permission modes and automatic
provider approval are policy, not a tool gate. A transport that exposes tool
notifications only *after* execution does not support `tool_gate`.

### Decision (2026-07-30): a capability needs every execution path

`BackendCapabilities` is a session-independent constant per `(agent_type, backend_id)`,
projected into `describe_options` (`options.py`, the `static` block) with no
session or sandbox context — but behavior now forks on the resolved sandbox
policy. Therefore a backend flips `interrupt`, `tool_gate`, or `resume` only when
**every production path** implements it: worker and in-process for worker-backed
SDKs, and outer-sandbox plus direct subprocess launch for CLIs.

*Rejected alternative:* making capability projection policy-aware. It changes a
documented static surface, forces start-time policy resolution into
`describe_options`, and returns a capability answer that depends on a field the
caller has not sent yet. Implementing every path is cheaper than changing the
capability contract. For SDKs, the in-process path is normally the simpler
mapping once the worker protocol exists. For CLIs, both paths still launch one
process; the difference is whether the backend-owned argv and provider state
roots pass through the outer sandbox adapter.

### Aggregation

Capabilities remain facts declared by each concrete backend; the reducer
(`summarize_session_capabilities`) keeps its conservative AND shape when
inputs flip, but its shipped second input is capture alone
(`captured_session_ids`) — and the sole production call site
(`SessionManager._session_capabilities`) does not pass it at all, so live
sessions always reduce over an empty capture set today. Stage 4 must wire
that call site to supply, per agent, "holds a fully eligible resume
descriptor" — captured id, eligible `last_turn_status`, valid
`prompt_event_cursor`, compatible fingerprint, not quarantined, mock agents
excluded — not merely reinterpret an argument that is never provided, or the
projection will report `resumable` wrongly in both directions: stuck false
because nothing is wired, or true for sessions the operation must reject.
Wiring the start-time call site alone is still wrong, because
`_session_capabilities` runs during start preparation and freezes
`SessionState.capabilities` — at that moment no descriptor can exist, so the
frozen value would pin `resumable` false for the session's life and survive
reload stale. The projection must be re-evaluated through the same reducer
after every identity capture and turn commit and when a persisted session is
restored, so the projected fact always matches what the operation would
decide. Session `resume` requires every selected non-mock
backend to support resume *and* every required descriptor to be eligible in
exactly that sense. Capture alone is not readiness. One consequence stated plainly rather than left to be derived from
three sections: with rebind out of scope, a single quarantined agent (see
*CLI establishment*) makes the whole session's `resume` permanently
unavailable — a new session is the only
recovery — and the projection must say so before the operation is attempted.
`interrupt` has a static and a dynamic face, and they must not be conflated:
the static `interruptible` projection keeps the reducer's conservative AND
over every *selected* backend, while the turn-level interrupt *operation* is
judged dynamically against the active turn — it succeeds only when every
backend currently in flight supports reliable interrupt, so a mixed-roster
session can be statically `interruptible=false` while a specific turn is
still interruptible, and the operation's structured error names the backend
that blocked it. Session `continuity` requires every selected backend to have
it. Tool approval is reported per agent/backend — never imply a workflow-wide
gate when one agent supports it.
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
| worker action | none — SIGKILLed before any frame (its `cancel` frame handler exists but the daemon never exercises it) | ask the SDK to abort the turn |
| worker afterwards | gone | alive, conversation retained |
| run outcome | transport torn down | `result` with an `interrupted` outcome |
| fallback | — | `cancel_active` kill-first teardown after the deadline |

Why the split matters: interrupt then becomes useful *during* a session rather
than only at its end. An interactive session that can stop a runaway turn, keep
the provider thread, and steer with `post_message` on the next delta prompt is a
materially better product than one whose only stop is burning the session down.

**Mechanics.** The blocker is transport property 1 — `run()` holds the lock for
the whole turn. Do not widen or drop it. Write the `interrupt` frame out of band:
`StreamWriter.write()` appends the whole encoded frame synchronously before any
await, so a frame written from a second task cannot interleave inside another
frame's bytes. Add a small dedicated write lock for clarity and shared
backpressure, route every daemon→worker frame through that helper, and keep
`_lock` as the run/state lock. No new worker→daemon frame type is needed: the
acknowledgement is the turn's own `result` frame — carrying an `interrupted`
outcome when the abort won, or the completed outcome when the provider finished
first — on the same validated `run_id`/`sequence` stream. Interrupting an
unknown or already-finished `run_id` is a no-op, not an error.

**Runner seam.** The stop path (and stage 4's turn-level operation) reaches the
backend through the defaulted `AgentRunner` interrupt-request method from stage
1: worker-backed runners write the out-of-band frame, in-process runners call
the adapter's held client, and the default returns not-supported. CLI and mock
runners therefore remain non-interruptible even when a CLI runner separately
implements resume, and `SessionManager` stays free of provider-specific types.

**Stop-path integration.** `SessionManager.stop_session` and the referee first
deny every pending approval — releasing any parked callback and, on the
in-process path, the adapter lock it holds (see *In-process path* under tool
gating) — then ask the active turn to interrupt, wait a short bounded
acknowledgement, and fall back to local task cancellation and the shipped
runner-close / `cancel_active` teardown. The deny precedes the interrupt
request because an interrupt delivered while the SDK is blocked in a
permission callback may not release that callback. Record sanitized detail:

```json
{"requested": true, "provider_acknowledged": true, "fallback_cancelled": false,
 "approvals_denied": 0}
```

`approvals_denied` counts the pending approvals the stop released; omitting it
would hide a side effect the operation itself caused.

Do not report `stopped` until cleanup finishes or the fallback deadline expires.
The completion race is decided at the *turn* level, not the session level: a
provider completion that wins commits its `completed` turn outcome and is
never rewritten as interrupted, while a stop that wins commits `interrupted`
— but a session with a stop requested terminates `stopped` either way, never
`done`, because planned stages remain abandoned regardless of how the last
turn ended. Exactly one turn outcome and one terminal session transition are
persisted.

**Turn-level interrupt** (stop this turn, keep the session alive) runs on the
same machinery and is the more valuable half, but it is a distinct public
surface: it belongs in stage 4 with the other surfaces, not smuggled into `stop`.
It also needs a referee contract the shipped code does not have: today any
non-`completed` required-turn outcome raises `RequiredTurnFailed` and the
daemon maps that to session `failed`, which would burn down exactly the
session the operation exists to keep alive. An operator-requested turn-level
interrupt is therefore exempt from every failure mapping a non-`completed`
outcome triggers today — `RequiredTurnFailed` on the planned stage loop and
on directed turns, *and* the parallel accept filter: a multi-member stage
whose members ended `interrupted` at the operator's request is an abandoned
stage, never `ParallelStageFailed` — and the exemption alone is not enough:
skipping the raise inside the stage loop would simply start the next planned
stage. The operation makes the referee abandon the remaining planned stages
for the current prompt as well — the turn commits its `interrupted` outcome,
the provider thread and runner are retained, and the interactive session
drops directly into its input loop, parking at `awaiting_input` so the next
`post_message` steers on the shipped delta prompt. An interrupted directed
turn returns to `awaiting_input` the same way; it never fails the session.
The completion race follows the stop rule at the turn level — a provider
completion that wins stays `completed` and is never rewritten — but the
abandonment and the park are effects of the *operation*, not of the turn
outcome: the remaining stages are still abandoned and the session still
parks, and the operator simply steers from a completed turn instead of an
interrupted one.
Once the persisted-resume increment ships, the interrupt path also writes the
session-level phase record (planned stages abandoned, parked in the input
loop) in the same transition that parks the session — otherwise a reload
while parked would resume a referee that re-runs the abandoned stages.
Two consequences must be stated rather than discovered. First, the fallback
degrades the contract, not the session: if the provider misses the bounded
acknowledgement and the `cancel_active` kill runs, the session still parks at
`awaiting_input`, but the worker is gone — `conversation_active()` is false,
the next prompt re-establishes through the #47 adapter contract (native
resume or structural failure, never a silent fresh thread), and the
operation's detail records `provider_acknowledged: false,
fallback_cancelled: true` so the caller knows continuity was lost. Second,
the persisted `last_turn_status` distinguishes the two: a provider-
*acknowledged* interrupt persists `interrupted` with an
`interrupt_acknowledged` marker, and a backend with verified `interrupt` may
include that state in its restart-eligible set from the outset — the
acknowledged abort is itself the "no provider work believed to be running"
proof the widening rule demands. An unacknowledged or fallback-killed
interrupt stays ineligible, so a reload while parked right after one forfeits
restart-safe resume — the same honestly-recorded cost as a *crash* landing on
a parked approval (a clean shutdown may fare better; see *Persistence*). On a non-interactive session the
operation is rejected with a structured conflict — there is no input loop to
continue into, and `stop` already covers ending the work.
Stage 4 owns its surface conventions, but the increment is deliberately
independent of the resume increments: it may land as soon as the first provider
abort mapping from stages 2–3 is verified. It does not ship earlier than that — a
public operation whose only possible answer is not-supported freezes its
response shape (sync/async, completion-race outcome) before the stage 2–3
findings that must inform it.

## Design: tool gating

Not yet implemented. The `can_use_tool` callback fires **inside the worker**, in
the middle of `backend.run()`. The clean shape keeps one receive loop and reuses
the bounded out-of-band control writer from interrupt; it never parks that loop
or the turn lock on a human decision.

**Worker side.** When the SDK invokes the permission callback, the worker backend
mints a request id, enqueues an `approval_request` payload onto the same
serve-loop queue that carries mid-run `event` payloads, and awaits a future in
a `pending_approvals` map. The wire shape is one thing, stated once: on the
run stream it is a first-class protocol v2 **frame type** — a peer of `event`,
carrying the same `run_id`/`sequence` discipline — not an `event` frame with a
special inner type; the v2 `SupervisedWorkerSession` receive loop accepts it
and dispatches `on_approval`, while today's loop would reject it as an
unexpected frame, which is exactly the version-skew failure the `hello` gate
catches first. The session-level `approval_request` *event* (the transcript
vocabulary from stage 1) is minted by the daemon when it registers the
request — the wire frame and the transcript event are distinct layers that
share a name. The callback never writes the socket itself: the serve loop
remains the single writer that assigns `sequence` and sends, exactly as
shipped — a direct send from the run task would race the serve loop's framing
and sequence numbering and tear the worker down on a corrupt stream. The serve loop is
already polling the control socket at 50 ms (transport property 2), so it
receives the `approval_decision` frame and resolves the future. The callback
returns the SDK's approve/deny result type and the turn continues — no restart,
no new client.

**Daemon side.** `SupervisedWorkerSession.run()` gains an optional injected
`on_approval` callback. When an `approval_request` arrives, the receive loop
registers it and starts a bounded decision task; it does **not** await the
registry while holding `_lock`. The task parks in the daemon's approval
registry, then sends the correlated `approval_decision` through the same small
out-of-band writer and write lock used by `interrupt`. The receive loop remains
able to observe a terminal result, while stop/close can deny and release the
pending decision without waiting for the turn lock. Finalization cancels and
denies any unresolved registry entry and waits only a bounded time for its task
to finish before releasing run state. There is still one framed writer at a
time, but no lock is held across human latency.

**In-process path.** Under `sandbox = "none"` the SDK callback runs in the
daemon and calls the same approval registry directly — same event, same status,
same operation, no frames. One asymmetry must be designed for, not discovered
at implementation time: all four SDK adapters serialize run/reset/close under a
single lock, and the in-process callback parks *inside* `adapter.run()`, so
that lock is held across human latency and a concurrent `runner.close()`
blocks on it. The stop path therefore denies pending registry entries
**before** closing runners — the deny releases the callback, lets `run()`
unwind, and frees the lock — and only a provider that then fails to unwind
promptly takes the bounded-close reaper path.

**Status and settle.** `awaiting_approval` is a live, non-terminal status
distinct from `awaiting_input`, and it needs its own settle predicate. The
`awaiting_input` arm of `_result_settled` cannot be reused: it requires
`input_accepting`, which only the interactive between-turns input loop sets —
an approval parks *mid-turn*, and in the primary `interactive=false` review
workflows that flag is never set at all — and it requires an empty
`input_queue.unfinished`, while `post_message` legitimately queues during an
active turn. Approval settle is keyed on the approval registry alone: the
session is settled while its status is `awaiting_approval` and the registry
holds at least one unresolved request, and the registry notifies waiters on
register and on resolve the way the input queue's task-done hook does today.
An ordinary `post_message` must never satisfy an approval; symmetrically, a
queued `post_message` must never block the approval park from settling — it
stays deferred on `input_queue` until the next input boundary, exactly as it
does during any other active turn. The status itself exists only while the
registry holds an unresolved request: resolving the last one returns the
session to its running state as the turn continues, and a terminal result
exits it through the normal turn transition.

**Clocks.** While a turn is parked on approval, the referee's per-turn timeout
must not keep running, or human latency silently converts gated turns into
`timed_out` outcomes — which, once persisted, forfeit the session's
restart-safe resume under the initial `completed`-only contract. The shipped
deadline is a single fire-and-forget `asyncio.sleep` in `_run_agent_turn`; it
cannot be paused, so it becomes a re-armed remaining-budget loop that excludes
parked intervals. The exclusion is fail-closed: excluded time is hard-capped
at one approval deadline per parked request and by a bounded per-turn total
across all parks, and any ambiguity — registry entry gone, worker lost,
unknown request id — resumes the clock rather than extending it.
The approval deadline itself is a bounded, configurable start setting with a
conservative default; expiry denies. The interactive idle timeout does not run
during a mid-turn park (the referee is not in its input loop). And the
exclusion covers only agent-collab's local deadline: agent-collab also
propagates the same configured timeout into some provider argv (Antigravity's
`--print-timeout`), and a provider SDK may hold its own decision deadline on a
pending callback — a per-backend fact stages 2–3 must record in Appendix A
(open question 9). Recording is not the whole obligation: if a backend's
provider-side decision deadline cannot be disabled or proven longer than the
configured approval deadline, that backend's `tool_gate` stays false — or the
effective approval deadline is clamped strictly below the provider's — because
a gate the provider can time out from under does not own the complete
lifecycle.

**The events.** First-class typed events, not prose (see the stage-1
vocabulary change):

```json
{
  "source": "tool",
  "type": "approval_request",
  "raw": {
    "request_id": "opaque-session-scoped-id",
    "agent_id": "claude",
    "tool_name": "Bash",
    "summary": "python -m unittest discover -s tests",
    "summary_truncated": false,
    "decision_options": ["approve", "deny"]
  }
}
```

The summary is built at the source by middle-elision — head and tail kept, the
elided span marked — because the tail of a long command is exactly what naive
head-truncation hides; `summary_truncated` says whether elision occurred. Full
tool input is not a new payload tier: it is available only through the
existing raw event view under the shipped `tool_output="full"` projection and
its `MAX_FULL_TOOL_BYTES` budget.

Every resolution emits a matching digest-sized `approval_resolved` event —
request id, outcome, and resolution reason, never tool input. Outcomes are
`approved` and `denied` (an authorized decision, recording the deciding
surface), `auto_denied` (timeout, stop, shutdown, worker loss, protocol error,
version skew, or callback failure — the reason is recorded), and `abandoned`
(the provider finished or dropped the turn while the request was pending;
nothing executed and no decider acted).

**Requirements.**

- Expose an explicit approval response operation over REST, MCP, CLI, and TUI.
  Authorization is the daemon's single shared bearer token — the same bar as
  `post_message` and stop. Agent-collab has no per-caller principal anywhere,
  so "authorized" means exactly token possession or in-process access, and the
  audit record carries only transport-verified attributes (surface, request
  id, outcome); it must not invent a client-supplied principal field.
- Bind every response to session ID, request ID, active agent turn, and — on the
  worker path — the worker's per-launch `instance` identifier carried in its
  `hello` frame, so a decision cannot be applied to a relaunched worker.
- Accept one decision; duplicates are idempotent or rejected with a structured
  conflict. A decision arriving after its turn's terminal result is stale and
  rejected the same way as a post-stop response — and the same rule holds
  inside the worker: an `approval_decision` frame for an unknown request id or
  a cleared run is a no-op, mirroring interrupt on an unknown `run_id`. It
  must never take the fatal unknown-frame path that tears the worker down;
  otherwise a decision racing the provider's own turn completion kills a
  healthy worker.
- **Deny by default** on timeout, stop, daemon shutdown, worker loss, protocol
  error, version skew, and callback failure. A worker that never receives a
  decision must not execute the tool; a daemon that loses the worker must not
  report an approval as delivered.
- A turn `result` that arrives while requests are still pending retracts them
  as `abandoned`: the worker resolves every pending future to the SDK's deny
  result **before** publishing the `result` — never a bare drop or
  cancellation, so a callback task the SDK abandoned internally still unwinds
  with a definite answer instead of hanging on a dead future — then the
  daemon removes the registry entries, exits `awaiting_approval` (the
  arriving result drives the normal turn-status transition), and notifies the
  settle machinery. Without this, a provider that abandons a tool call (model
  changed course, SDK errored out of the tool) pins the session parked
  forever.
- An interrupt or stop that lands while approvals are pending denies every
  pending approval, releases the callbacks, and yields one terminal outcome;
  the stop detail reports how many it denied. A local *turn-deadline* expiry
  behaves the same way: deny and release every registry entry for that turn
  before the runner is cancelled, so `awaiting_approval` cannot outlive its
  turn and no close path waits on a still-parked callback.
- Sanitize tool input at the source. The codec's secret scrubbing covers error
  text, not tool arguments.

## Design: restart-safe resume

Not yet implemented.

Resume is a variant of provider establishment, not recovery of a Python object
or child process. The shared layer passes a sanitized resume descriptor to the
selected backend; that backend owns the exact reopen mechanism and strict
provider failure. `SessionManager` remains free of provider-specific types.

### CLI establishment

A CLI resume launches another one-shot process. A resume-aware CLI runner:

1. uses the shipped ordinary command only while its state is `empty`;
2. when its parser captures an exact provider id, records `pending`; an eligible
   terminal outcome promotes that id to `active`, so later live-session turns
   use strict resume-by-id argv plus the referee's delta prompt;
3. after daemon reload, receives the same id only through the validated
   persisted resume descriptor;
4. treats every exit outside the backend's verified eligible-outcome set
   (default `{completed}`; see *One eligibility vocabulary, two proof bars*
   below), and every uncertain, conflicting-id, provider-rejected, or
   missing-local-state exit, while `pending` or `active` as `quarantined` and
   never retries the ordinary start command.

`SubprocessRunner` learns the id from the normalized `Event.provider_session`
metadata produced by its configured parser — the same event the daemon already
validates and persists — rather than adding a second provider-specific callback.
Its post-capture state machine has four states:

- `empty`: no provider id has ever been observed; ordinary establishment is
  allowed;
- `pending`: one exact id was observed in the current ordinary turn but is not
  active yet;
- `active`: the pending id reached an eligible terminal outcome; every later
  provider call must use strict resume-by-id;
- `quarantined`: a turn with a pending or active id ended outside the backend's
  verified eligible-outcome set, emitted conflicting ids, had a
  rejected/uncertain resume, or lost required local state.

Only `active` makes `conversation_active()` true. `empty` may run the ordinary
full-prompt command. `pending` exists in memory only inside `run_turn`, but the
same observed id is already in the persisted `in_flight` descriptor.
The runner sets an irreversible in-memory `id_seen` bit on the first valid id;
one `finally` transition covers normal return, timeout, stop, cancellation,
parser/transport exception, and conflicting identity: only an outcome inside
that backend's verified eligible-outcome set becomes `active`, and every other
id-bearing exit becomes `quarantined`. No cleanup path may clear the bit or
return to `empty`.

**One eligibility vocabulary, two proof bars.** Each backend has one verified
eligible-outcome vocabulary, recorded in Appendix A, from which two sets are
drawn: a restart-eligible set and an in-session-eligible set, both defaulting
to `{completed}`, with the in-session set always a subset of the restart set.
References to "the backend's verified eligible-outcome set" in this state
machine and its tests mean the in-session set. A credentialed partial-turn
proof is evaluated against both gates rather than only the restart one — the
appendix already records one
such tension: Claude sessions are resumable from the first delivered user
message, not the first terminal result. But the two gates do not carry the
same risk, so widening is asymmetric. Restart validation runs when the
previous local writer process is definitively dead; in-session promotion runs
in the runner's `finally`, when a locally-abandoned provider may still be
executing remotely — issuing the next strict resume-by-id turn against a
thread a live provider process is concurrently appending to. Widening the
*in-session* set beyond `completed` therefore additionally requires that
backend to hold a verified `interrupt` capability ("no provider work believed
to be running"), and `timed_out` — a local deadline expiry that aborted
nothing provider-side — is never in-session-eligible for any backend.
`interrupted` may enter an in-session set only bearing the same per-turn
`interrupt_acknowledged` marker restart eligibility requires; an
unacknowledged or fallback-killed interrupt is never in-session-eligible,
exactly like `timed_out`. Stated
plainly: one-shot CLI backends keep `interrupt=false` permanently, so their
in-session set is frozen at `{completed}`, and since SDK backends do not use
this state machine at all — their in-session continuity is the #47 adapter
contract (reconnect through native resume or fail structurally) — the
in-session set is in practice constant today. The verified-`interrupt` bar
above is written for the first real consumer: a future bidirectional CLI
transport, should one ship. Only
outcome classes are widenable at all: conflicting ids, provider-rejected or
uncertain resume, and missing local state are identity-integrity failures and
quarantine unconditionally regardless of any credentialed proof.
`quarantined` makes `conversation_active()` false **and** makes every later
`run_turn` return the stable structured failure without launching a subprocess.
It never transitions back to `empty`; a new agent-collab session is the only
fresh-start recovery. Persisted records may contain an early captured id for
diagnostics, but explicit resume accepts only a descriptor whose
`last_turn_status` is eligible under that backend's verified contract.

Restart construction has one separate, explicit re-entry:
`validated eligible descriptor → active`. Perform fingerprint, provider-id,
status, cursor, backend-policy, and state-root validation before constructing
the runner and before constructing the resumed `Referee`; then seed both the
runner's active id and the referee's watermark. An invalid descriptor fails the
public resume operation and never constructs an `empty` runner. Consequently,
the first post-reload prompt is a delta and the first provider command is strict
resume-by-id.

The descriptor is not encoded in `AgentConfig.args` or in the ordinary command
builder. Add one shared `prepare_cli_invocation` helper used by dry-run, direct,
and outer-sandbox paths:

1. build and validate the ordinary base argv, rejecting user-configured
   resume/session-ownership selectors for either sandbox policy;
2. apply `sandbox_plan.prepare_inner()` to that base argv (a no-op for
   `sandbox = "none"`);
3. pass the prepared argv plus the typed internal resume descriptor to a
   backend-owned finalizer: with no descriptor (`empty` ordinary establishment)
   it returns the prepared argv unchanged; with a validated descriptor it
   inserts only that provider's strict resume-by-id form at its declared locus;
4. return an immutable prepared prefix that still excludes the prompt.

`SubprocessRunner` calls the helper once for command preview and execution. On
the direct path it appends the prompt and executes the prepared prefix. On the
outer-sandbox path it passes that prefix to a new supervisor entrypoint that
renders the sandbox prompt, resolves the inner executable, and launches it
without calling `prepare_inner` again. The current raw-prefix
`SandboxSupervisor.launch_cli` call must not remain on this path: either replace
it with `launch_prepared_cli` or change its contract equivalently. This gives
ordinary and resumed turns one assembly owner and guarantees
`prepare_inner`/finalization each run exactly once.

The sandbox audit therefore never has to accept a user-provided resume flag,
while the finalizer cannot receive arbitrary user-supplied ownership argv.
Dry-run preview, emitted command preview, direct execution, and outer-sandbox
execution all consume the same prepared-prefix result so they cannot disagree.

The finalizers map the descriptor as follows:

- Claude: insert `--resume <session-id>` before `-p`/`--print` and before the
  outer-sandbox adapter's terminal `--`;
- Codex: `codex [global-options] exec resume --json [resume-options]
  <thread-id> ...`;
- Grok: insert `--resume <session-id>` before `-p`/`--single`;
- Antigravity, once identity capture exists:
  insert `--conversation <conversation-id>` before
  `-p`/`--print`/`--prompt`.

These are prefix transformations: the user prompt is never present during
finalization. Each finalizer requires one unambiguous executable and provider
print/subcommand boundary, preserves adapter-injected permission/sandbox
options, and fails structurally if the required boundary or terminal
end-of-options placement is malformed. It never appends an ownership flag after
`--` or treats a missing marker as permission to guess.

Codex is a structural rewrite, not token insertion. Its finalizer partitions
the version-pinned normalized options into root options (before `exec`) and
`exec resume` options (after `resume`), preserves `--json`, and rejects
unknown or ambiguous configured tokens instead of guessing their placement.
In particular, root-only profile, sandbox, approval, workdir, and search
options must not be copied after `exec resume`, where the installed CLI rejects
them. The ordinary and resumed builders share that partitioner.

Never implement this with `--continue`, "last conversation," a recent-session
selector, or a scan of mutable provider cache files. Do not loosen the sandbox
argv audit to accept user-configured ownership-changing flags either. In
particular, add `--session-id` to Grok's configured-argv rejection alongside
the resume/continue flags already rejected today. The typed internal descriptor
uses only the finalizer after workdir, sandbox, and state-root validation.

The provider homes already mounted as host-persistent writable exceptions
(`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME`, and Antigravity's `.gemini`
state) must remain available on both subprocess launch paths. Ephemeral or
no-session-persistence options are incompatible with advertised resume: reject
that combination at start, or keep `resume` false for it.

### SDK establishment

**On the worker path**, the worker does not exist when the user asks to resume.
The daemon relaunches it and includes a `resume` block in the `open` payload it
already builds per backend
(`<backend>/sandbox.py:worker_open_payload_for_agent`). The worker validates and
reopens strictly against the provider, or fails `open` — which the daemon
already surfaces as a structured `outer_sandbox_backend_incompatible` failure
and never retries as a fresh start.

**In-process**, resume goes through the adapter's existing reconnect code; for
most backends only the persistence and validation layer above it is new. Not
for `xai_sdk`: its shipped adapter best-effort deletes captured stored
completions on final close (Appendix A), which destroys exactly the
provider-side material a later `previous_response_id` resume needs — a
validated descriptor would still meet `NOT_FOUND`. Before that backend can
flip `resume`, close must retain the stored-response chain and retention must
own its lifetime; the "persistence layer only" shape does not hold there.

### Persistence

Expand `agent_sessions.<agent_id>` from the three shipped keys, without storing
credentials or raw SDK objects:

```json
{
  "backend": "codex_cli",
  "provider_session_id": "...",
  "provider_session_kind": "thread",
  "backend_version": "...",
  "resume_fingerprint": "...",
  "last_turn_status": "completed",
  "prompt_event_cursor": 123
}
```

The fingerprint covers what must not drift silently: provider type, canonical
backend, binary/SDK identity and version, model, workdir, permission/sandbox
posture and execution path, provider state-root identity, and normalized
backend-owned static configuration. It must not contain secrets.

Identity capture must become a keyed merge before these fields land:
`_maybe_capture_provider_session` today builds a fresh three-key entry and
assigns it over `agent_sessions[agent_id]`, which is correct for the shipped
schema but would silently drop `prompt_event_cursor`, `last_turn_status`,
`resume_fingerprint`, and `backend_version` the moment a mid-turn provider
event fires after the handoff persist. Stage 4 changes that write to merge
into the existing descriptor, and a hermetic test proves a mid-turn capture
preserves every handoff-persisted field.

Before invoking the runner, persist the new `prompt_event_cursor` and
`last_turn_status = "in_flight"` together. After the committed `TurnOutcome`,
the status uses that outcome vocabulary; strict reopen failures use
`resume_rejected` or `resume_uncertain`. The initial restart-safe contract
accepts only `completed`. `in_flight`, `interrupted`, `timed_out`, `failed`,
missing, and resume-failure states reject explicit resume — with one designed
exception: an `interrupted` status carrying the `interrupt_acknowledged`
marker (see *Turn-level interrupt*) may be restart-eligible for a backend with
verified `interrupt`, because the acknowledged abort is itself the proof that
no provider work was left running. A backend may widen
that set only after a credentialed test proves its exact partial-turn contract
and the appendix records it. That proof must cover the remote side, not merely
the local exit: for any status whose turn may still be executing
provider-side after the local process died — `timed_out` above all — widening
requires demonstrating that the provider thread can no longer be receiving
that turn's output at resume time, or that the provider's reopen API fences
or rejects a concurrent writer. Local process death proves only that the
local writer is gone. This is the restart half of the per-backend
eligible-outcome vocabulary that also governs in-session promotion (see *One
eligibility vocabulary, two proof bars* under CLI establishment), with the
in-session gate holding the strictly stronger proof bar. One composition to
record honestly: a daemon *crash* landing on a parked approval leaves the
handoff-persisted `in_flight` turn status, which the initial contract rejects
— the restore path marks the *session* interrupted but does not rewrite the
turn record. A *clean* shutdown that runs the stop path first denies the
approvals and, on a backend with verified `interrupt`, can commit an
acknowledged interrupt that remains restart-eligible. The resume cost of tool
gating therefore falls on crashes and on backends without verified interrupt
— still worth recording, because a long-parked approval is the likeliest
thing for either to catch. A plain deny is expected to be benign — the SDK turn typically
completes with a tool-denied result and persists `completed` — but that is a
per-backend fact to verify, not assume.

`prompt_event_cursor` is the referee's prompt-snapshot transcript length for
that agent. Persist it whenever a prompt is handed to the runner and restore it
into the new `Referee` before the first resumed prompt. Missing, negative, or
past-end cursors fail resume instead of defaulting to zero. This is distinct
from an MCP client's read cursor: it prevents a native provider thread from
receiving transcript events it already saw before reload.

Resume must also restore the *workflow phase*, or a resumed `Referee` replays
work: a freshly constructed referee runs its planned stages from the
beginning, so a session that completed stage 0 before the reload would
re-execute it — as a cheap delta turn, but still redoing provider work and
side effects — and a session parked at `awaiting_input` could not return to
its input loop. Persist alongside the per-agent descriptors a session-level
phase record (count of completed planned stages, and whether the session was
parked in its input loop); validate it with the descriptors; the resumed
referee starts at the recorded phase and never re-executes a completed stage.
An interactive session parked at `awaiting_input` resumes directly into the
input loop. A non-interactive session whose planned stages all completed is
terminal `done` and is not resumable — there is nothing to continue.

On daemon restart: keep the current behavior that an in-flight session becomes
`interrupted` and never auto-resume a paid or side-effecting operation; reload
the exact workdir config and backend enablement policy; require an explicit
resume request; reject it if the agent/backend disappeared, is disabled, has
incompatible settings, or does not advertise restart-safe resume; append to the
existing event cursor and transcript so the audit trail stays continuous.

A strict resume failure quarantines that runner's descriptor and persists the
failure in `last_turn_status`. `conversation_active()` becomes false, but the
runner is not allowed to execute an ordinary full-prompt start on a later turn;
it returns the stable structured resume failure without invoking the provider.
This prevents both an endless retry against a dead id and an accidental fresh
thread. Creating a new agent-collab session is the explicit recovery path; a
separate in-place reset/rebind operation is out of scope.

Add the shared operation only after this validation is defined — for example
`POST /sessions/{id}/resume`, `agent_collab_resume`, and
`agent-collab resume SESSION_ID`. The operation completes the design's race
set (interrupt on an unknown or finished `run_id` is a no-op; duplicate
approval decisions are idempotent or structured conflicts): concurrent resume
calls on one session admit exactly one, through an atomic per-session claim
held from the start of descriptor validation through task assignment (the
shipped precedent is `managed.post_lock`); the loser receives a structured
conflict. The claim must span the whole check-validate-start sequence because
validation is multi-step and awaits — a bare check of `managed.task` before
starting races, and `managed.task` is a single slot, so two unserialized
resumes would orphan one referee while double-driving the same provider
thread and cursor. Resume is likewise rejected with a structured conflict
while the session is live rather than reloaded/terminal, and a stop racing a
resume yields one winner and one persisted terminal state.

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

- **Park-state parity — of polling shape, not mechanics.** For the agent,
  `awaiting_approval` parks and returns the way `awaiting_input` does: one new
  status value, not a new polling pattern. Internally it is *not* the same
  machinery — settle is registry-keyed and mid-turn (see *Status and settle*)
  — and `awaiting_approval` must join `LIVE_WAIT_STATUSES` so `wait_events`
  keeps blocking instead of busy-spinning. The approval events must also join
  the documented digest filter: today's guidance polls
  `types=['message','error']`, which would never surface them.
  `agent_collab/mcp-guidance.md` and the delegate-flow guidance add
  `awaiting_approval` to the stop states, add the approval types to the
  documented filter, and state that an empty batch while parked is normal and
  that `timeout_ms` is unrelated to the approval deadline — all in the same
  change that ships the status.
- **Decide from the park payload alone.** The `wait_result`/status response for
  a parked approval carries `pending_approvals` — a list, because parallel
  turns (and, where open question 10 confirms it, parallel tool calls within
  one SDK turn) may hold several requests open at once — of blocks (request
  id, agent id, tool name, bounded elided summary, truncation flag, decision
  options), in event order under one documented total park-payload budget: the
  first blocks carry their summaries, any overflow is counted rather than
  silently dropped. The common flow stays one wait, plus one decision per
  request — a single park can carry several requests, so a multi-request park
  costs one wait, not one wait per request. Deciding one
  request while others remain unresolved leaves the session parked with the
  remainder; auto-denies and abandonments show up as `approval_resolved`
  events and in the shrinking list and overflow count, so the agent is never
  left inferring an invisible outcome. No `read_events` round trip for the
  un-elided common case.
- **Bounded summaries.** The approval `summary` is middle-elided at the source
  to a small fixed budget with an explicit truncation flag; full tool input is
  available only through the raw event view's existing `tool_output="full"`
  projection and `MAX_FULL_TOOL_BYTES` budget — no third payload tier. #50/#51
  showed collection-loop payload size is the dominant delegate cost — approval
  events must not regress it, and the digest view carries the summary, never
  the full input.
- **Gating is for exceptions, not throughput.** Preconfigured permission
  posture at start is the throughput path — the capability definition already
  classes it as policy, not a tool gate — and a gated turn pays one
  park/decide round trip per park, so an agent that wants every call gated
  is choosing that cost. The same guidance change says so.
- **Three operations, total.** One approval decision operation (approve/deny as
  a parameter), one turn-level interrupt, one resume — each mirrored across
  REST, MCP, CLI, and TUI. No per-provider or per-capability operation
  families. The decision operation stays single-request (idempotent
  duplicates), not a batch family. The approval deadline is one new *optional*
  start setting projected through `describe_options` like any other; nothing
  new is required in the hot loop, and capability flags ride the existing
  `describe_options`/settings payloads unchanged.
- **Resume preserves the agent's cursors.** Resume keeps the session id and
  appends to the existing event cursor and transcript, so a delegating agent
  re-attaches with its stored cursor instead of re-reading the session.
- **Resume preserves provider context.** Once an exact provider id is captured,
  each later turn uses the native thread plus the shipped delta prompt. Do not
  resend the full task, guardrails, or transcript merely because the backend is
  a one-shot CLI; avoiding that repeated context is the principal token-saving
  goal of this work.
- **Interrupt is the cheap correction path.** Stopping a runaway turn and
  steering with `post_message` rides the shipped delta-continuation prompts.
  The alternative — stop, restart, re-send the full task — costs a fresh
  provider context plus a full session re-read for the supervising agent. This
  is the economic argument for the steering interrupt over kill-only stop.

## Safety and failure rules

- Never automatically retry a provider operation after an uncertain interrupt.
- Never silently replace native resume/continuity with a new provider session.
- Resume only the exact id captured from that agent's provider event. Never use
  `--continue`, "last," or another mutable recent-session selector.
- Never infer a provider id by scanning provider state or reading a mutable
  current-conversation cache unless the provider documents a unique,
  turn-correlated identity record and the adapter validates that correlation.
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
acknowledgement, deadline expiry, and the `cancel_active` fallback each produce
exactly one terminal outcome; an `approval_request` parks the turn and one
decision resumes
it without holding `_lock` across the registry wait; concurrent close, interrupt,
and stop do not deadlock; a lost or oversized decision denies, while a
duplicate decision is idempotent or a structured conflict; an
interrupt or stop landing while approvals are pending denies them all and yields
one terminal outcome; a `result` arriving with unresolved requests retracts
them as `abandoned` and releases the park; a decision arriving after the
terminal result is rejected as stale; the approval settle predicate is
registry-keyed — an `interactive=false` session parks and settles, and a
queued `post_message` neither satisfies nor blocks the park; the turn deadline
excludes the parked interval, re-arms on decision, is hard-capped per park and
per turn, and resumes counting on any registry ambiguity;
any daemon/worker protocol-version skew fails at the `hello` handshake rather
than running ungated.

Per-backend coverage belongs under `tests/backends/<backend_id>/` with fake SDK
modules or fake CLI executables shaped to the verified installed version:
interrupt calls the provider exactly once and handles completion races;
interrupt fallback cancels and closes resources when the provider hangs; a
persisted identity is restored after a simulated daemon restart on **every**
production path; incompatible fingerprints and rejected/expired sessions fail
without creating a fresh provider session; tool requests enter
`awaiting_approval`, emit sanitized correlated events, park `wait_result` with
the `pending_approvals` list under its total budget, and resume on approve or
deny; duplicate, stale, cross-session, cross-worker-instance, post-stop, and
post-completion responses are rejected; approval timeout and daemon shutdown
deny and release the callback; on the in-process path a stop landing on a
parked callback denies registry entries before runner close and unwinds
without exceeding the bounded close; mixed workflows aggregate flags honestly;
session-index round trips
preserve only the sanitized resume descriptor; prompt handoff atomically
persists `in_flight` plus the per-agent cursor, turn commit persists the terminal
status, only `completed` is initially eligible — plus `interrupted` bearing the
`interrupt_acknowledged` marker on a backend with verified `interrupt` — and
reload restores the cursor before constructing the first delta; REST, direct MCP,
stdio-via-REST, CLI, and TUI share the same behavior.

CLI resume coverage additionally proves:

- the first argv is the ordinary start shape, its emitted id is persisted
  unchanged, and the next argv is the provider's exact resume-by-id shape;
- only the delta continuation prompt is sent after promotion to `active`; a turn
  with no captured id stays `empty` and receives the full prompt next time,
  while an id-bearing turn outside the eligible-outcome set becomes quarantined
  and receives no next provider prompt;
- an id becomes active only at an eligible turn boundary; missing, conflicting,
  failed-turn, and ineligible persisted identities never trigger resume argv;
  once an id is pending or active, every outcome outside the backend's verified
  eligible-outcome set — and every uncertain one — quarantines the runner and
  later turns invoke no provider command;
- direct and outer-sandbox launch preserve the same id, workdir, state root,
  and model/config posture;
- the shared preparation helper calls `prepare_inner` and the finalizer exactly
  once; direct execution and the supervisor's prepared-prefix entrypoint execute
  that result without rebuilding or re-preparing it;
- the finalizer is an identity transform for an absent descriptor, while a
  validated descriptor produces exactly one strict ownership selector;
- user-configured resume/session-ownership flags remain rejected by the sandbox
  audit under both policies, while the validated internal descriptor takes the
  finalizer path after base-argv preparation;
- Codex root-only options remain before `exec`, `--json` and resume options are
  accepted by `exec resume`, and unknown option placement fails before launch;
- exact argv fixtures for both launch paths prove Claude ownership flags precede
  print mode and the terminal `--`, Grok/Antigravity ownership flags precede
  their print marker, and the prompt remains the final positional operand;
- provider resume rejection exits structurally and does not invoke a fresh
  command as fallback; the descriptor is quarantined, later turns do not invoke
  the provider, and only a new agent-collab session establishes a fresh thread;
- reload constructs `active` plus the restored referee watermark only after all
  descriptor validation passes; invalid/ineligible descriptors construct no
  runner and project resume unavailable, while a fully eligible descriptor
  projects availability before the operation is called;
- `antigravity_cli` stays `continuity=false, resume=false` until a fake
  stream-JSON root event can prove exact conversation-id capture; fixtures cover
  `init`, `step_update`, terminal `result`, malformed records, additive unknown
  non-terminal records, unknown terminal outcomes, root versus subagent
  identity, and the no-plain-text-fallback rule; `clean_eof_fallback` is false.

The #47 fake-conversation tests already cover the adapter these controls extend.
Keep `AGENT_COLLAB_HOME` isolated everywhere. No hermetic test may import a real
optional SDK, read native credentials, or make a model call. Tests needing a real
Bubblewrap belong in `conditional_tests/`, already gated on the host having it.

### Credentialed

Opt-in, low-cost, per capability, under
`integration_tests/backends/<backend_id>/test_live.py` — and run on **every**
production path: worker plus in-process for worker-backed SDKs, outer-sandbox
plus direct subprocess for CLIs:

- interrupt a harmless long-running response, verify provider/client cleanup,
  then verify the conversation is still usable if the backend claims it;
- gate a harmless tool, deny once, then approve once, verifying no execution
  occurs before approval; verify a decision slower than the ordinary per-turn
  timeout still completes the turn (the excluded parked interval), and that a
  plain deny lets the turn run to a `completed` terminal outcome that remains
  resume-eligible;
- explicit resume after a simulated reload continues a captured codeword or
  other low-cost provider-held fact without transcript replay, and asserts that
  the first resumed prompt starts at the persisted per-agent
  `prompt_event_cursor`;
- before the Antigravity backend-wide transport floor ships, a harmless live
  `agy 1.1.8` root print turn produces the fixture-backed terminal `result`,
  completes successfully through the new parser, and records whether a stable
  root conversation id is present.

Skip when the installed SDK/CLI version, account, or provider does not support
the feature. A skipped provider keeps the production capability false.

## Acceptance criteria

- Capability flags are true only for pairs with implemented, tested behavior on
  every execution path that pair can take.
- Explicit resume after daemon restart reloads config/policy, validates a
  sanitized fingerprint, and never silently starts fresh.
- Once a CLI runner observes a provider id, any turn whose outcome falls
  outside that backend's verified eligible-outcome set (default `{completed}`),
  or that is conflicting or uncertain, quarantines it; no later turn may use
  ordinary establishment inside that agent-collab session, and a session
  containing a quarantined agent projects `resume` unavailable before the
  operation is called.
- Stop invokes verified provider cancellation when interrupt is advertised, and
  has a bounded fallback that still kills first when it must.
- Tool approval pauses before execution, is visible through all session surfaces
  — including the plural pending list and every resolution, auto-denies and
  abandonments included — accepts one correlated response, and defaults to deny
  on failure, including worker loss and protocol-version skew; a stop denies
  every pending approval and reports the count.
- `approval_request` and `approval_resolved` are typed events that pass the
  `types` filter and appear in the documented digest filter; `awaiting_approval`
  joins `LIVE_WAIT_STATUSES` and every hard-coded status enum and rendering
  surface.
- Session IDs, controller state, callbacks, and approval events expose no
  credentials or raw provider objects.
- Mixed workflows and persisted sessions report conservative, accurate
  capabilities.
- The MCP delegate loop absorbs approvals with one new status and one operation;
  digest payload budgets do not regress, and the park payload's
  `pending_approvals` list rides under a documented total budget with counted
  (never silent) overflow.
- `claude_cli`, `codex_cli`, and `xai_cli` may advertise `continuity` and
  `resume` independently: `continuity` requires two live turns through both
  launch paths, while `resume` additionally requires persisted cursor and
  fingerprint validation, daemon reload, and the explicit public operation;
  `antigravity_cli` remains false until exact print-mode id capture exists.
- Before `xai_cli` flips either flag, configured `--continue`, `--resume`,
  `--session-id`, and related ownership selectors are rejected under both
  sandbox policies; only the typed internal descriptor may select a session.
- `antigravity_cli` requires and probes `agy >= 1.1.8` for every start; an older
  binary receives an actionable version failure before session establishment.
- `antigravity_cli` uses `agy -p --output-format stream-json`, requires valid
  terminal semantics, and retires message-only/clean-EOF success; malformed
  NDJSON, an invalid or missing terminal result, and unknown terminal outcomes
  fail structurally rather than falling back to plain text, while additive
  non-terminal records remain forward-compatible; a credentialed successful
  root turn validates the fixture and terminal contract before the migration
  becomes the backend default.
- One-shot CLI backends keep `interrupt=false, tool_gate=false`; those flags
  require a separately verified bidirectional control transport.
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
6. Can `agy -p` emit the exact conversation id it just used through a stable
   machine-readable surface? CLI 1.1.8 added typed `init`, `step_update`, and
   `result` events after the current backend was designed; inspect a root turn,
   distinguish root identity from `subagent_info.conversation_id`, and add
   fixtures before deciding. If no root id exists, `antigravity_cli` resume
   stays false, but the independently accepted stream-JSON migration remains.
   (Stage 4)
7. What minimum CLI versions or feature probes should gate the other strict
   resume builders? `antigravity_cli` is decided at `agy >= 1.1.8`. The binary
   identity/version belongs in the fingerprint, but Claude, Codex, and Grok
   still need durable compatibility rules. (Stage 4)
8. Grok's current documentation describes `--session-id` differently from the
   installed 0.2.112 help, which says it creates a new session and must not
   already exist. Re-verify on upgrade; use explicit `--resume`, never
   `--session-id`, for the current pin. (Stage 4)
9. Do the provider SDKs or their bundled CLIs hold their own decision deadline
   on a pending permission callback? If one does, excluding parked time from
   agent-collab's turn clock is cosmetic beyond that bound; each SDK
   subsection must record the fact, and a deadline that can be neither
   disabled nor out-waited gates `tool_gate` or clamps the approval deadline
   (see *Clocks*). (Stages 2–3)
10. Can each SDK fire multiple permission callbacks concurrently within one
    turn, or are they serialized? The plural `pending_approvals` surface
    assumes concurrency is possible; the per-backend fact is unverified.
    (Stages 2–3)

---

## Appendix A: verified provider facts

**Re-verify the claims in a backend's subsection against the installed pin
before implementing that provider, and update the subsection and its date in the
same change.** Claims are tagged with the control they serve. Facts proven for
shipped continuity are reduced to one line plus the test that now pins them;
the tests, not this document, are their guarantee.

### claude_cli — Claude Code 2.1.219 (verified 2026-07-30)

- *[resume]* Installed help and the
  [official CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
  expose `--resume <session-id>` and `--continue`; print mode (`-p`) can be
  combined with explicit resume. Use only `--resume` because `--continue`
  selects the most recent conversation rather than the captured identity.
- *[resume substrate]* The shipped stream-JSON parser captures `session_id` from
  `system` and `result` records as provider identity kind `session`. The
  outer-sandbox adapter preserves the complete effective
  `CLAUDE_CONFIG_DIR`, so the local session material needed by a later process
  is host-persistent.
- *[negative]* `--no-session-persistence` is incompatible with advertised
  resume. The exact two-process reconnect and unknown-id failure behavior still
  require a credentialed smoke test on both launch paths before either flag
  flips.
- *[interrupt/tool_gate]* The one-shot print transport has no verified
  bidirectional control path; both remain false even when resume ships.

### codex_cli — `codex-cli` 0.145.0 (verified 2026-07-30)

- *[resume]* Installed help and the
  [official non-interactive reference](https://developers.openai.com/codex/noninteractive/)
  expose `codex exec resume <SESSION_ID> <PROMPT>`. The resume subcommand
  accepts the required non-interactive options, including `--json`, but not all
  root options accepted by the ordinary command. The backend must rebuild
  `codex [global-options] exec resume --json [resume-options] <SESSION_ID>
  <PROMPT>` through a version-pinned option partitioner rather than blindly
  append `resume` or copy all configured options after the subcommand. Use this
  strict command and propagate rejection; do not fall back to `codex exec`.
- *[resume substrate]* The shipped JSONL parser captures
  `thread.started.thread_id` as provider identity kind `thread`, and the
  outer-sandbox adapter preserves the complete effective `CODEX_HOME`.
  `--ephemeral` is incompatible with advertised resume.
- *[architecture]* t3.codes uses the longer-lived Codex app-server and its
  `thread/resume` JSON-RPC method, persisting `{threadId}` as its resume cursor
  ([source at the reviewed commit](https://github.com/pingdotgg/t3code/blob/e4829603ff70bdeac96780e1512ac2c09638e5a3/apps/server/src/provider/Layers/CodexSessionRuntime.ts#L457-L493)).
  That validates the provider primitive but is not a reason to convert this
  backend: `codex exec resume` supplies the lifecycle agent-collab needs with a
  smaller change. App-server remains the SDK/control-plane concern. t3.codes
  falls back to `thread/start` for selected not-found errors; agent-collab must
  deliberately differ and fail the resume because its capability contract
  forbids silent fresh sessions.
- *[negative]* The exact two-process reconnect and unknown-id failure behavior
  still require a credentialed smoke test on both launch paths before either
  flag flips. One-shot CLI interrupt/tool gating remain false.

### xai_cli — Grok CLI 0.2.112 (verified 2026-07-30)

- *[resume]* Installed help and the
  [official headless scripting reference](https://docs.x.ai/build/cli/headless-scripting)
  expose explicit `--resume <session-id>` and a recent-session `--continue`;
  use only `--resume` with `-p` and `streaming-json`.
- *[resume substrate]* The shipped parser captures `end.sessionId` as provider
  identity kind `session`, and the outer-sandbox adapter preserves the complete
  effective `GROK_HOME`.
- *[safety]* The current outer-sandbox audit rejects user-configured
  `--continue`, `--resume`, and related ownership-changing shapes, but does not
  yet reject `--session-id`. Stage 4 must add that rejection for both sandbox
  policies. Resume then uses only the typed finalizer path after the captured id
  and sandbox descriptor are validated.
- *[version caveat]* The official page currently describes `--session-id` as
  create-or-resume, while installed help says it creates a new session and the
  id must not already exist. Treat the installed pin's explicit `--resume` as
  authoritative for implementation and re-verify this mismatch on upgrade.
- *[negative]* The exact two-process reconnect and unknown-id failure behavior
  still require a credentialed smoke test on both launch paths. One-shot CLI
  interrupt/tool gating remain false.

### antigravity_cli — `agy` 1.1.8 (verified 2026-07-30)

- *[resume surface]* Installed help and the official
  [conversation guide](https://antigravity.google/docs/cli/conversations) and
  [resume command reference](https://antigravity.google/docs/cli/commands/resume)
  expose `--conversation <conversation-id>` and `--continue`. Only the explicit
  id form is acceptable.
- *[resume opportunity]* The shipped agent-collab backend still invokes the
  default plain-text `agy -p` transport, whose parser cannot correlate the turn
  with a provider conversation id. However, 1.1.8 added `--output-format json`
  and `stream-json`; its changelog describes typed `init`, `step_update`, and
  terminal `result` events. Inspect and fixture those shapes before
  implementation. A `subagent_info.conversation_id` is child identity and must
  not be mistaken for the root conversation id.
- *[minimum version]* The stream-JSON migration makes `agy >= 1.1.8` the
  backend-wide minimum, even if exact root identity is unavailable and resume
  remains false. The readiness probe must reject older versions with the
  observed and required versions before every session establishment.
- *[transport migration]* Convert the backend command to
  `agy -p --output-format stream-json ...`, replace the message-only parser with
  a typed NDJSON parser, set `clean_eof_fallback = False`, and update event
  fidelity/settings documentation in the same change. Invalid JSON, an invalid
  or missing terminal `result`, and an unknown terminal outcome are structural
  turn failures; never reinterpret those bytes as plain assistant prose.
  Unknown additive non-terminal records may be ignored or surfaced as bounded
  verbose status so the parser remains forward-compatible.
- *[safety]* Do not infer the id from a mutable "last conversation" cache.
  Resume can ship only after the exact print-mode turn emits or otherwise
  exposes a stable, uniquely correlated identity. The outer sandbox already
  preserves the complete `$HOME/.gemini` state required by the CLI.
- *[interrupt/tool_gate]* The one-shot print transport has no verified
  bidirectional control path; both remain false.

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
  and result types are unverified — as are callback concurrency within one
  turn and any SDK/CLI-side decision deadline (open questions 9–10).
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
  on the app-server are unverified — including callback concurrency and any
  provider-side decision deadline (open questions 9–10).
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
- *[tool_gate]* No permission-callback surface has been verified for the
  localharness connection; callback concurrency and any provider-side decision
  deadline are likewise unrecorded (open questions 9–10).
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
