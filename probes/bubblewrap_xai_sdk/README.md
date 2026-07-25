# Bubblewrap + xAI SDK execution-surface probe

This Linux probe verifies the request constructed by backend `xai_sdk` and
optionally runs a real model turn inside Bubblewrap. Its support decision is
**no local tool execution**: the current backend is model-only and does not
expose xAI built-in tools, custom function tools, MCP, hooks, plugins, or
subagents.

That conclusion is narrower than “the xAI SDK has no tools.” The public API
supports provider-hosted built-in tools and client-executed custom functions,
but the production backend passes neither. Unsupported capabilities are not
simulated by this probe.

## Current execution map

| Capability | Execution owner in the current backend |
| --- | --- |
| SDK client and request construction | In-process in the agent-collab daemon. |
| Model request | In-process asynchronous gRPC client to the remote xAI service. |
| File, shell, and child-process tools | Not exposed. |
| Custom function callbacks/tool runner | Not exposed; the backend rejects unexpected tool calls instead of executing them. |
| Provider built-in web/code tools | Not enabled. If added later, their provider-hosted environment would be outside the local filesystem claim. |
| MCP, hooks, plugins, and subagents | Not exposed. |
| Session/config/auth state | `XAI_API_KEY` is read from the configured environment. Conversation completions are stored remotely for continuation and deleted best-effort at close. No provider state root is written locally. |

Bubblewrap neither contains nor needs to contain a remote provider filesystem.
In the current backend it would constrain only the local Python SDK worker,
which performs network I/O and no exposed local tool effects.

## Requirements and commands

- Linux with `bwrap` on `PATH` and usable user namespaces.
- A Python environment containing `xai-sdk>=1.17,<2`.
- For the optional live comparison, `XAI_API_KEY` and network access.

The structural check constructs the exact production-mapped request against a
non-network fixture endpoint, asserts that it contains zero tools, and uses a
Python audit hook to confirm request construction starts no subprocess:

```bash
python3 probes/bubblewrap_xai_sdk/probe_bubblewrap_xai_sdk.py \
  --preflight-only \
  --sdk-python /path/to/xai-env/bin/python
```

Run the real model-only comparison with:

```bash
python3 probes/bubblewrap_xai_sdk/probe_bubblewrap_xai_sdk.py \
  --sdk-python /path/to/xai-env/bin/python
```

Use `--model`, `--sdk-python`, `--bwrap`, or `--timeout` for explicit
overrides. Credential values and response bodies are never printed. The probe
returns nonzero for missing credentials, provider failure, timeout, or a
failed host assertion.

## Boundary exercised

The complete Python worker runs in Bubblewrap with `/`, the private general
home, and a temporary workspace path containing spaces read-only. Only private
scratch is writable; there is no writable provider-state exception. Host-side
checks confirm the worker entered another mount namespace, kept the exact
workspace cwd, read tracked input in structural mode, and left workspace,
protected-host, and home markers absent.

## Observed result

On 2026-07-25, the structural probe passed with Bubblewrap 0.6.3 and
`xai-sdk` 1.17.0:

- the worker ran in a mount namespace different from the host;
- its cwd was the exact read-only temporary workspace;
- the production option mapper supplied only `model` and
  `reasoning_effort`;
- the constructed request contained zero tools;
- request construction started no subprocess; and
- all host markers remained absent.

The credentialed comparison could not run because `XAI_API_KEY` was
unavailable. The probe reports that sanitized blocker without inspecting
configuration or credential contents. Source tracing, production request
construction, and hermetic backend tests establish the current model-only
surface; a real turn would add provider-transport evidence but would not prove
an unconfigured local tool path.

## Cancellation and limitations

The SDK uses gRPC background runtime threads, not a local provider child
process. On cancellation, the backend shields an in-flight sample until it
settles before reset/close, then closes the client channel; the referee can
bound the runner and reap it later. With no local tool or child process there
is no descendant tool tree to contain or kill.

If xAI tools are enabled in a future backend revision, this conclusion expires.
Client-executed function calls would need a contained executor, while remote
built-ins must be labeled provider-hosted rather than local. The probe also
does not claim network isolation, credential confidentiality, resource limits,
or cross-platform support.

Primary provider references:

- <https://github.com/xai-org/xai-sdk-python>
- <https://docs.x.ai/developers/tools/function-calling>
