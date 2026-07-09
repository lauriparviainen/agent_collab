# Implementation Notes

Current handoff notes for coding agents. User-facing behavior belongs in
[README.md](../README.md); stable architecture belongs in the focused design
docs linked from [doc/README.md](README.md).

## Runtime Model

One global local daemon owns sessions. Runtime state lives under
`~/.agent-collab/data/`; override with `AGENT_COLLAB_HOME`.

Each session carries a `workdir`. The workdir selects project config from
`WORKDIR/.agent-collab/config.toml` and is also the subprocess cwd.

Config precedence is:

```text
built-in defaults < user config < project config < explicit start options
```

Built-in defaults live in
[agent_collab/default_config.toml](../agent_collab/default_config.toml).
Compatibility handling for old config shapes belongs in
`agent_collab/config_migrations.py`; runtime code consumes the latest schema.

Orchestration is a `workflow`, not a `mode`. Built-ins include `solo-claude`,
`solo-codex`, `cross-review` (default), and `compare`. This repository also
opts into `solo-antigravity` through its small project config.

Sessions persist in `data/session-index.json` across daemon restarts. Sessions
that were running or awaiting input when the daemon died are marked
`interrupted`. Start/status/list responses carry effective `settings` with a
prompt-free `command_preview` per agent.

## Backend Model

An agent's provider `type` (`claude`, `codex`, `antigravity`, `mock`) is
separate from its execution `backend` (`cli` or an in-process `sdk`). SDK
packages install with the project on Python >=3.10; their imports remain lazy.

Each pair lives in `agent_collab/backends/<provider>_<backend>/` with its own
`backend.py`, `options.toml`, optional static `config.toml`, and `README.md`. A single registry list registers
packages by `(agent_type, backend_id)`. Backend resolution order is:

```text
start request > agents.<id>.backend > cli
```

The default remains `cli`. Credentials stay external and provider-managed.

The resolved per-agent backend map is computed once at start validation and
threaded through `RefereeConfig` to the runner construction path. It must reach
execution, not only the returned settings.

Every backend owns a declarative `options.toml`, plus `normalize_options`,
`settings_summary`, `command_preview`, and runner construction. Requests use one
dynamic `backend_options` map keyed by canonical names such as `claude_cli` and
`codex_sdk`; there are no provider-wide option fields or central support table.
Only CLI backends infer values from argv. `describe_options` reports the exact
schema for every registered backend.

Static, non-MCP backend settings live directly under a backend-specific agent
section. Antigravity SDK owns and validates `vertex`, `project`, and `location`
through its colocated `config.toml`; its `[agents.<id>.options]` table contains
only MCP-overridable values such as `model`.

Backend capabilities (`resume`, `interrupt`, `tool_gate`) are honest runtime
facts and are not inferred from provider brand. Live backend health gates starts
on certainty and is reported by `describe_options`, not by daemon status.

Stage 5.1 A1 resolved all SDKs together under Python 3.12.13:

- `claude-agent-sdk==0.2.114`,
- `openai-codex==0.1.0b3` with
  `openai-codex-cli-bin==0.137.0a4`,
- `google-antigravity==0.1.5`.

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
