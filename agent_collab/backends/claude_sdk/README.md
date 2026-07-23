# Claude SDK backend

Registered as `claude_sdk` (`type="claude"`, `backend="sdk"`). It calls `claude-agent-sdk` in-process and maps typed messages and content blocks.

## Selection and requirements

Select with `backend="sdk"`. Python and `claude-agent-sdk>=0.2.126,<0.3.0`
are required. Authentication may use `ANTHROPIC_API_KEY` or the SDK's Claude
Code sign-in; agent-collab never stores credentials. The Agent SDK exposes
model selection but no public model-list API, so catalog suggestions remain
static.

## Options

[`options.toml`](options.toml) is authoritative for accepted keys and values;
[`defaults.toml`](defaults.toml) owns the shipped backend settings and disabled
Event Window target, including the read-only `permission_mode = "default"`
posture. `model`, `permission_mode`, `thinking_level`, and
`thinking_budget_tokens` map to `ClaudeAgentOptions`; level and raw-budget
requests conflict. SDK options are never inferred from CLI argv. Runs use
`setting_sources=[]` and the `claude_code` system/tool presets.

## Conversation lifecycle

One persistent `ClaudeSDKClient` per runner/session (verified on 0.2.126):
lazy connect on the first turn, then sequential `query()`/`receive_response()`
turns on the same live client and native provider session. After an abnormal
turn the adapter resets the live client but keeps the captured session id; the
next turn reconnects with `ClaudeAgentOptions(resume=<sid>,
fork_session=False)`. A rejected/expired resume fails the turn structurally —
never a silent fresh provider session. A prompt that never reached the
hand-off to the client (failed connect/resume) is retained and prepended to
the next delivered prompt; once handed to `query()`, delivery is uncertain and
it is never replayed. Run/reset/close are serialized inside the adapter;
`close()` drops all state and is idempotent.

## Events and identity

Typed text/tool/result blocks map to normalized events. Thinking signatures
are never emitted. `ResultMessage.session_id` is captured as provider identity
kind `session` and fed back to the adapter for reconnect-by-resume.

## Turn outcome

A terminal non-error `ResultMessage` is required for completion. An error
result, SDK/transport exception, missing result, or uncertain close fails and
triggers one bounded adapter reset (the captured session id survives). SDK
stream close is bounded; an over-grace close or reset transfers to a
background reaper without delaying timeout/interruption recording.

## Capabilities and security

`continuity` is true: follow-up turns in a live session continue the provider
thread natively and the referee sends delta continuation prompts.  `resume`,
`interrupt`, and `tool_gate` are false (issue #20's strict definitions: no
restart-safe resume, no provider-verified interrupt, no tool gating). The
disposable/session workdir is passed as SDK cwd. Missing wheels fail
availability probing; runtime/auth errors become transcript error events.

## Testing

Hermetic: `./agent_collab_dev.sh test -k claude_sdk`. Live: `./agent_collab_dev.sh integration-test claude_sdk`.
