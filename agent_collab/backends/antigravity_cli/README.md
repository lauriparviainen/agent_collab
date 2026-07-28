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

Every substantive stdout line becomes `antigravity/message`. Blank and
structural-only lines are ignored; explicit tool-failure markers become fatal
error events. Tool structure and provider conversation identity cannot be
recovered from print mode.

## Turn outcome

This message-only transport has the one provisional clean-EOF fallback: exit
zero plus at least one substantive stdout message completes; empty or
structural-only output, nonzero exit, or output/transport failure fails.
Explicit Antigravity `TOOL_ERROR:`/tool-action failure status lines are
retained as private terminal evidence, so a provider exit of zero cannot turn a
reported action failure into a successful turn. Ordinary response prose is not
classified as a provider cancellation or refusal.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution uses the resolved
cwd/add-dir configuration and closes stdin.

The separate top-level outer policy supports `sandbox="read-only"` in Stage 4.
It reuses the common Linux Bubblewrap launcher and declares exactly
`$HOME/.gemini` as complete persistent writable Antigravity state. `HOME` must
be absolute; the adapter validates the `.gemini` basename and maps the inner
`HOME` to its validated parent because Antigravity has no supported state-root
relocation variable. The state directory must already exist, be owned by the
daemon user, contain no symlink component, and not be group/world writable.
The `antigravity-cli/bin/agentapi` materialization path is also checked before
provider execution.

Only after the outer namespace proof ACK does the adapter replace configured
controls with:

- `--dangerously-skip-permissions`;
- `--mode accept-edits`; and
- `--sandbox=false`.

Every repeated `--add-dir PATH` or `--add-dir=PATH` remains provider-visible
but is normalized as read-only. Malformed add-dir or conflicting native-profile
arguments fail closed. Top-level `sandbox="none"` is the explicit rollback: it
preserves the exact original provider command and independently configured
native mode/sandbox posture.

The workspace and session-root Git storage are read-only, while all content
below `.gemini`—including credentials, settings, history, plugins, MCP/hooks,
and helpers—remains writable by Antigravity and descendants. The OS keyring is
reported as an external service outside the filesystem boundary. Keyring
contents and side effects, readable host files, network and remote services,
and delegated writes are not isolated or claimed as protected.

## Testing

Hermetic: `python3 -m unittest tests.backends.antigravity_cli.test_backend
tests.backends.antigravity_cli.test_sandbox`. Credential-free real namespace:
`./agent_collab_dev.sh bubblewrap-test`. Live:
`./agent_collab_dev.sh integration-test antigravity_cli`. The paid Stage 4
acceptance is skipped unless
`AGENT_COLLAB_IT_ANTIGRAVITY_SANDBOX_STATE` names an operator-authorized
dedicated complete `.gemini` directory; it covers native keyring authentication,
agentapi shell execution, child containment, persistent state, workspace denial,
and cleanup.
