# Backend turn outcomes and MCP failure reporting

**Status:** Open design and implementation task.

**Created:** 2026-07-13.

**Issue:** #17.

## Purpose

Give every supervised backend one explicit, backend-neutral way to say whether
an agent turn succeeded, was cancelled or interrupted, timed out, was refused,
or failed. Carry that outcome through the referee and daemon so CLI, REST, and
MCP callers receive a truthful terminal session state without interpreting
provider-specific transcript text or raw payloads.

This is both a correctness and an agent-ergonomics task. A calling agent should
be able to distinguish these cases directly:

- the provider returned a usable response;
- the provider stopped before returning a response;
- local supervision stopped or timed out the turn;
- authentication, entitlement, model selection, transport, or provider service
  failed;
- a tool permission was denied in a way that prevented the turn from
  completing;
- the provider returned partial prose and then failed.

## Triggering evidence

A real headless xAI CLI review produced an `end` record with
`stopReason=Cancelled` and a provider session ID, then exited with process code
zero. The xAI parser treated every `end` record with a session ID as successful
session bookkeeping. The daemon therefore completed the supervised session,
and `agent_collab_wait_events` returned only events and a cursor. The calling
agent had to inspect provider-specific `raw` data to discover that no review had
actually completed.

Exported provider history showed that the cancelled turn had requested a shell
tool whose interactive approval could not be answered by the headless process.
This exposed two separate gaps:

1. backend defaults must be suitable for non-interactive supervision; and
2. known provider failure evidence must survive as shared daemon/MCP state.

The first xAI-specific remediation was implemented on 2026-07-13: default
headless xAI CLI runs execute without approval prompts inside Grok's read-only
sandbox, guide the model toward individual inspection commands, expose Grok's
internal model/tool-loop ceiling as `provider_max_turns`, and map any terminal
reason other than `EndTurn` to a structured fatal provider event. That
remediation is useful evidence for this task, but does not complete the shared
contract.

Verification for this slice:

- Ruff and all 739 hermetic tests passed;
- `./agent_collab_dev.sh build --check` passed;
- `./agent_collab_dev.sh integration-test xai_cli --strict` completed a real
  headless turn and retained provider session identity;
- durable installation and daemon restart exposed the new defaults and typed
  option through MCP discovery;
- a high-reasoning `grok-build` review launched through agent-collab MCP with
  `provider_max_turns=100` completed with `stopReason=EndTurn` and reported no
  blockers.

## Current contract gaps

### Runner boundary

`AgentRunner.run()` is an async event iterator with no explicit result value.
Subprocess runners emit the local process exit code as a referee status event,
but provider protocols may report failure inside stdout while returning exit
code zero. SDK runners also expose errors through provider-specific event
mapping rather than one terminal result type.

An ordinary `Event(type="error")` is not currently sufficient to define the
session outcome. Some stderr or provider errors may be diagnostic and followed
by a usable response, while a definitive cancellation must fail the turn.

### Referee and daemon

The referee consumes events but does not retain a typed outcome for each turn.
After `Referee.run()` returns normally, the daemon marks a live session done
even if a backend emitted definitive fatal failure evidence. Local timeout is
an error event rather than a shared terminal result.

Persisted `SessionState.error` is a string intended for session-level failure;
it has no stable error code, backend/agent attribution, provider stop reason, or
local process result.

### REST and MCP

Event polling returns the cursor and new events, but a caller should not need a
second status call or provider-specific raw-event inspection to learn that the
session is terminal. MCP guidance cannot currently tell agents to rely on a
uniform failure object because none exists.

## Outcome semantics

Use a small, shared vocabulary. Provider-specific values remain sanitized
diagnostic fields; they do not become public status enums.

| Outcome | Meaning |
| --- | --- |
| `completed` | The backend produced a usable response and verified its provider success marker when one exists. |
| `cancelled` | The provider or a permission path cancelled the turn before successful completion. |
| `interrupted` | Agent-collab or the user intentionally stopped an active turn. |
| `timed_out` | A configured local deadline expired before terminal provider success. |
| `refused` | The provider completed normally but explicitly declined the task with no usable requested result. |
| `failed` | Authentication, entitlement, model, protocol, transport, tool, provider service, or unknown terminal failure prevented completion. |

Do not infer `completed` solely from process exit code zero, a provider session
ID, the presence of partial text, or absence of a Python exception.

Partial text plus a fatal terminal result remains a failed turn. Preserve the
partial text in the transcript, but keep the terminal outcome unambiguous.

## Shared result shape

Introduce an internal typed result owned by agent-collab rather than encoding
the contract only in event dictionaries. One illustrative shape is:

```python
@dataclass(frozen=True)
class TurnOutcome:
    status: Literal[
        "completed", "cancelled", "interrupted", "timed_out", "refused", "failed"
    ]
    code: str | None = None
    message: str | None = None
    agent_id: str | None = None
    backend: str | None = None
    provider_stop_reason: str | None = None
    process_exit_code: int | None = None
```

The exact delivery mechanism remains an implementation decision. Reasonable
options include a terminal event subtype consumed specially by the referee, an
explicit result future beside the event iterator, or a runner result object
that owns both the stream and terminal outcome. Requirements are:

- exactly one authoritative terminal outcome per started turn;
- provider session identity remains separate from success/failure;
- the outcome is available after normal EOF, exceptions, local cancellation,
  and timeout;
- a backend cannot accidentally report both success and fatal failure;
- cleanup and process-reaping behavior remains deterministic;
- shared daemon code never branches on provider SDK or CLI payload types.

## Error codes and sanitization

Machine-readable codes should be stable and backend-neutral where possible,
with provider-specific detail in bounded optional fields. Initial examples:

- `provider_turn_cancelled`
- `provider_turn_refused`
- `provider_terminal_failure`
- `provider_authentication_failed`
- `provider_model_unavailable`
- `provider_transport_failed`
- `provider_output_invalid`
- `local_turn_timed_out`
- `local_turn_interrupted`
- `subprocess_exit_nonzero`

Messages must be useful but sanitized. Never persist or return credentials,
authorization headers, environment values, full provider request/response
objects, unrestricted stderr dumps, or sensitive filesystem paths. Preserve
raw provider data in transcript events only under the existing event-safety
rules; the shared failure object should contain an allowlisted subset.

## Backend evidence matrix

Re-verify every backend against the installed pinned provider version. Do not
guess success markers or failure reasons from brand similarity.

### CLI backends

- `claude_cli`: distinguish stream `result` success/error subtypes, process
  failure, malformed/partial output, and permission cancellation.
- `codex_cli`: distinguish completed and failed turn records, command-level
  failures that the agent recovers from, process failure, and truncated JSONL.
- `antigravity_cli`: the current message-only transport may need a stronger
  machine-readable terminal surface or a documented conservative EOF rule.
- `xai_cli`: treat only fixture-confirmed `EndTurn` as success; map cancelled
  and other terminal reasons as fatal while retaining provider session
  identity separately.

### SDK backends

- `claude_sdk`: map result/error messages, SDK exceptions, refusal, local
  cancellation, and stream closure before a result.
- `codex_sdk`: map turn status/result objects, SDK exceptions, refusal, local
  cancellation, and client/thread cleanup failures.
- `antigravity_sdk`: map response resolution, explicit error payloads,
  exceptions, and unresolved-response cancellation.
- `xai_sdk`: map response completion, API exceptions, refusal or empty content,
  response identity, and cancellation.

Each backend needs fixture-backed hermetic tests. Credentialed model calls
belong only under `integration_tests/` and should use the lowest-cost reliable
model/effort that exercises the changed terminal contract.

## Daemon and persistence

Promote a fatal turn result to terminal daemon state. A normal return from the
referee must not overwrite `failed`, `stopped`, or another already-terminal
state with `completed`.

Persist a sanitized structured failure beside the existing human-readable
error string, for example:

```json
{
  "status": "failed",
  "error": "Grok ended the turn before producing a response",
  "failure": {
    "code": "provider_turn_cancelled",
    "agent_id": "xai_cli",
    "backend": "xai_cli",
    "provider_stop_reason": "Cancelled",
    "process_exit_code": 0
  }
}
```

The persistence schema and generated REST documentation must be updated
together. Old stored sessions without `failure` remain readable; no fabricated
failure detail should be synthesized for them.

## REST and MCP contract

`GET /sessions/{id}/events` and `agent_collab_wait_events` should always return
the current session state alongside new events:

```json
{
  "session_id": "opaque-id",
  "cursor": 7,
  "status": "failed",
  "terminal": true,
  "error": {
    "code": "provider_turn_cancelled",
    "agent": "xai_cli",
    "message": "Grok ended the turn before producing a response",
    "provider_stop_reason": "Cancelled",
    "process_exit_code": 0
  },
  "events": []
}
```

Requirements:

- include `status` and `terminal` even when no new events arrive;
- include structured error detail only when a terminal or actionable failure
  exists;
- preserve cursor semantics and long-poll behavior;
- make `agent_collab_status` and REST session detail expose the same failure;
- update `agent_collab_guidance` to require callers to inspect status/error
  fields rather than process-exit prose;
- avoid automatic retries in the daemon. The calling agent or user decides
  whether a safe retry is appropriate from the stable error code;
- keep start asynchronous; do not block `agent_collab_start` until a turn
  completes merely to return its outcome.

## CLI behavior

Watch/TUI output should render one clear fatal line using the repository marker
conventions, followed by stable session status. Human-readable output may show
provider detail, but scripts should consume REST/MCP structured fields rather
than scrape terminal text.

Provider process exit code remains useful diagnostic evidence and should still
be logged. It must not be presented as the authoritative turn outcome when a
provider protocol has already reported failure.

## Implementation plan

1. Finish the xAI CLI remediation and capture sanitized cancelled/success
   fixtures plus a credentialed headless smoke test.
2. Define the internal `TurnOutcome` contract and its precedence rules for
   provider terminal evidence, local timeout/stop, exceptions, and process exit.
3. Refactor the shared runner/referee boundary to return exactly one outcome
   without losing streamed events or deterministic cleanup.
4. Add daemon failure persistence and prevent terminal state overwrite.
5. Extend REST event polling/session detail and regenerate API artifacts.
6. Extend MCP tools and guidance with status, terminal, and structured error
   fields.
7. Map each remaining CLI and SDK backend using fixture-confirmed evidence.
8. Update CLI/TUI presentation and maintained architecture/configuration docs.
9. Run the full hermetic gate, build/documentation check, package smoke tests,
   and one strict credentialed smoke per behaviorally changed provider backend.

Keep each provider mapping reviewable. The shared contract may land before all
backends have rich provider-specific codes, but no backend may silently claim
success from known fatal evidence during the transition.

## Decisions

- Provider session identity is bookkeeping, never proof of success.
- Local process exit code is diagnostic, never sufficient proof of provider
  success when a richer terminal protocol exists.
- Fatal provider evidence fails the turn even when partial prose was emitted.
- MCP polling carries session outcome directly; callers do not infer it from
  transcript wording.
- Error payloads are allowlisted and sanitized before persistence or MCP/REST
  exposure.
- Automatic retry is outside this task.
- Native resume, interrupt acknowledgement, and interactive tool approval
  remain tracked separately in `sdk-session-control.md`.

## Verification

Minimum hermetic coverage:

- success, explicit failure, cancellation, refusal, timeout, nonzero process
  exit, malformed output, EOF without terminal evidence, and partial-text-plus-
  failure paths at the runner/referee contract;
- one fixture-backed terminal mapping suite per backend;
- daemon tests proving fatal outcome persistence and completed-state overwrite
  prevention;
- REST and MCP tests proving terminal status/error is returned with and without
  new events while cursors remain stable;
- security tests proving secrets and unrestricted raw provider payloads cannot
  enter structured failures;
- CLI/TUI tests for clear fatal rendering without unstable output scraping.

Final gates:

```bash
./agent_collab_dev.sh test
./agent_collab_dev.sh build --check
./agent_collab_dev.sh integration-test <changed-backend> --strict
```

Record provider CLI/SDK versions and the sanitized observed terminal shapes in
backend-owned fixture documentation.

## Open questions

1. Should `cancelled`, `interrupted`, `timed_out`, and `refused` become daemon
   session statuses, or remain `status=failed` with a more specific
   `failure.code`? Prefer the smallest stable public state machine.
2. Should a backend that reaches EOF without a verified success marker fail
   closed, or may message-only transports define non-empty clean EOF as success?
   Decide per verified backend capability, not globally by convenience.
3. Should fatal outcome delivery use a terminal event, a result future beside
   the event iterator, or a new streamed-run object? Choose the shape that makes
   exactly-once outcome and cancellation cleanup easiest to test.
4. How should a provider refusal with useful explanatory prose differ from a
   policy/tool denial that prevented the requested review? Both need stable
   machine-readable codes even if they share a daemon terminal status.
