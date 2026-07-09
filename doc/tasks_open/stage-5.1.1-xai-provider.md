# Stage 5.1.1: xAI provider (Grok CLI + xAI SDK)

## Purpose

Add `xai` as a fourth real provider `type`, with two backends:

- a `cli` backend for the installed `grok` command (Grok Build), and
- an `sdk` backend for the xAI Python SDK (`xai-sdk`).

This is split out of [Stage 5.1](../tasks_closed/stage-5.1-first-class-sdk-backends.md) on
purpose. Stage 5.1 only adds a *second backend* (`sdk`) to providers that already
exist (`claude`, `codex`, `antigravity`). xAI is different in kind: it adds a new
provider request bucket and provider source, which still fan out across several
API/config/event files even though backend option schemas are now self-owned.
Two source-attribution edits fail **silently** if missed (see "Central lists and
files to touch"). Doing
it separately keeps 5.1 focused and low-risk, and lets the xAI CLI parser wait on
a captured tool-using fixture without blocking the three SDK backends.

## Depends on

- **Stage 5.1 landed first.** 5.1 makes `sdk` a first-class, installed backend
  and adds the Claude/Codex SDK runners; this stage reuses that machinery
  (`AgentBackend`, `probe_sdk_backend`, `settings_summary`, fake-module test
  pattern). Do not start this stage until 5.1's SDK backends are merged.

## Current state (after Stage 5.1)

Implemented by 5.1:

- `cli` + `sdk` backends for `claude`, `codex`, and `antigravity`.
- SDK dependencies install with the project; `requires-python >= 3.10`.
- backend registry keyed by `(agent_type, backend_id)`, health probes, honest
  all-false capabilities, session-id capture plumbing.

Missing (this stage):

- `xai` provider `type`,
- `xai` CLI backend for the installed `grok` command + a `streaming-json` parser,
- `xai` SDK backend (message-only),
- `xai_options` threaded through the full start/validation/settings chain,
- `solo-xai` workflow (in project config),
- streaming-json fixtures (reasoning turn + a tool-using turn).

## Source facts to verify during implementation

These CLIs/SDKs are young; re-confirm before coding.

- **xAI CLI** — the installed `grok` command from Grok Build. Verified locally as
  `grok 0.2.93`. Confirmed against `grok --help` on this machine:
  - headless single-turn: `-p, --single <PROMPT>` (prints response, exits),
  - `--output-format` `[default: plain] [possible values: plain, json,
    streaming-json]`; `streaming-json` is newline-delimited JSON events,
  - `--permission-mode` `[possible values: default, acceptEdits, auto, dontAsk,
    bypassPermissions, plan]`,
  - `--model` / `-m`, `--sandbox <PROFILE>` (env `GROK_SANDBOX`; profile *values
    not enumerated by help*), `--reasoning-effort` (alias `--effort`), `--cwd`,
    `--always-approve`, `--system-prompt-override`, `--json-schema`,
    `--max-turns`, `--no-subagents`, `--prompt-file`,
  - ACP: `grok agent stdio` runs the agent over stdio (JSON-RPC; `session/new`,
    `session/prompt`, `session/update` with `agent_message_chunk`, session ids),
  - sessions under `~/.grok/sessions`; auth via `XAI_API_KEY` or `grok login`.
  - References:
    - https://docs.x.ai/build/overview
    - https://docs.x.ai/build/cli/headless-scripting
- **Observed `streaming-json` events** (real NDJSON runs — treat as authoritative
  for the parser until re-captured):
  - `{"type": "thought", "data": "..."}` — incremental reasoning,
  - `{"type": "text", "data": "..."}` — assistant prose,
  - `{"type": "end", "stopReason": "...", "sessionId": "...", "requestId": "..."}`.
  - Prose lives under `data`, **not** `text`/`content`. Flags parse both before
    and after the `-p` value in practice; tool-using event shapes are **not yet
    observed** (only thought/text/end captured).
- **xAI SDK** — `xai-sdk`, imported as `xai_sdk` (not the unrelated `xai`
  explainability package). gRPC-based, sync + async clients, Python 3.10+, reads
  `XAI_API_KEY`. Confirmed usage:
  `from xai_sdk import Client`; `from xai_sdk.chat import user, system`;
  `chat = client.chat.create(model="grok-4.5")`; `chat.append(user(...))`;
  `chat.sample().content` (plus streaming). Supports server-side tools
  (`web_search`, `x_search`, `code_execution` — executed remotely) and
  client-side function calling (caller runs the tool loop). It is an API client,
  not a local coding-agent runtime: no built-in local filesystem/shell/file-edit.
  - References:
    - https://docs.x.ai/overview
    - https://github.com/xai-org/xai-sdk-python
    - https://pypi.org/project/xai-sdk/

## Packaging

Stage 5.1 already bumped `requires-python >= 3.10` and installs the SDKs with the
project. This stage adds one dependency:

```toml
[project]
dependencies = [
  # ... claude-agent-sdk, openai-codex, google-antigravity (from 5.1) ...
  "xai-sdk>=1.17,<2",
]
```

Confirm the compatible range against the tested version before landing; do not
leave it unbounded. Credentials remain external and unmanaged by `agent-collab`
(`XAI_API_KEY` or the local `grok login` flow).

## Provider architecture

Add the registry pairs:

```text
(xai, cli)
(xai, sdk)
```

Add two standalone peer backend packages:

```text
agent_collab/backends/xai_cli/     # backend.py, parser.py, options.toml, README.md
agent_collab/backends/xai_sdk/     # backend.py, options.toml, README.md
```

Provider `type` is `xai`; `grok` is the CLI command and model family. Keep the
separation so model names like `grok-4.5` never leak into the provider layer.

## Config

Built-in default config ships the provider **disabled** with **no** workflow (a
workflow referencing a disabled agent fails `validate_workflow` at load — this is
why the built-in ships `antigravity` disabled with no `solo-antigravity`):

```toml
# agent_collab/default_config.toml
[agents.xai]
type = "xai"
command = "grok"
args = ["--output-format", "streaming-json", "-p"]
enabled = false

[agents.xai.options]
# permission_mode values are grok-specific (NOT claude's set).
permission_mode.allowed = ["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"]
# model.allowed / sandbox.allowed / reasoning_effort.allowed: fill from grok docs
# before landing; do not invent value lists.
```

`solo-xai` (and `enabled = true`) live in **project** config, mirroring how the
repo opts into antigravity, so the built-in default stays load-valid:

```toml
# .agent-collab/config.toml
[agents.xai]
enabled = true

[workflows.solo-xai]
sequence = ["xai"]
```

Enable it (in project config) only once the installed `grok` CLI and credentials
can be gated as reliably as Antigravity.

## xAI CLI backend

Use the installed `grok` command. **Initial transport: `--output-format
streaming-json` via `-p`.** ACP (`grok agent stdio`) is explicitly future work
(see Risks).

Option mapping (typed `xai_options`, explicit only):

- `model` -> `--model`,
- `permission_mode` -> `--permission-mode` (grok values:
  default, acceptEdits, auto, dontAsk, bypassPermissions, plan),
- `sandbox` -> `--sandbox` (profile names are grok-specific and not enumerated
  by `--help`; leave `allowed` open until confirmed),
- `reasoning_effort` / `thinking_level` -> `--reasoning-effort` (alias `--effort`;
  confirm the allowed set from grok — do not copy codex/claude levels),
- workdir -> **subprocess cwd only**. Do NOT inject `--cwd`; `SubprocessRunner`
  already runs in `run_dir`, so `--cwd` would double-specify.

Argument ordering (grounded in observed runs): flags parse both before and after
the `-p` value in practice, so the conservative rule is not strictly required —
**but insert flags before `-p` anyway**, because `SubprocessRunner` appends the
prompt as the final argv element, so a mapped flag must stay ahead of the
trailing `-p` (identical to the antigravity ordering). Extend the print-prompt
sentinel used by `_insert_before_print_prompt` to include grok's long form
`--single`, not just `-p`.

`permission_mode` and `sandbox` are **CLI-only**; reject them on the `xai` sdk
backend at start validation (mirror `_reject_unsupported_antigravity_mode`).

Event mapping — a dedicated `parse_xai_line` (do NOT reuse `_first_text`; grok
puts prose under `data`, which the shared walker's TEXT_KEYS does not include).
Observed streaming-json NDJSON events:

```json
{"type": "thought", "data": "..."}
{"type": "text",    "data": "..."}
{"type": "end", "stopReason": "...", "sessionId": "...", "requestId": "..."}
```

- `type=="text"`  -> `source="xai", type="message"` (from `data`),
- `type=="thought"` -> verbose `status` (reasoning text only; never displayed
  by default), consistent with claude thinking / antigravity thoughts handling,
- `type=="end"` -> verbose `status`; capture `sessionId`/`requestId` into
  provider session state; map a failure/cancel `stopReason` to `source="error"`,
- tool calls / shell / file edits -> `type="tool_call"|"command"|"file_change"`
  **only once a tool-using fixture confirms the shape**. Until then, an unknown
  `type` degrades to a verbose `status`, never a dropped or mis-sourced line,
- non-JSON / partial lines -> tolerated (skip empty; never crash the reader).

Do not fall back to parsing human text unless `streaming-json` is unavailable.

Health: register with `probe_binary="grok"` and a `xai_credentials()` that returns
`ok` when `XAI_API_KEY` is set or a cached `~/.grok/sessions` login exists, else
`unknown` (never `missing` on uncertainty — mirrors `gemini_api_key_credentials`).
Decide `block_on_unavailable` deliberately; antigravity's `True` (opt-in provider,
fail-fast on a missing binary / definite sign-out) is the precedent.

## xAI SDK backend

Use `xai_sdk`. It is a gRPC **chat API client**, not a local coding-agent runtime.

Option mapping (explicit only):

- `model` -> `client.chat.create(model=...)`,
- `reasoning_effort` / `thinking_level` -> create() kwarg **only if the SDK
  confirms one** for reasoning models,
- system prompt -> `system(...)` message; prompt -> `user(...)` message,
- `permission_mode` / `sandbox` -> **rejected** (CLI-only; no SDK equivalent).

Event mapping — **message-only, by construction**:

- `chat.sample().content` -> `source="xai", type="message"` (or streamed message
  chunks if the SDK's streaming API is confirmed and mapped),
- response id / metadata -> verbose `status` + `agent_sessions.xai.provider_response_id`,
- errors -> `source="error", type="error"`.

Why message-only (state honestly in the module docstring): xAI **server-side**
tools (`web_search`, `x_search`, `code_execution`) execute *remotely inside xAI*
and fold into the response — agent-collab never observes or gates them as local
actions. **Client-side** function calling requires the caller to run a full
tool-use loop, which agent-collab has no generic local executor for. So the SDK
backend emits no `file_change`/`command`/`tool_call` events and does NOT reach
Grok-CLI file-edit parity. Capabilities stay all-false. Fake-module tests drive
the mapper without `XAI_API_KEY`.

## Session identity and capabilities

xAI session identity is backend-specific:

```json
{
  "agent_sessions": {
    "xai": {
      "backend": "cli",
      "provider_session_id": "...",
      "provider_request_id": "..."
    }
  }
}
```

- the **cli** backend captures `sessionId`/`requestId` from the streaming-json
  `end` event,
- the **sdk** backend captures the response id (`provider_response_id`).

Record what each backend actually returns. `resume`/`interrupt`/`tool_gate` stay
false until the runtime behavior is implemented and tested; do not infer them from
provider brand or SDK marketing. The existing `summarize_session_capabilities`
reducer needs no change — it already ANDs `cap.resume` with an actually-captured
session id.

## Central lists and files to touch

Adding the `xai` type is a fan-out edit. Miss one and it fails *silently* (source
rewritten to `error`, options dropped) rather than loudly. Do the first two
first.

1. `events.py` — `VALID_SOURCES` (**silent-fail**: `Event.create` rewrites an
   unknown source to `"error"`, so xai messages render as errors). Also add
   `parse_xai_line`.
2. `runners.py` — `PROVIDER_SOURCES` (**silent-fail**: stderr-status attribution
   + the mock-source fallback to `codex`).
3. `config.py` — `SUBPROCESS_AGENT_TYPES` (drives `AGENT_TYPES`, config type
   validation).
4. Backend option declarations — put exact contracts/defaults in each package's
   `options.toml`; keep normalization and argv/SDK mapping in that backend.
5. Add `"xai_cli"` and `"xai_sdk"` to `_BUILTIN_BACKENDS`; the generic
   `backend_options` request needs no new wire field or MCP schema entry.
6. Add `xai_credentials()` to `backends/common/health.py`.
7. Extend provider config validation and event-source attribution for `xai`.
8. Add mirrored hermetic and integration test packages.
12. `cli.py` — `--xai-options`.
13. `default_config.toml` (+ project `.agent-collab/config.toml` for `solo-xai`,
    `enabled=true`).
14. `doc/mcp-guidance.md`, `doc/implementation-notes.md` (once implemented).

## Config and UX

Keep `cli` as the xai default backend until live SDK behavior is verified.

`agent_collab_describe_options` must show, for `xai`:

- both `cli` and `sdk` backend availability,
- installed `xai-sdk` version and `grok` version when knowable,
- credential status when safely knowable,
- capability flags (all false),
- the effective `xai_options` schema,
- clear errors for an unavailable/misconfigured backend.

Start validation must reject:

- `permission_mode` / `sandbox` on the xai `sdk` backend (CLI-only),
- SDK-only options on the `cli` backend,
- `--backend sdk` when the xai SDK backend is unavailable,
- option values outside the configured allowed sets.

## Implementation steps

1. Add `"xai"` to `VALID_SOURCES` and `PROVIDER_SOURCES` **first** (the two
   silent-fail lists), with a test that asserts their membership.
2. Add `"xai"` to `SUBPROCESS_AGENT_TYPES`; confirm config type validation.
3. Add backend-owned xAI `OptionSpec` declarations and normalization, the public
   `xai_options` bucket, and explicit xAI CLI argv rendering. Discovery must
   come automatically from the registered backend schemas.
4. Thread `xai_options` end to end (api_schema `WIRE_FIELDS`, MCP inputSchema,
   daemon dataclass, referee, cli) in **one commit** so the contract test passes.
5. Add `parse_xai_line` (tolerant NDJSON) + capture streaming-json fixtures
   (reasoning turn AND a tool-using turn) before asserting tool-event mapping.
6. Register the Grok `CliBackend` + `xai_credentials()` health probe.
7. Add `XaiSdkBackend` (message-only) with fake-module tests; rename the SDK
   factory functions to avoid the import shadow.
8. Declare CLI-only options only on the xAI CLI backend; generic validation must
   reject them automatically when the SDK backend is selected.
9. Add `xai` to `pyproject.toml` dependencies.
10. Add `solo-xai` to the repo project config with `enabled = true`.
11. Update `describe_options`, status, list, and session settings snapshots.
12. Add live smoke commands guarded by env vars, skipped by default.
13. Run full unit tests + a live smoke for each xai backend on a credentialed
    machine before closing.

## Tests

Unit tests:

- add-a-provider guard: `"xai"` present in `VALID_SOURCES` and `PROVIDER_SOURCES`
  (so xai events are not silently re-sourced to `error`/`codex`),
- registry registers `(xai, cli)` and `(xai, sdk)`,
- `parse_xai_line` fixtures: thought -> verbose status, text -> message,
  end -> session-id capture + status, failure `stopReason` -> error, unknown
  `type` -> tolerated verbose status, malformed/partial line -> no crash,
- xAI CLI option mapping: grok-specific `permission_mode`/`sandbox`, flags
  inserted before `-p`/`--single`, `--reasoning-effort` mapping, no `--cwd`,
- CLI-only (`permission_mode`, `sandbox`) rejected on the xai sdk backend,
- xAI SDK fake-module message mapping (content -> message, id -> status,
  error -> error; asserts NO tool/command/file_change events emitted),
- xai `probe()` reports a missing `grok` / missing `xai-sdk` without crashing
  imports; installed versions appear in backend summaries when available,
- provider session-id capture (cli `sessionId`/`requestId`, sdk `response.id`),
- capability reducer remains honest for xai,
- api-schema contract test still green with `xai_options` added,
- MCP `describe_options` includes xai backend availability and its schema.

Integration / live (must not run by default in CI or unit runs):

- `python3 -m unittest discover -s tests`, `./agent_collab.sh smoke`,
- live xAI CLI one-turn smoke when `grok` is installed and authenticated or
  `XAI_API_KEY` is present,
- live xAI SDK one-turn smoke when `XAI_API_KEY` is present.

### xAI fixtures

New `tests/fixtures/xai/` (mirror `tests/fixtures/antigravity/`):

- `grok-version.txt` — `grok --version` output for the version-runner test.
- `streaming-json-reasoning.ndjson` — a real thought+text+end turn; locks
  message + verbose-status + session-id capture.
- `streaming-json-tooluse.ndjson` — **the blocking one**: a turn with a file
  edit and/or shell command, so command/file_change mapping is fixture-backed
  (currently unobserved).
- `streaming-json-error.ndjson` — an `end` with a failure/cancel `stopReason`
  (and/or an error event).
- `sdk-response-sample.json` — fake `chat.sample()` shape (`.content`,
  response `.id`) for the message-only mapper.
- `README.md` — provenance (grok version, command, date, real-vs-synthesized),
  same honesty bar as the antigravity fixtures' README.

## Acceptance criteria

- A normal project install also installs `xai-sdk`.
- `agent_collab_describe_options` reports both `cli` and `sdk` for `xai`, with
  versions and capability flags (all false).
- `agent_collab_start(..., workflow="solo-xai")` works with the Grok CLI backend
  when `grok` is installed and authenticated (project config enables xai).
- `agent_collab_start(..., workflow="solo-xai", backend="sdk")` works
  message-only when `XAI_API_KEY` is present.
- xai messages are attributed to `source="xai"` (not silently rewritten to
  `error`), and mock/stderr attribution is correct.
- `permission_mode`/`sandbox` on the sdk backend fail before session creation
  with field-path details.
- Session settings accurately show which xai backend ran and the applied options.
- Capability flags remain false unless the runtime behavior is implemented.

## Risks and follow-ups

- xAI is the only provider adding a new `type`; the edit fans out across the
  central lists above, and `VALID_SOURCES` / `PROVIDER_SOURCES` fail *silently*.
  Land the two source-list edits and their guard test first.
- Grok Build CLI (local coding agent with tools) and `xai-sdk` (remote chat
  client) are genuinely asymmetric. SDK message-only is correct and permanent
  until a local tool-executor exists — do not imply file-edit parity.
- streaming-json tool-event shapes are unobserved so far (only thought/text/end
  captured). Do not assert tool/command/file_change mapping or flip any
  capability until a tool-using fixture exists.
- Grok CLI exposes both headless `streaming-json` and ACP (`grok agent stdio`,
  JSON-RPC: `session/new` · `session/prompt` · `session/update`). Prefer
  `streaming-json` now; keep ACP as a follow-up with its different session
  lifecycle, and adopt it only when it can be tested deterministically.
- Grok's `--sandbox` profile names and `--reasoning-effort` allowed values are
  not enumerated by `--help`; confirm from grok docs before pinning `allowed`
  lists, or leave them open.
