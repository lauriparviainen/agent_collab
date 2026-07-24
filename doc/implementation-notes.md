# Implementation Notes

Current handoff notes for coding agents. User-facing behavior belongs in
[README.md](../README.md); stable architecture belongs in the focused design
docs linked from [doc/README.md](README.md).

## Runtime Model

One global local daemon owns sessions. Runtime state lives under
`~/.agent-collab/data/`; override with `AGENT_COLLAB_HOME`.

Each session carries a `workdir`. The workdir selects project config from
`WORKDIR/.agent-collab/config.toml` and is the default subprocess cwd, not an
operating-system sandbox. Optional user-global `[workdir].restrict_workdir_roots`
confinement limits which resolved directories sessions may use.

Config precedence is field-specific:

```text
agent execution: built-in defaults < user config < explicit start options
agent names:     built-in defaults < user config < project config
workflows:       built-in defaults < user config < safe project workflows
```

Project agent tables may set only `name`; all execution fields and project-only
agents are ignored. Project workflows may reference only agents already enabled
by built-in or global user config.

General built-in defaults live in
[agent_collab/default_config.toml](../agent_collab/default_config.toml);
backend-specific shipped settings and Event Window targets live in each
backend's `defaults.toml`. `builtin_config()` validates and assembles these
package-data files into one built-in layer before applying user configuration.
Compatibility handling for old config shapes belongs in
`agent_collab/config_migrations.py`; runtime code consumes the latest schema.

Orchestration is a `workflow`, not a `mode`. Built-ins are `solo`,
`cross-review` (default), and the concurrent `dual-review` group; start-time
member selection fills any shape's slots with other enabled agents. Sequential workflows normalize to singleton stages; a flat
`parallel` workflow normalizes to one group of two to four distinct agents.
Additional parallel workflows are global-user-only, while safe project
workflows remain sequential.

Sessions persist in `data/session-index.json` across daemon restarts. Sessions
that were running or awaiting input when the daemon died are marked
`interrupted`. Start/status/list responses carry effective `settings` with a
prompt-free `command_preview` per agent.

Schema 9 adds global `[system]` and `[usage_windows]` policy. The daemon loads
it once, runs no scheduler when all packaged targets remain disabled, and
starts enabled targets through normal visible `usage-window` sessions. Pure
planning and private `data/daemon/usage-window-state.json` provide persisted
jitter, fingerprint invalidation, duplicate guards, and fail-closed bounded
catch-up. The scheduler's isolated workdir exemption is an internal-only
`StartSessionRequest` field absent from the wire DTO.

## Backend Model

An agent's provider `type` (`claude`, `codex`, `antigravity`, `xai`, `mock`) is
separate from its execution `backend` (`cli` or an in-process `sdk`). SDK
packages install with the project on Python >=3.10; their imports remain lazy.

Each pair lives in `agent_collab/backends/<provider>_<backend>/` with its own
`backend.py`, `options.toml`, `defaults.toml`, optional static `config.toml`,
and `README.md`. A single registry list registers packages by
`(agent_type, backend_id)`. Backend resolution order is:

```text
start request > the backend kind encoded in the canonical section name > cli
```

Since config schema 8, an agent's backend kind is fixed by its
`[backends.<canonical>]` section; only mock agents have no backend. Credentials
stay external and provider-managed.

The resolved per-agent backend map is computed once at start validation and
threaded through `RefereeConfig` to the runner construction path. It must reach
execution, not only the returned settings.

Every backend owns declarative `options.toml` and `defaults.toml` files, plus
`normalize_options`, `settings_summary`, `command_preview`, and runner
construction. The option manifest declares accepted keys and values only;
shipped default values live in the backend fragment's
`[backends.<canonical>.options]` table and rank below argv inference and
user-config options (`configured_defaults` in
`normalize_declared_options`). Requests use one dynamic `backend_options` map
keyed by canonical names such as `claude_cli` and `codex_sdk`; there are no
provider-wide option fields or central support table. Only CLI backends infer
values from argv. `describe_options` reports the exact schema for every
registered backend, with the shipped defaults overlaid.

Static, non-MCP backend settings live directly under the backend's
`[backends.<canonical>]` section. Antigravity SDK owns and validates `vertex`,
`project`, and `location` through its colocated `config.toml`; the section's
`options` table contains only MCP-overridable values such as `model`.

All runners implement one sink-plus-return boundary: `run_turn` awaits each
event sink call for streaming backpressure and returns exactly one immutable
`TurnOutcome` after bounded cleanup. Provider parsers retain terminal markers
as private evidence; the runner resolves fatal evidence, transport/parser
failure, process exit, verified success, and an explicitly declared clean-EOF
fallback in that order. The referee assigns `turn-N` identity before launch and
commits each `TurnOutcomeRecord` with its boundary event through one awaited
daemon callback.

New sessions persist a packed append-only `turn_outcomes` list and an optional
canonical `failure`; restored legacy sessions keep both fields null rather than
fabricating history. Event read/wait batches carry the same status, terminal,
error, failure, and outcome view as session detail without changing transcript
cursor semantics. Session terminal transitions are monotonic.

Runners are created once per session and reused across turns, so a backend may
hold provider-side context between them: `conversation_active()` tells the
referee to send a delta continuation prompt (only new events since that agent's
prompt-snapshot watermark) instead of re-sending guardrails, task, and window,
and the referee closes every runner in a bounded, shielded teardown step. Both
default to a stateless no-op, so CLI and mock runners are unchanged.

Backend capabilities (`resume`, `interrupt`, `tool_gate`, `continuity`) are
honest runtime facts and are not inferred from provider brand. `continuity` is
the in-session provider-thread continuation fact; the session reducer reports it
true only when every selected backend has it (false for every backend until the
per-backend #47 stages flip it with proof). Live backend health gates starts on
certainty and is reported by `describe_options`, not by daemon status.

The original Stage 5.1 A1 spike resolved all SDKs together under Python
3.12.13:

- `claude-agent-sdk==0.2.114`,
- `openai-codex==0.1.0b3` with
  `openai-codex-cli-bin==0.137.0a4`,
- `google-antigravity==0.1.5`.
- `xai-sdk==1.17.0` (bounded by the project to `>=1.17,<2`).

The 2026-07-24 Stage 6 refresh leaves the active verified floors at:

- `claude-agent-sdk==0.2.126`,
- `openai-codex==0.144.4`,
- `google-antigravity==0.1.8`,
- `xai-sdk==1.17.0`.

Codex 0.144.4 exposes `AsyncCodex.models()` and xAI 1.17.0 exposes
`AsyncClient.models.list_language_models()`; those are the two SDK dynamic
catalog sources. Claude Agent SDK and Google Antigravity expose model selection
but no public catalog method, so those two backends retain static suggestions.
SDK discovery and turns resolve the same agent-scoped credentials. Catalog
fingerprints incorporate a non-reversible digest of effective agent/process
API keys so account changes invalidate cached SDK catalogs without persisting
secrets. Codex's response also carries `next_cursor`, but its public
`AsyncCodex.models()` method has no cursor argument; such a response is
therefore stored as incomplete and never treated as authoritative.

Codex's installed `AsyncCodex` initialized its bundled app-server and created
and read an ephemeral thread without a model call. Antigravity's installed
`ChatResponse` confirmed async `resolve()` plus independent buffered async
cursors for thoughts/tool calls. Claude's installed options and typed message
blocks confirmed the coding presets, effort/budget fields, tool results, and
result metadata used by the backend.

The built-in Codex model default is `gpt-5.6-sol`. The SDK backend
intentionally uses the agent's configured local `codex` executable through
`CodexConfig(codex_bin=...)` when it resolves on `PATH`; otherwise it falls
back to the SDK-pinned runtime. The backend summary reports which runtime path
is active.

Antigravity is opt-in. Its `cli` path uses `agy` print mode as message-only
plain text. Its `sdk` path targets the installed `google-antigravity` 0.1.8
shapes:

- `Agent`
- `LocalAgentConfig(workspaces=...)`
- `ChatResponse.resolve()` and typed `Text`/`Thought` chunks
- `ToolCall.args`
- `ToolResult`
- `BuiltinTools`
- strict `conversation_id` + `SessionContinuationMode.RESUME` reconnect

The 0.1.8 generated protobuf code requires protobuf 7.35+, which conflicts with
xAI SDK 1.17's protobuf `<7` constraint in one shared environment; the backend
probe reports that state unavailable and the credentialed Antigravity fixture
uses an isolated provider environment. Missing SDK distribution-version
metadata is unavailable too because the runtime compatibility cannot be
verified. The `antigravity` extra pins protobuf 7.35+, while `all` deliberately
omits that conflicting runtime constraint so the shared xAI environment
remains installable and health-gated. The
credentialed two-turn Vertex provider-memory fixture passed with
`gemini-2.5-flash`, a stable native conversation id, and a Stage 3 delta prompt
that omitted the original task and codeword. Strict reconnect also retains one
runner-owned trajectory `save_dir` across resets and removes it on close.
`antigravity_sdk.continuity` is therefore true; restart-safe `resume`,
`interrupt`, and `tool_gate` remain false.

xAI is opt-in. Grok Build 0.2.93 passed a real headless CLI turn and exposed
`thought`, `text`, `end`, and explicit `error` records. A disposable shell-tool
turn emitted no typed action record, so `xai_cli` deliberately maps no tool
events while capturing `end.sessionId` as provider identity kind `session`.
Headless CLI runs default to permission-bypassed execution inside Grok's
read-only sandbox, expose Grok's internal turn limit as `provider_max_turns`,
and emit a terminal reason other than `EndTurn` as a fatal provider error
instead of a successful empty response.
The installed `xai-sdk` 1.17.0 confirmed async client context management,
`chat.create`, `chat.append(user(...))`, `await chat.sample()`, and response
`content`/`id`. `xai_sdk` is remote message-only chat and captures identity kind
`response`; no SDK live call was made because this host has no `XAI_API_KEY`.

## Agent Safety Notes

Do not let supervised agents recursively spawn Claude, Codex, `agent-collab`,
or other agent processes.

Daemon logs should not dump full transcript events by default.

MCP agents should call `agent_collab_guidance` for usage guidance and
`agent_collab_describe_options` with the required workdir before passing
non-default model, reasoning, sandbox, permission, backend, or provider
settings. Invalid `agent_collab_start` options should be fixed from returned
field-path details, not retried by guessing.

Tests must isolate `AGENT_COLLAB_HOME` by pointing it at a temp dir, so nothing
writes to the real `~/.agent-collab`.

Open tasks are indexed in [doc/README.md](README.md).
