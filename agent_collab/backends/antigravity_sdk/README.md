# Antigravity SDK backend

Registered as `antigravity_sdk` (`type="antigravity"`, `backend="sdk"`). It uses `google-antigravity` in-process, resolves the typed response once, and maps text, thoughts, tool calls, and tool results.

## Selection and requirements

Select with `backend="sdk"`. The `google-antigravity` wheel and Gemini/Vertex credentials are required. The probe recognizes `GEMINI_API_KEY`; absence is `unknown` because ADC or explicit SDK configuration may work. Credentials are never stored by agent-collab.

## Options

[`options.toml`](options.toml) is authoritative. `model` maps to `LocalAgentConfig`; CLI `mode` is unsupported. Nothing is inferred from CLI argv.

## Events and identity

Typed text becomes messages; tool calls/results become tool, command, file-change, status, or error events. Thought signatures are never emitted. `Agent.conversation_id` is captured as identity kind `conversation`, but resume is not implemented.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. `LocalAgentConfig(workspaces=[...])` receives only the resolved workspace.

## Testing

Hermetic: `./agent_collab.sh test -k antigravity_sdk`. Live: `./agent_collab.sh integration-test antigravity sdk`.
