# Antigravity CLI backend

Registered as `antigravity_cli` (`type="antigravity"`, `backend="cli"`). It runs `agy` print mode. Since output is plain text, fidelity is message-only.

## Selection and requirements

Select with `backend="cli"`; `agy` must be on PATH. The health probe checks the binary and version, and looks for a cached Antigravity OAuth token or an active Google account under `~/.gemini/`. It never returns a definite "missing" for credentials: recent `agy` may sign in through the OS keyring, so an unverifiable sign-in is reported as `unknown` (a start warning, not a block). Agent-collab never manages those credentials. This backend is enabled by default and blocks start only when `agy` itself is definitely unavailable.

## Options

[`options.toml`](options.toml) is authoritative for accepted keys and values;
[`defaults.toml`](defaults.toml) owns the shipped backend settings and disabled
Event Window target. `model` and `mode` map to flags before print mode and may
be inferred from argv, and the boolean `sandbox` option maps to the `--sandbox`
terminal-restriction flag. The shipped `mode` default is the read-only `plan`;
`accept-edits` is the explicit write opt-in (it auto-approves edits, including
destructive ones). The resolved workdir is supplied with `--add-dir` unless
already configured. Agent-collab also supplies `--print-timeout` from the
session's per-agent turn timeout (900 seconds by default), preventing `agy -p`'s
shorter five-minute default from ending a turn early. An explicit
`--print-timeout` in the configured backend `args` is preserved as an
intentional override.

## Events and identity

Every non-empty stdout line becomes `antigravity/message`. Tool structure and provider conversation identity cannot be recovered from print mode.

## Turn outcome

This message-only transport has the one provisional clean-EOF fallback: exit
zero plus at least one non-empty stdout message completes; empty output,
nonzero exit, or output/transport failure fails. No prose is classified as a
provider cancellation or refusal. A stronger marker contract remains pending
provider evidence.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution is confined to the resolved cwd/add-dir configuration.

## Testing

Hermetic: `./agent_collab_dev.sh test -k antigravity_cli`. Live: `./agent_collab_dev.sh integration-test antigravity_cli`.
