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
also includes `xai-sdk` 1.17, which requires protobuf `<7`, so `all`
intentionally omits the conflicting Antigravity runtime floor and the health
probe reports Antigravity unavailable there. Until the providers publish
compatible constraints, use separate provider environments when both SDKs are
needed and verify each with `pip check` plus an import. Do not work around this
by changing system libraries. The probe also reports unavailable when installed
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
model = "gemini-3.6-flash-high"
```

## Events and identity

Typed text becomes messages; tool calls/results become tool, command,
file-change, status, or error events. Thought signatures are never emitted.
The runner lazily opens one `Agent` and reuses it across sequential turns.
`Agent.conversation_id` is captured as identity kind `conversation`. After an
abnormal turn, reset closes the suspect live object but retains the ID; the
next connection uses `LocalAgentConfig(conversation_id=...,
session_continuation_mode=RESUME)` with the same runner-owned trajectory
`save_dir`. That directory survives resets and is removed on final close. A
rejected ID fails structurally and never falls back to `CREATE_OR_RESUME` or a
fresh conversation. If an abnormal first connection never exposes an ID, the
next continuation attempt fails structurally once; only a later, explicit
full-prompt user turn may open a new conversation.

## Turn outcome

A resolved response with a non-empty text result completes. Empty resolved
buffers, resolve/transport exceptions, or uncertain bounded response close
fail conservatively. Tool-result prose is never interpreted as cancellation or
refusal.

## Capabilities and security

`continuity` is true. The persistent lifecycle and strict reconnect path are
source-verified on 0.1.8, covered hermetically, and passed a credentialed
two-turn Vertex provider-memory proof with `gemini-2.5-flash`: the follow-up
delta prompt omitted the original task and generated codeword, the response
recalled the codeword, and both turns reported one stable conversation id.
`resume`, `interrupt`, and `tool_gate` remain false.
`LocalAgentConfig(workspaces=[...])` receives only the resolved workspace.

## Testing

Hermetic: `./agent_collab_dev.sh test -k antigravity_sdk`. Live: `./agent_collab_dev.sh integration-test antigravity_sdk`.
