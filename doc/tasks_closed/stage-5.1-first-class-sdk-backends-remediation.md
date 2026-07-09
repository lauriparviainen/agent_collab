# Stage 5.1 remediation: verify and repair the SDK backends

**Status: Complete — closed 2026-07-10.**

## Post-implementation note (2026-07-10)

Stage 5.1 and both remediation slices are complete. The final implementation
landed the following:

- Six standalone backend packages under
  `agent_collab/backends/<provider>_<backend>/`, each with backend-owned option
  declarations, normalization, settings summaries, health, runner construction,
  documentation, and hermetic tests.
- Installed and import-verified SDKs in the default Python 3.12 environment:
  `claude-agent-sdk==0.2.114`, `openai-codex==0.1.0b3`, and
  `google-antigravity==0.1.6`. The source wrapper prefers
  `~/.agent-collab/venv`, enforces Python 3.10+, and falls back through Python
  3.12, 3.11, and 3.10.
- Backend-qualified MCP/session options such as `claude_sdk` and `codex_cli`.
  Static, non-MCP Antigravity SDK settings (`vertex`, `project`, `location`) are
  declared by its colocated `config.toml`; MCP-overridable values such as
  `model` remain in `options.toml` and `[agents.<backend-agent>.options]`.
- Config schema 3 and backend-specific agent sections, allowing `claude_cli`,
  `claude_sdk`, `codex_cli`, `codex_sdk`, `antigravity_cli`, and
  `antigravity_sdk` to be configured simultaneously without option leakage.
- Separate hermetic and credentialed suites. Live tests use canonical selectors
  (`claude_sdk`, `codex_cli`, and so on), economical model/reasoning defaults,
  disposable workspaces, and never run as part of the normal unit suite.
- Wheel verification for backend `README.md`, `options.toml`, and the
  Antigravity SDK static `config.toml`. The final hermetic suite passes all 356
  tests.

Claude and Codex SDK paths have passed real credentialed turns during the
remediation work. Antigravity SDK configuration and ADC are valid, but its
bundled `localharness` cannot start on this Oracle Linux 9 host: the binary
requires `GLIBC_ABI_DT_RELR` (glibc 2.36+) while the host provides glibc 2.34.
That external native-binary limitation is prominently documented in
`integration_tests/README.md`; it does not leave an unimplemented Stage 5.1
code path. Resolution requires a newer host runtime or an EL9-compatible binary
from Google. The SDK remains installed, discoverable, configured, and covered
by constructor/event-mapping tests.

Relevant follow-up commits include `ed5abac` for canonical integration backend
names and `170c877` for the Antigravity glibc blocker note. xAI remains the
separate open [Stage 5.1.1](../tasks_open/stage-5.1.1-xai-provider.md).

## Purpose

Repair the Stage 5.1 SDK implementation without replacing unverified API guesses
with new guesses, then finish the backend abstraction as a separate slice.

One provider may have several interchangeable execution backends. Ultimately,
adding a backend for an existing provider should require a backend module,
registration, and backend-contract tests—not edits to the daemon, API layer,
referee, or central provider/backend option tables. That architectural goal must
not delay the immediate packaging and runtime correctness fixes.

This is a remediation plan for
[Stage 5.1](stage-5.1-first-class-sdk-backends.md), not a new provider stage.
xAI remains in [Stage 5.1.1](../tasks_open/stage-5.1.1-xai-provider.md).

## Evidence levels

Keep these categories distinct throughout implementation and review.

### Verified from the local code

- `openai-codex>=0.1,<1` does not admit the currently targeted prerelease
  `0.1.0b3`.
- The Codex implementation calls `start_thread(working_directory=...)` and
  `run_streamed()`. Those calls are not supported by the Python surface the
  implementation claims to target.
- Backend option support is duplicated in the central
  `BACKEND_OPTION_SUPPORT` map, so a new backend requires central changes and an
  unlisted custom backend bypasses support validation.
- CLI-derived defaults are inferred centrally even when the selected backend is
  SDK.
- `agent_collab_describe_options` does not report an effective schema per
  backend.
- The Claude implementation does not explicitly select the Claude Code system
  prompt preset, ignores `ToolResultBlock`, and uses a broad `TypeError`
  fallback that can silently discard `cwd` or settings isolation.
- The active interpreter is Python 3.9 and none of the three SDK imports is
  available there.
- The hermetic fake-module suite passing does not prove the real SDK call paths.

### Published candidate facts, not yet verified in this environment

As of the 2026-07-09 documentation review, the candidate distributions are:

- `claude-agent-sdk`, imported as `claude_agent_sdk`,
- `openai-codex`, expected to import as `openai_codex`,
- `google-antigravity`, expected to import as `google.antigravity`.

Primary docs/source suggest candidate runtime shapes such as:

- Codex `AsyncCodex`, `thread_start`, `Thread.run`, and `TurnResult`,
- Antigravity `ChatResponse.resolve()` and async `thoughts`/`tool_calls`
  cursors,
- Claude `ClaudeAgentOptions`, `query`, typed content blocks, and
  `ResultMessage`.

These are inputs to the verification step, not implementation instructions.
Until the candidate wheels install and their public objects/signatures are
captured locally, the exact Codex and Antigravity replacement call shapes remain
unconfirmed.

### Requires a credentialed live call

- Authentication behavior for each supported auth path.
- Real message/item/chunk event shapes returned during a turn.
- Provider session/thread/conversation ID location and lifetime.
- Antigravity tool-call behavior.
- Whether provider defaults preserve the intended coding-agent behavior.

## Confirmed gaps

1. The Codex dependency constraint is not installable for the targeted beta.
2. The current Codex production runner cannot call its claimed Python SDK.
3. Codex and Antigravity fake tests encode unverified production API shapes.
4. Backend option ownership is split between backend modules and central
   `options.py` tables.
5. Session settings can be influenced by CLI defaults for a non-CLI backend.
6. Option discovery is provider-wide rather than backend-specific.
7. Claude event/options mapping is incomplete and can silently degrade.
8. No clean Python >=3.10 environment has passed imports and a real turn for all
   three SDKs.

## Delivery strategy and dependency order

Deliver two independently reviewable slices:

```text
Slice A: verify dependencies/APIs -> repair packaging and SDK runners
                                      |
                                      v
                         green installed-SDK tests
                                      |
                                      v
Slice B: move option/schema policy behind the backend contract
```

Slice A is the urgent correctness work. Slice B is the larger architecture
refactor. Slice B must not be used as a prerequisite for fixing broken package
constraints or runner calls.

The verification gate in Slice A step 1 is mandatory:

- Codex and Antigravity runtime rewrites must not begin until their installed
  distributions, import names, public call signatures, result types, and
  resource lifetimes have been captured.
- Documentation examples may guide what to inspect, but must not be copied into
  production code before that capture.
- If a candidate SDK fails its go/no-go gate, do not build a compatibility shim
  from guesses and do not register a backend that cannot run.

## Slice A: dependency and runtime correctness

### A1. Create a real SDK environment and run go/no-go checks

Use Python 3.12 (or another supported Python >=3.10) in a dedicated virtual
environment. Start from a clean environment so a globally installed CLI or SDK
does not hide missing package dependencies.

For each candidate distribution:

1. Ask pip to resolve and install the explicit candidate version.
2. Record the installed distribution name/version and dependency tree.
3. Import the expected module.
4. Inspect the public objects and signatures needed by the backend without
   making a model call.
5. Construct/enter the documented client or configuration object where that is
   side-effect-free.
6. Save an introspection fixture containing the facts the fake tests will model.

Candidate import smoke:

```bash
python -c "import claude_agent_sdk, openai_codex, google.antigravity"
```

This command is evidence only after pip reports which distributions were
installed into the same interpreter.

#### Codex go/no-go

The Codex Python package is a hard decision point. Verify all of the following:

- pip resolves the candidate official distribution on a supported platform,
- the wheel imports as `openai_codex`,
- the wheel supplies or declares its required Codex runtime,
- it exposes a usable thread/turn API,
- a thread ID and final response can be obtained through public objects,
- the client has a documented lifetime/close mechanism suitable for an async
  daemon.

If any item fails:

- remove/defer the Codex SDK dependency and `(codex, sdk)` registration,
- document the failed fact and tested version,
- create a focused Codex Python SDK follow-up,
- explicitly revise Stage 5.1's scope and acceptance criteria before landing,
- keep the Codex CLI backend unchanged.

The stage must not claim first-class Codex SDK support merely because a package
name or documentation page exists.

Apply the same honesty to Claude or Antigravity if their candidate wheel cannot
install/import on a platform the project intends to support.

#### A1 verification record (2026-07-09)

Completed in `/tmp/agent-collab-sdk-venv` with Python 3.12.13 on Linux x86-64:

- `claude-agent-sdk==0.2.114` installs and imports as `claude_agent_sdk`.
  `ClaudeAgentOptions` accepts `cwd`, `setting_sources`, the Claude Code
  `system_prompt`/`tools` presets, `model`, `permission_mode`, `effort`, and
  `max_thinking_tokens`. The installed typed message/block constructors match
  the fields used by A5.
- `openai-codex==0.1.0b3` installs and imports as `openai_codex`, and declares
  `openai-codex-cli-bin==0.137.0a4`. Outside the restricted filesystem sandbox,
  `AsyncCodex` initialized the bundled app-server, created an ephemeral
  read-only thread, exposed `AsyncThread.id`, and returned the same ID from
  `thread.read()`. No model call was made.
- `google-antigravity==0.1.5` installs and imports as
  `google.antigravity`. The installed `ChatResponse.resolve()` is async;
  `thoughts`, `tool_calls`, and `chunks` are async-iterator properties over an
  independent shared buffer. A constructed response resolved typed `Text`,
  `Thought`, `ToolCall`, and `ToolResult` objects, then allowed sequential text,
  thought, and tool-call cursor reads. No model call was made.
- `python -m pip check` reports no broken requirements.

This clears the package/import/no-model API gate for all three providers.
Credentialed turn shapes and authentication remain A6/live-smoke evidence.

### A2. Lock packaging to the versions that pass A1

Only after A1 passes, update `pyproject.toml` to ranges based on the installed
and tested versions. Candidate ranges—not final values until verified—are:

```toml
dependencies = [
  "claude-agent-sdk>=0.2.114,<0.3.0",
  "openai-codex>=0.1.0b3,<0.2.0",
  "google-antigravity>=0.1.5,<0.2.0",
]
```

Use an explicit prerelease floor for Codex. Confirm with pip that the selected
specifier resolves without a separate `--pre` flag. Keep explicit patch-level
upper bounds for readability and to communicate the intended release boundary.

Verification:

```bash
python -m pip install -e .
python -m pip check
python -c "import claude_agent_sdk, openai_codex, google.antigravity"
```

Update the Stage 5.1 source-facts section with the installed versions and the
captured signatures. Remove all "replace with actual tested floor" language.

If the Codex go/no-go fails, omit Codex from these commands and dependencies and
record the explicitly approved scope change.

### A3. Repair the Codex runner from captured facts

Do not prescribe a replacement call chain in advance. Use the introspection
fixture from A1 to decide:

- sync versus async client,
- context-manager/close lifetime,
- thread creation method and cwd parameter name,
- turn execution or streaming method,
- final response and collected item types,
- thread ID location,
- model, sandbox, and reasoning option mappings.

The published `AsyncCodex -> thread_start -> run -> TurnResult` shape is a
hypothesis to verify, not an acceptance criterion.

Implementation requirements that are independent of the provider API shape:

- Keep the SDK client alive until the turn and all event mapping complete.
- Never block the daemon event loop with a synchronous SDK call; use the async
  surface or isolate blocking calls appropriately.
- Start message-first if final-response collection is the only verified stable
  surface.
- Map only captured public item/event types. Unknown values may become verbose
  status/raw data; do not invent CLI JSONL parity.
- Emit one provider-session event from the confirmed thread ID.
- Surface startup/auth/turn failures as error events. Use
  `BackendUnavailable` only for missing or incompatible runtime setup.

Replace `_Item(type=..., thread_id=...)` fakes with an object graph derived from
the A1 fixture. Add a production-factory test that would fail on the current
`start_thread`, `working_directory`, and `run_streamed` calls.

If A1 fails the Codex go/no-go, skip A3 and execute the documented deferral
instead.

### A4. Repair the Antigravity runner from captured facts

Do not assume in advance that `response.resolve()` or the documented async
cursors are the best integration point. A1 must determine:

- which response accessors exist in the installed wheel,
- which are awaitable, async iterable, or buffered,
- whether independent cursors can be consumed sequentially/concurrently,
- the typed text/thought/tool-call/tool-result shapes,
- conversation ID and usage metadata locations.

Then choose one verified path:

- resolve a typed buffered response once, or
- consume verified async streams with `async for`.

Map confirmed values into the existing event contract:

- assistant text -> one `antigravity` message,
- thoughts -> verbose status without signatures,
- tool calls -> `tool_call`, `command`, or `file_change`,
- tool results/errors -> correlated tool/error data,
- usage -> verbose status when available.

Replace the string/list fake response with the installed SDK's actual response
protocol. Include a regression test using async-generator properties so the
current `'async_generator' object is not iterable` failure cannot return.

### A5. Complete the verified Claude mapping

The Claude target is sufficiently established to plan concretely, but still run
it through A1 against the pinned wheel.

- Build `ClaudeAgentOptions` with tested values for:
  - `cwd`,
  - `setting_sources=[]`,
  - `system_prompt={"type": "preset", "preset": "claude_code"}`,
  - requested `model` and `permission_mode`.
- Confirm whether the pinned SDK needs an explicit Claude Code tool preset.
- Remove the broad `TypeError` fallback. An incompatible options constructor
  must produce an actionable error rather than silently drop cwd/isolation.
- Map `ToolResultBlock` with `tool_use_id`, content, and `is_error`.
- Map `ResultMessage` usage/cost as verbose status without exposing signatures.
- Keep provider session capture independent of verbosity.

Tests cover text, tool use, tool result success/error, result usage, options,
and session ID using the pinned SDK's real field shapes.

### A6. Prove the correctness slice independently

Before starting Slice B:

- run the complete hermetic suite,
- run the mock smoke with isolated `AGENT_COLLAB_HOME`,
- run imports in the clean SDK environment,
- run production factory/constructor tests against the installed wheels without
  credentials or model calls,
- run credentialed one-turn smokes where credentials are available,
- record anything still blocked by credentials separately from API-shape proof.

Slice A must be reviewable and revertible without Slice B. Do not mix backend
contract refactoring into the same commits as the provider runner repairs.

#### A6 verification record (2026-07-09)

- The full hermetic suite passed under both the system Python 3.9 interpreter
  and `/tmp/agent-collab-sdk-venv` Python 3.12: 337 tests, with only the four
  explicitly gated live-smoke tests skipped.
- The mock smoke passed with `AGENT_COLLAB_HOME` isolated under `/tmp`.
- `pip check`, all three imports, the real Claude and Antigravity constructors,
  and a real ephemeral read-only Codex thread passed without a model call.
- A credentialed Claude turn passed in a fresh empty temporary workspace and
  emitted both an assistant message and a provider session ID.
- A credentialed Codex turn passed in a fresh empty temporary workspace with
  `gpt-5.4`, proving the repaired Python runner's real thread ID and final
  response mapping. The SDK context currently emits upstream `ResourceWarning`
  messages for two subprocess pipe handles during shutdown.
- The project default is now `gpt-5.6-sol`. The latest Python beta
  (`openai-codex==0.1.0b3`) pins Codex `0.137.0-alpha.4`, which is too old for
  that model. The backend therefore intentionally uses the configured local
  Codex executable through the documented `CodexConfig(codex_bin=...)` override
  when it is resolvable. The installed standalone Codex `0.141.0` is also too
  old for `gpt-5.6-sol`; `codex update` discovered version `0.144.0` but failed
  because no matching standalone or npm release assets were available. Re-run
  the Codex live gate after that upstream runtime is downloadable.
- The Antigravity live smoke reached the installed SDK but was blocked before a
  turn because no `GEMINI_API_KEY` is available. Its required tool-call smoke
  remains a credentialed release item.

All live smokes use disposable empty workspaces so they do not expose the
checkout to provider tools. The current official Codex SDK documentation is
<https://developers.openai.com/codex/sdk>; it confirms the Python beta package,
pinned runtime behavior, and the intentional `CodexConfig(codex_bin=...)`
override.

## Slice B: self-describing, swappable backend contract

Start only after Slice A is green for every backend still in Stage 5.1 scope.

### B1. Define the backend-owned option contract

Extend `AgentBackend` with a self-describing contract equivalent to:

```python
class AgentBackend(Protocol):
    id: str
    agent_type: str
    capabilities: BackendCapabilities

    def probe(self) -> BackendHealth: ...
    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]: ...
    def normalize_options(
        self, agent: AgentConfig, requested: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    def settings_summary(
        self, agent: AgentConfig, options: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner: ...
```

`OptionSpec` is declarative data—type, allowed values, numeric constraints, and
whether a value may be inferred. Provider-specific validation code stays out of
the session manager. The generic validator consumes the schema and preserves
the existing field-path error format.

Project configuration may narrow a backend's allowed values but must not expand
the schema to an option the backend cannot honor.

### B2. Move option ownership behind the contract

- Move `BACKEND_OPTION_SUPPORT` data from `options.py` into CLI/SDK backend
  definitions.
- Move CLI argument/default inference into the CLI backend's
  `normalize_options`.
- Ensure SDK backends never inherit a value solely from configured CLI args.
- Resolve backends before validating provider option payloads.
- Validate a provider-wide request against every selected agent of that
  provider. If agents use different backends, the option must be valid for all
  and errors must identify the incompatible agent/backend.
- Treat an incomplete backend contract as a registration error; never bypass
  validation for an unlisted custom backend.

Keep the existing `claude_options`, `codex_options`, and
`antigravity_options` request buckets for API compatibility. They remain
provider namespaces; the resolved backend defines valid contents.

### B3. Make discovery and settings backend-driven

- Add each backend's declarative schema to
  `describe_options.backends.<provider>.entries.<backend>.option_schema`.
- Preserve the top-level provider schema temporarily as a documented union of
  registered backend schemas, not a separate hand-written table.
- Make `build_session_settings` use the same normalized options passed to the
  runner plus the selected backend's `settings_summary`.
- Keep provider session IDs on the shared `provider_session_event` path so the
  daemon remains provider-agnostic.

Built-in registration may remain explicit in `backends/__init__.py`. A new
built-in backend may add one registration import/factory entry, but no
provider/backend branches elsewhere in core code.

### B4. Add backend contract tests

Add a test-only backend with a unique option. Without changing `options.py`, the
daemon, API/MCP code, or another backend, the test must be able to:

- register and discover the backend,
- report its option schema and health,
- validate and normalize its unique option,
- reject an unknown/invalid option with a field path,
- build accurate session settings,
- select and execute its runner,
- unregister without polluting other tests.

Run the generic contract assertions over every built-in backend.

### B5. Re-run all runtime and live checks

The contract refactor is not complete merely because unit tests for schemas
pass. Re-run Slice A's installed-wheel constructor tests and credentialed smokes
to prove option normalization and runner selection still reach the repaired
provider integrations.

#### B5 verification record (2026-07-09)

- The final hermetic suite passes under both Python 3.9 and Python 3.12: 346
  tests, with seven explicitly gated SDK import/constructor/live tests skipped.
- `tests/test_backend_contract.py` registers a test-only Claude backend with a
  unique option and proves discovery, health, validation, normalization,
  settings, runner selection/execution, mixed-backend rejection, and clean
  unregister. It also checks every built-in backend's contract and rejects an
  incomplete registration.
- A mixed Claude workflow proves CLI `--effort` inference reaches only the CLI
  agent and never the SDK agent. Exact per-agent normalized values are carried
  through daemon/referee execution and settings.
- The isolated mock smoke, `pip check`, all SDK imports, and all three installed
  no-model constructor checks pass after the refactor.
- The credentialed Claude turn passes again in a disposable empty workspace.
- The credentialed Codex turn passes again with `gpt-5.4`, including a real
  thread ID and final response, after routing through the configured local
  Codex executable. The requested `gpt-5.6-sol` default remains blocked by the
  unavailable newer Codex runtime asset described in A6.
- Antigravity's live tool-call gate remains blocked by the missing
  `GEMINI_API_KEY`; no repository data was exposed and no tool call was made.

## Shared session identity and capabilities rules

- Retain uniform `provider_session_id` and `provider_session_kind` fields.
- Reject session-identity events whose `agent_id` is not selected or whose
  source/provider does not match the configured agent.
- Keep `resume`, `interrupt`, and `tool_gate` false. Capturing an ID alone does
  not establish any capability.

## Verification commands

Hermetic checks:

```bash
python3 -m unittest discover -s tests
AGENT_COLLAB_HOME=/tmp/agent-collab-smoke ./agent_collab.sh smoke
```

Installed-SDK checks run with the Python >=3.10 virtual environment's `python`,
not the checkout's current Python 3.9 executable:

```bash
python -m pip check
python -m unittest tests.test_sdk_live_smoke.SdkImportSmokeTests -v
```

Run one credentialed turn for each backend still in scope. At least one
Antigravity smoke must exercise a tool call. At least one Codex smoke, if Codex
passes A1, must verify its real thread ID and final response.

Live tests remain skipped by default, but all unblocked provider smokes must be
recorded before moving the task to `tasks_closed`.

## Expected files by slice

### Slice A

- `pyproject.toml`
- `agent_collab/backends/codex_sdk.py`
- `agent_collab/backends/antigravity_sdk.py`
- `agent_collab/backends/claude_sdk.py`
- provider fake/introspection fixtures
- `tests/test_backend_codex_sdk.py`
- `tests/test_backend_sdk.py`
- `tests/test_backend_claude_sdk.py`
- `tests/test_sdk_live_smoke.py`
- Stage 5.1 source facts and package-install documentation

### Slice B

- `agent_collab/backends/base.py`
- `agent_collab/backends/__init__.py`
- `agent_collab/backends/cli.py`
- all SDK backend modules for their option declarations
- `agent_collab/options.py`
- `agent_collab/daemon.py` for session attribution hardening
- `tests/test_backend_contract.py` (new)
- `tests/test_backend_options.py`
- `tests/test_options.py` and affected MCP tests
- backend architecture/configuration documentation

## Acceptance criteria

### Slice A

- Every SDK backend left in Stage 5.1 scope has a recorded distribution,
  import, public signature fixture, and resource-lifetime decision from the
  installed wheel.
- A clean Python >=3.10 environment resolves `pip install -e .` without manual
  dependency installation or `--no-deps`.
- SDK import and production-constructor tests pass in that environment.
- Real provider runners use only installed-and-captured public API shapes.
- Claude preserves the intended coding preset and maps tool results.
- Codex either passes its go/no-go and completes a real turn, or is explicitly
  removed from Stage 5.1 scope with a documented follow-up and revised
  acceptance criteria.
- Antigravity consumes its verified response shape without synchronous-iterator
  errors and maps a real tool call when credentials are available.
- CLI backends remain unchanged.

### Slice B

- A test-only backend with a unique option can be registered, discovered,
  validated, summarized, and executed without changing `options.py`, daemon,
  API/MCP code, or another backend.
- Adding a built-in backend for an existing provider requires only its module,
  tests, and one registration entry.
- `agent_collab_describe_options` reports the effective schema for every
  backend.
- Unsupported options fail before session creation with field paths.
- Session settings show exactly the normalized options passed to the runner.
- The installed-SDK and live checks from Slice A still pass.
- Capability flags remain false.

## Out of scope

- Changing the default backend from `cli` to `sdk`.
- Provider-session resume, mid-turn cancellation, or referee tool approval.
- xAI provider work.
- Dynamic discovery of third-party backend packages through Python entry
  points. The contract should permit this later, but Stage 5.1 only needs
  modular built-in backends.
- Exact event parity with provider CLI JSON/JSONL streams.
