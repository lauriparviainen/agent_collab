# Antigravity SDK backend

Registered as `antigravity_sdk` (`type="antigravity"`, `backend="sdk"`). It uses `google-antigravity` in-process, resolves the typed response once, and maps text, thoughts, tool calls, and tool results.

## Selection and requirements

Select with `backend="sdk"`. The `google-antigravity` wheel and Gemini/Vertex credentials are required. The probe recognizes `GEMINI_API_KEY`; absence is `unknown` because ADC may work. Credentials are never stored by agent-collab. Vertex uses Google Application Default Credentials, including gcloud's standard `~/.config/gcloud/application_default_credentials.json` file.

## Options

[`options.toml`](options.toml) declares the MCP/session option `model`.
[`config.toml`](config.toml) separately declares static `vertex`, `project`, and
`location` configuration; project and location are required when Vertex is
enabled. CLI `mode` is unsupported. Nothing is inferred from CLI argv.

```toml
[agents.antigravity_sdk]
type = "antigravity"
backend = "sdk"
env = { GOOGLE_APPLICATION_CREDENTIALS = "/home/me/.config/gcloud/application_default_credentials.json" }
vertex = true
project = "my-gcp-project"
location = "us-central1"

[agents.antigravity_sdk.options]
model = "Gemini 3.1 Pro (High)"
```

## Events and identity

Typed text becomes messages; tool calls/results become tool, command, file-change, status, or error events. Thought signatures are never emitted. `Agent.conversation_id` is captured as identity kind `conversation`, but resume is not implemented.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. `LocalAgentConfig(workspaces=[...])` receives only the resolved workspace.

## Testing

Hermetic: `./agent_collab.sh test -k antigravity_sdk`. Live: `./agent_collab.sh integration-test antigravity sdk`.
