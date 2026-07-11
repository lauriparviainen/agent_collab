# Codex SDK backend

Registered as `codex_sdk` (`type="codex"`, `backend="sdk"`). It uses `openai-codex` `AsyncCodex`, starts an ephemeral thread, runs a turn, and maps the collected result.

## Selection and requirements

Select with `backend="sdk"`. The `openai-codex` wheel and its runtime are required. Authentication uses `OPENAI_API_KEY` or Codex local sign-in and remains provider-managed.

## Options

[`options.toml`](options.toml) is authoritative. Model, effort, and sandbox map to SDK fields. `thinking_level` and `reasoning_effort` are aliases. CLI profile, approval, and search options are unsupported. Nothing is inferred from CLI argv.

## Events and identity

The stable final response is message-first; known command/file/reasoning items are mapped and unknown items become verbose status. The thread id is captured with identity kind `thread`; resume is not implemented. A configured local `codex` binary is preferred over the SDK-pinned runtime when available.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Threads are ephemeral and cwd-scoped. Missing/incompatible runtime setup fails probing or produces an error event.

## Testing

Hermetic: `./agent_collab.sh test -k codex_sdk`. Live: `./agent_collab.sh integration-test codex_sdk`.
