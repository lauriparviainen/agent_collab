# AGENTS.md

This file is the handoff briefing for coding agents working in this repository.

## Project Goal

`agent-collab` is a Linux terminal tool that supervises collaboration between Claude Code and Codex.

The user wants one foreground server process that owns collaboration sessions and lets humans and agents attach to the same live transcript.

Current direction:

```text
agent-collab serve
  owns sessions
  starts Claude/Codex subprocesses
  writes JSONL/Markdown logs
  serves CLI clients
  will serve MCP Streamable HTTP at /mcp
```

## Current Implementation

Implemented now:

- One-shot CLI session runner.
- Foreground local server at `127.0.0.1:8765`.
- Daemon-owned sessions through `SessionManager`.
- CLI client commands: `serve`, `start`, `list`, `status`, `events`, `watch`, `stop`.
- JSONL and Markdown logs under `WORKDIR/.agent-collab/sessions/`.
- Mock and dry-run runners for testing without Claude/Codex.
- Configurable agents and modes through `.agent-collab/config.toml`.
- Stdio MCP adapter that connects to the foreground server.

Not implemented yet:

- MCP Streamable HTTP endpoint in `agent-collab serve`.
- TUI watch mode.
- Auth/workdir allowlist hardening.
- Background daemonization or service management.

## Next Task

The next planned implementation stage is:

[doc/tasks_open/stage-4.25-foreground-streamable-http-server.md](doc/tasks_open/stage-4.25-foreground-streamable-http-server.md)

Do this before TUI work.

High-level goal:

- Add `POST /mcp` to `agent-collab serve`.
- Implement enough MCP Streamable HTTP for `initialize`, `tools/list`, and `tools/call`.
- Make MCP tools call the same in-process `SessionManager` as the CLI HTTP routes.
- Keep the server foreground-only with useful request/session logs.

## Important Files

- `agent_collab/cli.py`: CLI entrypoint and client command routing.
- `agent_collab/server_http.py`: foreground HTTP server.
- `agent_collab/daemon.py`: `SessionManager`, session state, event history, lifecycle.
- `agent_collab/referee.py`: bounded turn loop and prompts.
- `agent_collab/runners.py`: Claude/Codex/mock/dry-run subprocess runners.
- `agent_collab/events.py`: normalized event model and stream parsers.
- `agent_collab/client.py`: HTTP client used by CLI watch/start/list/status.
- `agent_collab/mcp_server.py`: current stdio MCP adapter.
- `agent_collab/logging.py`: JSONL/Markdown session logs.
- `doc/`: architecture docs and staged task docs.
- `tests/`: stdlib `unittest` test suite.

## Commands

Run tests:

```bash
python3 -m unittest discover -s tests
```

Run one-shot mock session:

```bash
python3 -m agent_collab.cli --mock --workdir . "Smoke test"
```

Run foreground server:

```bash
python3 -m agent_collab.cli serve
```

Start and watch a mock server-owned session:

```bash
python3 -m agent_collab.cli start --mock --watch --workdir . "Smoke test"
```

Watch latest server-owned session:

```bash
python3 -m agent_collab.cli watch
```

## Local Runtime Notes

The server binds to `127.0.0.1:8765` by default.

When launching real Claude/Codex subprocesses, the server may need to be started outside a restricted sandbox so the child CLIs can see normal user credentials.

This repo has project-local agent config at:

```text
.agent-collab/config.toml
```

It currently configures Claude with:

```bash
claude -p --model sonnet --output-format stream-json --verbose
```

The Codex runner uses:

```bash
codex exec --json
```

`SubprocessRunner` closes child stdin with `DEVNULL`; keep this. It prevents `codex exec --json` from waiting on the server terminal for additional stdin.

## Design Constraints

- Keep dependencies minimal.
- Prefer Python standard library.
- Keep `agent-collab serve` foreground-only for now.
- Do not daemonize, add pidfiles, add systemd, or background fork yet.
- Keep localhost as the default security boundary.
- Do not dump every transcript event into server logs by default.
- Preserve cursor-based event reads and long-polling.
- Do not let agents recursively spawn Claude, Codex, `agent-collab`, or other agent processes.
- Keep `watch` plain and pipe-friendly; TUI is a later additive mode.

## Testing Expectations

Add focused tests with each behavior change.

For the Stage 4.25 MCP-over-HTTP work, add tests for:

- `POST /mcp` `initialize`,
- `POST /mcp` `tools/list`,
- `POST /mcp` `tools/call`,
- MCP-started session visible through normal session listing,
- MCP-started session readable through normal event endpoints,
- non-local `Origin` rejected for `/mcp`.

Before handing back, run:

```bash
python3 -m unittest discover -s tests
```

If live smoke is needed, use mock mode first. Real Claude/Codex smoke can be expensive and may need unsandboxed credentials.
