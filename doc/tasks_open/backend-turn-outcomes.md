# Backend turn outcomes and MCP failure reporting

**Status:** Design complete and independently reviewed; implementation not started.

**Created:** 2026-07-13.

**Last refined:** 2026-07-13.

**Issue:** #17.

## Purpose

Give every supervised backend one explicit, backend-neutral way to report how
an individual turn ended, then aggregate those turn outcomes into truthful
session state for sequential, interactive, and future parallel workflows.
REST, MCP, CLI, and TUI consumers must not infer success from provider-specific
transcript prose, raw payloads, provider identity, or process exit alone.

This is a design and planning document. Production implementation is not part
of this pass.

## Relationship to the parallel workflow

This shared contract must land before the referee/runtime slice of
`parallel-review-workflow.md` (#19).

The parallel design originally treated a member as successful when it emitted
at least one non-error message. That is insufficient: a provider may stream
partial prose and then report a fatal terminal result. Parallel aggregation
must consume the explicit per-member outcome and may accept a review only when
the turn completed and also satisfied the stage's review-output predicate.

An owner decision (2026-07-13) bounded #19 v1 to a flat `parallel` member
list — one concurrent group per workflow, the calling agent reconciles, and
composed `stages` (e.g. a synthesis turn after the group) are deferred. For
this contract that means v1 parallel sessions have exactly one stage
(`stage_index` stays, always 1 in #19 v1) and there is no fallback-parser
work in #19 anymore. Configuration and validation work for #19 may be
developed separately after the shared outcome types are frozen, but must not
become externally visible on its own (cross-plan review): landing the schema-7
parsing, discovery surface, and the built-in `dual-review` workflow before the
stage runtime would let the daemon advertise and select a workflow the referee
cannot execute. The config slice, discovery, the built-in workflow, and the
runtime become available together; only #19's concurrent runner/referee and
stage-aggregation work depends on this task.

Recommended ordering:

1. implement the shared outcome, runner/referee, persistence, and wire contract;
2. map backends using verified evidence;
3. implement parallel runtime aggregation on those outcomes.

## Triggering evidence and completed xAI slice

A real headless xAI CLI review emitted an `end` record with
`stopReason=Cancelled` and a provider session ID, then exited with code zero.
The parser retained the identity but the daemon still completed the session.
The MCP caller had to inspect provider-specific `raw` data to learn that no
review had completed.

Commit `cac09fa` completed the first provider-specific remediation:

- xAI CLI runs non-interactively in Grok's read-only sandbox by default;
- `provider_max_turns` exposes Grok's internal model/tool-loop limit;
- only `stopReason=EndTurn` is treated as successful terminal evidence;
- cancelled and other unsuccessful reasons emit a fatal diagnostic event while
  retaining provider session identity separately.

That behavior is evidence to preserve. The shared implementation will move
terminal authority from event `raw` into the typed outcome without redoing the
xAI headless defaults.

Verification already completed for that slice:

- Ruff and 739 hermetic tests passed;
- `./agent_collab_dev.sh build --check` passed;
- a strict credentialed xAI CLI turn completed and retained provider identity;
- durable option discovery exposed the new defaults and
  `provider_max_turns`;
- a Grok Build review with medium-or-higher reasoning completed with
  `EndTurn`.

## Current code gaps

### Runner and parser boundary

`AgentRunner.run()` is an async event generator. Python async generators cannot
return a result value, so a runner can stream events but cannot deliver one
authoritative outcome. `SubprocessRunner` emits process exit as a referee status
event, SDK runners turn exceptions into transcript error events, and provider
terminal markers remain ordinary parser output.

An `Event(type="error")` is deliberately not enough to decide the turn:

- a recoverable command or tool failure can precede a successful answer;
- stderr may be diagnostic noise;
- a provider can emit partial prose before a fatal terminal marker;
- a provider can emit a fatal marker and still exit zero.

### Referee and daemon

The referee currently catches a turn timeout, emits an error event, and
continues. It records no typed per-turn result. A normal `Referee.run()` return
therefore lets the daemon set a live session to `done`, regardless of fatal
provider evidence in the transcript.

Session transitions are not monotonic: `_set_status` assigns any requested
state, so cleanup or a late normal return can overwrite another terminal cause.

### Persistence and APIs

`SessionState.error` is one unstructured string. There is no stable code,
decisive-turn reference, backend attribution, or list of outcomes for sessions
with several turns.

`EventBatch` returns only `session_id`, `cursor`, and `events`. Although
`wait_events` already wakes when a session leaves a live state, the response
does not contain that state. A terminal transition can therefore wake a caller
with `events=[]` and an unchanged cursor but still require a separate status
call.

## Separate turn and session state machines

A backend turn and a multi-agent session answer different questions.

### Turn outcomes

| Outcome | Meaning |
| --- | --- |
| `completed` | The backend reached verified successful completion under its declared terminal contract. |
| `cancelled` | Provider or terminal permission evidence cancelled the turn before successful completion. |
| `interrupted` | Agent-collab intentionally stopped an active turn. |
| `timed_out` | The local per-turn deadline expired before the runner returned an outcome. |
| `refused` | Structured provider evidence explicitly reports refusal with no requested result. Never infer this from prose. |
| `failed` | Authentication, entitlement, model, protocol, transport, provider service, output, cleanup, or another terminal failure prevented completion. |

Partial output never upgrades a non-completed outcome. Provider session identity
is bookkeeping only.

### Session statuses

Keep the existing small public vocabulary:

| Status | Meaning |
| --- | --- |
| `running` | Planned or directed work is active. |
| `awaiting_input` | An interactive session completed its current turns and can accept more input. |
| `done` | The workflow's aggregation policy was satisfied. This may include a documented degraded parallel stage. |
| `failed` | A required turn or stage did not complete successfully. |
| `stopped` | The user or caller explicitly stopped the live session. |
| `interrupted` | The daemon restarted while the session was live. |

Do not add `cancelled`, `timed_out`, or `refused` as session statuses. They are
turn outcomes and structured failure codes. A parallel session can be `done`
while retaining one failed member outcome, which is another reason not to make
turn outcome values session statuses.

## Internal contract

### Backend-owned result

Use a small immutable internal value. Illustrative names are normative in
semantics but may be adjusted mechanically during implementation:

```python
TurnOutcomeKind = Literal[
    "completed", "cancelled", "interrupted", "timed_out", "refused", "failed"
]

@dataclass(frozen=True)
class TurnOutcome:
    outcome: TurnOutcomeKind
    code: str | None = None
    message: str | None = None
    provider_stop_reason: str | None = None
    process_exit_code: int | None = None
```

`message` is selected from a canonical code-to-message table. It is not an
arbitrary provider or exception string.

The referee wraps the backend-owned result with occurrence identity assigned
before launch:

```python
@dataclass(frozen=True)
class TurnOutcomeRecord:
    turn_id: str                 # turn-1, turn-2, ...
    stage_index: int             # one-based
    agent_id: str
    backend: str                 # canonical name, for example codex_cli
    outcome: TurnOutcomeKind
    code: str | None = None
    message: str | None = None
    provider_stop_reason: str | None = None
    process_exit_code: int | None = None
```

`turn_id` allocation follows configured workflow/member order, not completion
order. It therefore distinguishes repeated agents and concurrent members.

### Runner delivery mechanism

Replace the async-generator boundary with an awaited method that receives an
async event sink and returns one value:

```python
AsyncEventSink = Callable[[Event], Awaitable[None]]

class AgentRunner:
    async def run_turn(
        self,
        prompt: str,
        workdir: Path,
        emit: AsyncEventSink,
    ) -> TurnOutcome:
        ...
```

Why this shape:

- `await emit(event)` preserves streaming and backpressure;
- a normal async function can return exactly one result;
- there is no separate outcome future that can be abandoned when iteration
  stops;
- cancellation and resource cleanup remain inside one awaited operation;
- CLI and SDK runners implement the same contract;
- outcomes do not become transcript events or cursor entries.

A streamed-run object with separate `events` and `outcome` futures creates two
completion channels and more abandoned-future/cancellation states. A terminal
event makes an ordinary event authoritative and can be accidentally filtered,
persisted twice, or mistaken for provider prose. The sink-plus-return API has
one terminal channel.

The referee commits each resolved record through one awaited referee-to-daemon
operation (cross-plan review with #19), illustratively
`record_turn_outcome(record: TurnOutcomeRecord, boundary_event: Event)`. Its
daemon implementation appends the outcome to `turn_outcomes`, persists it,
appends the paired boundary event, snapshots state and events coherently, and
schedules one watcher notification — all as a single event-loop operation, so
an outcome mutation can never race the notification for its own boundary
event. #19's parallel aggregation reuses this operation per member; it is the
concrete interface behind the "atomically paired" requirement in the REST/MCP
contract below.

### Parser evidence, not parser authority

Provider parsers return normalized events plus private terminal evidence to
their runner. The exact helper can be a small `ParsedRecord`/`TerminalEvidence`
type; it is not public or persisted.

Provider terminal markers are evidence, not immediately committed outcomes.
The runner accumulates evidence until process/SDK teardown completes. This is
necessary because an earlier success marker can be followed by malformed
output, a transport exception, or a nonzero exit that must still fail the turn.

Duplicate identical terminal records may be deduplicated diagnostically.
Conflicting terminal records fail closed with `provider_protocol_conflict`.
The runner resolves and returns exactly once.

## Supervision, precedence, and cancellation

### Referee arbitration

For each turn, the referee starts the runner task and a deadline task. The
deterministic arbitration rule is:

1. if the runner task is already done when arbitration occurs, consume its
   returned outcome;
2. otherwise the first registered local control cause wins:
   - deadline -> `timed_out` / `local_turn_timed_out`;
   - explicit session stop -> `interrupted` / `local_turn_interrupted`;
   - explicit referee policy cancellation -> `interrupted` /
     `referee_turn_cancelled`;
3. cancel the runner, perform bounded cleanup, then record the one local
   outcome;
4. an unexpected bare cancellation without a registered control cause is
   `failed` / `referee_cancelled_unexpected`.

The "explicit session stop" cause requires a defined daemon-to-referee stop
signal (cross-plan review with #19): `stop_session` registers the control
cause with the referee **before** cancelling the session task — bare
`task.cancel()` alone would classify every unwinding member as
`failed`/`referee_cancelled_unexpected` instead of `interrupted`. The signal
propagates to every active member supervisor (sequential and parallel alike),
and outcome recording plus bounded cleanup are shielded from the cancellation.
Terminal `stopped` has exactly **one** publisher: it is set only after the
session task has settled and member outcomes are recorded (cancel → await
task → set `stopped`), with a direct status set only for sessions that have
no live task; `_run_session`'s cancellation handler must defer to that
ordering rather than publish `stopped` itself.

This is causal rather than a global enum precedence. A provider that reports
`Cancelled` while unwinding a local deadline remains `timed_out`; the provider
record is diagnostic.

Outcome recording and the matching daemon state update must be protected from
cancellation so the terminal cause cannot be lost during unwind.

### Bounded cleanup

Cleanup must never delay outcome recording indefinitely.

- Subprocesses receive a fixed terminate/reap grace period, then are killed and
  reaped. The current `SubprocessRunner` already has the basis of this behavior.
- SDK cancellation/close receives a fixed grace period. If it expires, the
  causal timeout/interrupt outcome is still recorded and a daemon-owned
  background reaper owns residual cleanup.
- A safe diagnostic event may report cleanup timeout, but arbitrary cleanup
  exception text does not enter the structured outcome.
- A cleanup failure on a normally finishing turn is fatal when completion or
  resource ownership is uncertain. An explicitly documented best-effort close
  failure may remain diagnostic.

The implementation should centralize the grace constants and cover terminate,
kill, cancellation, close failure, and reaper ownership hermetically.

### Evidence precedence for a normally finishing runner

When no local control cause won, resolve accumulated evidence in this order:

1. explicit provider fatal terminal evidence;
2. parser, output transport, or SDK exception;
3. nonzero subprocess exit;
4. verified provider success plus clean process/SDK teardown;
5. backend-declared clean-EOF fallback.

Partial output, exit zero, provider identity, and absence of a Python exception
are never sufficient when the backend has a stronger terminal protocol.

A provider success marker followed by abnormal transport or process failure is
`failed` unless fixture-backed evidence for that exact backend proves the
trailing condition benign.

## Workflow aggregation

### Sequential planned turns

- `completed`: record the outcome and continue.
- `cancelled`, `timed_out`, `refused`, or `failed`: record the outcome, stop
  remaining planned turns, and fail the session.
- explicit stop: record `interrupted` for the active turn and leave the session
  `stopped`.

If a later turn fails after earlier agents completed, retain every earlier
outcome and all transcript output. The overall session is still `failed`.
Do not emit a final summary claiming every planned turn completed.

### Interactive sessions and directed messages

Enter `awaiting_input` only after every planned turn completed. Each directed
follow-up starts a new occurrence with a new `turn_id` and outcome. A completed
directed turn returns the session to `awaiting_input`; a non-completed directed
turn fails the session. Untargeted messages remain transcript/context input and
do not create a backend outcome.

Restarting the daemon marks a previously live session `interrupted`; it cannot
fabricate the outcome of a turn whose in-memory runner was lost.

### Parallel stages

The future parallel implementation assigns all member turn IDs before launch,
retains every member outcome, and applies stage policy after members finish:

- a review member is accepted only if its outcome is `completed` and it
  satisfies the explicit review-output predicate;
- partial messages from cancelled, timed-out, refused, or failed members never
  count;
- a stage with at least one accepted member may complete in degraded form;
- a stage with no accepted member fails the session;
- if composed workflows land later (deferred in #19), a subsequent sequential
  turn — e.g. a synthesis step — can fail the overall session after a
  successful or degraded review stage; the aggregation rules above are written
  per stage so this needs no contract change;
- session stop cancels and records every active member turn.

The stage-summary event uses all six outcome kinds and identifies accepted
members. It is a convenient wake-up/summary surface, while `turn_outcomes`
remains the authoritative per-turn history.

Parallel interactive sessions remain out of scope for v1.

## Stable codes

Initial backend-neutral codes:

- `provider_turn_cancelled`
- `provider_turn_refused`
- `provider_terminal_failure`
- `provider_protocol_conflict`
- `provider_authentication_failed`
- `provider_entitlement_failed`
- `provider_model_unavailable`
- `provider_transport_failed`
- `provider_output_invalid`
- `provider_output_incomplete`
- `provider_empty_response`
- `local_turn_timed_out`
- `local_turn_interrupted`
- `referee_turn_cancelled`
- `referee_cancelled_unexpected`
- `subprocess_exit_nonzero`
- `parallel_stage_no_accepted_member` (stage-level orchestration failure for
  #19's `ParallelStageFailed`: zero members were accepted; canonical message
  "No parallel reviewer produced an accepted review")

The implementation may add codes only with a canonical safe message and tests.
Provider-specific enum values stay bounded optional diagnostics; they do not
become public outcome or session-status values.

## Persistence and compatibility

Add two optional fields to `SessionState` and `SessionStateModel`:

```json
{
  "error": "The provider cancelled the turn",
  "failure": {
    "code": "provider_turn_cancelled",
    "message": "The provider cancelled the turn",
    "turn_id": "turn-2",
    "agent_id": "reviewer",
    "backend": "xai_cli",
    "outcome": "cancelled",
    "provider_stop_reason": "Cancelled",
    "process_exit_code": 0
  },
  "turn_outcomes": [
    {
      "turn_id": "turn-1",
      "stage_index": 1,
      "agent_id": "lead",
      "backend": "claude_cli",
      "outcome": "completed"
    },
    {
      "turn_id": "turn-2",
      "stage_index": 2,
      "agent_id": "reviewer",
      "backend": "xai_cli",
      "outcome": "cancelled",
      "code": "provider_turn_cancelled",
      "message": "The provider cancelled the turn",
      "provider_stop_reason": "Cancelled",
      "process_exit_code": 0
    }
  ]
}
```

Compatibility rules:

- old records without either field load with `failure=null` and
  `turn_outcomes=null`, meaning not recorded/unknown;
- a new session starts with `turn_outcomes=[]`;
- do not synthesize details for legacy sessions;
- keep the legacy `error` string as the same canonical safe message;
- keep `INDEX_VERSION=1`: the index envelope is unchanged, existing restore
  already filters unknown fields, and the additions are optional;
- add forward/backward compatibility tests, including an older reader ignoring
  the new keys.

The `failure` record also supports a **stage-level** cause (cross-plan review
with #19): a zero-accepted parallel stage may have no truthful decisive member
(e.g. several `completed` members none of which produced review output), so a
stage-level failure carries the stable orchestration code and canonical
message, `stage_index`, and null `turn_id`/`agent_id`/`backend`/
`provider_stop_reason`/`process_exit_code`. `ParallelStageFailed` is converted
through this structured path — never through a generic exception path that
would persist arbitrary `str(exc)` text.

During parallel execution `turn_outcomes` is a packed, append-only list in
outcome-observation order. It is never sparse or null-filled. Array position has
no semantic meaning; clients key and, if desired, sort by deterministic
`turn_id`. This avoids reordering earlier responses when members complete out of
configured order.

### Monotonic session transitions

Centralize status writes in a compare-and-set helper that permits:

- live -> live;
- live -> one terminal status;
- a same-terminal idempotent write.

Reject terminal -> different terminal and terminal -> live. Normal referee
return may set `done` only through this helper. `stop_session`, exception paths,
restart restoration, and interactive transitions use the same rule.

## REST and MCP contract

Define one reusable session-outcome view:

```json
{
  "status": "failed",
  "terminal": true,
  "error": "The provider cancelled the turn",
  "failure": {
    "code": "provider_turn_cancelled",
    "message": "The provider cancelled the turn",
    "turn_id": "turn-2",
    "agent_id": "reviewer",
    "backend": "xai_cli",
    "outcome": "cancelled",
    "provider_stop_reason": "Cancelled",
    "process_exit_code": 0
  },
  "turn_outcomes": []
}
```

REST session detail and `agent_collab_status` include this view alongside their
existing session metadata. `GET /sessions/{id}/events`, its wait route,
`agent_collab_read_events`, and `agent_collab_wait_events` use the same field
names and nesting in their event batch:

```json
{
  "session_id": "opaque-id",
  "cursor": 7,
  "status": "failed",
  "terminal": true,
  "error": "The provider cancelled the turn",
  "failure": {},
  "turn_outcomes": [],
  "events": []
}
```

Requirements:

- include `status`, `terminal`, `error`, `failure`, and `turn_outcomes` even
  when no new event exists;
- snapshot state and the event list together on the event-loop thread before
  potentially expensive projection in a worker;
- the cursor continues to count transcript events only;
- state and outcome changes never advance the cursor;
- a terminal transition wakes a long poll and returns the terminal snapshot
  with an unchanged cursor when appropriate;
- a mid-parallel-stage outcome update is atomically paired with a structured
  per-member boundary event before watcher notification, so long polls wake
  with the matching updated outcome snapshot;
- keep `agent_collab_start` asynchronous;
- do not add automatic retries;
- update MCP guidance to inspect structured status/failure/outcomes rather than
  transcript wording or process-exit prose.

The fields are additive. Keep the current REST major version unless the
implementation reveals a client that rejects extra response keys; new client
parsers should tolerate an older daemon omitting the additions and report that
outcome detail is unavailable.

## Sanitization boundary

Structured outcome/failure persistence is stricter than transcript storage.
Permit only:

- workflow-owned `turn_id` and `agent_id`;
- canonical backend name;
- shared outcome enum;
- stable code and its canonical message;
- an allowlisted, length-bounded provider terminal enum token;
- integer process exit code.

Never persist or return through these objects:

- arbitrary provider or exception text;
- unrestricted stderr;
- provider request/response objects;
- authorization headers, tokens, or environment values;
- prompts or transcript content;
- sensitive or machine-specific filesystem paths.

Backend classification code may inspect private exception/status detail but
must output only the shared category. Add adversarial tests for credentials,
headers, paths, oversized strings, forged raw keys, and hostile exception text.

Detailed error events remain governed separately by existing transcript-safety
rules. Improving broad transcript redaction is not a reason to weaken this new
allowlist.

## Backend evidence matrix

Evidence inspected during this design pass:

- Claude Code CLI 2.1.191;
- Codex CLI 0.144.0;
- Antigravity CLI 1.1.1;
- Grok Build CLI 0.2.93;
- `claude-agent-sdk` 0.2.114;
- `openai-codex` 0.1.0b3 with its pinned runtime;
- `google-antigravity` 0.1.6;
- `xai-sdk` 1.17.0.

Installed types and existing fixtures establish some mappings but not all
failure behavior. The implementation must not promote provisional entries to
facts without fixtures or a credentialed verification.

| Backend | Supported evidence and planned mapping | Remaining verification |
| --- | --- | --- |
| `claude_cli` | Require terminal `result`; `subtype=success` with `is_error=false` plus clean exit is success. Error result is fatal. EOF without result fails. | Capture sanitized success, error subtype, permission-denial/cancellation, malformed/truncated output, and exit interaction. Do not infer refusal. |
| `codex_cli` | `thread.started` is identity only. `turn.completed` is success evidence; `turn.failed` is fatal. Failed command/item records remain diagnostic when the turn later completes. EOF without a turn terminal fails. | Add real sanitized terminal fixtures for installed CLI 0.144.0, including partial output and failure. |
| `antigravity_cli` | Message-only provisional fallback: clean exit zero plus at least one nonempty stdout message completes; nonzero, transport error, or empty EOF fails. | Capture success, authentication/service failure, print timeout, and permission behavior. Determine whether failure prose can arrive with exit zero; if so, document the limitation or seek a stronger provider surface. |
| `xai_cli` | Preserve `cac09fa`: only `end/EndTurn` succeeds; `Cancelled` maps `cancelled`; other end reasons fail; identity is separate; EOF without `end` fails even after partial text. | Commit sanitized success and cancelled fixtures rather than relying only on inline synthetic records; test conflicting/duplicate terminals and process exit. |
| `claude_sdk` | Require terminal `ResultMessage`; `is_error` is fatal; stream closure without a result fails. Installed fields include subtype, stop reason, permission denials, errors, and API status. | Fixture-confirm exact permission cancellation/denial and any structured refusal mapping. Ensure error result plus trailing process error becomes one outcome. |
| `codex_sdk` | Map installed `TurnStatus.completed`, `interrupted`, and `failed`; reject `inProgress` as a collected terminal result. Command item failure is diagnostic unless the `TurnResult` fails. | Fixture interruption, failed result, missing/invalid result, cleanup failure, and any structured refusal evidence. |
| `antigravity_sdk` | A resolved response is terminal evidence. Installed exceptions distinguish cancellation, connection, execution, and validation failures. | Fixture empty resolved buffers, cancellation propagation, partial chunks plus exception, cleanup timeout, and whether any explicit refusal surface exists. |
| `xai_sdk` | Inspect `finish_reason`; for the current no-tools backend, `STOP` with nonempty content succeeds. Invalid, length/context/time limits, unexpected tool calls, exceptions, and empty content fail. Response ID is identity only. | Capture sanitized credentialed finish-reason behavior and exception categories before finalizing names. No structured refusal field is currently proven. |

`refused` remains in the shared vocabulary for providers that expose verified
structured evidence. It is acceptable for a backend to have no refused mapping.
Classifying ordinary model prose would be less reliable than returning
`completed` with that prose or another evidence-backed outcome.

Credentialed calls belong only under `integration_tests/` and should use the
lowest-cost reliable model/effort that exercises the changed terminal path.

## CLI and TUI

Watch/TUI/status rendering should show one clear structured fatal or degraded
line with repository output markers, followed by stable session status. Avoid
duplicating one failure for the provider event, exit status, outcome, and
session transition.

Human text is not a scripting contract. Machine consumers use REST/MCP fields.
The process exit code remains a useful diagnostic but is never presented as
the authoritative result when stronger provider evidence exists.

## Implementation slices and ordering

Each slice gets focused hermetic coverage and passes the applicable local gate
before it lands. The final slice repeats the full gate; tests are not deferred
until the end.

1. **Shared contract and runner boundary**
   - outcome/evidence types and stable code/message table;
   - async event-sink API;
   - fake runners and parser evidence accumulator;
   - precedence, conflict, partial-output, and exactly-once tests.
2. **Referee supervision and aggregation**
   - deterministic occurrence IDs;
   - sequential and interactive semantics;
   - timeout/stop arbitration;
   - bounded cleanup and reaper ownership;
   - outcome recording protected from cancellation.
3. **Daemon, persistence, REST, and MCP**
   - optional failure/outcome fields and legacy compatibility;
   - monotonic session transition helper;
   - coherent detail/status/event-batch DTOs;
   - atomic event/state snapshots and outcome-boundary wake-ups;
   - client, MCP guidance, generated API docs, security tests.
4. **Preserve and adapt xAI CLI**
   - translate existing EndTurn/cancel behavior to private evidence;
   - add sanitized fixture files and process/EOF conflict tests;
   - do not revisit the completed permission/sandbox defaults.
5. **Individual backend mappings**
   - one reviewable slice per remaining CLI/SDK backend;
   - backend-owned fixture notes and hermetic mapping tests;
   - leave refusal unmapped until proven.
6. **Parallel workflow dependency**
   - update #19 runtime to consume `TurnOutcomeRecord` values;
   - replace the message-only success predicate;
   - retain all member outcomes and emit the stage summary;
   - then implement concurrent runtime behavior.
7. **CLI/TUI and maintained docs**
   - one non-duplicated failure/degradation rendering;
   - architecture, configuration, runtime, backend README, and changelog updates.
8. **Final verification**
   - full hermetic and generated-doc gates;
   - package/mock smoke;
   - only explicitly selected, lowest-cost strict provider integrations for
     behaviorally changed backends.

The shared contract may land before every backend has a rich provider-specific
code, but no backend may claim success from known fatal evidence during the
transition. A temporary conservative `provider_terminal_failure` is preferable
to guessed specificity.

## Verification plan

### Shared contract and supervision

- verified success and clean teardown;
- explicit cancellation, refusal (synthetic contract test only until a real
  provider mapping is proven), timeout, interruption, and generic failure;
- partial output plus fatal terminal;
- success evidence followed by parser/transport failure or nonzero exit;
- duplicate and conflicting terminal evidence;
- timeout/provider-cancel and stop/provider-cancel races;
- runner finishes concurrently with the deadline/control signal;
- bounded terminate/kill/reap and SDK close/reaper behavior;
- exactly one outcome for every started turn.

### Workflow and daemon

- later sequential failure preserves earlier completed records and fails the
  session;
- failed directed follow-up leaves `awaiting_input` and fails the session;
- explicit stop records active interruptions without terminal overwrite;
- daemon restart marks the session interrupted without fabricating a turn;
- degraded parallel stage accepts only completed members satisfying output
  policy;
- all parallel members fail -> session failed;
- a non-completed sequential turn after a completed stage fails the session
  (covers the deferred composed-workflow shape; exercised with a sequential
  workflow until #19 adds `stages`);
- terminal-to-different/live status writes are rejected.

### Persistence and APIs

- old records without new fields load as unknown/null;
- new empty and multi-outcome records round-trip;
- older readers ignore unknown keys;
- outcome list is packed and append-only, keyed semantically by turn ID;
- status/detail/read/wait share field names and nesting;
- terminal long poll returns unchanged cursor with `events=[]`;
- mid-stage outcome + boundary event wakes polling with a consistent snapshot;
- event projection cannot produce cursor/status skew.

### Security

- secrets, headers, env values, paths, raw provider objects, prompts, stderr,
  and hostile exception strings cannot enter failure/outcome objects;
- provider stop reason accepts only bounded allowlisted tokens;
- canonical messages are stable and length bounded.

### Provider mappings

- one fixture-backed terminal suite per backend;
- success, explicit failure, malformed/incomplete output, and cancellation or
  interruption where supported;
- CLI process-exit interaction;
- SDK exception and cleanup interaction;
- strict credentialed smoke only after the relevant hermetic mapping passes.

Local gates:

```bash
./agent_collab_dev.sh test
./agent_collab_dev.sh build --check
git diff --check
```

Credentialed verification, when explicitly authorized during implementation:

```bash
./agent_collab_dev.sh integration-test <changed-backend> --strict
```

## Design review findings and disposition

Two independent read-only reviews were run through agent-collab in two rounds:
Grok Build at medium reasoning and Gemini 3.1 Pro (High) in plan mode. Every
session was followed with cursor-based polling until the daemon reported a
terminal state.

### Round 1

**Grok Build**

- Blocker: polling must carry the atomic session snapshot, not only events and
  cursor. **Accepted**; the REST/MCP contract and snapshot rule are explicit.
- Blocker: terminal states need monotonic compare-and-set semantics.
  **Accepted**; all session transitions use one helper.
- Blocker: Antigravity CLI EOF is a provisional weaker contract requiring
  fixtures and acceptance-criteria wording. **Accepted**.
- Important: callback-plus-return is preferable to a separate streamed-run
  future. **Accepted**.
- Important: parallel aggregation must consume outcomes, sanitization must be
  allowlisted, and refusal must be evidence-only. **Accepted**.
- Provider mappings without fixture/type evidence are untestable assumptions.
  **Accepted**; they are recorded as verification items rather than facts.

**Gemini 3.1 Pro (High)**

- Blocker: #19's message-only member-success predicate misclassifies partial
  output plus fatal terminal. **Accepted**; issue #17 precedes #19 runtime and
  the parallel task document is corrected.
- Important: the callback-plus-return runner, current small session state
  machine, additive index compatibility, conservative Antigravity EOF, and
  evidence-only refusal are sound. **Accepted**.
- Unsupported assumption: every backend can distinguish refusal. **Accepted**;
  issue #17 acceptance criteria are narrowed to verified evidence.

### Round 2

The revised proposal changed provider markers from immediately committed
outcomes to accumulated evidence resolved only after teardown. This removed a
contradiction between “first marker wins” and “later nonzero/transport failure
overrides success.”

**Grok Build**

- No remaining blocker, contradictory transition, unsafe structured-error
  exposure, or provider claim beyond the stated evidence boundaries.
- Confirmed the runner/referee arbitration, multiple outcomes, atomic polling,
  small session status machine, and implementation ordering. **Accepted**.

**Gemini 3.1 Pro (High)**

- Blocker: unbounded cleanup can prevent timeout/interrupt outcome recording.
  **Accepted**; cleanup has a fixed grace, force/reaper fallback, and cannot
  block terminal state indefinitely.
- Important: mid-stage outcome mutations must wake pollers. **Accepted**;
  outcome recording is paired atomically with a boundary event.
- Important: incomplete parallel outcome collections must not be sparse or
  ambiguously ordered. **Accepted**; use a packed append-only list and semantic
  turn IDs.
- Optional: hermetic gates should apply per slice, not only at the end.
  **Accepted**.

### Cross-plan round with #19 (2026-07-13)

A Codex (gpt-5.6-sol high) review of this document together with the revised
flat-parallel `parallel-review-workflow.md` returned COMPATIBLE WITH CHANGES:
the data models compose, but three interfaces were unspecified. All are now
defined here: the daemon-to-referee stop signal registered before cancellation
with a single post-settle `stopped` publisher (Referee arbitration), the
awaited `record_turn_outcome(record, boundary_event)` commit operation (Runner
delivery mechanism), and the stage-level failure record with
`parallel_stage_no_accepted_member` (Stable codes; Persistence). The
relationship section also gained the ship-together rule: #19 config work may
be developed separately but becomes externally visible only with the stage
runtime.

No material review suggestion was rejected. Two reviewer statements were
clarified rather than adopted literally:

- the index version remains 1 because the envelope is unchanged; compatibility
  comes from optional fields and tests, not from fabricating a migration;
- provider terminal behavior noted from installed interfaces remains
  provisional until the listed fixture or credentialed verification exists.

## Decisions

- Implement issue #17 before the parallel referee/runtime work in issue #19.
- Use an async event sink plus one returned outcome; do not use a terminal event
  or a separate result future.
- Accumulate provider terminal evidence and commit only after clean teardown or
  referee control arbitration.
- Keep the six-value turn vocabulary and the existing six-value session status
  vocabulary separate.
- Persist every new turn outcome; keep one session failure pointer for the
  decisive terminal cause.
- Provider identity and partial output never prove success.
- Fatal provider evidence beats exit zero; abnormal exit/transport beats prior
  success evidence on normal runner completion.
- Timeout/stop are causal referee outcomes when the runner had not already
  completed at arbitration.
- Cleanup is bounded and cannot prevent terminal outcome recording.
- Marker-bearing transports fail closed on EOF without their marker.
- Antigravity CLI uses a documented provisional clean-EOF fallback pending
  fixtures.
- Refusal is never inferred from prose and need not be mapped by every backend.
- Keep session statuses small; use structured codes and per-turn records for
  detail.
- Keep `INDEX_VERSION=1`; old missing fields remain unknown/null.
- Event polling carries current outcome state directly without changing cursor
  semantics.
- Structured failures are allowlisted and sanitized before persistence.
- Automatic retry remains outside scope.
- Native resume, interrupt acknowledgement, and interactive tool approval
  remain tracked in `sdk-session-control.md`.

## Remaining open questions

These are evidence-gathering questions, not unresolved shared-contract choices:

1. Can Antigravity CLI emit exit zero with failure/auth/timeout prose, and is a
   stronger machine-readable print surface available in the supported CLI?
2. Which providers expose structured refusal evidence, if any, in the pinned
   CLI/SDK versions?
3. What exact sanitized terminal fixtures do Claude CLI permission denials,
   Claude SDK result subtypes, Codex CLI failure records, Antigravity SDK empty
   responses, and xAI SDK finish reasons produce?
4. Which cleanup failures in each SDK leave completion/resource ownership
   uncertain versus being documented best-effort close diagnostics?

None requires authorization before implementing the shared hermetic slices.
Credentialed fixture capture and provider smoke tests require the normal
explicit implementation-time authorization and must remain under
`integration_tests/`.
