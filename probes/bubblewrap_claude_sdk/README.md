# Bubblewrap + Claude SDK execution-ownership probe

This Linux probe answers two questions for backend `claude_sdk`:

1. can the complete Python SDK worker, its Claude Code runtime, a local tool
   action, and that action's child run inside Bubblewrap; and
2. does the current agent-collab SDK runner already establish that boundary?

The tested answers are **yes** and **no**. The explicit support decision is
therefore **feasible with out-of-process worker**. This is research evidence,
not production sandboxing.

## Current execution map

The production backend creates a persistent `ClaudeSDKClient` in the
agent-collab daemon. The SDK starts its bundled Claude Code executable as a
local child with the configured workspace cwd and inherited environment.

| Capability | Execution owner in the current backend |
| --- | --- |
| SDK client, stream mapping, policies, and optional callbacks | Daemon Python process. The current backend configures no callbacks or hooks. |
| Model request | Claude Code runtime to the remote Anthropic service. |
| Built-in file and shell tools | Local Claude Code runtime child. |
| Shell descendants | Children of Claude Code; they inherit its cwd and mount namespace. |
| Built-in subagent and skill tools | Selected by Claude Code. Their local built-in tool effects retain runtime ownership. No custom agents or plugins are configured. |
| SDK MCP servers and tool callbacks | In-process SDK MCP handlers and `can_use_tool` callbacks would run in daemon Python, but the current backend supplies none. |
| External stdio MCP | Would be launched by Claude Code. The backend passes no explicit MCP configuration; the command is not strict-MCP, so ambient runtime MCP configuration remains a production-design concern. |
| Server tools such as provider web tools | Provider-hosted; their remote filesystem is outside the local containment claim. |
| Session/auth/config state | The complete `CLAUDE_CONFIG_DIR`. The current backend disables filesystem setting sources, but the runtime still needs mutable session/environment state. |

Wrapping only the Claude Code executable would miss future in-process SDK
callbacks and handlers. The viable boundary moves the complete backend runner
into a supervised worker and places that worker inside Bubblewrap.

## Requirements and commands

- Linux with `bwrap` on `PATH` and usable user namespaces.
- Python 3.10+ for the structural controls.
- A provider environment containing the production-compatible
  `claude-agent-sdk`.
- For live comparisons, working Claude credentials in
  `CLAUDE_CONFIG_DIR`/`~/.claude` or the supported environment and network
  access.

Run the credential-free structural state controls:

```bash
python3 probes/bubblewrap_claude_sdk/probe_bubblewrap_claude_sdk.py \
  --sdk-python /path/to/claude-sdk-env/bin/python \
  --preflight-only --state-mode writable
python3 probes/bubblewrap_claude_sdk/probe_bubblewrap_claude_sdk.py \
  --sdk-python /path/to/claude-sdk-env/bin/python \
  --preflight-only --state-mode read-only
```

Run the complete worker inside Bubblewrap:

```bash
python3 probes/bubblewrap_claude_sdk/probe_bubblewrap_claude_sdk.py \
  --sdk-python /path/to/claude-sdk-env/bin/python \
  --execution-mode wrapped-worker --state-mode writable
```

Run the deliberate current-architecture negative control:

```bash
python3 probes/bubblewrap_claude_sdk/probe_bubblewrap_claude_sdk.py \
  --sdk-python /path/to/claude-sdk-env/bin/python \
  --execution-mode current-architecture
```

Use `--model`, `--claude-runtime`, `--source-claude-config`, `--bwrap`, or
`--timeout` for explicit overrides. Credentialed calls may consume provider
usage. The probe checks only credential availability and never reads or prints
credential contents.

The negative control invokes the exact production backend factory without
Bubblewrap. A passing negative control means its controlled markers reached
the host and the runtime shared the caller's host namespace; it does **not**
mean the architecture is contained.

## Boundary exercised

Wrapped mode puts every permissive worker/runtime command after Bubblewrap
namespace setup. It uses a private workspace whose real path contains spaces
and keeps that path as the host cwd, Bubblewrap cwd, SDK workspace, runtime
cwd, tool cwd, and child cwd. It mounts:

- `/` read-only;
- private scratch writable;
- only the complete selected `CLAUDE_CONFIG_DIR` writable in writable-state
  mode;
- general `HOME` read-only; and
- the workspace read-only last.

One controlled Python action and one child independently verify cwd, tracked
input readability, blocked workspace/protected-host/general-home writes,
provider-state behavior, writable scratch, and mount-namespace inheritance.
Host-side files prove that the action ran exactly once.

## Observed result

On 2026-07-25, Bubblewrap 0.6.3, `claude-agent-sdk` 0.2.126, and its bundled
Claude Code 2.1.218 produced:

- both structural state modes passed all direct, child, production-mapping,
  namespace, and host assertions;
- the credentialed writable-state worker executed one real Bash action and
  contained the SDK caller, Claude Code, action, and child;
- the current production factory executed the same action without an outer
  boundary: all controlled writes reached the host and Claude Code shared the
  host namespace;
- read-only provider state failed with read-only/session-environment errors
  after a Bash command event but before the action script ran; all guarded
  host markers remained absent; and
- a forced one-second timeout killed the wrapped process group and the
  recorded Claude Code descendant was gone.

The tested live worker required no writable filesystem exception outside the
complete private provider root and scratch. That state root may contain more
mutable data than a future minimized contract.

## Native controls, cancellation, and limitations

The feasibility worker maps `bypassPermissions` only after the outer boundary
exists and leaves Claude's native sandbox disabled, matching the intended
separation between approval policy and OS enforcement. Provider-native
permission checks are not proof of containment.

The production backend closes/reset streams through the SDK, whose transport
then attempts graceful runtime termination and escalation. SDK cancellation
has edge cases, and the referee's bounded close can leave reaping in the
background. A production worker supervisor must own forceful process-tree
termination; the probe's process-group timeout demonstrates that boundary.

Bubblewrap preserves read access to the host and network access. It does not
protect readable credentials from disclosure, contain provider-hosted tools,
or impose CPU, memory, or syscall limits. Ambient MCP configuration,
callbacks, hooks, custom SDK MCP handlers, plugins, alternative auth/keyrings,
token refresh, future runtime changes, and non-Linux platforms need fresh
evidence.

Primary provider references:

- <https://platform.claude.com/docs/en/agent-sdk/overview>
- <https://platform.claude.com/docs/en/managed-agents/migration>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview>
- <https://github.com/anthropics/claude-agent-sdk-python>
