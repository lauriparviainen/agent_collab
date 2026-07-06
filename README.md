# agent-collab

`agent-collab` is a terminal referee for supervised collaboration between Claude Code and Codex.

The prototype runs bounded turn-based sessions, streams visible agent/tool events as they arrive, writes JSONL and Markdown transcripts, and exposes sessions through both a CLI client and MCP tools.

## Current Status

Implemented:

- One-shot CLI runner.
- Mock and dry-run modes.
- Configurable agent commands and collaboration modes.
- Foreground local session server at `127.0.0.1:8765`.
- Project-local background daemon lifecycle commands.
- CLI client commands: `serve`, `daemon`, `start`, `list`, `status`, `events`, `watch`, `stop`.
- MCP Streamable HTTP endpoint at `http://127.0.0.1:8765/mcp`.
- Stdio MCP adapter that connects to the local server.
- Cursor-based event reads and long-polling.
- Typed `codex_options` and `claude_options` with pre-launch validation.
- MCP option discovery through `agent_collab_describe_options`.
- JSONL and Markdown logs under `WORKDIR/.agent-collab/sessions/`.
- Daemon runtime data and daemon-owned session logs under `WORKDIR/.agent-collab/data/`.

Current transition:

- `agent-collab serve` is the long-running foreground process that owns sessions.
- `agent-collab daemon start` runs the same server model in the background.
- MCP clients can connect directly to `agent-collab serve` over Streamable HTTP.
- The stdio MCP adapter remains available for clients that launch MCP servers as subprocesses.
- TUI watch remains planned as an additive mode.

## Install Locally

```bash
python3 -m pip install -e .
```

Runtime dependencies are intentionally minimal; the current package uses the Python standard library.

## Quick Start

From a source checkout, the thin shell wrapper is the easiest entrypoint:

```bash
./agent_collab.sh help
./agent_collab.sh smoke
```

The wrapper sets `PYTHONPATH` to the repo root and passes normal commands through to `python3 -m agent_collab.cli`.

Run a mock one-shot session without Claude or Codex installed:

```bash
python3 -m agent_collab.cli --mock --workdir . "Review this repository"
```

Run the foreground server:

```bash
python3 -m agent_collab.cli serve
```

Or start a project-local background daemon:

```bash
./agent_collab.sh daemon start
```

From another terminal, start and watch a mock server-owned session:

```bash
./agent_collab.sh start --mock --watch --workdir . "Smoke test"
```

Watch the latest server-owned session:

```bash
./agent_collab.sh watch
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
./agent_collab.sh serve
./agent_collab.sh daemon start
./agent_collab.sh daemon status
./agent_collab.sh daemon logs --tail 100
./agent_collab.sh daemon stop
./agent_collab.sh smoke
agent-collab serve
agent-collab daemon start --workdir /path/to/project
agent-collab daemon status --workdir /path/to/project
agent-collab daemon logs --workdir /path/to/project --tail 100
agent-collab daemon stop --workdir /path/to/project
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
- `--codex-options '{"thinking_level":"medium"}'`
- `--claude-options '{"model":"opus","thinking_level":"high"}'`

`agent-collab watch` without a session id watches the latest server-owned session. `agent-collab watch --workdir /path/to/project` resolves JSONL logs from `.agent-collab/data/sessions/` first, then falls back to the legacy `.agent-collab/sessions/` directory.

## Logs

One-shot and foreground server logs default to:

```text
WORKDIR/.agent-collab/sessions/
```

Daemon runtime data defaults to:

```text
WORKDIR/.agent-collab/data/
  daemon/
    pid
    state.json
    daemon.log
    daemon.stderr.log
  sessions/
    SESSION.jsonl
    SESSION.md
```

Each session writes:

- `SESSION.jsonl`
- `SESSION.md`

The JSONL file preserves normalized events and raw agent payloads. The Markdown file is a readable transcript. Daemon operational logs do not dump full transcript events by default.

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

This repo currently includes a project config at `.agent-collab/config.toml` that defaults Claude to Opus with high effort and Codex to high reasoning effort:

```bash
claude -p --output-format stream-json --verbose --model opus --effort high "prompt"
codex exec --json -c model_reasoning_effort="high" "prompt"
```

The referee invokes agents as subprocesses. Agent prompts include guardrails telling them not to spawn Claude, Codex, `agent-collab`, or other agent subprocesses.

## MCP

Preferred MCP shape:

```text
MCP client -> http://127.0.0.1:8765/mcp -> in-process SessionManager
```

Start the foreground server first:

```bash
python3 -m agent_collab.cli serve
```

Then configure an MCP client to use:

```text
http://127.0.0.1:8765/mcp
```

The `/mcp` endpoint implements the Streamable HTTP JSON POST path for `initialize`, `tools/list`, and `tools/call`. It accepts MCP notifications and client responses with `202 Accepted`, validates non-local `Origin` headers, validates supported `MCP-Protocol-Version` headers, and returns `405 Method Not Allowed` for `GET /mcp` because SSE streams are not implemented yet. It uses the same live `SessionManager` as the CLI HTTP routes, so sessions started through MCP can be listed, watched, and stopped with the normal CLI client commands.

Codex can register the direct Streamable HTTP endpoint with:

```bash
codex mcp add agent-collab --url http://127.0.0.1:8765/mcp
```

Compatibility stdio shape:

```text
MCP client
  launches agent_collab.mcp_server over stdio
    connects to agent-collab serve at 127.0.0.1:8765
      starts/watches server-owned sessions
```

Configure a subprocess-based MCP client to run:

```bash
python3 -m agent_collab.mcp_server
```

Compatibility Codex stdio config:

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

- `agent_collab_describe_options`
- `agent_collab_start`
- `agent_collab_list_sessions`
- `agent_collab_status`
- `agent_collab_read_events`
- `agent_collab_wait_events`
- `agent_collab_read_transcript`
- `agent_collab_stop`

Agents should call `agent_collab_describe_options` before passing non-default model, reasoning, sandbox, or permission settings. Prefer `thinking_level` over provider-specific raw fields: Codex accepts `minimal`, `low`, `medium`, `high`, or `xhigh`; Claude accepts `low`, `medium`, `high`, `xhigh`, or `max`. `agent_collab_start` rejects unknown keys, wrong types, unsupported values, and options that do not apply to the selected mode before any subprocess is launched.

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
- `agent_collab/client.py`: HTTP client used by CLI watch/start/list/status.
- `agent_collab/daemon_supervisor.py`: background daemon PID/state/log lifecycle.
- `agent_collab/options.py`: typed start option schemas, validation, and explicit CLI flag mapping.
- `agent_collab/paths.py`: project data and session log path helpers.
- `agent_collab/mcp_server.py`: current stdio MCP adapter.
- `agent_collab/mcp_tools.py`: shared MCP tool schemas and dispatch.

For agent handoff notes, read [AGENTS.md](AGENTS.md).
