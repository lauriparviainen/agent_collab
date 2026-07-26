# Claude CLI backend

Registered as `claude_cli` (`type="claude"`, `backend="cli"`). It runs Claude Code as a subprocess and maps stream-JSON stdout into agent-collab events.

## Selection and requirements

Select with `backend="cli"`; the configured agent needs a `claude` command. Authentication is owned by Claude Code and is not stored by agent-collab. The health probe checks the binary and version; credential status is `unknown` because local sign-in cannot be verified safely.

## Options

[`options.toml`](options.toml) is authoritative for accepted keys and values;
[`defaults.toml`](defaults.toml) owns the shipped backend settings and disabled
Event Window target. `model`, `permission_mode`, `thinking_level`, and
`thinking_budget_tokens` map to CLI flags and may be inferred from configured
argv. Level and raw-budget requests conflict. The shipped `permission_mode`
default is `default` (headless runs deny write/exec tools instead of prompting);
`plan` is the strictest read-only mode and `acceptEdits` is the write opt-in.

## Events and identity

Text becomes `claude/message`; tool blocks become `tool/tool_call`, `command`, or `file_change`; errors become `error/error`. Thinking is emitted only as verbose status and signatures are never emitted. A result/system `session_id` is captured as provider identity kind `session`, but resume is not implemented.

## Turn outcome

The stream must end with a `result` marker. `subtype=success` with
`is_error=false` plus clean process teardown completes the turn; an error
result, malformed/unfinished stream, transport failure, or nonzero exit fails
it. Partial text and exit zero do not replace the marker.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution uses the resolved
agent cwd and closes stdin. Recursive agent spawning remains prohibited by
referee guardrails.

The separate top-level outer policy supports `sandbox="read-only"` in Stage 2.
It reuses the common Linux Bubblewrap launcher and declares the complete
effective `CLAUDE_CONFIG_DIR` (normally `~/.claude`) as persistent writable
provider state. The directory must already exist, be owned by the daemon user,
have no symlink component, and not be group/world writable. Claude's
`session-env` and other mutable state remain below that writable root. The
legacy `~/.claude.json` file is not a writable exception.

Only after the outer mount and process boundary is proved does the adapter:

- replace configured permission controls with
  `--dangerously-skip-permissions`;
- supply transient `{"sandbox":{"enabled":false}}` settings;
- force `--strict-mcp-config` with an empty explicit MCP configuration, so
  ambient user/project MCP definitions cannot expand the local process tree;
- point `CLAUDE_CODE_TMPDIR` at the same private turn scratch as `TMPDIR`; and
- disable provider self-update attempts inside the read-only installation.

Explicit settings/managed-settings arguments, malformed MCP arguments, and
system admin-managed settings or managed MCP are incompatible and fail before
the provider starts. Configured `--add-dir` paths are normalized as read-only
provider-visible paths. Top-level `sandbox="none"` preserves the original
provider command and its independently configured permission mode exactly.

The boundary protects the workspace and its session-root Git storage, not
Claude state: credentials, settings, history, plugins, skills, and other
content below `CLAUDE_CONFIG_DIR` remain writable by Claude and descendants.
Readable host files, network access, remote tools/services, and workspace
symlinks into writable Claude state remain outside the MVP integrity claim.

## Testing

Hermetic: `./agent_collab_dev.sh test -k claude_cli`. Credential-free real
namespace boundary: `./agent_collab_dev.sh bubblewrap-test`. Live:
`./agent_collab_dev.sh integration-test claude_cli`. The paid outer-sandbox
acceptance is skipped unless `AGENT_COLLAB_IT_CLAUDE_SANDBOX_STATE` names an
operator-authorized dedicated complete Claude state root.
