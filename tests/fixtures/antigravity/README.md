# Antigravity spike fixtures (Stage 4.9, step 7)

Captured 2026-07-08 to drive `parse_antigravity_line` (cli) and the SDK event
mapper. Parsers/mappers are written against these samples, not guessed.

## CLI (`agy`) — CONFIRMED live

- Binary: `agy`, version **1.1.0** (`agy-version.txt`).
- Command: `agy --mode accept-edits -p "<prompt>"` in a throwaway git repo,
  signed in via the cached `~/.gemini` OAuth token.
- `agy-print-sample.stdout.txt` — real stdout with one machine-local
  documentation link replaced by neutral prose. `agy-print-sample.stderr.txt`
  — real stderr (empty).

**Finding (matches the plan's "Verified provider facts"):** print mode emits
**free-form plain text / Markdown prose** — multiple lines, blank lines, `###`
headers, `*` bullet lists, and fenced code blocks. There is **no** JSON, no
NDJSON, and **no stable per-line event marker**. So `parse_antigravity_line`
emits one `antigravity` `message` event per non-empty stdout line (message-only,
low fidelity). The referee still emits the `command` start and `status` exit
events it emits for every subprocess runner.

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
