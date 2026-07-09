# AGENTS.md

Short entrypoint for coding agents working in this repository.

## What This Is

`agent-collab` is a local CLI/daemon for supervising collaboration between
Claude Code, Codex, and other configured agent backends. The server owns
sessions, starts agent subprocesses, writes transcripts, and exposes CLI plus
MCP access to the same live session state.

## Start Here

- [README.md](README.md): user-facing overview and common commands.
- [doc/README.md](doc/README.md): design-doc and task index.
- [doc/development.md](doc/development.md): local commands, smoke tests, and
  coding constraints.
- [doc/implementation-notes.md](doc/implementation-notes.md): current runtime,
  backend, and handoff notes for agents.
- [doc/daemon-architecture.md](doc/daemon-architecture.md): server, session,
  CLI, and MCP architecture.
- [doc/agent-configuration.md](doc/agent-configuration.md): config, workflows,
  and typed start options.
- [doc/runtime-layout.md](doc/runtime-layout.md): global runtime layout and
  config precedence.
- [doc/mcp-guidance.md](doc/mcp-guidance.md): guidance served to MCP agents by
  `agent_collab_guidance`.

For commands, test expectations, and live-agent cautions, use
[doc/development.md](doc/development.md).
