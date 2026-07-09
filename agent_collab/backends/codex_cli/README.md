# Codex CLI backend

Registered as `codex_cli` (`type="codex"`, `backend="cli"`). It runs `codex exec --json` and maps JSONL items into normalized events.

## Selection and requirements

Select with `backend="cli"`; the agent needs a configured `codex` command. Codex owns sign-in/API-key authentication. The probe checks binary presence/version and reports credential status as `unknown`.

## Options

[`options.toml`](options.toml) is authoritative. Model, profile, effort, sandbox, approval policy, and search map to CLI flags/config. `thinking_level` and `reasoning_effort` are aliases and must agree. Values may be inferred from argv.

## Events and identity

Agent messages, commands, file changes, tools, errors, and verbose statuses are mapped from JSONL. No resumable thread identity is captured from this CLI stream.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution is cwd-scoped with stdin closed; sandbox and approval policy remain explicit options.

## Testing

Hermetic: `./agent_collab.sh test -k codex_cli`. Live: `./agent_collab.sh integration-test codex cli`.
