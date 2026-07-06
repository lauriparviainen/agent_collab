# Runtime Layout

## Direction

`agent-collab` should move toward one global local daemon and one global session registry. Each session carries its own `workdir`, and that `workdir` determines which project config applies.

This avoids scattering daemon state and session logs across repositories. It also supports starting from project A while asking an agent to work on project B.

## Target Layout

Global user-owned state:

```text
~/.agent-collab/
  config.toml
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

Project-owned config:

```text
PROJECT/.agent-collab/
  config.toml
```

Project `.agent-collab/config.toml` can be tracked in git and should be treated as shared project defaults or policy. Runtime files, temp review workdirs, daemon state, and session logs should not be written under project `.agent-collab/` by default.

## Config Precedence

For a session with `workdir = PROJECT`, effective config should be:

```text
built-in defaults
< ~/.agent-collab/config.toml
< PROJECT/.agent-collab/config.toml
< explicit session/start options
```

The caller's current shell directory should not affect project config unless it is also the session `workdir`.

## Current Legacy Layout

The current implementation still has project-local runtime paths:

```text
PROJECT/.agent-collab/data/
PROJECT/.agent-collab/sessions/
PROJECT/.agent-collab/mcp-review-workdir/
```

These are legacy/fallback locations during the migration. New runtime ownership work is tracked in:

[tasks_open/stage-4.8-global-runtime-and-config-migrations.md](tasks_open/stage-4.8-global-runtime-and-config-migrations.md)

## Session Records

Global sessions should store the execution project explicitly:

```json
{
  "session_id": "daemon-abc123",
  "status": "running",
  "task": "Review project B",
  "workdir": "/home/devel/projects/project-b",
  "jsonl_path": "~/.agent-collab/data/sessions/daemon-abc123.jsonl",
  "markdown_path": "~/.agent-collab/data/sessions/daemon-abc123.md",
  "created_at": "...",
  "updated_at": "..."
}
```
