# xAI SDK backend

Registered as `xai_sdk` (`type="xai"`, `backend="sdk"`). This is the remote xAI
chat API, not the local Grok Build coding runtime. It requires `xai-sdk>=1.17,<2`
and `XAI_API_KEY`; imports are lazy and the async client is closed
deterministically after each collected turn.

Select it with `backend="sdk"`; `backend_options.xai_sdk.model` is required until a
credentialed account/model check establishes a safe built-in default.
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

Hermetic tests: `python3 -m unittest tests.backends.xai_sdk.test_backend`.
Credentialed test: `./agent_collab.sh integration-test xai_sdk --strict`.
