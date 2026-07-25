# Bubblewrap + Codex SDK execution-ownership probe

This Linux probe answers two questions for backend `codex_sdk`:

1. can the complete Python SDK worker, its local Codex app-server, a local
   shell action, and that action's child run inside Bubblewrap; and
2. does the current agent-collab SDK runner already establish that boundary?

The tested answers are **yes** and **no**. The explicit support decision is
therefore **feasible with out-of-process worker**. This is research evidence,
not production sandboxing.

## Current execution map

The backend constructs a persistent `openai_codex.AsyncCodex` client in the
agent-collab daemon. The SDK starts `codex app-server --listen stdio://` as a
local subprocess. Its stdout reader and stderr drainer run in daemon threads.

| Capability | Execution owner in the current backend |
| --- | --- |
| SDK client, event mapping, and app-server reader threads | Daemon Python process. |
| Model request | Local app-server to the remote OpenAI service. |
| Built-in file and shell tools | Local app-server process. |
| Shell descendants | Children of app-server; they inherit its cwd and mount namespace. |
| MCP, hooks, skills, plugins, and subagents | Runtime-owned when enabled through ambient Codex configuration. The backend supplies no per-thread configuration that disables or adds them. |
| Client callbacks/custom tool executor | The high-level Python SDK exposes approval handling but no backend-configured local custom-tool executor. |
| Provider web tools | Remote/provider-hosted effects are outside the local filesystem claim. |
| Session/auth/config state | `CODEX_HOME` plus its configured SQLite location. The runtime reads ambient config, auth, sessions, skills, and package metadata there. |

Wrapping app-server alone would omit the SDK caller and its threads, and would
not give a clean place to supervise cancellation. The viable boundary moves
the complete backend runner into a supervised worker and places that worker
inside Bubblewrap.

## Requirements and commands

- Linux with `bwrap` on `PATH` and usable user namespaces.
- Python 3.10+ for the structural controls.
- A provider environment containing production-compatible `openai-codex` and
  `openai-codex-cli-bin`, or an explicit compatible Codex runtime.
- For live comparisons, working Codex credentials in `CODEX_HOME`/`~/.codex`
  or the supported environment and network access.

Run the credential-free structural state controls:

```bash
python3 probes/bubblewrap_codex_sdk/probe_bubblewrap_codex_sdk.py \
  --sdk-python /path/to/codex-sdk-env/bin/python \
  --preflight-only --state-mode writable
python3 probes/bubblewrap_codex_sdk/probe_bubblewrap_codex_sdk.py \
  --sdk-python /path/to/codex-sdk-env/bin/python \
  --preflight-only --state-mode read-only
```

Run the complete worker inside Bubblewrap:

```bash
python3 probes/bubblewrap_codex_sdk/probe_bubblewrap_codex_sdk.py \
  --sdk-python /path/to/codex-sdk-env/bin/python \
  --codex-runtime /path/to/codex \
  --execution-mode wrapped-worker --state-mode writable
```

Run the deliberate current-architecture negative control:

```bash
python3 probes/bubblewrap_codex_sdk/probe_bubblewrap_codex_sdk.py \
  --sdk-python /path/to/codex-sdk-env/bin/python \
  --codex-runtime /path/to/codex \
  --execution-mode current-architecture
```

Use `--model`, `--codex-runtime`, `--source-codex-home`, `--bwrap`, or
`--timeout` for explicit overrides. Credentialed calls may consume provider
usage. The probe checks only credential availability and never reads or prints
credential contents.

The negative control invokes the exact production backend factory without
Bubblewrap. A passing negative control means its controlled markers reached
the host and app-server shared the caller's host namespace; it does **not**
mean the architecture is contained.

## Boundary exercised

Wrapped mode puts every permissive worker/runtime command after Bubblewrap
namespace setup. It uses a private workspace whose real path contains spaces
and keeps that path as the host cwd, Bubblewrap cwd, SDK workspace, app-server
cwd, tool cwd, and child cwd. It mounts:

- `/` read-only;
- private scratch writable;
- only the complete selected `CODEX_HOME` writable in writable-state mode;
- general `HOME` read-only; and
- the workspace read-only last.

One controlled Python action and one child independently verify cwd, tracked
input readability, blocked workspace/protected-host/general-home writes,
provider-state behavior, writable scratch, and mount-namespace inheritance.
Host-side files prove that the action ran exactly once.

## Observed result

On 2026-07-25, Bubblewrap 0.6.3, `openai-codex` 0.144.4, its pinned runtime
0.144.4, and the selected Codex CLI 0.145.0 produced:

- both structural state modes passed all direct, child, production-mapping,
  namespace, and host assertions;
- the credentialed writable-state worker executed one real shell action and
  contained the SDK caller, app-server, action, and child;
- the current production factory executed the same action without an outer
  boundary: all controlled writes reached the host and app-server shared the
  host namespace;
- read-only provider state failed before shell dispatch with a read-only
  filesystem error; all guarded host markers remained absent; and
- a forced one-second timeout killed the wrapped process group and the
  recorded app-server descendant was gone.

The tested live worker required no writable filesystem exception outside the
complete private provider root and scratch. That state root includes runtime
configuration and session databases and may be broader than a future
minimized contract.

## Native controls, cancellation, and limitations

The feasibility worker maps Codex `danger-full-access` only after the outer
boundary exists. The SDK's default approval mode remains `auto_review`.
Provider-native sandbox presets and approvals are separate from, and do not
prove, the outer containment boundary.

Production turn cancellation shields the in-flight provider run until it
settles; it does not issue an app-server turn interrupt. The referee may adopt
that still-running task for background reaping, while reset/close can remain
blocked on the conversation lock. SDK close terminates and then kills
app-server, but does not wait again after the kill. A production worker
supervisor must own forceful process-tree termination; the probe's
process-group timeout demonstrates that boundary.

Bubblewrap preserves read access to the host and network access. It does not
protect readable credentials from disclosure, contain provider-hosted tools,
or impose CPU, memory, or syscall limits. Ambient MCP/hooks/skills/plugins,
external helpers, custom approval behavior, alternative auth/keyrings, token
refresh, future runtime changes, and non-Linux platforms need fresh evidence.

Primary provider references:

- <https://developers.openai.com/codex/sdk/>
- <https://developers.openai.com/codex/app-server/>
- <https://developers.openai.com/codex/security/>
- <https://github.com/openai/codex>
