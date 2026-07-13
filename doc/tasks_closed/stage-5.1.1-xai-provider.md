# Stage 5.1.1: xAI provider (`grok` CLI + `xai-sdk`)

## Status

**Complete — closed 2026-07-10.**

Implemented against the backend-package and backend-qualified option
architecture delivered by Stage 5.1. Hermetic, packaging, mock-smoke, and
credentialed `xai_cli` checks pass. The SDK dependency/API surface, option
mapping, async client lifetime, response mapping, and failure paths are covered
by installed-package introspection and hermetic tests. A credentialed
`xai_sdk --strict` call was not made because this host has no `XAI_API_KEY`; the
selector returns the intended strict-missing exit `2`. That external credential
gap is recorded below and is not an incomplete implementation path.

## Purpose

Add `xai` as a fourth real provider type with two first-class backends:

- `xai_cli`: the installed Grok Build `grok` command in headless
  `streaming-json` mode;
- `xai_sdk`: the official `xai-sdk` Python package, initially message-only.

This remains separate from Stage 5.1 because it adds a new provider identity,
not merely another backend for an existing provider. Backend-specific behavior
is now self-owned, but a new provider still has a small, intentional fan-out for
event attribution and config type validation.

## Architectural baseline

[Stage 5.1 backend packages and test split](stage-5.1-backend-packages-and-integration-tests.md)
and the
[SDK/backend-contract remediation](stage-5.1-first-class-sdk-backends-remediation.md)
are complete. This stage must use their landed contract rather than recreate
the former provider-wide plumbing.

Two peer packages own the implementation:

```text
agent_collab/backends/xai_cli/
  __init__.py
  backend.py
  parser.py
  options.toml
  README.md

agent_collab/backends/xai_sdk/
  __init__.py
  backend.py
  options.toml
  README.md
```

The public request contract is already generic:

```json
{
  "backend_options": {
    "xai_cli": {"model": "grok-build"}
  }
}
```

For an SDK-selected start, the selected canonical key is `xai_sdk` instead.
Do not send both entries for `solo-xai`; the generic validator correctly rejects
options for a backend the workflow did not select.

Consequences:

- Do **not** add `xai_options`, `--xai-options`, an xAI wire field, an MCP input
  field, a daemon field, or referee/TUI provider-option plumbing.
- `backend_options.xai_cli` and `backend_options.xai_sdk` are discovered from
  the registered backends and their colocated manifests.
- The CLI parser belongs in `xai_cli/parser.py`, not `events.py`.
- SDK runner and event mapping stay in `xai_sdk/backend.py`; imports stay lazy.
- Backend-specific option rejection is declarative: an option absent from the
  selected backend's `options.toml` fails with a
  `backend_options.<canonical-backend>.<field>` path.
- `agent_collab_describe_options`, settings summaries, dry-run, REST, MCP, and
  CLI start all consume the existing backend contract. Add xAI-specific code to
  those layers only if a focused test proves a remaining hard-coded provider
  assumption.
- No TUI-specific work is part of this stage. The TUI should receive xAI through
  the same dynamic describe/start contract as every other backend.

Adding a backend for an existing provider remains one package plus one registry
entry. The three central provider lists below are needed only because `xai` is a
new event/config provider identity.

## Verified inputs and implementation gates

These surfaces are young. Keep observed facts separate from facts that still
need a checked-in fixture.

### Grok Build CLI

Re-verified locally on 2026-07-10 with `grok 0.2.93`:

- headless single turn: `-p, --single <PROMPT>`;
- output formats: `plain`, `json`, and newline-delimited `streaming-json`;
- `--model`, `--permission-mode`, `--sandbox`, and
  `--reasoning-effort`/`--effort`;
- permission modes: `default`, `acceptEdits`, `auto`, `dontAsk`,
  `bypassPermissions`, and `plan`;
- sessions: `--session-id`, `--resume`, and `--continue`;
- ACP: `grok agent stdio` (future work for this repository).

The current official headless documentation recommends `--no-auto-update` for
scripts and confirms that headless sessions live under `~/.grok/sessions`.
`grok 0.2.93` accepts that flag even though its top-level help does not list it.

Historical real `streaming-json` observations, to be recaptured as fixtures
before parser assertions are landed:

```json
{"type":"thought","data":"..."}
{"type":"text","data":"..."}
{"type":"end","stopReason":"...","sessionId":"...","requestId":"..."}
```

`data`, not `text` or `content`, carries thought/text deltas. Tool-event shapes
have not been captured. Do not infer them from Claude or Codex JSON.

Primary references:

- <https://docs.x.ai/build/cli/headless-scripting>
- <https://docs.x.ai/build/overview>

### xAI Python SDK

Current primary documentation confirms:

- distribution `xai-sdk`, import `xai_sdk`, Python 3.10+;
- synchronous `Client` and asynchronous `AsyncClient`;
- `client.chat.create(...)`, `chat.append(user(...))`, and
  `await chat.sample()` on the async surface;
- response prose at `.content` and response identity at `.id`;
- `reasoning_effort` is a `chat.create(...)` parameter;
- authentication defaults to `XAI_API_KEY`;
- the API is stateless unless an explicit stateful-response flow is requested;
- server-side tools execute at xAI, while client-side tools require the caller
  to implement and run the tool loop.

PyPI currently reports `xai-sdk 1.17.0`, but the package is not installed in
this checkout's selected interpreter. Therefore `>=1.17,<2` is a **candidate**
constraint, not a verified landing constraint.

Before production SDK code is written:

1. Install the candidate in the same Python 3.12 environment used by the
   project.
2. Record the installed distribution version and `python -m pip check` result.
3. Import `xai_sdk` and introspect `AsyncClient`, chat creation, async sampling,
   response fields, error types, and the client's close/context-manager
   lifetime.
4. Save non-secret facts in `tests/fixtures/xai/sdk-introspection.json`.
5. Base the injectable fake-module/object graph on that capture.

If the candidate cannot install, import, expose a non-blocking turn API, or be
closed deterministically, do not register a guessed SDK backend. Record the
failed version and split SDK support into a follow-up rather than landing a
backend that cannot run.

Primary references:

- <https://github.com/xai-org/xai-sdk-python>
- <https://docs.x.ai/developers/model-capabilities/text/reasoning>
- <https://docs.x.ai/developers/tools/advanced-usage>
- <https://pypi.org/project/xai-sdk/>

## Scope

Delivered by this stage:

- provider source/type `xai`;
- registered `(xai, cli)` and `(xai, sdk)` backends;
- backend-owned option manifests, normalization, summaries, probes, runners,
  docs, and tests;
- a fixture-backed Grok `streaming-json` parser;
- a message-only async xAI SDK runner;
- uniform provider-session identity capture;
- built-in disabled xAI config and project `solo-xai` opt-in;
- hermetic and credentialed test coverage;
- bounded `xai-sdk` packaging based on the verified installed version.

Not delivered:

- ACP transport;
- provider resume, interrupt, or tool approval/denial;
- a generic local client-side tool executor for chat SDKs;
- SDK file-edit or shell-command parity with Grok Build;
- TUI-specific provider handling.

## Central provider identity edits

Make these explicit new-provider edits and guard them with tests:

1. `agent_collab/events.py`: add `"xai"` to `VALID_SOURCES`. Missing this
   silently rewrites xAI events to `source="error"`.
2. `agent_collab/runners.py`: add `"xai"` to `PROVIDER_SOURCES`. Missing this
   misattributes subprocess stderr status and xAI-flavored mock events.
3. `agent_collab/config.py`: add `"xai"` to `SUBPROCESS_AGENT_TYPES` (despite
   the legacy name, this is the current real-provider type allowlist).
4. `agent_collab/backends/__init__.py`: add `"xai_cli"` and `"xai_sdk"` to
   `_BUILTIN_BACKENDS`.

Then search for closed enumerations of the three existing real providers in
tests, integration harnesses, docs, and snapshots. Update only enumerations
that mean “all built-in real providers”; do not turn provider-neutral code into
an xAI branch.

No change is expected in `api_schema.py`, the start request dataclass, MCP's
generic `backend_options` schema, or the CLI argument parser.

## Backend option contracts

Manifests are the shipped source of accepted options. Default config contains
concrete agent values only; it does not declare `allowed` sets.

### `xai_cli/options.toml`

Declare:

- `model`: string, CLI-inferred from `--model`, with no closed model allowlist;
- `permission_mode`: string, CLI-inferred, allowed values exactly
  `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`;
- `sandbox`: string, CLI-inferred, with no `allowed` list because Grok does not
  enumerate profile names;
- `thinking_level`: string, CLI-inferred, preferred cross-provider spelling;
- `reasoning_effort`: string, CLI-inferred, xAI-native alias.

### `xai_sdk/options.toml`

Declare only:

- `model`: required string with no manifest default; the caller must select a
  model explicitly so a stale SDK default cannot fail after session creation;
- `thinking_level`: string, preferred spelling;
- `reasoning_effort`: string, xAI-native alias.

For both backends, normalize `thinking_level` and `reasoning_effort` as aliases:

- if one is provided, use it as the effective reasoning effort;
- if both are provided with different values, raise `BackendOptionError` on
  `reasoning_effort`;
- map only one effective value to the provider;
- keep supported values aligned with the surface that consumes them. Grok
  Build accepts `low`, `medium`, `high`, plus model-specific `xhigh`; the
  bounded `xai-sdk` accepts `none`, `low`, `medium`, and `high`. Do not expose
  CLI-only `xhigh` through the SDK manifest without installed-SDK evidence.

Do not put `permission_mode` or `sandbox` in the SDK manifest. Generic backend
validation will then reject them before session creation. Conversely, do not
invent SDK-only options until the installed API capture proves them.

## Built-in and project config

Ship one disabled CLI-default agent and no built-in workflow:

```toml
# agent_collab/default_config.toml
[agents.xai]
type = "xai"
command = "grok"
args = ["--no-auto-update", "--output-format", "streaming-json", "-p"]
backend = "cli"
enabled = false
```

The built-in config must remain load-valid when Grok is absent. A workflow may
not reference a disabled agent, so `solo-xai` belongs in the repository's
project config:

```toml
# .agent-collab/config.toml
schema_version = 4

[agents.xai]
enabled = true

[workflows.solo-xai]
sequence = ["xai"]
```

Enable the project entry only after the local CLI health/auth check and live
smoke pass, and update the existing project file's schema version to the current
config schema while touching it. Keep `cli` as the configured/default backend;
a solo start can select SDK through the existing `backend="sdk"` request
override.

## `xai_cli` backend

### Command construction

Use `SubprocessRunner` and an explicit command builder, matching the peer CLI
packages.

Map normalized options as follows:

- `model` -> `--model`;
- `permission_mode` -> `--permission-mode`;
- `sandbox` -> `--sandbox`;
- effective reasoning effort -> `--reasoning-effort`.

Use subprocess `cwd` as the working directory. Do not also inject Grok's
`--cwd`, because `SubprocessRunner` already executes in the resolved run dir.

`SubprocessRunner` appends the prompt as the last argv item, while `-p` or
`--single` consumes that item. Mapped flags must therefore be inserted before
the print-prompt sentinel. Extend
`backends/common/cli.py:insert_before_print_prompt()` to recognize `--single`
in addition to the existing `-p`, `--print`, and `--prompt`, and cover both Grok
spellings in its shared helper tests.

Do not fall back to plain output. The configured transport is
`streaming-json`; a non-JSON stdout line is an unexpected verbose status (or is
ignored when not verbose), never guessed assistant prose.

### Parser and event mapping

Implement `parse_xai_line` in `xai_cli/parser.py`. It must tolerate blank,
malformed, scalar, unknown, and partial-final lines without crashing.

Fixture-backed minimum mapping:

- `type == "text"` with string `data` -> `source="xai"`, `type="message"`;
- `type == "thought"` with string `data` -> verbose xAI `status` only;
- explicit error event or a fixture-confirmed failing `stopReason` ->
  `source="error"`, `type="error"`;
- successful `type == "end"` -> xAI `status` emitted regardless of verbosity
  when it carries a session ID;
- unknown dict event -> verbose xAI `status` with compact raw data;
- empty/non-JSON input -> no default message event and no exception.

Map tool, command, and file-change events only after the real tool-use fixture
confirms their fields. Use `source="tool"` for those action events, consistent
with the other CLI parsers. Until then, unknown tool-shaped events remain
verbose status data and capabilities remain false.

### CLI session identity

The daemon persists only the uniform session-event keys:

```json
{
  "provider_session_id": "...",
  "provider_session_kind": "session",
  "agent_id": "xai"
}
```

An xAI `end` event's raw `sessionId` must be translated to those keys. Bind the
workflow agent id in the backend (for example with a parser closure or partial),
because the generic `Parser(line, verbose)` interface does not otherwise know
it. Preserve Grok's `sessionId` and `requestId` in raw transcript data, but do
not add xAI-only fields to persisted `SessionState`. `requestId` is request
metadata, not the provider session identity.

Set `provider_session_id_kind = "session"`. Capturing an ID does not make the
backend resumable; `BackendCapabilities()` stays all false.

### CLI health and credentials

Probe `grok` with `probe_cli_backend` and the common version runner. Add a
side-effect-free credential helper that returns:

- `ok` when `XAI_API_KEY` is non-empty;
- `ok` when `~/.grok/auth.json` exists, parses, and contains a non-empty cached
  auth entry;
- `unknown` when the file is unreadable/malformed or neither signal is present.

Never read credential values into events/logs/tests. A `~/.grok/sessions`
directory is **not** proof of authentication and must not be used as the signal.

Set `checks_credentials = true` and `block_on_unavailable = true`: this is an
opt-in provider, and a definitely missing CLI should fail before session state
is created. An `unknown` credential result is attempted; the first real turn
remains authoritative.

## `xai_sdk` backend

The SDK is a remote chat API, not the Grok Build local coding runtime.

### Runtime construction

- Import `xai_sdk` lazily inside the production factory/probe path.
- Use the verified `AsyncClient` surface; never block the daemon loop with the
  synchronous `Client`.
- Keep the client alive until sampling and event mapping complete, and close it
  using the lifetime mechanism captured from the installed version.
- Create a chat with normalized `model` and effective `reasoning_effort` only.
- Append the turn prompt as `user(prompt)`. There is no generic agent-collab
  system-prompt start option to map in this stage.
- Begin with `await chat.sample()` rather than streaming. The collected response
  is the smallest stable message-first contract; streaming can follow once its
  installed async shapes and cancellation behavior are fixture-backed.
- Make the turn factory injectable so hermetic tests neither import the SDK nor
  call xAI.

### SDK events and identity

Map only captured public fields:

- non-empty `response.content` -> xAI `message`;
- non-empty `response.id` -> `provider_session_event("xai", agent_id, id,
  "response")`;
- SDK/auth/turn exception -> `sdk_error_event("xai", exc)`;
- optional response metadata -> verbose status only after its installed shape
  is captured.

Set `event_fidelity = "message_only"` and
`provider_session_id_kind = "response"`. A response ID is useful correlation
metadata but is not a resumable conversation by itself; capabilities remain all
false.

Do not enable server-side or client-side tools in this stage. Server-side tools
run remotely and are not local gated actions. Client-side tools would require a
generic executor and a complete call/result loop that agent-collab does not
have. The backend therefore emits no `tool_call`, `command`, or `file_change`
events and makes no file-edit parity claim.

### SDK health

Probe module `xai_sdk` with `probe_sdk_backend`, report the installed
`xai-sdk` distribution version, and use a side-effect-free API-key helper:

- `ok` when `XAI_API_KEY` is non-empty;
- otherwise `unknown`, because per-agent environment/config may supply it.

Set `checks_credentials = true` and `block_on_unavailable = true`. Missing SDK
imports fail before session creation; unknown credentials are attempted.

## Packaging and backend documentation

After the SDK introspection gate passes, add a bounded dependency based on the
tested version. Candidate only:

```toml
dependencies = [
  # existing first-class SDKs ...
  "xai-sdk>=1.17,<2",
]
```

Verify installation and import in the project interpreter:

```bash
python -m pip install -e .
python -m pip check
python -c "import xai_sdk; print(xai_sdk.__version__)"
```

The existing setuptools package-data glob already includes each backend's
`README.md` and `options.toml`; no xAI-specific package-data entry is expected.
Build a wheel and assert both xAI packages' data files are present.

Each backend README must cover requirements, authentication, option mapping,
event fidelity, provider identity, capabilities, security, limitations, and
hermetic/live test commands. The SDK README must prominently distinguish remote
chat from the local coding CLI.

## Fixtures and tests

### Fixtures

Create `tests/fixtures/xai/README.md` recording capture date, exact version,
command, real-versus-synthetic provenance, and any redaction. Never commit
prompts, paths, IDs, or payloads that disclose secrets or private repository
content.

Required files:

- `grok-version.txt`: real `grok --version` output;
- `streaming-json-reasoning.ndjson`: real thought/text/end turn;
- `streaming-json-tooluse.ndjson`: real disposable-workspace file edit and/or
  shell command; this gates typed action mapping, not basic backend registration;
- `streaming-json-error.ndjson`: real or clearly labeled synthesized error/end
  shape based on captured fields;
- `sdk-introspection.json`: installed public SDK signatures/lifetime facts;
- `sdk-response-sample.json`: redacted illustrative values in the captured
  response shape.

### Hermetic backend tests

Mirror the package layout:

```text
tests/backends/xai_cli/test_backend.py
tests/backends/xai_sdk/test_backend.py
```

Cover:

- registration of `(xai, cli)` and `(xai, sdk)` and canonical names;
- xAI membership in all three central provider/source allowlists;
- declarative option discovery and field-path rejection;
- reasoning alias agreement/conflict behavior;
- CLI inference and explicit flag rendering before both `-p` and `--single`;
- no injected `--cwd` and correct subprocess run dir;
- parser mappings, malformed/unknown tolerance, and fixture-backed tool mapping;
- the CLI end-event translation into uniform daemon session state;
- CLI/SDK health for missing dependencies, unknown credentials, and reported
  versions without touching real home/credentials;
- SDK fake response content, response identity, async lifetime/close, and error
  mapping;
- absence of SDK tool/command/file-change events;
- all-false capability and non-resumable session summaries;
- `describe_options`, settings, dry-run, and backend policy output through the
  existing generic contract;
- config validation for disabled built-in xAI and enabled project `solo-xai`;
- exact built-in backend/schema sets and other intended all-provider loops.

Add focused tests for `insert_before_print_prompt(..., "--single")` in the
shared CLI helper suite. Do not add an API-schema test for `xai_options`; that
field must not exist.

### Credentialed integration tests

Add:

```text
integration_tests/backends/xai_cli/test_live.py
integration_tests/backends/xai_sdk/test_live.py
```

Extend `integration_tests/harness.py`:

- add `xai` to `PROVIDERS`;
- add economical explicit xAI live defaults confirmed by the account/model
  list;
- support `AGENT_COLLAB_IT_XAI_MODEL` and the existing thinking-level override
  convention.

The canonical selectors then work without changing the integration CLI:

```bash
./agent_collab_dev.sh integration-test xai_cli --strict
./agent_collab_dev.sh integration-test xai_sdk --strict
```

Each live test uses the existing disposable workspace and isolated
`AGENT_COLLAB_HOME`. The CLI test requires `grok` plus local auth or
`XAI_API_KEY`; the SDK test requires `XAI_API_KEY`. Assert event kinds and
identity, not response prose or raw credential-bearing data.

## Documentation updates

Once implementation passes, update:

- `README.md`;
- `doc/agent-configuration.md`;
- `doc/development.md`;
- `doc/implementation-notes.md`;
- `integration_tests/README.md`.

Document canonical selectors `xai_cli`/`xai_sdk`, generic
`backend_options.xai_*`, the CLI/SDK capability asymmetry, credentials, and the
message-only SDK limitation. Do not document provider-wide `xai_options`.

## Implementation order

1. Capture the CLI fixtures and complete the SDK install/import/introspection
   gate. Fix the dependency range from evidence.
2. Add `xai` to the three central provider/source sets with silent-failure guard
   tests.
3. Add the two standalone backend packages, manifests, READMEs, and registry
   entries.
4. Implement CLI option normalization, command construction, health, parser,
   and uniform session event mapping.
5. Implement the async message-only SDK backend from the introspection fixture.
6. Add disabled built-in config and validate it without Grok installed.
7. Extend hermetic all-provider sets/snapshots and run the full unit suite.
8. Add the bounded dependency, verify editable install/import, and verify wheel
   contents.
9. Add integration harness/tests and run both canonical selectors on a
   credentialed machine.
10. Only after the CLI smoke succeeds, enable `xai` and add `solo-xai` in the
    repository project config.
11. Update current documentation and close this task with exact test/live-call
    evidence.

## Verification

Closure evidence from 2026-07-10:

- `python3 -m compileall -q agent_collab integration_tests tests`: passed;
- `./agent_collab_dev.sh test`: 473 tests passed;
- `./agent_collab_dev.sh smoke`: passed;
- `python -m pip check`: no broken requirements;
- wheel/package-data verification: passed, including both xAI manifests;
- `./agent_collab_dev.sh integration-test xai_cli --strict`: passed against
  `grok-build` and persisted the Grok session identity;
- `./agent_collab_dev.sh integration-test xai_sdk --strict`: expected exit `2`
  because `XAI_API_KEY` is absent; the installed `xai-sdk` 1.17.0 contract is
  captured in `tests/fixtures/xai/sdk-introspection.json` and exercised through
  hermetic async response/identity tests;
- final Grok Build review: completed with exit code `0`, no timeout or error,
  and no blocking findings.

Hermetic and packaging checks:

```bash
python3 -m compileall -q agent_collab integration_tests tests
./agent_collab_dev.sh test
./agent_collab_dev.sh smoke
python -m pip check
python -c "from agent_collab import backends; print(backends.registered_backend_names())"
```

Credentialed, opt-in checks:

```bash
./agent_collab_dev.sh integration-test xai_cli --strict
./agent_collab_dev.sh integration-test xai_sdk --strict
```

## Acceptance criteria

- A normal project install resolves the bounded, tested `xai-sdk` and imports
  `xai_sdk` in the selected Python 3.10+ interpreter.
- The registry exposes `xai_cli` and `xai_sdk`; `describe_options` reports their
  dynamic schemas, versions/health, event fidelity, identity kind, and all-false
  capabilities.
- There is no xAI-specific public start field or CLI flag; starts use
  `backend_options.xai_cli` / `backend_options.xai_sdk`.
- Built-in config loads with xAI disabled and without `grok`; project
  `solo-xai` is enabled only after its live CLI smoke passes.
- `solo-xai` completes through Grok `streaming-json`, attributes messages to
  `source="xai"`, and persists the translated session ID under the uniform
  `agent_sessions` schema.
- `solo-xai` with backend override `sdk` completes through `AsyncClient`, emits
  message-only xAI events, and captures response identity as kind `response`.
- CLI-only options are rejected on SDK with exact backend-qualified field paths.
- Tool/command/file-change mapping is backed by a real Grok fixture; absent a
  confirmed shape, it remains status-only rather than guessed.
- Missing dependencies fail before session creation; indeterminate credentials
  are attempted and real turn errors reach the transcript.
- Session settings report the selected backend and exact normalized options.
- Resume, interrupt, and tool-gate capabilities remain false.
- Hermetic tests remain credential-free and cannot discover live tests. The
  CLI selector passed credentialed verification; the SDK selector and its
  strict missing-credential behavior are covered, with a paid model call left
  as an optional environment-dependent follow-up.

## Risks and follow-ups

- Grok Build and `xai-sdk` are intentionally asymmetric: one is a local coding
  agent, the other a remote chat API. Do not imply parity.
- CLI event shapes and SDK APIs can change quickly. Fixtures and bounded package
  versions are the compatibility boundary.
- CLI `xhigh` reasoning is model-specific, while the bounded SDK does not
  expose it. Keep the two manifests honest and let the provider reject
  unsupported model/effort combinations unless a stable local compatibility
  table is captured.
- ACP may eventually provide a stronger session/update protocol than headless
  NDJSON, but it has a different lifecycle and is a separate stage.
- Stateful SDK responses (`store_messages`/`previous_response_id`) do not make
  agent-collab resumable until continuation is implemented and tested end to
  end.
