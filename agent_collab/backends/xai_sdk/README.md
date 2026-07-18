# xAI SDK backend

Registered as `xai_sdk` (`type="xai"`, `backend="sdk"`). This is the remote xAI
chat API, not the local Grok Build coding runtime. It requires `xai-sdk>=1.17,<2`
and `XAI_API_KEY`; imports are lazy and the async client is closed
deterministically after each collected turn.

[`options.toml`](options.toml) declares accepted MCP/session options;
[`defaults.toml`](defaults.toml) owns the shipped option values and disabled
Event Window target.

Select it with `backend="sdk"`; the shipped normal-session model is
`grok-4.5`, currently the SDK transport's verified model selection. The schema
still requires a model after defaults are resolved, so a custom configuration
that removes the shipped default must supply one; other provider-supported
model IDs remain accepted. Normal sessions default to
`thinking_level=high`; `grok-4.5` also supports `low` and `medium`.
`thinking_level` is the preferred spelling and `reasoning_effort` is an alias;
one effective `none`, `low`, `medium`, or `high` value maps to
`chat.create(reasoning_effort=...)`. CLI-only
`permission_mode` and `sandbox` are rejected by the declarative schema.

The runner appends `user(prompt)`, awaits `chat.sample()`, maps only non-empty
`response.content` to an xAI message, and captures `response.id` as provider
identity kind `response`. It enables no remote or client-side tools and emits no
tool, command, or file-change events. Event fidelity is message-only; response
IDs are correlation metadata, not resumable conversation state. Resume,
interrupt, and tool-gate capabilities are all false. Credential values and SDK
responses are never logged by health probes.

For this no-tools backend, `finish_reason=STOP` with non-empty content is the
only verified completion. Empty content, length/token limits, unexpected tool
calls, other finish reasons, SDK exceptions, and uncertain bounded close fail
conservatively. No structured refusal mapping is claimed.

Hermetic tests: `python3 -m unittest tests.backends.xai_sdk.test_backend`.
Credentialed test: `./agent_collab_dev.sh integration-test xai_sdk --strict`.
