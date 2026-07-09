# Stage 5.1: First-class SDK backends and xAI provider

## Purpose

Make `sdk` a first-class, installed backend for all supported real providers,
and add xAI as a real provider with both CLI and SDK execution:

- Claude via the Claude Agent SDK,
- Codex via the Codex Python SDK,
- Antigravity via the Google Antigravity SDK,
- xAI via the Grok Build CLI and the xAI Python SDK.

The current Stage 4.9 backend registry already separates provider `type`
(`claude`, `codex`, `antigravity`, `mock`) from execution `backend` (`cli`,
`sdk`). This stage should finish that design by making SDK dependencies install
with the project, adding real SDK runners for Claude and Codex, refreshing
Antigravity SDK support, and adding `xai` as a fourth real provider.

This intentionally changes the Stage 4.9 packaging decision. Stage 4.9 kept the
base install standard-library only and made Antigravity SDK optional. This stage
makes SDK support part of the project install. CLI backends remain supported, but
the default installed project should have the SDK modules available without
extra install commands.

## Current state

Implemented:

- `cli` backend for `claude`, `codex`, and `antigravity`.
- `sdk` backend for `antigravity`, registered lazily and extras-gated.
- backend registry keyed by `(agent_type, backend_id)`.
- start-time backend resolution:
  `start request > agents.<id>.backend > "cli"`.
- backend health surface in `agent_collab_describe_options`.
- conservative capability flags, currently all false.

Missing:

- installed-by-default SDK dependencies,
- `claude` SDK backend,
- `codex` SDK backend,
- `xai` provider type,
- `xai` CLI backend for the installed `grok` command,
- `xai` SDK backend,
- common SDK session-id capture and resume plumbing,
- live smoke coverage for installed SDKs,
- docs that explain SDK-vs-CLI tradeoffs now that SDKs are first-class.

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
- xAI CLI: use the installed `grok` command from Grok Build.
  Local verification on this machine shows `grok 0.2.93` installed at
  `/home/devel/.grok/bin/grok`. Official docs say Grok Build is a coding agent
  usable as an interactive TUI, in headless scripts, or through Agent Client
  Protocol (ACP). Headless mode supports `-p, --single <PROMPT>`, `--cwd`,
  `--model`, `--permission-mode`, `--sandbox`, and `--output-format` with
  `plain`, `json`, or `streaming-json`; `streaming-json` emits newline-delimited
  events. ACP is available as `grok agent stdio` over JSON-RPC.
  References:
  - https://docs.x.ai/build/overview
  - https://docs.x.ai/build/cli/headless-scripting
- xAI SDK: use `xai-sdk`, imported as `xai_sdk`, not the unrelated `xai`
  explainability package. Official docs say it is the xAI Python SDK, gRPC-based,
  supports sync and async clients, requires Python 3.10+, reads `XAI_API_KEY`,
  and can be used as:
  `from xai_sdk import Client`; `client.chat.create(model="grok-4.5")`;
  append `xai_sdk.chat.user(...)`; call `chat.sample().content`.
  xAI's REST API is also OpenAI/Anthropic-compatible, but this backend should use
  the native Python SDK first because this stage explicitly wants SDK support in
  Python.
  References:
  - https://docs.x.ai/overview
  - https://docs.x.ai/developers/model-capabilities/text/generate-text
  - https://github.com/xai-org/xai-sdk-python
  - https://pypi.org/project/xai-sdk/

## Packaging plan

Change `pyproject.toml` so SDK packages install with the project:

```toml
[project]
requires-python = ">=3.10"
dependencies = [
  "claude-agent-sdk>=0.2,<1",
  "openai-codex>=0,<1",
  "google-antigravity>=0.1,<1",
  "xai-sdk>=1.17,<2",
]
```

Implementation should confirm current compatible version ranges before landing.
Do not leave the dependency set as unbounded latest-only imports. Some of these
packages are pre-1.0 or recently renamed, so use constraints based on the
versions tested in this stage.

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
- xAI SDK and Grok CLI authentication remains external, such as `XAI_API_KEY` or
  the local `grok login` flow.

## Architecture plan

Keep the backend registry shape:

```text
(claude, cli)
(claude, sdk)
(codex, cli)
(codex, sdk)
(antigravity, cli)
(antigravity, sdk)
(xai, cli)
(xai, sdk)
```

Add one backend module per SDK:

```text
agent_collab/backends/claude_sdk.py
agent_collab/backends/codex_sdk.py
agent_collab/backends/antigravity_sdk.py
agent_collab/backends/xai_sdk.py
```

`antigravity_sdk.py` already exists. This stage should keep it, refresh it
against current SDK docs, and remove the extras-gated language.

The xAI CLI backend can live in the existing `agent_collab/backends/cli.py`
registry, but it needs an xAI-specific event parser for Grok Build's
`streaming-json` output.

Each SDK backend must implement:

- `probe()` with no model call,
- `create_runner()` returning an `AgentRunner`,
- `settings_summary()` with package/backend details instead of a CLI
  `command_preview`,
- explicit option mapping,
- event mapping into the existing `Event` contract,
- fake-module tests that exercise event mapping without provider credentials.

Do not pass unknown option keys through blindly. Unknown or unsupported SDK
options should be rejected during start validation with field paths.

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
- tool use / tool result messages -> `tool_call`, `command`, or `file_change`
  where the SDK exposes enough structure,
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

### xAI / Grok

Add a new provider type:

```text
xai
```

Use `xai` as the provider type, not `grok`. `grok` is the installed CLI command
and the model family/product name; keeping provider type `xai` leaves room for
model names like `grok-4.5` without mixing naming layers.

Initial built-in agent and workflow:

```toml
[agents.xai]
type = "xai"
command = "grok"
args = ["--output-format", "streaming-json", "-p"]
enabled = false

[workflows.solo-xai]
sequence = ["xai"]
```

The provider should be disabled by default unless the implementation decides the
installed `grok` CLI and credentials can be gated as reliably as Antigravity.

#### xAI CLI backend

Use the installed `grok` command.

Initial mapping targets:

- `xai_options.model` -> `grok --model`,
- `xai_options.permission_mode` -> `grok --permission-mode`,
- `xai_options.sandbox` -> `grok --sandbox`,
- `xai_options.reasoning_effort` / `thinking_level` -> `grok --reasoning-effort`,
- workdir -> subprocess cwd and/or `grok --cwd`,
- output format -> `streaming-json` by default for structured parsing.

Important argument-order rule:

- `grok -p <prompt>` accepts the prompt as an argument. Confirm whether flags
  after `-p` are parsed as flags or prompt text before implementing option
  insertion. If ambiguous, keep all mapped flags before `-p`, as with the fixed
  Antigravity CLI ordering.

Event mapping should prefer `--output-format streaming-json`:

- assistant text chunks/messages -> `source="xai", type="message"`,
- tool calls / shell commands -> `source="tool", type="tool_call"` or
  `type="command"`,
- file edits -> `source="tool", type="file_change"`,
- failures -> `source="error", type="error"`,
- session IDs / completion metadata -> verbose `status` events and/or captured
  provider session state.

If `streaming-json` schemas are unstable, add fixtures captured from local
`grok --output-format streaming-json` runs and keep the parser tolerant. Do not
fall back to parsing human text unless `streaming-json` is unavailable.

Also evaluate `grok agent stdio` (ACP) as a future richer transport. ACP returns
JSON-RPC `session/update` chunks and may be a better fit for continuation,
tool-gating, and IDE-style integration than one-shot headless mode. Do not make
ACP the initial backend unless its protocol can be tested deterministically.

#### xAI SDK backend

Use `xai_sdk`.

Initial mapping targets:

- `xai_options.model` -> `client.chat.create(model=...)`,
- `xai_options.timeout` -> `Client(..., timeout=...)` if confirmed,
- `xai_options.reasoning_effort` / `thinking_level` -> SDK option only if
  confirmed,
- system prompt / base instructions -> SDK chat `system(...)` message if needed,
- prompt -> SDK chat `user(...)` message.

Event mapping starts with:

- `chat.sample().content` -> `source="xai", type="message"`,
- response id / metadata -> verbose `status` and provider session state if
  exposed,
- errors -> `source="error", type="error"`.

The SDK is an API client, not necessarily a local coding-agent runtime like
Grok Build CLI. Unless the SDK exposes local tools or client-side tool pause /
resume in a way `agent-collab` can execute, the xAI SDK backend should be
message-only at first. Do not imply file-edit parity with the `grok` CLI.

## Session identity and capabilities

The current capabilities are honest all-false facts. This stage should flip
capabilities only when the runtime behavior is implemented and tested.

Add a structured place in session state for provider session identifiers:

```json
{
  "agent_sessions": {
    "claude": {
      "backend": "sdk",
      "provider_session_id": "..."
    },
    "codex": {
      "backend": "sdk",
      "provider_thread_id": "..."
    },
    "antigravity": {
      "backend": "sdk",
      "conversation_id": "..."
    },
    "xai": {
      "backend": "sdk",
      "provider_response_id": "..."
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
enabled = true

[agents.xai]
type = "xai"
command = "grok"
backend = "cli"
enabled = true
```

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
8. Add `xai` provider support:
   - config type validation,
   - Grok CLI backend registration and `streaming-json` parser,
   - `XaiSdkBackend` with fake-module tests,
   - `solo-xai` workflow and docs.
9. Add or update option validation for backend-specific option support.
10. Add provider session-id capture fields without claiming resume unless resume
   is implemented end to end.
11. Update `describe_options`, status, list, and session settings snapshots.
12. Add live smoke commands guarded by env vars and skipped by default.
13. Run full unit tests and at least one live smoke for each SDK on a credentialed
    development machine before closing the task.

## Tests

Unit tests:

- registry registers all eight real provider/backend pairs,
- missing SDK imports are reported by `probe()` without crashing imports,
- installed package versions appear in backend summaries when available,
- option mapping for Claude SDK,
- option mapping for Codex SDK,
- refreshed option mapping for Antigravity SDK,
- xAI Grok CLI `streaming-json` parser fixtures,
- xAI CLI option mapping and argument order,
- xAI SDK fake-module message mapping,
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
  present,
- live xAI CLI one-turn smoke when `grok` is installed and authenticated or
  `XAI_API_KEY` is present,
- live xAI SDK one-turn smoke when `XAI_API_KEY` is present.

Live tests must not run by default in CI or normal local unit test runs.

## Acceptance criteria

- A normal project install installs `claude-agent-sdk`, `openai-codex`,
  `google-antigravity`, and `xai-sdk`.
- `agent_collab_describe_options` reports `sdk` for `claude`, `codex`, and
  `antigravity`, and reports both `cli` and `sdk` for `xai`.
- `agent_collab_start(..., backend="sdk")` works for `solo-claude`,
  `solo-codex`, `solo-antigravity`, and `solo-xai` when credentials are
  available.
- `agent_collab_start(..., workflow="solo-xai")` works with the Grok CLI backend
  when `grok` is installed and authenticated.
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
- Claude, Codex, Antigravity, and xAI do not share the same auth model. Health
  checks should be helpful but must not require model calls.
- xAI adds two different execution personalities: Grok Build CLI is a local
  coding agent with tools, while `xai-sdk` is a Python API client. Treat SDK
  message-only behavior as acceptable until local tool execution is implemented.
- Grok CLI exposes both headless `streaming-json` and ACP. Choose one initial
  transport deliberately and keep the other as a follow-up if it would require
  a different session lifecycle.
- SDK event streams may not match CLI JSON streams one-to-one. Preserve the
  `agent-collab` event contract and be honest about fidelity in backend
  summaries.
- Resume, interrupt, and tool-gating are separate runtime features. Do not make
  them implicit side effects of adding SDK execution.
