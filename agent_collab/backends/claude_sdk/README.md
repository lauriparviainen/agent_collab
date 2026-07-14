# Claude SDK backend

Registered as `claude_sdk` (`type="claude"`, `backend="sdk"`). It calls `claude-agent-sdk` in-process and maps typed messages and content blocks.

## Selection and requirements

Select with `backend="sdk"`. Python and the `claude-agent-sdk` wheel are required. Authentication may use `ANTHROPIC_API_KEY` or the SDK's Claude Code sign-in; agent-collab never stores credentials.

## Options

[`options.toml`](options.toml) is authoritative for accepted keys and values; shipped defaults live in the built-in config, including the read-only `permission_mode = "default"` posture. `model`, `permission_mode`, `thinking_level`, and `thinking_budget_tokens` map to `ClaudeAgentOptions`; level and raw-budget requests conflict. SDK options are never inferred from CLI argv. Runs use `setting_sources=[]` and the `claude_code` system/tool presets.

## Events and identity

Typed text/tool/result blocks map to normalized events. Thinking signatures are never emitted. `ResultMessage.session_id` is captured as provider identity kind `session`, but resume is not implemented.

## Turn outcome

A terminal non-error `ResultMessage` is required for completion. An error
result, SDK/transport exception, missing result, or uncertain close fails. SDK
stream close is bounded; an over-grace close transfers to a background reaper
without delaying timeout/interruption recording.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. The disposable/session workdir is passed as SDK cwd. Missing wheels fail availability probing; runtime/auth errors become transcript error events.

## Testing

Hermetic: `./agent_collab_dev.sh test -k claude_sdk`. Live: `./agent_collab_dev.sh integration-test claude_sdk`.
