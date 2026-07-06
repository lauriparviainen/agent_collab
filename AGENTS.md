# AGENTS.md

This file is the handoff briefing for coding agents working in this repository.

## Project Goal

`agent-collab` is a Linux terminal tool that supervises collaboration between Claude Code and Codex.

The user wants one local server process that owns collaboration sessions and lets humans and agents attach to the same live transcript. It can run either as a foreground server for debugging or as a project-local background daemon.

Current direction:

```text
agent-collab serve
  foreground debugging/development server
  owns sessions
  starts Claude/Codex subprocesses
  writes JSONL/Markdown logs
  serves CLI clients
  serves MCP Streamable HTTP at /mcp

agent-collab daemon start
  background lifecycle for the same server model
  writes PID/state/daemon logs under WORKDIR/.agent-collab/data/
```

## Current Implementation

Implemented now:

- One-shot CLI session runner.
- Foreground local server at `127.0.0.1:8765`.
- Server-owned sessions through `SessionManager`.
- CLI client commands: `serve`, `daemon`, `start`, `list`, `status`, `events`, `watch`, `stop`.
- JSONL and Markdown logs under `WORKDIR/.agent-collab/sessions/`.
- Daemon runtime data and daemon-owned session logs under `WORKDIR/.agent-collab/data/`.
- Mock and dry-run runners for testing without Claude/Codex.
- Configurable agents and modes through `.agent-collab/config.toml`.
- Typed Claude/Codex start options with validation feedback.
- MCP option discovery through `agent_collab_describe_options`.
- MCP Streamable HTTP endpoint in `agent-collab serve`.
- Stdio MCP adapter that connects to the foreground server.

Not implemented yet:

- TUI watch mode.
- Auth/workdir allowlist hardening.
- Service-manager integration.

## Next Task

The next planned implementation stage is:

[doc/tasks_open/stage-4.5-tui-watch.md](doc/tasks_open/stage-4.5-tui-watch.md)

High-level goal:

- Add a TUI watch mode as an additive layer.
- Keep existing plain `watch` pipe-friendly.
- Do not replace cursor-based event reads or long-polling.

## Important Files

- `agent_collab/cli.py`: CLI entrypoint and client command routing.
- `agent_collab/server_http.py`: foreground HTTP server.
- `agent_collab/daemon.py`: `SessionManager`, session state, event history, lifecycle.
- `agent_collab/referee.py`: bounded turn loop and prompts.
- `agent_collab/runners.py`: Claude/Codex/mock/dry-run subprocess runners.
- `agent_collab/events.py`: normalized event model and stream parsers.
- `agent_collab/client.py`: HTTP client used by CLI watch/start/list/status.
- `agent_collab/daemon_supervisor.py`: background daemon PID/state/log lifecycle.
- `agent_collab/options.py`: typed start option schemas, validation, and explicit CLI flag mapping.
- `agent_collab/paths.py`: project data and session log path helpers.
- `agent_collab/mcp_server.py`: current stdio MCP adapter.
- `agent_collab/mcp_tools.py`: shared MCP tool schemas and dispatch.
- `agent_collab/logging.py`: JSONL/Markdown session logs.
- `doc/`: architecture docs and staged task docs.
- `tests/`: stdlib `unittest` test suite.

## Commands

Source checkout helper:

```bash
./agent_collab.sh help
./agent_collab.sh test
./agent_collab.sh smoke
```

The wrapper is a thin passthrough to `python3 -m agent_collab.cli`; daemon helper commands default to `--workdir .` unless the caller passes another `--workdir`.

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

Start and inspect the project-local daemon:

```bash
./agent_collab.sh daemon start
./agent_collab.sh daemon status
./agent_collab.sh daemon logs --tail 100
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
claude -p --output-format stream-json --verbose --model opus --effort high
```

It currently configures Codex with:

```bash
codex exec --json -c model_reasoning_effort="high"
```

`SubprocessRunner` closes child stdin with `DEVNULL`; keep this. It prevents `codex exec --json` from waiting on the server terminal for additional stdin.

## Design Constraints

- Keep dependencies minimal.
- Prefer Python standard library.
- Keep `agent-collab serve` foreground-only; add separate daemon lifecycle commands instead of making `serve` fork itself.
- Do not add systemd, launchd, or other service-manager integration until a later hardening stage.
- Keep localhost as the default security boundary.
- Do not dump every transcript event into server logs by default.
- Preserve cursor-based event reads and long-polling.
- Do not let agents recursively spawn Claude, Codex, `agent-collab`, or other agent processes.
- Keep `watch` plain and pipe-friendly; TUI is a later additive mode.
- MCP agents should call `agent_collab_describe_options` before passing non-default model, reasoning, sandbox, or permission settings.
- Invalid `agent_collab_start` options should be fixed from the returned field-path details, not retried by guessing.

## Testing Expectations

Add focused tests with each behavior change.

For server and MCP changes, cover the affected route or tool behavior plus one shared-session path when relevant.

Before handing back, run:

```bash
python3 -m unittest discover -s tests
```

If live smoke is needed, use mock mode first. Real Claude/Codex smoke can be expensive and may need unsandboxed credentials.
