# Stage 4.8: Global runtime and config migrations

## Status: implemented (2026-07-08)

All acceptance criteria below are met. Implementation notes:

- `agent_collab/paths.py` provides `AgentCollabHome`/`GlobalDataPaths` with the `AGENT_COLLAB_HOME` override, resolved per call; tests always point it at a temp dir.
- Daemon pid/state/logs and all session logs live under the global data root; `daemon status/stop/logs` take no `--workdir`, and `daemon start --workdir` only sets the session-default workdir.
- `mode` became `workflow` everywhere with built-ins `solo-claude`, `solo-codex`, `cross-review` (default; same sequence as the old `claude-leads`), and `compare`. `[modes.*]` is rejected with a hint.
- `agent_collab/config_migrations.py` migrates each config file to `CURRENT_CONFIG_SCHEMA = 2` before merge/validation; v1 is the pre-`schema_version` era.
- `agent_collab/options.py:build_session_settings` produces the effective settings block (workflow sequence, per-agent typed options, prompt-free `command_preview` shared with the real runner command builder); persisted on `SessionState` and returned by start/status/list on HTTP, MCP, and CLI.
- `agent_collab/session_index.py` persists sessions to `data/session-index.json` (atomic replace); on daemon restart, formerly `running` sessions get the new terminal status `interrupted`, and their events replay from JSONL.
- `agent_collab_guidance` serves `doc/mcp-guidance.md` whole or by topic section; `initialize.instructions` shrank to five pointers.
- Extra beyond spec: `agent-collab config show --workdir PATH` prints the effective merged config and loaded paths.
- Deferred to stage 5: session pruning; explicit `config migrate --write`; import of old project-local daemon state (fallback watch of legacy project log dirs works).

## Purpose

Move runtime state from project-local data directories to one global user data root, while keeping project config as per-session overrides.

The current project-local daemon model is awkward when a user starts from project A but wants an agent session to inspect or modify project B. The daemon, session registry, and logs should be global. Each session should carry its own `workdir`, and that `workdir` should determine which project config applies.

This stage should also introduce a centralized config migration layer so the rest of the code always works with the latest config shape.

This is also the point to rename `modes` to `workflows`. The current `claude-leads`, `codex-leads`, and `debate` names are prototype turn-loop language. A session should have a `workflow` that tells the server what orchestration pattern it is running.

## Target model

Global runtime:

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

Project config:

```text
PROJECT/.agent-collab/
  config.toml
```

The project config can be tracked in git and should be treated as shared project defaults or policy. Runtime files, temp review workdirs, daemon state, and session logs should not be written under project `.agent-collab/` by default.

## Config precedence

For a session with `workdir = PROJECT`, effective config is:

```text
built-in defaults
< ~/.agent-collab/config.toml
< PROJECT/.agent-collab/config.toml
< explicit session/start options
```

The caller's current shell directory should not affect project config unless it is also the session `workdir`.

Examples:

```bash
cd /home/user/projects/project-a
agent-collab start --workdir /home/user/projects/project-b "Review project B"
```

This should load:

```text
built-in defaults
~/.agent-collab/config.toml
/home/user/projects/project-b/.agent-collab/config.toml
```

It should not load project A config.

## Workflows

Replace `modes` with `workflows` everywhere in working code, config, docs, CLI, and MCP payloads.

The project has not been deployed outside this checkout, so this stage does not need compatibility aliases for `modes`. The implementing agent should manually update the existing repo config and tests to the new shape.

Target config shape:

```toml
[workflows.solo-claude]
sequence = ["claude"]

[workflows.solo-codex]
sequence = ["codex"]

[workflows.cross-review]
sequence = ["claude", "codex", "claude"]

[workflows.compare]
sequence = ["claude", "codex"]
```

Start payloads should use `workflow`:

```json
{
  "task": "Review project B",
  "workdir": "/home/user/projects/project-b",
  "workflow": "cross-review"
}
```

The old `mode` field and `[modes.*]` config sections should be removed as part of this task, not kept as a migrated legacy shape.

Good workflow names should describe the orchestration, not who "leads":

- `solo-claude`
- `solo-codex`
- `cross-review`
- `compare`

The default workflow can be `cross-review` unless implementation finds a better repo-wide default.

## Runtime ownership

There should be one global local daemon by default.

Daemon commands should manage the global daemon:

```bash
agent-collab daemon start
agent-collab daemon status
agent-collab daemon logs --tail 100
agent-collab daemon stop
```

`daemon start` may accept a default `--workdir` used only as a fallback for sessions that do not pass one explicitly. It should not use `--workdir` to choose PID, state, or daemon log locations.

Session records should include:

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

The `settings` block is the user/agent confirmation of what was actually started. It should reflect effective config and explicit start options after validation and normalization.

Do not include the full task prompt in command metadata. A redacted command prefix or `command_preview` without the prompt is enough.

## Session metadata and start confirmation

When a client starts a session, the response should include enough metadata for the caller to confirm what the server is about to run.

`agent_collab_start` and the CLI `start` response should include:

- `session_id`,
- `status`,
- `workdir`,
- `workflow`,
- `jsonl_path`,
- `markdown_path`,
- effective workflow sequence,
- effective agent settings used by that workflow,
- effective typed options such as model, thinking level, sandbox, approval/permission mode,
- a prompt-free command preview for each participating subprocess agent.

This metadata should be persisted in session state and returned by:

- `agent_collab_list_sessions`,
- `agent_collab_status`,
- CLI `list`,
- CLI `status`,
- any session index read from disk after daemon restart.

`list` may print a compact view, but the structured response should retain the full settings metadata for MCP callers and future UI views.

Example compact CLI view:

```text
SESSION_ID          STATUS   WORKFLOW       WORKDIR                         AGENTS
daemon-abc123      running  cross-review   /home/user/projects/project-b   claude(opus/high), codex(high)
```

Example MCP/session JSON shape:

```json
{
  "session_id": "daemon-abc123",
  "status": "running",
  "workdir": "/home/user/projects/project-b",
  "workflow": "cross-review",
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
        "permission_mode": "default",
        "command_preview": ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", "opus", "--effort", "high"]
      },
      "codex": {
        "type": "codex",
        "thinking_level": "high",
        "sandbox": "workspace-write",
        "approval_policy": "on-request",
        "command_preview": ["codex", "exec", "--json", "-c", "model_reasoning_effort=\"high\""]
      }
    }
  }
}
```

If a setting is unavailable or not applicable for an agent, omit it rather than inventing a value.

## Data paths

Replace project-local daemon runtime assumptions with a global path helper.

Proposed module shape:

```text
agent_collab/paths.py
  AgentCollabHome
  GlobalDataPaths
  project_config_path(workdir)
  user_config_path(home)
```

Default home:

```text
~/.agent-collab
```

Add an environment override for tests and isolated development:

```text
AGENT_COLLAB_HOME=/tmp/agent-collab-home
```

This keeps tests from writing to a real user home and gives users a way to run isolated daemon instances.

## Config migration layer

Add a dedicated migration module:

```text
agent_collab/config_migrations.py
```

Config load flow should become:

```text
read TOML
parse into raw dict
migrate raw dict to CURRENT_CONFIG_SCHEMA
validate latest shape
convert to dataclasses
```

Working code should never need scattered checks for old config shapes. All compatibility handling belongs in the migrator.

Suggested API:

```python
CURRENT_CONFIG_SCHEMA = 2

def migrate_config_data(data: dict, source: str = "") -> dict:
    ...
```

Suggested internals:

```python
MIGRATIONS = {
    1: migrate_v1_to_v2,
}
```

Missing schema version should mean version 1. The migrator should return a new dict and avoid mutating caller-owned parsed data.

Do not add `modes` -> `workflows` migration for this repo. That rename is a manual shape change in this stage. The migration layer is for future compatibility once config shapes are actually used outside this checkout.

## Lazy fix behavior

Migrations should be lazy and in-memory by default:

- accept known old shapes,
- normalize them before validation,
- attach or emit migration warnings,
- do not rewrite files automatically.

Add a later explicit write path if needed:

```bash
agent-collab config migrate --write --path PROJECT/.agent-collab/config.toml
```

The initial stage only needs load-time migration, not write-back.

## Migration examples

Initial migrations should focus on shapes the project is likely to need after this stage lands.

Examples:

- add missing `schema_version`,
- normalize legacy top-level option aliases,
- convert old agent option field names to current typed option names,
- preserve unknown fields as validation errors after migration rather than silently dropping them.

The migration contract should be conservative: fix shapes that are clearly old valid config, but still reject ambiguous or unsafe data.

## Implementation approach

1. Add global home/data path helpers.
2. Change daemon supervisor PID/state/log paths to use global data.
3. Change daemon-owned session logs to use global `data/sessions`.
4. Add a global session index if current in-memory listing is not enough for daemon restarts.
5. Keep each session's `workdir` in state and use it for config loading and subprocess cwd.
6. Rename `mode` to `workflow` in dataclasses, CLI args, MCP schemas, HTTP payloads, config, docs, and tests.
7. Add effective session settings metadata to start/status/list responses and persisted session state.
8. Add config migration module and call it from `load_config`.
9. Add MCP guidance Markdown and expose it through an MCP guidance tool.
10. Update CLI help and docs from "project-local daemon" to "global local daemon with per-session workdir".
11. Move temp review workdir usage, if kept, under global `data/tmp/`.

## MCP guidance tool

Add a read-only MCP tool:

```text
agent_collab_guidance
```

Purpose: return Markdown guidance for agents that are using agent-collab over MCP.

Tool schema:

```json
{
  "name": "agent_collab_guidance",
  "description": "Return Markdown guidance for using agent-collab MCP tools safely and effectively.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "topic": {
        "type": "string",
        "enum": ["overview", "start", "watch", "options", "errors", "workflows"]
      }
    }
  }
}
```

Guidance source:

```text
doc/mcp-guidance.md
```

The tool should serve the Markdown content. It can return the whole file for `overview`, or a topic-specific section if the implementation keeps headings stable. Keep the tool static and read-only in this stage.

The MCP `initialize.instructions` should remain short and mention:

- call `agent_collab_guidance` for full guidance,
- call `agent_collab_describe_options` before non-default options,
- use `agent_collab_start` with `task`, `workdir`, and `workflow`,
- use `agent_collab_wait_events` with a cursor,
- fix field-path validation errors instead of guessing.

Initial `doc/mcp-guidance.md` should cover:

- what a session is,
- how `workdir` controls project config and subprocess cwd,
- what `workflow` means,
- when to call `agent_collab_describe_options`,
- how to start a session,
- how to read the returned session settings confirmation,
- how to watch with cursors,
- how to handle validation errors.

## Compatibility

Keep these during transition:

- one-shot CLI sessions still work,
- direct JSONL `watch PATH` still works,
- `watch --workdir` can fall back to old project-local `.agent-collab/sessions`,
- MCP tool names stay stable,
- event JSONL schema stays append-compatible.

The `mode` -> `workflow` rename is an intentional breaking change during local pre-release development. Update all internal callers and tests in the same change.

Old project-local daemon state under `PROJECT/.agent-collab/data/` does not need to be loaded automatically by the new global daemon. If migration is needed, add an explicit import command later.

## Tests

Add focused tests for:

- global home path resolution with `AGENT_COLLAB_HOME`,
- daemon start/status/log paths under global data,
- session state includes per-session `workdir`,
- session logs are written under global `data/sessions`,
- config precedence: built-in, user, project, explicit options,
- project A current directory does not affect a project B session,
- config accepts `[workflows.*]` and no longer accepts `[modes.*]`,
- CLI and MCP start payloads use `workflow`,
- start/status/list responses include effective session settings metadata,
- session index persists settings metadata for daemon restart,
- `agent_collab_guidance` returns Markdown guidance,
- config migration from missing-version config,
- config migration does not mutate parsed input,
- migrated config validates through the normal latest-schema validator,
- invalid unknown fields still fail after migration.

## Documentation updates

Update:

- `README.md`,
- `AGENTS.md`,
- `doc/daemon-architecture.md`,
- `doc/agent-configuration.md`,
- `doc/runtime-layout.md`,
- new `doc/mcp-guidance.md`.

The docs should explain:

- runtime state is global under `~/.agent-collab/data`,
- project config remains under `PROJECT/.agent-collab/config.toml`,
- config is loaded from the session `workdir`,
- orchestration is called `workflow`, not `mode`,
- MCP agents can call `agent_collab_guidance` for usage guidance,
- project-local runtime directories are legacy/fallback only.

## Acceptance criteria

- A user can start one daemon and run sessions for multiple projects.
- `agent-collab list` shows sessions across projects.
- `agent-collab status SESSION_ID` shows the session `workdir`.
- start responses confirm the effective workflow, agent models, and relevant settings.
- list/status responses expose the same settings metadata in structured responses.
- Session logs are under the global data root by default.
- Project config from the session `workdir` overrides user config.
- `workflow` replaces `mode` in config, CLI, MCP, and internal state.
- `agent_collab_guidance` serves Markdown guidance over MCP.
- Old known config shapes are migrated in one centralized module before validation.
- The rest of the runtime code only consumes latest-shape config.
