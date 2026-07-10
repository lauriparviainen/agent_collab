# Changelog

All notable changes to agent-collab are documented here.

This project follows Semantic Versioning. The package version is declared in
`pyproject.toml` and `agent_collab/__init__.py`; keep it in sync with the latest
released version in this document.

Changelog entries stay concise. Refer to the design docs under `doc/` (indexed
from `AGENTS.md`) and the task documents in `doc/tasks_open/` and
`doc/tasks_closed/` for implementation details instead of expanding this file
into a detailed work log.

## [Unreleased]

## [0.2] - 2026-07-10 - First-class backends and daemon hardening

- Promote the Claude Code, Codex, and Antigravity SDK integrations to packaged,
  self-describing backends; add backend-qualified configuration, automatic
  supported-Python selection, and a hermetic/live test split. The base install
  now requires Python >= 3.10 and includes the supported SDK dependencies.
- Add xAI as a first-class provider with CLI and SDK `grok-build` backends.
- Add backend discovery, recommendation, availability, health, and remediation
  reporting, including safer subprocess transport and explicit enablement for
  backends that require it.
- Complete the calm TUI refresh with provider brand colors, a stable source
  gutter, layered Escape behavior, clipped status text, and UTF-8-safe chrome.
- Bump the daemon REST contract to API v2: typed client responses and event and
  transcript reads that summarize tool payloads by default, with
  `tool_output=full` and single-event `limit` retrieval available when needed.
- Require a per-daemon-lifetime bearer token on every HTTP route except the
  minimal `/health` probe; local token, pid, and state files are owner-only and
  daemon readiness now proves authenticated access to a protected route.
- Add `./agent_collab.sh setup` to validate effective config and generate the
  daemon REST API artifacts under `doc/daemon_api_doc/`; `setup --check`
  provides a non-writing drift gate.

## [0.1] - 2026-07-09 - Initial release

First tagged version of the agent-collab prototype: a local terminal referee
that runs bounded, turn-based collaboration sessions between Claude Code, Codex,
and other configured agent backends, streaming visible agent/tool events and
writing JSONL + Markdown transcripts.

Current state:

- One global local daemon (`127.0.0.1:8765`) owns sessions across projects, with
  a persistent session index that survives restarts; runtime state lives under
  `~/.agent-collab/data/` (override with `AGENT_COLLAB_HOME`).
- CLI client (`serve`, `daemon`, `start`, `list`, `status`, `events`, `watch`,
  `stop`, `config show`) plus MCP access to the same live sessions: a Streamable
  HTTP endpoint at `/mcp` and a stdio adapter, with cursor-based event reads and
  long-polling.
- Configurable agents and workflows; a per-session `workdir` selects the project
  config and subprocess cwd. Typed `codex_options` / `claude_options` /
  `antigravity_options` with pre-launch validation, discoverable through
  `agent_collab_describe_options`.
- Pluggable agent backends: a provider `type` is separate from its execution
  `backend` (default standard-library-only `cli`; optional extras-gated `sdk`),
  with availability/health probes and honest per-session capability flags.
- Standard-library-only base install (Python >= 3.9), no runtime dependencies.
- Typed HTTP API contract: shared request/response DTOs are the single source of
  truth for the CLI/daemon REST API, carrying an explicit `X-Agent-Collab-API`
  version with a client compatibility check. See
  `doc/tasks_closed/stage-5.3-daemon-api-contract.md`.
