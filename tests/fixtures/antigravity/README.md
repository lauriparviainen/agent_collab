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

## SDK (`google-antigravity`) — API CONFIRMED live; only the call is blocked

The earlier draft recorded the SDK as fully blocked because system Python is 3.9
(< the SDK's required 3.10). That is now resolved: **Python 3.12 was installed
(`dnf install python3.12`) and `google-antigravity` 0.1.5 installs from PyPI**,
so the real API was introspected live. `sdk-introspection.json` is that
authoritative dump.

Confirmed shapes (used by `agent_collab/backends/antigravity_sdk.py`):

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
- **`Agent.conversation_id` exists** — a stable, resume-capable id (resolves the
  plan's open question 3: yes).
- There is no `--mode` equivalent; execution posture is `CapabilitiesConfig` /
  `policies`, so `backend_options.antigravity_sdk.mode` remains unsupported.

`sdk-response-sample.json` holds a resolved typed-buffer sample in the confirmed
shape (illustrative values) that drives the fake-module tests in
`tests/test_backend_sdk.py`.

**Only the live *chat* is blocked:** the SDK requires a Gemini API key
(`GEMINI_API_KEY` env or `LocalAgentConfig(api_key=...)`); it does **not** use the
`~/.gemini` OAuth that `agy` uses. agent-collab never manages credentials, so it
passes the environment through and the first turn's real error is the authority.
Verified end to end against the installed SDK: the probe reports `ok`/0.1.5, the
runner constructs the real `LocalAgentConfig`/`Agent` (no arg errors), and the
missing-key error surfaces as an `error` event. To exercise a real turn, set
`GEMINI_API_KEY` and re-run; replace `sdk-response-sample.json` values with a real
capture if the turn produces different structure.
