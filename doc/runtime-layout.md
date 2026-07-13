# Runtime Layout

## Model

`agent-collab` runs one global local daemon and one global session registry. Each session carries its own `workdir`, and that `workdir` determines which project config applies and where agent subprocesses run.

This keeps daemon state and session logs out of repositories and supports starting from project A while asking an agent to work on project B.

## Layout

Global user-owned state (root overridable with `AGENT_COLLAB_HOME`):

```text
~/.agent-collab/
  config.toml        (also holds [daemon].token — a credential; keep 0600)
  data/
    daemon/
      pid
      state.json
      daemon.log
      daemon.stderr.log
    sessions/
      SESSION_ID.jsonl
      SESSION_ID.md
    tmp/
    session-index.json
```

A source-helper user installation defaults to a separate durable environment
and command link:

```text
~/.agent-collab/venv/
~/.local/bin/agent-collab -> ~/.agent-collab/venv/bin/agent-collab
```

The link makes `agent-collab tui` and the rest of the CLI available without
activating the venv. Neither location contains daemon registration state.

On Linux, `agent-collab daemon autostart enable` additionally writes the
owner-managed unit below (respecting `XDG_CONFIG_HOME`):

```text
~/.config/systemd/user/agent-collab.service
```

The unit contains an absolute interpreter path, PATH for provider executable
discovery, and non-secret daemon options. Tokens and provider credentials stay
in their existing owner-only stores. `autostart disable` removes only this
unit; it preserves the venv, command link, config, daemon logs, and sessions.

`tmp/` is reserved for future temp review workdirs. `session-index.json` is the persistent session index that lets `list`/`status` survive daemon restarts.

The daemon directory is mode `0700`; `pid` and `state.json` are atomically
replaced with mode `0600`. The daemon's permanent bearer token lives in the
user config as `[daemon].token`. On first start the daemon generates one and
persists it into `~/.agent-collab/config.toml` (creating the file owner-only,
or appending a `[daemon]` section without rewriting existing content); every
later start reuses the stored value, so the token survives daemon restarts.
Local clients read it automatically; `AGENT_COLLAB_TOKEN` overrides it for
manual or remote clients. `GET /health` remains unauthenticated, while every
other REST route and `/mcp` require `Authorization: Bearer <token>`.

Because the user config now holds a credential, keep it owner-only
(`chmod 600`) and never commit or share it. The daemon refuses to generate a
token into a group/world-readable config and warns when loading one. A
`[daemon]` section in a project config is ignored with a warning, so a shared
repository can never inject or read daemon credentials. To rotate the token,
edit or delete the `token` line and restart the daemon (a deleted token is
regenerated).

The token prevents other local users from casually controlling the loopback
daemon. It does not isolate the daemon from processes running as the same OS
user, which can read the owner-owned config file.

Project-owned config:

```text
PROJECT/.agent-collab/
  config.toml
```

Project `.agent-collab/config.toml` can be tracked in git. It may rename existing
agents and define workflows from agents already enabled by built-in or global
user config. Execution-relevant agent fields and daemon-global policy are
ignored with sanitized warnings. Runtime files, temp review workdirs, daemon
state, and session logs are not written under project `.agent-collab/` by
default.

Set `AGENT_COLLAB_HOME` to run an isolated daemon instance (tests do this so they never touch the real home):

```bash
AGENT_COLLAB_HOME=/tmp/agent-collab-home agent-collab daemon start
```

Set `AGENT_COLLAB_DAEMON_READY_TIMEOUT` (seconds, default 3) if `daemon start`
times out waiting for daemon readiness on a slow cold start.

## Config Precedence

For a session with `workdir = PROJECT`, effective precedence is field-specific:

```text
agent execution: built-in defaults < global user config < explicit start options
agent names:     built-in defaults < global user config < project config
workflows:       built-in defaults < global user config < safe project workflows
daemon policy:   built-in defaults < global user config
```

The caller's current shell directory does not affect project config unless it
is also the session `workdir`. Config files declare a `schema_version`
(currently 6, missing means 1); `agent_collab/config_migrations.py` migrates
known old shapes in memory before validation. Inspect the merged result and any
ignored-project warnings with `agent-collab config show --workdir PROJECT`.

The optional global-user `[workdir].restrict_workdir_roots` list confines resolved
session workdirs. A missing key or empty list means unrestricted, and each
populated entry may be a broad root or one exact exceptional directory. Project
config cannot widen the list. Workdir is a config root and default cwd, not an
operating-system sandbox.

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
  "workdir": "/home/user/projects/project-b",
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

`settings` reflects effective config plus validated start options; `command_preview` never contains the task prompt. Statuses are `running`, `awaiting_input`, `done`, `failed`, `stopped`, and `interrupted` (the session was running or awaiting input when the daemon died).

## Session Retention

Terminal sessions (`done`, `failed`, `stopped`, `interrupted`) are retained for
30 days by default, then the daemon removes their index records and managed
transcripts under `data/sessions/`. The policy is user-config-only (a project
`[sessions]` section is ignored with a warning) and takes effect on daemon
restart:

```toml
[sessions]
retention_days = 30          # 0 disables automatic pruning
cleanup_interval_hours = 24
```

`agent-collab sessions prune` previews the same selection; `--apply` deletes,
`--older-than 7d` overrides the configured age for one run, and `--keep N`
always preserves the newest `N` terminal sessions. Running sessions are never
eligible, transcripts outside the managed session directory are preserved even
when their expired records are removed, and pruning is convergent: a crash or
failure mid-run leaves records that the next run re-selects and finishes. If
the user config cannot be read at daemon startup, automatic retention is
disabled rather than defaulting to deletion. See
[session-retention-and-pruning.md](tasks_closed/session-retention-and-pruning.md)
for the full design.
