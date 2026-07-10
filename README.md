# agent-collab

`agent-collab` is a terminal referee for supervised collaboration between Claude Code and Codex.

The prototype runs bounded turn-based sessions, streams visible agent/tool events as they arrive, writes JSONL and Markdown transcripts, and exposes sessions through both a CLI client and MCP tools.

## Current Status

Implemented:

- One-shot CLI runner.
- Mock and dry-run runners.
- Configurable agent commands and collaboration workflows.
- One global local daemon with runtime state under `~/.agent-collab/data/` (override with `AGENT_COLLAB_HOME`).
- Per-session `workdir` that selects the project config and the subprocess cwd, so one daemon serves sessions across projects.
- Persistent session index; `list`/`status` survive daemon restarts, and sessions that were running or awaiting input when the daemon died are marked `interrupted`.
- Foreground local session server at `127.0.0.1:8765`.
- CLI client commands: `serve`, `daemon`, `options`, `start`, `list`, `status`, `events`, `watch`, `stop`, `config init`, `config show`.
- MCP Streamable HTTP endpoint at `http://127.0.0.1:8765/mcp`.
- Stdio MCP adapter that connects to the local server.
- Cursor-based event reads and long-polling.
- Backend-qualified `backend_options` with schemas/defaults owned by each backend package and pre-launch validation.
- Pluggable agent backends: an agent's provider (`type`) is separate from its execution mechanism (`backend`). The default `cli` subprocess backend runs the provider CLI; a first-class `sdk` backend runs the provider SDK in-process. Claude, Codex, and Antigravity each register both `(cli)` and `(sdk)`; SDK imports are lazy so a missing wheel is an unavailable backend, not an import error. Backends, availability/health, and honest per-session capability flags are discoverable via `agent_collab_describe_options`.
- MCP option discovery through `agent_collab_describe_options` and usage guidance through `agent_collab_guidance`.
- Start/status/list responses include the effective session settings: workflow sequence, per-agent typed options, and a prompt-free `command_preview`.
- Centralized config schema migrations (`schema_version`, currently 4).
- JSONL and Markdown session logs under `~/.agent-collab/data/sessions/`.

Current transition:

- `agent-collab serve` is the long-running foreground process that owns sessions.
- `agent-collab daemon start` runs the same server model in the background as the global daemon.
- MCP clients can connect directly to `agent-collab serve` over Streamable HTTP.
- The stdio MCP adapter remains available for clients that launch MCP servers as subprocesses.
- Project-local `.agent-collab/data/` and `.agent-collab/sessions/` directories are legacy; `watch --workdir` still falls back to them for old logs.
- TUI watch remains planned as an additive mode.

## Install Locally

```bash
python3 -m pip install -e .
```

A normal install (Python ≥ 3.10) brings the first-class `sdk` backends with it: the
Claude Agent SDK (`claude-agent-sdk`), the Codex SDK (`openai-codex`), and the
Antigravity SDK (`google-antigravity`) install as project dependencies. Every SDK
import is lazy, so a missing wheel degrades to an unavailable backend rather than an
import error, and the `cli` backends keep working with the provider CLIs regardless.
Credentials are still never managed by agent-collab — provide the provider's own
auth (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, or each tool's local
sign-in) in the environment.

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

Or start the global background daemon:

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
agent-collab --workflow compare --workdir /path/to/project "Implement the task"
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
agent-collab daemon start
agent-collab daemon status
agent-collab daemon logs --tail 100
agent-collab daemon stop
agent-collab start --mock --watch --workdir /path/to/project "Task"
agent-collab options --workdir /path/to/project
agent-collab options --workdir /path/to/project --fresh --json
agent-collab list
agent-collab status SESSION_ID
agent-collab events SESSION_ID --cursor 0
agent-collab watch SESSION_ID
agent-collab stop SESSION_ID
agent-collab config show --workdir /path/to/project
agent-collab config init
```

The daemon is global: one daemon serves sessions for any number of projects, and each session's `--workdir` decides which project config applies and where agent subprocesses run. `daemon start --workdir DIR` only sets the default workdir for sessions that do not pass one; it never changes where daemon state lives.

Useful options:

- `--workflow solo-claude | solo-codex | cross-review | compare` (default `cross-review`)
- `--max-turns 3`
- `--timeout 900`
- `--workdir /path/to/project`
- `--log-dir /path/to/logs`
- `--server-url http://127.0.0.1:8765`
- `--backend-options '{"codex_cli":{"thinking_level":"medium"},"claude_cli":{"model":"opus"}}'`
- `--backend sdk` (only when every selected agent's type registers it)

`agent-collab start` and `agent-collab status` print the effective session settings: the workflow sequence, each agent's model/thinking settings, and a prompt-free `command_preview` of the exact subprocess command. `agent-collab list` shows sessions across all projects with their workflow and agents.

`agent-collab watch` without a session id watches the latest server-owned session. `agent-collab watch --workdir /path/to/project` resolves JSONL logs from the global `~/.agent-collab/data/sessions/` first, then falls back to the legacy project-local `.agent-collab/data/sessions/` and `.agent-collab/sessions/` directories.

## Runtime layout

All runtime state is global and user-owned (override the root with `AGENT_COLLAB_HOME`):

```text
~/.agent-collab/
  config.toml            user config
  data/
    daemon/
      pid
      state.json
      daemon.log
      daemon.stderr.log
    sessions/
      SESSION.jsonl
      SESSION.md
    tmp/
    session-index.json   persistent session index
```

Project directories only carry config, which can be tracked in git as shared project policy:

```text
PROJECT/.agent-collab/config.toml
```

Nothing is written under project `.agent-collab/` by default. If you have a stale project-local daemon from an older checkout (`PROJECT/.agent-collab/data/daemon/pid`), stop it manually once; the global daemon commands do not manage it.

Each session writes:

- `SESSION.jsonl`
- `SESSION.md`

The JSONL file preserves normalized events and raw agent payloads. The Markdown file is a readable transcript. Daemon operational logs do not dump full transcript events by default.

## Agent Configuration

Agent commands are configured through (highest precedence first):

```text
explicit session/start options
SESSION_WORKDIR/.agent-collab/config.toml
~/.agent-collab/config.toml        (or $AGENT_COLLAB_HOME/config.toml)
built-in defaults
```

The built-in defaults live in [agent_collab/default_config.toml](agent_collab/default_config.toml). Project config comes from the session `workdir`, never from the caller's shell directory. Config files carry a `schema_version` (currently 4); known old shapes are migrated in memory at load time by a centralized migration layer, and unknown fields are still rejected afterwards. Inspect the effective merged config with `agent-collab config show --workdir /path/to/project`. See [doc/agent-configuration.md](doc/agent-configuration.md).

The user config may disable any registered execution backend globally with
`[backends.<provider>_<backend>] enabled = false`. Project config cannot
re-enable it. `agent-collab config init` generates explicit entries for the
backends registered in the current build; absent entries remain enabled for
backward compatibility.

The built-in defaults include Claude Opus with high effort and Codex high reasoning effort:

```bash
claude -p --output-format stream-json --verbose --model opus --effort high "prompt"
codex exec --json -c model_reasoning_effort="high" "prompt"
```

This repo's `.agent-collab/config.toml` is intentionally small: it only opts this checkout into Antigravity and adds the local `solo-antigravity` workflow.

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

- `agent_collab_guidance`
- `agent_collab_describe_options`
- `agent_collab_start`
- `agent_collab_list_sessions`
- `agent_collab_status`
- `agent_collab_read_events`
- `agent_collab_wait_events`
- `agent_collab_read_transcript`
- `agent_collab_stop`

Agents can call `agent_collab_guidance` for full Markdown usage guidance (source: [doc/mcp-guidance.md](doc/mcp-guidance.md)), and should call `agent_collab_describe_options` with the intended absolute `workdir` before selecting a workflow or backend. The response is a versioned discovery snapshot with canonical backends, effective per-agent/workflow selections, enablement policy, probe freshness, readiness, remediation, and backend-qualified option schemas. `health_refresh` accepts `cached` (default) or `fresh`; either result is advisory. Start reloads the same workdir config, rejects disabled selections, validates options, and freshly probes only selected backends whose policy acts on health. Prefer `thinking_level` over provider-specific raw fields: Codex accepts `minimal`, `low`, `medium`, `high`, or `xhigh`; Claude accepts `low`, `medium`, `high`, `xhigh`, or `max`.

## Development

Run tests:

```bash
./agent_collab.sh test
./agent_collab.sh integration-test claude_sdk  # live, credentialed, opt-in
```

Important implementation files:

- `agent_collab/cli.py`: CLI entrypoint and client command routing.
- `agent_collab/server_http.py`: foreground HTTP server.
- `agent_collab/daemon.py`: in-memory session manager and session lifecycle.
- `agent_collab/referee.py`: bounded turn loop.
- `agent_collab/runners.py`: runner primitives (subprocess/mock/dry-run) and the registry-backed `configured_runner`.
- `agent_collab/backends/`: six standalone `<provider>_<backend>` packages, their option manifests, parsers/runners, and shared infrastructure.
- `agent_collab/events.py`: normalized provider-neutral event model.
- `agent_collab/client.py`: HTTP client used by CLI watch/start/list/status.
- `agent_collab/daemon_supervisor.py`: background daemon PID/state/log lifecycle.
- `agent_collab/options.py`: generic backend-option validation and session settings metadata.
- `agent_collab/paths.py`: global home (`AGENT_COLLAB_HOME`) and session log path helpers.
- `agent_collab/config_migrations.py`: centralized config schema migrations.
- `agent_collab/session_index.py`: persistent session index for daemon restarts.
- `agent_collab/mcp_server.py`: current stdio MCP adapter.
- `agent_collab/mcp_tools.py`: shared MCP tool schemas, guidance tool, and dispatch.

For coding-agent handoff, start with [AGENTS.md](AGENTS.md); detailed current
implementation notes live in [doc/implementation-notes.md](doc/implementation-notes.md).
