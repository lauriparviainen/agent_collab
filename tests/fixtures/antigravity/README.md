# Antigravity fixtures

CLI stream-JSON shapes were reconstructed on 2026-08-16 from a cheap
`agy 1.1.13` print-mode turn (`--output-format stream-json`, `--mode plan`)
and from official headless-mode documentation. Values are sanitized: no
secrets, tokens, host paths, usernames, or live transcript text.

SDK samples remain the 0.1.8 wheel facts used by
`agent_collab/backends/antigravity_sdk/backend.py`.

## CLI (`agy`) — stream-json (1.1.8+)

Installed help and changelog confirm `--output-format json` / `stream-json`,
typed `init` / `step_update` / `result`, `--conversation`, and `--continue`.
`--output-format` must appear before `-p`, or the prompt must follow `-p`
immediately; otherwise `-p` consumes the next token as the prompt.

Observed print-mode NDJSON (root turn):

- `{"event":"init","conversation_id":"<uuid>","init":{...}}`
- `{"event":"step_update","step_update":{...}}`
- `{"event":"result","result":{...}}`

A stable **root** conversation id is present on `init.conversation_id`
(top-level), `step_update.conversation_id`, and `result.conversation_id`,
and those three slots matched on the captured root turn. Identity capture
is out of scope for this increment; fixtures keep redacted ids only so
later resume work can distinguish root from child.

`subagent_info.conversation_id` is child identity. It was not emitted on
the cheap root turn; `stream-json-subagent.ndjson` reconstructs the
documented child payload so parsers cannot treat it as the root.

`init` also carried additive `expanded_commands` (ignored). One
`step_update` used `step_type=unknown` (still a non-terminal `step_update`).
A failed invalid-model turn emitted a single terminal `result` with
`status=ERROR` and an empty `conversation_id` (no `init`).

| File | Role |
| --- | --- |
| `stream-json-success.ndjson` | `init`, `step_update`, terminal `result` `SUCCESS` |
| `stream-json-failed.ndjson` | failed terminal `result` (`ERROR`) |
| `stream-json-malformed.ndjson` | invalid NDJSON |
| `stream-json-unknown-nonterminal.ndjson` | additive unknown event, then success |
| `stream-json-unknown-terminal.ndjson` | unknown terminal `status` |
| `stream-json-subagent.ndjson` | root id vs `subagent_info.conversation_id` |
| `stream-json-missing-result.ndjson` | no terminal `result` |
| `stream-json-invalid-result.ndjson` | `result` missing required `status` |
| `agy-print-sample.stdout.txt` | **negative**: plain text is not a successful turn |
| `agy-version.txt` | captured CLI version (`1.1.13`) |

## SDK (`google-antigravity`) — 0.1.8 installed-wheel facts

Stage 6 re-introspected PyPI's latest release, `google-antigravity` 0.1.8, on
Python 3.14.4 and glibc 2.43. `sdk-introspection.json` is the refreshed dump.
The bundled `localharness` ELF's newest versioned libc symbol is `GLIBC_2.26`.

Confirmed shapes (used by `agent_collab/backends/antigravity_sdk/backend.py`):

- `from google.antigravity import Agent, LocalAgentConfig` — `Agent` is an async
  context manager; `response = await agent.chat(prompt)` returns a
  `types.ChatResponse`.
- `await response.resolve()` drains the response once into a list containing
  typed `Text`, `Thought`, `ToolCall`, and `ToolResult` values. `text()` is also
  async, while `thoughts` and `tool_calls` are properties that each return an
  independent **async cursor** over the shared response buffer.
- `Text(step_index, text)` and `Thought(step_index, text, signature)` carry
  streamed deltas. Thought signatures are opaque and must never be emitted.
- `ToolCall` has `.name` (a `BuiltinTools` enum — e.g. `CREATE_FILE`,
  `EDIT_FILE`, `RUN_COMMAND`, `VIEW_FILE` — or a `str`), `.args` (dict, **not
  `input`**), `.id`, and `.canonical_path`. `ToolResult` has the correlating
  `.id` plus `.name`, `.result`, `.error`, and `.exception`.
- `response.usage_metadata` exposes optional prompt/cache/candidate/thought/total
  token counts after the response is resolved.
- `LocalAgentConfig(workspaces=[<workdir>], model=...)` — the working directory
  is a workspace, **not** a `working_directory` kwarg.
- `Agent.conversation_id` returns `None` before start and is documented as
  available after message exchange.
- Strict reopen is public:
  `LocalAgentConfig(conversation_id=<id>,
  session_continuation_mode=SessionContinuationMode.RESUME)`. The distinct
  `CREATE_OR_RESUME` mode may create fresh and is not used by agent-collab.
  `save_dir` becomes localharness trajectory storage and must remain stable
  across reopened Agent objects.
- `ChatResponse.cancel()` delegates to the active conversation cancel path;
  cancelling only a local `resolve()` consumer does not call it automatically.
- There is no `--mode` equivalent; execution posture is `CapabilitiesConfig` /
  `policies`, so `backend_options.antigravity_sdk.mode` remains unsupported.

`sdk-response-sample.json` holds a resolved typed-buffer sample in the confirmed
shape (illustrative values) that drives the fake-module tests in
`tests/test_backend_sdk.py`.

The 0.1.8 generated protobuf files require runtime 7.35+, while the wheel
metadata allows older protobuf. `xai-sdk` 1.17 requires protobuf `<7`, so the two
SDKs cannot currently share one dependency environment. Stage 6 uses an
isolated Antigravity environment with protobuf 7.35.1; its provider-specific
extra pins that runtime, while `all` omits the conflicting floor and the backend
health probe reports the incompatible shared environment unavailable.

The source/config/runtime fixture is no-model. The separate credentialed
integration test is the only evidence allowed to prove provider-held
multi-turn memory and flip `continuity`.
