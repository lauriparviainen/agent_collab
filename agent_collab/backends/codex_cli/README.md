# Codex CLI backend

Registered as `codex_cli` (`type="codex"`, `backend="cli"`). It runs `codex exec --json` and maps JSONL items into normalized events.

## Selection and requirements

Select with `backend="cli"`; the agent needs a configured `codex` command. Codex owns sign-in/API-key authentication. The probe checks binary presence/version and reports credential status as `unknown`.

## Options

[`options.toml`](options.toml) is authoritative for accepted keys and values;
[`defaults.toml`](defaults.toml) owns the shipped backend settings and disabled
Event Window target. Model, profile, effort, sandbox, approval policy, and
search map to CLI flags/config. `thinking_level` and `reasoning_effort` are
aliases and must agree. Values may be inferred from argv. The shipped `sandbox`
default is `read-only` (commands run, writes are blocked); `workspace-write` is
the write opt-in.

## Events and identity

Agent messages, commands, file changes, tools, errors, and verbose statuses are mapped from JSONL. `thread.started.thread_id` is captured as provider identity kind `thread`, but resume is not implemented.

## Turn outcome

`turn.completed` is verified success and `turn.failed` is terminal failure.
`thread.started` is identity only, and failed command/item events remain
diagnostic when the enclosing turn later completes. EOF without a turn marker,
malformed output, transport failure, or nonzero exit fails closed.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution is cwd-scoped with
stdin closed. Provider-native sandbox and approval policy remain explicit
backend options.

The separate top-level outer policy supports `sandbox="read-only"` in Stage 1.
It resolves the complete effective `CODEX_HOME` as persistent writable state,
requires that directory to exist with safe ownership and permissions, and
proves a Linux Bubblewrap boundary before adding
`--dangerously-bypass-approvals-and-sandbox`. Conflicting Codex-owned native
sandbox/approval flags are removed only for that proven outer profile.
Top-level `sandbox="none"` preserves the command byte-for-byte. Other
backends remain outer-read-only unsupported until their own implementation
stages.

## Testing

Hermetic: `./agent_collab_dev.sh test -k codex_cli`. Credential-free real
namespace boundary: `./agent_collab_dev.sh bubblewrap-test`. Live:
`./agent_collab_dev.sh integration-test codex_cli`. The outer-sandbox live
acceptance is skipped unless `AGENT_COLLAB_IT_CODEX_SANDBOX_STATE` names an
operator-authorized dedicated complete Codex state root.
