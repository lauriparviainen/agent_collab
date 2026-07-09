# Claude CLI backend

Registered as `claude_cli` (`type="claude"`, `backend="cli"`). It runs Claude Code as a subprocess and maps stream-JSON stdout into agent-collab events.

## Selection and requirements

Select with `backend="cli"`; the configured agent needs a `claude` command. Authentication is owned by Claude Code and is not stored by agent-collab. The health probe checks the binary and version; credential status is `unknown` because local sign-in cannot be verified safely.

## Options

[`options.toml`](options.toml) is authoritative. `model`, `permission_mode`, `thinking_level`, and `thinking_budget_tokens` map to CLI flags and may be inferred from configured argv. Level and raw-budget requests conflict.

## Events and identity

Text becomes `claude/message`; tool blocks become `tool/tool_call`, `command`, or `file_change`; errors become `error/error`. Thinking is emitted only as verbose status and signatures are never emitted. CLI output does not currently provide resumable identity.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution uses the resolved agent cwd, closes stdin, and relies on the configured permission mode. Recursive agent spawning remains prohibited by referee guardrails.

## Testing

Hermetic: `./agent_collab.sh test -k claude_cli`. Live: `./agent_collab.sh integration-test claude cli`.
