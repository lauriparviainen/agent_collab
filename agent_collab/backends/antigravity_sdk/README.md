# Antigravity SDK backend

Registered as `antigravity_sdk` (`type="antigravity"`, `backend="sdk"`). It uses `google-antigravity` in-process, resolves the typed response once, and maps text, thoughts, tool calls, and tool results.

## Selection and requirements

Select with `backend="sdk"`. `google-antigravity>=0.1.8,<0.2.0` and
Gemini/Vertex credentials are required. The probe recognizes `GEMINI_API_KEY`;
absence is `unknown` because ADC may work. Credentials are never stored by
agent-collab. Vertex uses Google Application Default Credentials, including
gcloud's standard `~/.config/gcloud/application_default_credentials.json`
file. The SDK exposes model targets but no public model-list API, so catalog
suggestions remain static.

The 0.1.8 wheel's generated protobuf code requires protobuf 7.35+, but its
published dependency currently permits older runtimes. The provider-specific
`antigravity` extra adds `protobuf>=7.35,<8`. Agent-collab's `all` environment
also includes `xai-sdk` 1.17, which declares protobuf `<7`, so `all`
intentionally omits the Antigravity runtime floor and a plain `pip install
'.[all]'` resolves to protobuf 6, where the health probe reports Antigravity
unavailable. The durable install (`./agent_collab.sh install`) resolves this:
it upgrades protobuf to the 7.35+ floor in a second phase, which is safe for
xai-sdk because its shipped protobuf-6 gencode is inside protobuf's
one-major-back runtime guarantee — only xai-sdk's own import-time version
gate objects, and the xai_sdk backend defeats it with a deliberate import
shim (see [`../xai_sdk/compat.py`](../xai_sdk/compat.py) and the
[xai_sdk README](../xai_sdk/README.md), verified 2026-07-27). When upgrading
either SDK, re-check whether xai-sdk accepts protobuf 7 upstream so the shim
and second install phase can be retired. Do not work around any of this by
changing system libraries. The probe also reports unavailable when installed
SDK distribution-version metadata is missing, because it cannot verify the
runtime compatibility contract.

The 0.1.8 Linux wheel bundles `localharness`; its newest versioned libc symbol
is `GLIBC_2.26`. The backend probes older glibc Linux hosts unavailable and
never recommends replacing the host libc.

## Options

[`options.toml`](options.toml) declares the MCP/session option `model`.
[`config.toml`](config.toml) separately declares static `vertex`, `project`, and
`location` configuration; project and location are required when Vertex is
enabled. [`defaults.toml`](defaults.toml) owns the shipped backend settings and
disabled Event Window target. CLI `mode` is unsupported. Nothing is inferred
from CLI argv.

```toml
[backends.antigravity_sdk]
enabled = true
env = { GOOGLE_APPLICATION_CREDENTIALS = "/absolute/path/to/credentials.json" }
vertex = true
project = "my-gcp-project"
location = "us-central1"

[backends.antigravity_sdk.options]
model = "gemini-3.7-flash-high"
```

## Events and identity

Typed text becomes messages; tool calls/results become tool, command,
file-change, status, or error events. Thought signatures are never emitted.
The runner lazily opens one `Agent` and reuses it across sequential turns.
`Agent.conversation_id` is captured as identity kind `conversation`. After an
abnormal turn, reset closes the suspect live object but retains the ID; the
next connection uses `LocalAgentConfig(conversation_id=...,
session_continuation_mode=RESUME)` against the same host-persistent,
session-keyed trajectory `save_dir`
(`$AGENT_COLLAB_HOME/trajectories/<session_id>`). That directory survives
resets and session close; agent-collab does not sweep it. A rejected or
missing ID, or a missing/unusable `save_dir`, fails structurally and never
falls back to `CREATE_OR_RESUME` or a fresh conversation. If an abnormal
first connection never exposes an ID, the next continuation attempt fails
structurally once; only a later, explicit full-prompt user turn may open a
new conversation.

## Turn outcome

A resolved response with a non-empty text result completes. A distinguishable
`AntigravityCancelledError` from `ChatResponse.cancel()` maps to
`interrupted` / `local_turn_interrupted` and retains the conversation. A host
`asyncio.CancelledError` is not that mapping. Empty resolved buffers,
resolve/transport exceptions, or uncertain bounded response close fail
conservatively. Tool-result prose is never interpreted as cancellation or
refusal.

## Capabilities and security

`continuity` is true. The persistent lifecycle and strict reconnect path are
source-verified on 0.1.8, covered hermetically, and passed a credentialed
two-turn Vertex provider-memory proof with `gemini-2.5-flash`: the follow-up
delta prompt omitted the original task and generated codeword, the response
recalled the codeword, and both turns reported one stable conversation id.
The adapter publishes the live `ChatResponse` and issues
`ChatResponse.cancel()` out of band on both worker and in-process paths
without taking the run lock. Host `policy.ask_user("*")` is wired on the
worker path and on in-process sessions that have a session approval
callback. `tool_gate` is true: both production paths park `ask_user`
for an explicit approve/deny (issue #20). `resume` is true: both worker
and in-process paths passed a credentialed daemon-reload + public
`resume_session` + delta-prompt proof against the durable trajectory
root. `interrupt` is true: both worker and in-process paths passed a
credentialed public `interrupt_session` park at `awaiting_input`, an
accepted follow-up `post_message`, and the same conversation id after
cancel. `LocalAgentConfig(workspaces=[...])` receives only the resolved
workspace.

## Outer filesystem sandbox

Stage 7 advertises outer `sandbox = "read-only"` as an `sdk_worker` backend.
Agent-collab launches a supervised Bubblewrap namespace, proves establishment,
then runs `python -I -m agent_collab.sandbox.sdk_worker`. The worker owns the
complete Antigravity SDK client, callbacks, and bundled `localharness` plus
tool descendants. A host-persistent, session-keyed trajectory directory is mounted writable
for the session; session-private app-data and home directories are still
created and removed at session end. Configured Application Default
Credentials are mounted read-only. After outer proof, the worker installs `policy.ask_user("*")` so
tool calls park for host approval. It does **not** force `allow_all` — that
policy skips the host gate (Claude analog of `bypassPermissions`). Ungated
in-process (`sandbox = "none"`) keeps the SDK default
`confirm_run_command` (denies `run_command`, allows writes) unless a
session approval callback is bound. Protobuf and glibc floors fail closed
when incompatible. OS keyring use remains an external service outside the
filesystem guarantee.

`sandbox = "none"` keeps the historical in-process daemon runner
and does not start Bubblewrap. Explicit outer `none` is the rollback path.

## Testing

Hermetic: `./agent_collab_dev.sh test -k antigravity_sdk`. Live: `./agent_collab_dev.sh integration-test antigravity_sdk`.
