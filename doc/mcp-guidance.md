# agent-collab MCP guidance

Guidance for agents using the agent-collab MCP tools. Fetch a single topic
with `agent_collab_guidance` and `topic` set to one of: `overview`, `start`,
`watch`, `options`, `errors`, `workflows`. Calling it without a topic (or
with `overview`) returns this whole document.

## Overview

agent-collab runs supervised collaboration sessions between subprocess
coding agents (Claude Code and Codex). One global daemon owns all sessions;
sessions for any number of projects can run side by side.

A session is one supervised run of a task:

- it has a `session_id`, a `status` (`running`, `awaiting_input`, `done`,
  `failed`, `stopped`, `interrupted`), a `task`, a `workflow`, and a `workdir`,
- its events are appended to a JSONL log and mirrored to a Markdown
  transcript under the global data root (`~/.agent-collab/data/sessions`),
- it survives daemon restarts in a persistent session index; sessions that
  were running when the daemon died are marked `interrupted`.

The session `workdir` matters twice:

- project config is loaded from `WORKDIR/.agent-collab/config.toml` and
  layered over the user config and built-in defaults,
- agent subprocesses run with `workdir` as their working directory.

Always pass an explicit absolute `workdir`; MCP start and describe-options calls
require it. The daemon's own location and the caller's shell directory never
affect a session's config.

## Workflows

A `workflow` names the orchestration pattern a session runs: the ordered
sequence of agents that take turns on the task. Built-in workflows:

- `solo-claude` — one Claude turn,
- `solo-codex` — one Codex turn,
- `cross-review` — Claude, then Codex review, then Claude revision
  (the default),
- `compare` — Claude and Codex each answer once.

Projects and users can define more under `[workflows.*]` in config. Call
`agent_collab_describe_options` to list the workflows that exist for a
given `workdir`, including each workflow's sequence and agent types.

## Options

Call `agent_collab_describe_options` (with the session's required `workdir`) before
passing non-default `codex_options` or `claude_options`. It returns:

- available workflows and their agent types,
- registered execution backends with health, capabilities, and an effective
  `option_schema` for each backend,
- the union of accepted option fields per agent type, with allowed values and
  configured defaults (for example `claude_options.model`,
  `claude_options.thinking_level`, `codex_options.sandbox`,
  `codex_options.approval_policy`).

Only pass options for agent types that the chosen workflow actually uses and
choose fields declared by every selected backend of that provider; anything
else is rejected. Omit options you do not need — backend-specific configured
defaults are applied automatically and echoed back in the start response.

## Start

Start a session with `agent_collab_start`:

```json
{
  "task": "Review project B",
  "workdir": "/home/user/projects/project-b",
  "workflow": "cross-review"
}
```

Optional fields: `max_turns`, `timeout`, `mock`, `dry_run`, `interactive`,
`interactive_idle_timeout`, `codex_options`, `claude_options`.

The response is your confirmation of what the server is about to run. Check
it before watching:

- `workflow` and `settings.workflow.sequence` — the effective turn order,
- `settings.agents.<id>` — the effective typed options per agent (model,
  thinking level, permission/sandbox settings where applicable),
- `settings.agents.<id>.backend_summary` — the selected backend's own summary
  of the exact normalized options passed to its runner,
- `settings.agents.<id>.command_preview` — the exact subprocess command
  prefix, without the task prompt,
- `jsonl_path` / `markdown_path` — where logs are written.

If a setting is missing from the response it was not configured; nothing is
invented. The same `settings` block is returned by
`agent_collab_list_sessions` and `agent_collab_status`.

Interactive sessions may move to `awaiting_input` after the planned workflow
finishes. Use `agent_collab_post_message` with `text` and optional `target` to
append referee input or ask one enabled session agent a directed question.
Messages are accepted only for live sessions that were started with
`interactive: true`.

## Watch

Read events incrementally with a cursor:

1. call `agent_collab_read_events` with `cursor: 0` to get existing events
   and the next cursor,
2. loop `agent_collab_wait_events` with the last returned `cursor` and a
   bounded `timeout_ms` (for example 30000); it returns as soon as new
   events exist or the timeout elapses,
3. stop when `agent_collab_status` reports a terminal status and no new
   events arrive. `awaiting_input` is live, not terminal.

Never make one unbounded blocking call. Always pass the cursor from the
previous response, not a guess. `agent_collab_read_transcript` returns the
whole Markdown transcript when you want a readable summary instead of raw
events.

## Errors

If `agent_collab_start` returns `isError` with `invalid_start_options`, the
`details` list contains one entry per problem with a field `path` (for
example `claude_options.model`) and a `message` naming the allowed values.
Fix exactly the named fields and retry; do not guess or drop the options.
If a workflow or agent is unknown, call `agent_collab_describe_options` for
the same `workdir` and choose from what it lists. Unknown `session_id`
errors usually mean the id was mistyped or belongs to a different daemon.
