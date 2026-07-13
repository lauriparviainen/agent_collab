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
- [agent_collab/mcp-guidance.md](agent_collab/mcp-guidance.md): guidance served to MCP agents by
  `agent_collab_guidance`.

For commands, test expectations, and live-agent cautions, use
[doc/development.md](doc/development.md).

Work tracking uses GitHub issues for discrete tasks and `doc/tasks_open/` task
documents for larger design work. The conventions — including the
public-content guardrail for issues — live in
[.claude/skills/github-issues/SKILL.md](.claude/skills/github-issues/SKILL.md);
versioning and the release procedure live in
[.claude/skills/release/SKILL.md](.claude/skills/release/SKILL.md); shell
entrypoint rules and the CLI output marker convention live in
[.claude/skills/cli-scripting/SKILL.md](.claude/skills/cli-scripting/SKILL.md).
Follow them for any issue, task-document, release, or CLI-output change
regardless of which agent harness you run under.

Backend implementations live in peer `agent_collab/backends/<provider>_<backend>/`
packages. Each package owns an `options.toml` and README. Hermetic tests belong
under `tests/`; credentialed model calls belong only under `integration_tests/`.
