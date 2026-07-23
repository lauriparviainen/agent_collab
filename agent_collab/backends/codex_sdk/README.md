# Codex SDK backend

Registered as `codex_sdk` (`type="codex"`, `backend="sdk"`). It keeps one
`openai-codex` `AsyncCodex` client/thread per live runner, runs collected turns
on that thread, and reconnects a captured thread id after an abnormal turn.

## Selection and requirements

Select with `backend="sdk"`. `openai-codex>=0.144.4,<0.145.0` and its runtime
are required. Authentication uses `OPENAI_API_KEY` or Codex local sign-in and
remains provider-managed. Dynamic model discovery uses the SDK's public
`AsyncCodex.models()` catalog. Agent-scoped environment values are passed to
both discovery and turns. If Codex reports a `next_cursor` that its public SDK
cannot follow, agent-collab marks the observation incomplete and retains
static suggestions rather than treating the first page as authoritative.

## Options

[`options.toml`](options.toml) is authoritative for accepted keys and values;
[`defaults.toml`](defaults.toml) owns the shipped backend settings and disabled
Event Window target, including the read-only `sandbox = "read-only"` posture.
Model, effort, and sandbox map to SDK fields. `thinking_level` and
`reasoning_effort` are aliases. CLI profile, approval, and search options are
unsupported. Nothing is inferred from CLI argv.

## Events and identity

The stable final response is message-first and marked as the final ledger answer;
known command/file/reasoning items are mapped and unknown items become verbose
status. The thread id is captured with identity kind `thread`. A configured
local `codex` binary is preferred over the SDK-pinned runtime when available.
An abnormal turn closes the live client but keeps the id; the next turn calls
native `thread_resume`. A rejected resume fails structurally and never starts a
fresh thread. A prompt that could not reach `thread.run()` because connect or
resume failed is retained and prepended after a later successful reconnect, so
the referee's already-advanced delta watermark cannot discard it.

## Turn outcome

Collected `TurnStatus.completed` completes, `interrupted` maps to provider
`cancelled`, and `failed` fails. An in-progress/unknown collected status,
missing result, SDK exception, or uncertain bounded reset fails. Item-level
command failures are diagnostic unless the collected turn itself fails.
Reset cleanup never overwrites an already definitive provider cancellation or
terminal failure; an over-grace reset continues under background ownership.

## Capabilities and security

`continuity` is true: follow-up turns within one live agent-collab session use
the held provider thread and receive delta prompts. `resume`, `interrupt`, and
`tool_gate` remain false under their stricter public definitions. The adapter
serializes run/reset/close; cancelling the asyncio waiter does not claim a
provider interrupt. Missing/incompatible runtime setup fails probing or produces
an error event.

## Testing

Hermetic: `./agent_collab_dev.sh test -k codex_sdk`. Live: `./agent_collab_dev.sh integration-test codex_sdk`.
