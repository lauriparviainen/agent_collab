# Stage 5.1: First-class SDK backends

**Status: Complete — closed 2026-07-10.**

## Purpose

Make `sdk` a first-class, installed backend for all supported real providers:

- Claude via the Claude Agent SDK,
- Codex via the Codex Python SDK,
- Antigravity via the Google Antigravity SDK.

The current Stage 4.9 backend registry already separates provider `type`
(`claude`, `codex`, `antigravity`, `mock`) from execution `backend` (`cli`,
`sdk`). This stage should finish that design by making SDK dependencies install
with the project, adding real SDK runners for Claude and Codex, and refreshing
Antigravity SDK support.

Adding `xai` as a fourth real provider is split into its own focused sub-task,
[Stage 5.1.1](../tasks_open/stage-5.1.1-xai-provider.md), and should land after
this stage.
xAI adds a new provider `type` — a wider, riskier fan-out than adding a second
backend to an existing type — so it is kept separate to keep this stage focused.

This intentionally changes the Stage 4.9 packaging decision. Stage 4.9 kept the
base install standard-library only and made Antigravity SDK optional. This stage
makes SDK support part of the project install. CLI backends remain supported, but
the default installed project should have the SDK modules available without
extra install commands.

## Completed state

Implemented and verified:

- `cli` and installed-by-default `sdk` backends for `claude`, `codex`, and
  `antigravity`, with lazy SDK imports,
- backend registry keyed by `(agent_type, backend_id)` and start-time resolution
  `start request > agents.<id>.backend > "cli"`,
- backend-owned declarative option schemas, normalization, settings summaries,
  and per-agent normalized option flow,
- per-backend discovery schemas, health, honest all-false capabilities, and
  validated provider-session capture,
- real-wheel constructor checks plus credentialed Claude and Codex turns in
  disposable workspaces.

Known external follow-up evidence:

- re-run the requested `gpt-5.6-sol` Codex default after a sufficiently new
  standalone Codex release asset is downloadable,
- run the Antigravity tool-call smoke on a glibc 2.36+ host or after Google
  publishes an EL9-compatible `localharness`. ADC and Vertex configuration are
  already valid; Oracle Linux 9's glibc 2.34 blocks the bundled binary before a
  turn begins.

See the [remediation verification records](stage-5.1-first-class-sdk-backends-remediation.md)
for installed API shapes and exact blocked/live evidence. They supersede any
candidate call shapes in the original provider plans below.

## SDK source facts to verify during implementation

Implementation must refresh these docs before coding because these SDKs and CLIs
are young and may change:

- Claude: use `claude-agent-sdk`, not deprecated `claude-code-sdk`.
  Official docs say the Agent SDK is programmable in Python/TypeScript, install
  with `pip install claude-agent-sdk`, and requires Python 3.10+.
  Migration docs say `claude-code-sdk` was renamed to `claude-agent-sdk`.
  References:
  - https://code.claude.com/docs/en/agent-sdk/overview
  - https://code.claude.com/docs/en/agent-sdk/migration-guide
- Codex: use `openai-codex`.
  Official Codex SDK docs say the Python SDK controls the local Codex app-server
  over JSON-RPC, requires Python 3.10+, and published builds include a pinned
  Codex CLI runtime dependency.
  Reference:
  - https://developers.openai.com/codex/sdk
- Antigravity: use `google-antigravity`.
  The SDK repo says the package must be installed from PyPI because platform
  wheels include a compiled runtime binary; the SDK exposes `Agent`,
  `LocalAgentConfig`, `ChatResponse`, streaming responses, thoughts, and tool
  calls.
  References:
  - https://github.com/google-antigravity/antigravity-sdk-python
  - https://antigravity.google/product/antigravity-sdk
- xAI (CLI `grok` + `xai-sdk`): out of scope here; see
  [Stage 5.1.1](../tasks_open/stage-5.1.1-xai-provider.md) for the verified `grok 0.2.93`
  flags, `streaming-json` event shapes, and SDK usage.

## Packaging plan

Change `pyproject.toml` so SDK packages install with the project:

```toml
[project]
requires-python = ">=3.10"
dependencies = [
  "claude-agent-sdk>=0.2.114,<0.3.0",
  "openai-codex>=0.1.0b3,<0.2.0",
  "google-antigravity>=0.1.5,<0.2.0",
  # xai-sdk is added by Stage 5.1.1 (xAI provider).
]
```

These ranges were resolved together in a clean Python 3.12.13 environment on
2026-07-09. The Codex wheel installs its pinned
`openai-codex-cli-bin==0.137.0a4` runtime dependency. See the
[remediation verification record](stage-5.1-first-class-sdk-backends-remediation.md#a1-verification-record-2026-07-09).

Remove or repurpose `[project.optional-dependencies].antigravity-sdk`; after this
stage it should not be required for normal project installs. If a future
`cli-only` install profile is needed, add it deliberately later rather than
keeping SDK support half-installed.

Bumping `requires-python` to `>=3.10` is expected. Claude Agent SDK, Codex SDK,
and the existing Antigravity SDK spike all require or assume Python 3.10+.

Credentials are still not installed or managed by `agent-collab`:

- Claude SDK authentication remains external, such as `ANTHROPIC_API_KEY` or
  provider-supported local auth.
- Codex SDK authentication remains external, through Codex's supported local or
  API-key auth paths.
- Antigravity SDK authentication remains external, such as `GEMINI_API_KEY` or
  Vertex/ADC configuration.

## Architecture plan

Keep the backend registry shape:

```text
(claude, cli)
(claude, sdk)
(codex, cli)
(codex, sdk)
(antigravity, cli)
(antigravity, sdk)
```

Add one backend module per SDK:

```text
agent_collab/backends/claude_sdk.py
agent_collab/backends/codex_sdk.py
agent_collab/backends/antigravity_sdk.py
```

`antigravity_sdk.py` already exists. This stage should keep it, refresh it
against current SDK docs, and remove the extras-gated language.

Backend registration must not rely on a bare per-module `build_sdk_backends`
factory name. Today `backends/__init__.py` does
`from .antigravity_sdk import build_sdk_backends`; the moment a second SDK module
exports the same symbol, the imports shadow each other. Because this stage adds
`claude_sdk.py` and `codex_sdk.py` alongside the existing
`antigravity_sdk.py`, the collision lands **in Stage 5.1**, not later. Resolve it
here by either:

- giving each module a distinct factory name
  (`build_claude_sdk_backends`, `build_codex_sdk_backends`,
  `build_antigravity_sdk_backends`), or
- importing the module namespaces (`from . import claude_sdk` and calling
  `claude_sdk.build_sdk_backends()`).

Pick one convention and apply it to all SDK modules so Stage 5.1.1 (xAI) only has
to follow the pattern.

Each SDK backend must implement:

- `probe()` with no model call,
- `option_schema()` and `normalize_options()` for backend-owned validation,
- `create_runner()` returning an `AgentRunner`,
- `settings_summary()` with package/backend details alongside the normalized
  settings used by execution,
- explicit option mapping,
- event mapping into the existing `Event` contract,
- fake-module tests that exercise event mapping without provider credentials.

Do not pass unknown option keys through blindly. Unknown or unsupported SDK
options should be rejected during start validation with field paths.

When `xai` lands (Stage 5.1.1) it adds `(xai, cli)` + `(xai, sdk)` and a fourth
SDK module; it simply follows the non-shadowing factory convention established
above, so no rename is deferred to that stage.

## Provider plans

### Claude SDK

Use `claude_agent_sdk`.

Initial mapping targets:

- `claude_options.model` -> `ClaudeAgentOptions(model=...)`,
- `claude_options.permission_mode` -> SDK permission mode if confirmed,
- `claude_options.thinking_level` -> SDK option only if the API exposes an
  equivalent,
- session cwd/workdir -> SDK option if confirmed,
- Claude Code preset/system prompt behavior -> explicitly choose the coding
  preset if needed to preserve current Claude Code semantics.

Event mapping should cover:

- assistant text messages -> `source="claude", type="message"`,
- tool use / tool result messages -> `source="tool"` with `type` of `tool_call`,
  `command`, or `file_change` where the SDK exposes enough structure,
- errors -> `source="error", type="error"`,
- usage/cost metadata as verbose `status` events if available.

Open design point:

- Decide whether Claude SDK should load user/project settings by default.
  The migration docs say current behavior loads filesystem settings unless
  `settingSources` is overridden. For `agent-collab`, this should be explicit in
  the backend summary so runs are predictable.

### Codex SDK

Use `openai_codex`.

Initial mapping targets:

- `codex_options.model` -> `thread_start(model=...)`,
- `codex_options.sandbox` -> `Sandbox.read_only`,
  `Sandbox.workspace_write`, or `Sandbox.full_access`,
- `codex_options.reasoning_effort` / `thinking_level` -> SDK option only if
  confirmed,
- `codex_options.profile` and `approval_policy` -> SDK option only if confirmed.

Event mapping should cover:

- final response -> `source="codex", type="message"`,
- SDK item/thread/turn events if exposed -> existing event types,
- command execution -> `source="tool", type="command"`,
- file edits -> `source="tool", type="file_change"`,
- failures -> `source="error", type="error"`.

If the Python SDK only returns final responses for the stable API, ship a
message-only SDK backend first and mark richer event streaming as a follow-up.
Do not fake JSONL parity with `codex exec --json`.

### Antigravity SDK

Refresh the existing `antigravity` SDK backend.

Keep the current confirmed mapping if still valid:

- `google.antigravity.Agent`,
- `LocalAgentConfig(workspaces=[...], model=...)`,
- `await agent.chat(prompt)`,
- `await response.text()`,
- `response.thoughts`,
- `response.tool_calls`,
- `agent.conversation_id`.

Update if current docs require the newer streaming shape:

- `async for token in response`,
- `async for thought in response.thoughts`,
- `async for call in response.tool_calls`.

`antigravity_options.mode` remains CLI-only unless the SDK exposes a faithful
equivalent. If SDK policies/capabilities replace `mode`, add a new SDK-specific
validated option rather than reusing `mode` loosely.

## Session identity and capabilities

The current capabilities are honest all-false facts. This stage should flip
capabilities only when the runtime behavior is implemented and tested.

Add a structured place in session state for provider session identifiers:

Use one uniform key, `provider_session_id`, across providers so persisted session
state has a single stable schema. Keep the provider's own term (thread id,
conversation id) in a sibling `provider_session_kind` field rather than encoding
it in the key name:

```json
{
  "agent_sessions": {
    "claude": {
      "backend": "sdk",
      "provider_session_id": "...",
      "provider_session_kind": "session"
    },
    "codex": {
      "backend": "sdk",
      "provider_session_id": "...",
      "provider_session_kind": "thread"
    },
    "antigravity": {
      "backend": "sdk",
      "provider_session_id": "...",
      "provider_session_kind": "conversation"
    }
  }
}
```

Rules:

- `resume=true` only after `agent-collab` can continue a prior provider session
  from persisted central session state.
- `interrupt=true` only if the SDK exposes reliable mid-turn cancellation and
  the daemon stop path actually calls it.
- `tool_gate=true` only if `agent-collab` can programmatically approve/deny SDK
  tool calls through the existing session/referee path.
- Do not infer capabilities from provider brand or SDK marketing.

It is acceptable for Stage 5.1 to ship SDK execution with all capabilities still
false if resume/interrupt/tool-gating are not complete.

## Config and UX plan

Keep `cli` as the default backend until live SDK behavior is verified across all
providers. Then decide whether to switch defaults per provider.

Config examples:

```toml
[agents.claude]
type = "claude"
backend = "sdk"

[agents.codex]
type = "codex"
backend = "sdk"

[agents.antigravity]
type = "antigravity"
backend = "sdk"
```

(`enabled` defaults to true; set `enabled = false` to keep a provider defined but
out of workflows.)

`agent_collab_describe_options` must show:

- SDK backend availability,
- installed package version,
- credential status when safely knowable,
- capability flags,
- effective SDK option schema,
- clear errors for unavailable/misconfigured SDKs.

CLI/MCP start validation must reject:

- `--backend sdk` for workflows containing a provider whose SDK backend is not
  registered,
- CLI-only options used with SDK backends,
- SDK-only options used with CLI backends,
- option values outside configured allowed sets.

## Implementation steps

1. Refresh provider docs and lock confirmed package/API versions in the task
   implementation notes.
2. Update `pyproject.toml` dependencies and Python requirement.
3. Adjust packaging docs to say SDKs install with the project and credentials
   remain external.
4. Add shared SDK probe helpers where useful, but keep provider-specific API
   mapping in provider modules.
5. Add `ClaudeSdkBackend` with fake-module tests.
6. Add `CodexSdkBackend` with fake-module tests.
7. Refresh `AntigravitySdkBackend` for installed-by-default packaging and
   current SDK streaming/API shapes.
8. Add or update option validation for backend-specific option support.
9. Add provider session-id capture fields without claiming resume unless resume
   is implemented end to end.
10. Update `describe_options`, status, list, and session settings snapshots.
11. Add live smoke commands guarded by env vars and skipped by default.
12. Run full unit tests and at least one live smoke for each SDK on a credentialed
    development machine before closing the task.

Adding the `xai` provider is tracked separately in
[Stage 5.1.1](../tasks_open/stage-5.1.1-xai-provider.md).

## Tests

Unit tests:

- registry registers all six real provider/backend pairs,
- missing SDK imports are reported by `probe()` without crashing imports,
- installed package versions appear in backend summaries when available,
- option mapping for Claude SDK,
- option mapping for Codex SDK,
- refreshed option mapping for Antigravity SDK,
- CLI-only and SDK-only option rejection paths,
- fake SDK message mapping,
- fake SDK tool/command/file-change mapping,
- provider session-id capture,
- capability reducer remains honest,
- MCP `describe_options` includes SDK availability and schemas.

Integration tests:

- `python3 -m unittest discover -s tests`,
- `./agent_collab.sh smoke`,
- SDK import smoke in the project environment,
- live Claude SDK one-turn smoke when `ANTHROPIC_API_KEY` or supported auth is
  present,
- live Codex SDK one-turn smoke when supported Codex auth is present,
- live Antigravity SDK one-turn smoke when `GEMINI_API_KEY` or Vertex ADC is
  present.

Live tests must not run by default in CI or normal local unit test runs.

## Acceptance criteria

- A normal project install installs `claude-agent-sdk`, `openai-codex`, and
  `google-antigravity`.
- `agent_collab_describe_options` reports `sdk` for `claude`, `codex`, and
  `antigravity`.
- `agent_collab_start(..., backend="sdk")` works for `solo-claude`,
  `solo-codex`, and `solo-antigravity` when credentials are available.
- Mixed workflows can use SDK backends only when every selected real provider has
  an available SDK backend.
- CLI backends still work unchanged.
- Unsupported options fail before session creation with field-path details.
- Session settings accurately show which backend ran and what options were
  applied.
- Capability flags remain false unless the corresponding runtime behavior is
  actually implemented.

## Risks and follow-ups

- Installing all SDKs by default drops Python 3.9 support. That is acceptable
  only if the project chooses first-class SDK support over the old standard
  library base install.
- SDK package APIs are young and may break across minor releases; keep tests
  fake-module based and pin tested versions.
- Claude, Codex, and Antigravity do not share the same auth model. Health
  checks should be helpful but must not require model calls.
- SDK event streams may not match CLI JSON streams one-to-one. Preserve the
  `agent-collab` event contract and be honest about fidelity in backend
  summaries.
- Resume, interrupt, and tool-gating are separate runtime features. Do not make
  them implicit side effects of adding SDK execution.
- xAI is added separately in [Stage 5.1.1](../tasks_open/stage-5.1.1-xai-provider.md); see that
  task for its provider-specific risks (new `type` fan-out with two silent-fail
  lists, Grok-CLI-vs-SDK asymmetry, unobserved tool-event shapes, ACP follow-up).
