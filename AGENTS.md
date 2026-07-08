# AGENTS.md

Short entrypoint for coding agents working in this repository.

## What This Is

`agent-collab` is a local CLI/daemon for supervising collaboration between Claude Code and Codex. The server owns sessions, starts agent subprocesses, writes transcripts, and exposes CLI plus MCP access to the same live session state.

## Start Here

- [README.md](README.md): user-facing overview and common commands.
- [doc/README.md](doc/README.md): design-doc index.
- [doc/daemon-architecture.md](doc/daemon-architecture.md): server, session, CLI, and MCP architecture.
- [doc/agent-configuration.md](doc/agent-configuration.md): agent config, workflows, and typed start options.
- [doc/runtime-layout.md](doc/runtime-layout.md): global runtime layout and config precedence.
- [doc/mcp-guidance.md](doc/mcp-guidance.md): guidance served to MCP agents by `agent_collab_guidance`.
- [doc/development.md](doc/development.md): local commands, smoke tests, and coding constraints.

## Architecture Snapshot

Stage 4.8 landed the global runtime model:

- one global local daemon; runtime state lives under `~/.agent-collab/data/` (override with `AGENT_COLLAB_HOME`, which tests must always set),
- each session carries a `workdir` that selects project config and subprocess cwd,
- config precedence: built-ins < user config < project config (from the session workdir) < explicit start options, with centralized schema migrations in `agent_collab/config_migrations.py`,
- orchestration is a `workflow` (`single-claude`, `single-codex`, `cross-review` default, `compare`), not a `mode`,
- sessions persist in `data/session-index.json` across daemon restarts; interrupted sessions get status `interrupted`,
- start/status/list responses carry effective `settings` with prompt-free `command_preview` per agent.

Stage 4.9 added pluggable agent backends:

- an agent's provider (`type`: `claude`, `codex`, `antigravity`, `mock`) is separate from its execution `backend` (`cli` subprocess, or the extras-gated in-process `sdk`); backends live in `agent_collab/backends/` in a registry keyed by `(agent_type, backend_id)` with resolution `start request > agents.<id>.backend > default "cli"`,
- the base install and default `cli` backend stay standard-library only; all SDK imports are lazy and behind the `antigravity-sdk` extra,
- the resolved per-agent backend map is computed once at start validation and threaded into `RefereeConfig` → `Referee._runners()` → `configured_runner` (it must reach execution, not only the start settings), reusing the start-time config snapshot,
- backend capabilities (`resume`/`interrupt`/`tool_gate`) are all `false` this stage and are honest runtime facts, never inferred from provider brand; live backend health gates starts on certainty and is reported in `describe_options` (not `daemon status`),
- Antigravity is disabled by default and opt-in; its `cli` path is message-only (agy print mode is plain text) and its `sdk` path is hypothesis-driven against a fake module because the SDK could not be captured live (see the closed stage-4.9 doc).

Open tasks are indexed in [doc/README.md](doc/README.md).

## Essential Commands

```bash
python3 -m unittest discover -s tests
./agent_collab.sh smoke
./agent_collab.sh daemon start
./agent_collab.sh daemon status
./agent_collab.sh daemon logs --tail 100
```

Use mock mode before any live Claude/Codex smoke. Real Claude/Codex runs can need unsandboxed credentials and may cost money.

## Non-Negotiables

- Prefer Python standard library and keep dependencies minimal.
- Do not let agents recursively spawn Claude, Codex, `agent-collab`, or other agent processes.
- Preserve cursor-based event reads and long-polling.
- Keep plain `watch` pipe-friendly; TUI is additive.
- Do not dump full transcript events into daemon logs by default.
- MCP agents should call `agent_collab_describe_options` before passing non-default model, reasoning, sandbox, or permission settings, and `agent_collab_guidance` for usage guidance.
- Invalid `agent_collab_start` options should be fixed from returned field-path details, not retried by guessing.
- Tests must isolate `AGENT_COLLAB_HOME` (point it at a temp dir) so nothing writes to the real `~/.agent-collab`.
- All config shape compatibility handling belongs in `agent_collab/config_migrations.py`; runtime code only consumes the latest schema.
