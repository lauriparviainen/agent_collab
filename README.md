# agent-collab

`agent-collab` is a terminal referee for supervised collaboration between Claude Code and Codex.

The prototype runs bounded turn-based sessions, streams visible agent/tool events as they arrive, writes JSONL and Markdown transcripts, and exposes sessions through both a CLI client and MCP tools.

## Current Status

Implemented:

- One-shot CLI runner.
- Mock and dry-run modes.
- Configurable agent commands and collaboration modes.
- Foreground local session server at `127.0.0.1:8765`.
- CLI client commands: `serve`, `start`, `list`, `status`, `events`, `watch`, `stop`.
- Stdio MCP adapter that connects to the local server.
- Cursor-based event reads and long-polling.
- JSONL and Markdown logs under `WORKDIR/.agent-collab/sessions/`.

Current transition:

- `agent-collab serve` is the long-running foreground process that owns sessions.
- The current MCP implementation is still a stdio adapter launched by an MCP client.
- Next plumbing stage is to add MCP Streamable HTTP directly to `agent-collab serve` at `/mcp`.
- TUI watch is planned after the Streamable HTTP plumbing.

## Install Locally

```bash
python3 -m pip install -e .
```

Runtime dependencies are intentionally minimal; the current package uses the Python standard library.

## Quick Start

Run a mock one-shot session without Claude or Codex installed:

```bash
python3 -m agent_collab.cli --mock --workdir . "Review this repository"
```

Run the foreground server:

```bash
python3 -m agent_collab.cli serve
```

From another terminal, start and watch a mock server-owned session:

```bash
python3 -m agent_collab.cli start --mock --watch --workdir . "Smoke test"
```

Watch the latest server-owned session:

```bash
python3 -m agent_collab.cli watch
```

## CLI Commands

One-shot mode:

```bash
agent-collab --mock "Review this repository and suggest the smallest next improvement"
agent-collab --mode codex-leads --workdir /path/to/project "Implement the task"
agent-collab --dry-run --workdir /path/to/project "Task"
```

Foreground server and client mode:

```bash
agent-collab serve
agent-collab start --mock --watch --workdir /path/to/project "Task"
agent-collab list
agent-collab status SESSION_ID
agent-collab events SESSION_ID --cursor 0
agent-collab watch SESSION_ID
agent-collab stop SESSION_ID
```

Useful options:

- `--mode claude-leads | codex-leads | debate`
- `--max-turns 3`
- `--timeout 900`
- `--workdir /path/to/project`
- `--log-dir /path/to/logs`
- `--server-url http://127.0.0.1:8765`

`agent-collab watch` without a session id watches the latest server-owned session. `agent-collab watch --workdir /path/to/project` watches the newest JSONL log in that workdir.

## Logs

Logs default to:

```text
WORKDIR/.agent-collab/sessions/
```

Each session writes:

- `SESSION.jsonl`
- `SESSION.md`

The JSONL file preserves normalized events and raw agent payloads. The Markdown file is a readable transcript.

## Agent Configuration

Agent commands are configured through:

```text
WORKDIR/.agent-collab/config.toml
~/.agent-collab/config.toml
built-in defaults
```

Project config wins over user config. See [doc/agent-configuration.md](doc/agent-configuration.md).

Built-in defaults are:

```bash
claude -p --output-format stream-json --verbose "prompt"
codex exec --json "prompt"
```

This repo currently includes a project config at `.agent-collab/config.toml` that uses a lower-tier Claude model for testing:

```bash
claude -p --model sonnet --output-format stream-json --verbose "prompt"
```

The referee invokes agents as subprocesses. Agent prompts include guardrails telling them not to spawn Claude, Codex, `agent-collab`, or other agent subprocesses.

## MCP

Current MCP shape:

```text
MCP client
  launches agent_collab.mcp_server over stdio
    connects to agent-collab serve at 127.0.0.1:8765
      starts/watches server-owned sessions
```

Start the server first:

```bash
python3 -m agent_collab.cli serve
```

Then configure an MCP client to run:

```bash
python3 -m agent_collab.mcp_server
```

Example Codex MCP config:

```toml
[mcp_servers.agent_collab]
command = "python3"
args = ["-m", "agent_collab.mcp_server"]
cwd = "/home/devel/projects/agent_collab"
env = { PYTHONPATH = "/home/devel/projects/agent_collab", AGENT_COLLAB_SERVER = "http://127.0.0.1:8765" }
startup_timeout_sec = 10
tool_timeout_sec = 60
enabled = true
```

Exposed tools:

- `agent_collab_start`
- `agent_collab_list_sessions`
- `agent_collab_status`
- `agent_collab_read_events`
- `agent_collab_wait_events`
- `agent_collab_read_transcript`
- `agent_collab_stop`

Planned MCP shape:

```text
MCP client -> http://127.0.0.1:8765/mcp -> in-process SessionManager
```

That is documented in [doc/tasks_open/stage-4.25-foreground-streamable-http-server.md](doc/tasks_open/stage-4.25-foreground-streamable-http-server.md).

## Development

Run tests:

```bash
python3 -m unittest discover -s tests
```

Important implementation files:

- `agent_collab/cli.py`: CLI entrypoint and client command routing.
- `agent_collab/server_http.py`: foreground HTTP server.
- `agent_collab/daemon.py`: in-memory session manager and session lifecycle.
- `agent_collab/referee.py`: bounded turn loop.
- `agent_collab/runners.py`: Claude/Codex/mock/dry-run subprocess runners.
- `agent_collab/events.py`: normalized event model and stream parsers.
- `agent_collab/mcp_server.py`: current stdio MCP adapter.

For agent handoff notes, read [AGENTS.md](AGENTS.md).
