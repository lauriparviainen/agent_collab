# AGENTS.md

Short entrypoint for coding agents working in this repository.

## What This Is

`agent-collab` is a local CLI/daemon for supervising collaboration between Claude Code and Codex. The server owns sessions, starts agent subprocesses, writes transcripts, and exposes CLI plus MCP access to the same live session state.

## Start Here

- [README.md](README.md): user-facing overview and common commands.
- [doc/README.md](doc/README.md): design-doc index.
- [doc/daemon-architecture.md](doc/daemon-architecture.md): server, session, CLI, and MCP architecture.
- [doc/agent-configuration.md](doc/agent-configuration.md): agent config, modes, and typed start options.
- [doc/runtime-layout.md](doc/runtime-layout.md): config/data ownership and target global runtime layout.
- [doc/development.md](doc/development.md): local commands, smoke tests, and coding constraints.

## Current Architecture Task

The next architecture correction is:

[doc/tasks_open/stage-4.8-global-runtime-and-config-migrations.md](doc/tasks_open/stage-4.8-global-runtime-and-config-migrations.md)

High-level goal:

- use one global local daemon and global session registry,
- attach a `workdir` to each session,
- load project config from the session `workdir`,
- keep runtime data under `~/.agent-collab/data/`,
- centralize config compatibility fixes in a config migrator.

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
- MCP agents should call `agent_collab_describe_options` before passing non-default model, reasoning, sandbox, or permission settings.
- Invalid `agent_collab_start` options should be fixed from returned field-path details, not retried by guessing.
