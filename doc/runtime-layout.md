# Runtime Layout

## Model

`agent-collab` runs one global local daemon and one global session registry. Each session carries its own `workdir`, and that `workdir` determines which project config applies and where agent subprocesses run.

This keeps daemon state and session logs out of repositories and supports starting from project A while asking an agent to work on project B.

## Layout

Global user-owned state (root overridable with `AGENT_COLLAB_HOME`):

```text
~/.agent-collab/
  config.toml
  data/
    daemon/
      pid
      state.json
      token
      daemon.log
      daemon.stderr.log
    sessions/
      SESSION_ID.jsonl
      SESSION_ID.md
    tmp/
    session-index.json
```

`tmp/` is reserved for future temp review workdirs. `session-index.json` is the persistent session index that lets `list`/`status` survive daemon restarts.

The daemon directory is mode `0700`; `pid`, `state.json`, and `token` are
atomically replaced with mode `0600`. The serving process mints a new `token`
for every daemon lifetime before it accepts protected requests. Local clients
read it automatically; `AGENT_COLLAB_TOKEN` overrides it for manual or remote
clients. `GET /health` remains unauthenticated, while every other REST route and
`/mcp` require `Authorization: Bearer <token>`.

The token prevents other local users from casually controlling the loopback
daemon. It does not isolate the daemon from processes running as the same OS
user, which can read the owner-owned token file.

Project-owned config:

```text
PROJECT/.agent-collab/
  config.toml
```

Project `.agent-collab/config.toml` can be tracked in git and should be treated as shared project defaults or policy. Runtime files, temp review workdirs, daemon state, and session logs are not written under project `.agent-collab/` by default.

Set `AGENT_COLLAB_HOME` to run an isolated daemon instance (tests do this so they never touch the real home):

```bash
AGENT_COLLAB_HOME=/tmp/agent-collab-home agent-collab daemon start
```

## Config Precedence

For a session with `workdir = PROJECT`, effective config is:

```text
built-in defaults
< ~/.agent-collab/config.toml
< PROJECT/.agent-collab/config.toml
< explicit session/start options
```

The caller's current shell directory does not affect project config unless it is also the session `workdir`. Config files declare a `schema_version` (currently 4, missing means 1); `agent_collab/config_migrations.py` migrates known old shapes in memory before validation. Inspect the merged result with `agent-collab config show --workdir PROJECT`. Canonical `[backends.*].enabled` policy is user-config-only; a project copy is stripped with a warning so project precedence cannot re-enable a daemon-user-disabled backend.

The built-in defaults are stored in [agent_collab/default_config.toml](../agent_collab/default_config.toml). They are still the lowest-precedence layer, but they are an inspectable TOML file rather than an embedded Python dict.

## Legacy Project-Local Layout

Older checkouts wrote runtime data under the project:

```text
PROJECT/.agent-collab/data/
PROJECT/.agent-collab/sessions/
```

These are fallback locations only: `agent-collab watch --workdir PROJECT SESSION_ID` still resolves old logs there after checking the global `data/sessions`. The global daemon does not load old project-local daemon state; if a stale project-local daemon is still running, stop it manually once by killing the pid in `PROJECT/.agent-collab/data/daemon/pid`.

## Session Records

Session records store the execution project and the effective settings confirmation:

```json
{
  "session_id": "daemon-abc123",
  "status": "running",
  "task": "Review project B",
  "workdir": "/home/devel/projects/project-b",
  "workflow": "cross-review",
  "jsonl_path": "~/.agent-collab/data/sessions/daemon-abc123.jsonl",
  "markdown_path": "~/.agent-collab/data/sessions/daemon-abc123.md",
  "created_at": "...",
  "updated_at": "...",
  "settings": {
    "workflow": {
      "name": "cross-review",
      "sequence": ["claude", "codex", "claude"]
    },
    "agents": {
      "claude": {
        "type": "claude",
        "model": "opus",
        "thinking_level": "high",
        "command_preview": ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", "opus", "--effort", "high"]
      },
      "codex": {
        "type": "codex",
        "thinking_level": "high",
        "command_preview": ["codex", "exec", "--json", "-c", "model_reasoning_effort=\"high\""]
      }
    }
  }
}
```

`settings` reflects effective config plus validated start options; `command_preview` never contains the task prompt. Statuses are `running`, `awaiting_input`, `done`, `failed`, `stopped`, and `interrupted` (the session was running or awaiting input when the daemon died). The session index grows without bound for now; a `sessions prune` command is planned for stage 5.
