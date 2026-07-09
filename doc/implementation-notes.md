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
separate from its execution `backend` (`cli`, or an extras-gated in-process
`sdk` where implemented).

Backends live in `agent_collab/backends/` and are registered by
`(agent_type, backend_id)`. Backend resolution order is:

```text
start request > agents.<id>.backend > cli
```

The base install and default `cli` backend stay standard-library only. SDK
imports must be lazy and gated behind extras.

The resolved per-agent backend map is computed once at start validation and
threaded through `RefereeConfig` to the runner construction path. It must reach
execution, not only the returned settings.

Backend capabilities (`resume`, `interrupt`, `tool_gate`) are honest runtime
facts and are not inferred from provider brand. Live backend health gates starts
on certainty and is reported by `describe_options`, not by daemon status.

Antigravity is opt-in. Its `cli` path uses `agy` print mode as message-only
plain text. Its `sdk` path is implemented against the live-confirmed
`google-antigravity` 0.1.5 shapes:

- `Agent`
- `LocalAgentConfig(workspaces=...)`
- `ChatResponse`
- `ToolCall.args`
- `BuiltinTools`

Only a live SDK chat remains unexercised because it needs `GEMINI_API_KEY`,
which agent-collab does not manage. The mapper is tested with a fake module
built to the confirmed shapes.

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
