# SDK session control: resume, interrupt, and tool approval

**Status:** Open design and implementation task.

## Purpose

Turn the SDK backends' captured provider session identities into real,
provider-tested session control:

- continue an agent through its native provider session instead of relying only
  on transcript text in the next prompt;
- interrupt an active SDK turn through a verified provider cancellation path;
- surface SDK tool approval requests through the daemon, REST, MCP, CLI, and
  TUI, then return an explicit approve or deny decision to the waiting SDK.

Do not flip a capability merely because an SDK exposes a suggestive method or
returns a session ID. A backend advertises `resume`, `interrupt`, or `tool_gate`
only after agent-collab owns the complete lifecycle and the behavior is covered
by hermetic tests plus a credentialed provider smoke test.

## Current state

All eight backends deliberately report these capabilities as false:

```json
{"resume": false, "interrupt": false, "tool_gate": false}
```

The SDK backends already capture a uniform provider identity:

| Backend | Provider term | Captured field |
| --- | --- | --- |
| `claude_sdk` | session | `provider_session_id` |
| `codex_sdk` | thread | `provider_session_id` |
| `antigravity_sdk` | conversation | `provider_session_id` |
| `xai_sdk` | response | `provider_session_id` |

`SessionState.agent_sessions` persists those IDs and their provider-specific
kind, but nothing feeds an ID back into a later turn. Each current SDK runner
opens a new provider context per turn:

- Claude calls the one-shot `query(prompt=..., options=...)` helper;
- Codex creates `AsyncCodex`, starts a new thread, runs one turn, then closes
  the client;
- Antigravity creates a new `Agent` context and captures its conversation ID
  only after the response resolves.

The daemon stop path cancels the local session task, but no SDK backend proves
that the provider operation was cancelled and acknowledged. SDK permission
callbacks are not connected to session state, and there is no approval response
operation. `agent_collab_post_message` queues a later referee turn; it does not
inject text or decisions into the active provider turn.

## Capability semantics

Keep these definitions strict and backend-specific.

### `resume`

`resume = true` means agent-collab can continue a captured provider session:

1. during a later turn in the same live agent-collab session; and
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
cancellation mechanism, closes the active response stream/client, and reaches a
known terminal result within a bounded timeout.

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
exact API used.

### Claude SDK

Verify whether the installed `claude-agent-sdk` supports:

- native continuation through `ClaudeAgentOptions.resume`, a persistent
  `ClaudeSDKClient`, or another documented session API;
- a reliable client `interrupt()`/cancel path and its completion semantics;
- a `can_use_tool` or equivalent async permission callback, its request ID,
  tool name/input shape, and approve/deny result types;
- cleanup behavior when the callback is waiting and the session is stopped.

The current one-shot `query()` backend may need to move to a persistent client.
Do not retain the simpler helper if it cannot support the required controls.

### Codex SDK

Verify the installed `openai-codex`/app-server shapes for:

- retaining a thread for several turns while the client stays open;
- reopening or resuming a thread by persisted ID after client/daemon restart;
- turn cancellation and acknowledgement;
- command/file-change approval notifications and response methods.

Starting a new thread with the same transcript is not thread resume. If the
pinned SDK only supports in-client continuation, implement and advertise that
fact separately until restart-safe resume exists; keep the public `resume`
capability false under the strict definition above.

### Antigravity SDK

Verify the installed `google-antigravity` API for:

- constructing or reopening an `Agent` from `conversation_id`;
- cancelling an unresolved `ChatResponse` and confirming termination;
- intercepting local `BuiltinTools` or tool callbacks before execution.

The presence of `Agent.conversation_id` is identity evidence, not proof that
all three controls exist.

## Shared runner-control contract

Extend the backend/runner boundary with an explicit controller rather than
provider-specific checks in the daemon. An illustrative shape is:

```python
class ActiveTurnController(Protocol):
    async def interrupt(self) -> InterruptResult: ...
    async def respond_to_tool(self, request_id: str, decision: ToolDecision) -> None: ...

class AgentRunner(Protocol):
    async def run(
        self,
        prompt: str,
        workdir: Path,
        provider_session: ProviderSession | None = None,
    ) -> AsyncIterator[Event]: ...
```

The exact types may differ, but the contract must provide:

- one active controller per agent turn;
- registration and removal under the referee/daemon session lock;
- a durable provider-session input and sanitized provider-session output;
- idempotent interruption;
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
  `awaiting_input`;
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
active controller to interrupt, waits for a short bounded acknowledgement, and
then falls back to local task cancellation and resource cleanup.

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

Capabilities remain facts declared by each concrete backend. It is valid to
land `claude_sdk.resume = true` while the other SDKs remain false.

Keep the existing session reducer conservative:

- session resume requires every selected non-mock backend to support resume and
  every required provider session ID to be captured;
- session interrupt requires every currently active backend turn to support
  reliable interrupt;
- tool approval is reported per agent/backend; do not imply a workflow-wide
  gate when only one agent supports it.

`agent_collab_describe_options`, start settings, session status, and the TUI
must all project the same backend capability facts.

## Safety and failure rules

- Never automatically retry a provider operation after an uncertain interrupt.
- Never silently replace native resume with a new provider session.
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

- two turns reuse the exact captured provider session;
- a persisted identity is restored after a simulated daemon restart;
- incompatible resume fingerprints and rejected/expired sessions fail without
  creating a fresh provider session;
- interrupt calls the provider exactly once, acknowledges within the bound,
  and handles completion races;
- interrupt fallback cancels and closes resources when the provider hangs;
- tool requests enter `awaiting_approval`, emit sanitized correlated events,
  and resume on approve or deny;
- duplicate, stale, cross-session, and post-stop approval responses are
  rejected;
- approval timeout and daemon shutdown deny and release the SDK callback;
- mixed workflows aggregate capability flags honestly;
- session-index round trips preserve only the sanitized resume descriptor;
- REST, direct MCP, stdio-via-REST, CLI, and TUI share the same behavior.

Keep `AGENT_COLLAB_HOME` isolated in every test. No hermetic test may import a
real optional SDK, read native credentials, or make a model call.

### Credentialed integration tests

Add opt-in, low-cost tests separately for each provider capability:

- native second-turn continuity demonstrates provider memory not supplied in
  the second prompt;
- interrupt a harmless long-running response and verify provider/client
  cleanup;
- gate a harmless tool, deny once, then approve once, verifying no execution
  occurs before approval.

Skip when the installed SDK version, account, or provider does not support the
feature. A skipped provider keeps the corresponding production capability
false.

## Staged delivery

1. **Shared control/state contract.** Add controller registration, persisted
   resume descriptors, approval event/state types, race-safe terminal
   transitions, and fake-runner tests while all production capabilities remain
   false.
2. **Claude SDK controls.** Re-confirm the installed persistent-client,
   continuation, interrupt, and permission-callback APIs; implement only the
   capabilities proven by that version.
3. **Codex SDK controls.** Re-confirm thread reopen, cancellation, and app-server
   approval APIs; flip each capability independently.
4. **Antigravity SDK controls.** Re-confirm conversation reopen, response
   cancellation, and pre-execution tool interception; unsupported capabilities
   remain false.
5. **Restart-safe resume and public surfaces.** Land explicit resume and
   approval operations across REST/MCP/CLI/TUI once persistence and
   authorization semantics are stable.

Each stage must be independently shippable and must not make existing CLI or
message-first SDK workflows less reliable.

## Acceptance criteria

- Capability flags are true only for provider/backend pairs with implemented,
  tested behavior.
- Repeated turns use native provider continuity when resume is advertised.
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
- Existing CLI backends remain `resume=false`, `interrupt=false`, and
  `tool_gate=false` unless a separate bidirectional CLI protocol task lands.
- Hermetic tests pass, and every enabled production capability has a separate
  credentialed integration test.
