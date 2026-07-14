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

Built-in defaults live in
[agent_collab/default_config.toml](../agent_collab/default_config.toml).
Compatibility handling for old config shapes belongs in
`agent_collab/config_migrations.py`; runtime code consumes the latest schema.

Orchestration is a `workflow`, not a `mode`. Built-ins include `solo-claude`,
`solo-codex`, `cross-review` (default), and the concurrent
`dual-review` group. Sequential workflows normalize to singleton stages; a flat
`parallel` workflow normalizes to one group of two to four distinct agents.
Additional parallel workflows are global-user-only, while safe project
workflows remain sequential.

Sessions persist in `data/session-index.json` across daemon restarts. Sessions
that were running or awaiting input when the daemon died are marked
`interrupted`. Start/status/list responses carry effective `settings` with a
prompt-free `command_preview` per agent.

## Backend Model

An agent's provider `type` (`claude`, `codex`, `antigravity`, `xai`, `mock`) is
separate from its execution `backend` (`cli` or an in-process `sdk`). SDK
packages install with the project on Python >=3.10; their imports remain lazy.

Each pair lives in `agent_collab/backends/<provider>_<backend>/` with its own
`backend.py`, `options.toml`, optional static `config.toml`, and `README.md`. A single registry list registers
packages by `(agent_type, backend_id)`. Backend resolution order is:

```text
start request > the backend kind encoded in the canonical section name > cli
```

Since config schema 8, an agent's backend kind is fixed by its
`[backends.<canonical>]` section; only mock agents have no backend. Credentials
stay external and provider-managed.

The resolved per-agent backend map is computed once at start validation and
threaded through `RefereeConfig` to the runner construction path. It must reach
execution, not only the returned settings.

Every backend owns a declarative `options.toml`, plus `normalize_options`,
`settings_summary`, `command_preview`, and runner construction. Requests use one
dynamic `backend_options` map keyed by canonical names such as `claude_cli` and
`codex_sdk`; there are no provider-wide option fields or central support table.
Only CLI backends infer values from argv. `describe_options` reports the exact
schema for every registered backend.

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

Backend capabilities (`resume`, `interrupt`, `tool_gate`) are honest runtime
facts and are not inferred from provider brand. Live backend health gates starts
on certainty and is reported by `describe_options`, not by daemon status.

Stage 5.1 A1 resolved all SDKs together under Python 3.12.13:

- `claude-agent-sdk==0.2.114`,
- `openai-codex==0.1.0b3` with
  `openai-codex-cli-bin==0.137.0a4`,
- `google-antigravity==0.1.5`.
- `xai-sdk==1.17.0` (bounded by the project to `>=1.17,<2`).

Codex's installed `AsyncCodex` initialized its bundled app-server and created
and read an ephemeral thread without a model call. Antigravity's installed
`ChatResponse` confirmed async `resolve()` plus independent buffered async
cursors for thoughts/tool calls. Claude's installed options and typed message
blocks confirmed the coding presets, effort/budget fields, tool results, and
result metadata used by the backend.

The built-in Codex model default is `gpt-5.6-sol`. The latest Python beta pins
an older Codex runtime, so the SDK backend intentionally uses the agent's
configured local `codex` executable through `CodexConfig(codex_bin=...)` when
it resolves on `PATH`; otherwise it falls back to the SDK-pinned runtime. The
backend summary reports which runtime path is active. On 2026-07-09 the local
standalone `0.141.0` was still too old for `gpt-5.6-sol`, while `codex update`
found `0.144.0` but no downloadable release asset, so the 5.6 live gate remains
an upstream-runtime release check.

Antigravity is opt-in. Its `cli` path uses `agy` print mode as message-only
plain text. Its `sdk` path targets the installed `google-antigravity` 0.1.5
shapes:

- `Agent`
- `LocalAgentConfig(workspaces=...)`
- `ChatResponse.resolve()` and typed `Text`/`Thought` chunks
- `ToolCall.args`
- `ToolResult`
- `BuiltinTools`

Credentialed turns remain separate live-smoke evidence. The local environment
currently has no `GEMINI_API_KEY`, so Antigravity's no-model API verification
does not claim a successful chat.

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
