# Bubblewrap + Antigravity SDK execution-ownership probe

This Linux probe answers two different questions for backend
`antigravity_sdk`:

1. can a complete standalone SDK worker and its bundled `localharness` child
   run inside Bubblewrap; and
2. does the current agent-collab SDK runner already establish that boundary?

The answers for the tested version are **yes** and **no**, respectively. The
support decision is therefore **feasible with out-of-process worker**. This is
research evidence, not a production sandbox implementation.

## Current execution map

The current backend constructs `google.antigravity.Agent` in the
agent-collab daemon. The SDK starts its bundled `localharness` executable as a
child with inherited cwd, environment, and mount namespace. The harness
performs the built-in file and shell operations and starts configured local
MCP stdio processes. The current backend does not configure custom Python
tools, hooks, triggers, MCP servers, skills, plugins, or custom subagents.

| Capability | Execution owner in the current backend |
| --- | --- |
| SDK caller, policies, and callbacks | Daemon Python process. The current backend supplies no custom callbacks. |
| Model request | Daemon SDK client through the local harness to the remote provider. |
| Built-in file tools | Local `localharness` child. They can reach the supplied workspace. |
| Built-in shell tool | Local harness, but the SDK's default command policy denies it in the current backend. |
| Child processes from shell | Would be descendants of the local harness; unavailable under the current default deny policy. |
| Built-in subagent | Enabled in the harness by the SDK's default capabilities; any local built-in tool it uses has the same harness ownership. |
| Web, URL, and image tools | Requested through the harness and provider service; their remote effects are outside the local filesystem claim. |
| Custom Python tools/hooks/triggers | Public SDK extension points would execute in the SDK caller, but the backend does not expose them. |
| MCP stdio/HTTP and skills | Public SDK configuration exists, but the backend supplies none. A configured stdio server would be launched by the harness; no such unsupported path was fabricated for this probe. |
| Session/auth/config state | Agent-collab supplies a private trajectory `save_dir`; the SDK also has app-data defaults. Vertex authentication reads Application Default Credentials. |

Wrapping only `localharness` would therefore be incomplete: the SDK caller and
any future in-process callback remain in the daemon. The viable design is to
move the complete backend runner into a supervised worker and place that
worker inside the common outer sandbox.

## Requirements

- Linux with `bwrap` on `PATH` and usable user namespaces.
- Python 3.10+ for the structural controls.
- For live comparisons, a provider-specific environment containing
  `google-antigravity>=0.1.8,<0.2.0` and compatible protobuf 7.35+, Vertex
  Application Default Credentials, a configured project, and network access.

`google-antigravity` 0.1.8 and `xai-sdk` 1.17.0 currently have incompatible
protobuf requirements. Use separate provider environments and verify them
with `pip check`; do not change system libraries.

Credentialed calls may consume provider usage. The probe never prints or
reads credential contents. It mounts the selected ADC file read-only.

## Commands

Run both free structural state controls:

```bash
python3 probes/bubblewrap_antigravity_sdk/probe_bubblewrap_antigravity_sdk.py \
  --preflight-only --state-mode writable
python3 probes/bubblewrap_antigravity_sdk/probe_bubblewrap_antigravity_sdk.py \
  --preflight-only --state-mode read-only
```

Run the complete standalone worker inside Bubblewrap:

```bash
python3 probes/bubblewrap_antigravity_sdk/probe_bubblewrap_antigravity_sdk.py \
  --sdk-python /path/to/antigravity-env/bin/python \
  --execution-mode wrapped-worker --state-mode writable
```

Run the deliberate current-architecture negative control:

```bash
python3 probes/bubblewrap_antigravity_sdk/probe_bubblewrap_antigravity_sdk.py \
  --sdk-python /path/to/antigravity-env/bin/python \
  --execution-mode current-architecture
```

The negative control uses the exact production backend factory without
Bubblewrap and asks its file tool to write only inside the probe's private
temporary workspace. A passing negative control means the host marker appeared
and the harness shared the SDK caller's host mount namespace; it does **not**
mean the architecture is safely contained.

Use `--model`, `--vertex-location`, `--sdk-python`, `--bwrap`, or `--timeout`
for explicit overrides. The project is resolved from the integration-test or
Google Cloud environment and then from the local gcloud configuration. The
probe returns nonzero for provider failure, timeout, missing/repeated action
execution, or any failed host assertion.

## Boundary exercised

The wrapped-worker mode creates a private workspace whose real path contains
spaces and preserves that path as the host cwd, Bubblewrap cwd, SDK workspace,
harness cwd, and tool cwd. It mounts:

- `/` read-only;
- private scratch writable;
- one private Antigravity trajectory/app-data root writable when
  `--state-mode writable`;
- general `HOME` read-only; and
- the workspace read-only last.

The controlled shell action and one child process independently verify exact
cwd, tracked input readability, blocked workspace/protected-host/general-home
writes, the declared provider-state result, writable scratch, and mount
namespace inheritance. Host-side files prove the action ran exactly once.

## Observed result

On 2026-07-25, Bubblewrap 0.6.3 and `google-antigravity` 0.1.8 produced:

- both structural state modes passed every direct and child assertion;
- the credentialed writable-state standalone worker passed, including a real
  `run_command` dispatch exactly once;
- the SDK caller, `localharness`, action, and action child all shared the new
  Bubblewrap mount namespace and differed from the host namespace;
- the current production backend completed a real `create_file` call, the
  write reached the temporary host workspace, and `localharness` shared the
  unsandboxed caller namespace;
- the live read-only-state comparison failed during conversation
  initialization, before the model or tool action, because the harness could
  not create its trajectory database; and
- a forced one-second timeout killed the wrapped process group and the
  recorded `localharness` descendant was gone.

The writable standalone run needed no writable filesystem exception outside
the private provider root and scratch. This does not prove that every future
SDK feature or authentication flow has the same state contract.

## Cancellation and limitations

The production backend closes an active response before leaving the agent
context. SDK disconnect then closes communication tasks and terminates or
kills `localharness`; the referee bounds close and can continue reaping a slow
runner. The probe's timeout exercises process-group termination and confirms
the recorded harness process exits. It does not manufacture an allowed
production shell descendant, because the backend's command policy denies that
capability.

Bubblewrap leaves the read-only host visible and preserves network access.
It does not protect readable credentials from disclosure, contain remote
provider filesystems, or provide CPU, memory, or syscall limits. Custom
callbacks, configured MCP servers, alternative authentication, token refresh,
future SDK capabilities, and cross-platform behavior need new evidence before
being advertised as supported.

Primary provider references:

- <https://github.com/google-antigravity/antigravity-sdk-python>
- <https://antigravity.google/docs/mcp>
