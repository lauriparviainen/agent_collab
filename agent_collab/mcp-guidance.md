# agent-collab MCP guidance

Guidance for agents using the agent-collab MCP tools. Fetch a single topic
with `agent_collab_guidance` and `topic` set to one of: `overview`, `start`,
`watch`, `options`, `errors`, `workflows`. Calling it without a topic (or
with `overview`) returns this whole document.

## Overview

agent-collab runs supervised collaboration sessions between coding agents
(Claude Code and Codex by default; other providers can be configured). One
global daemon owns all sessions;
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

Call `agent_collab_describe_options` with the intended absolute `workdir` before
selecting a workflow or starting. Use `health_refresh: "cached"` normally, or
`"fresh"` when a newer advisory snapshot matters. It returns:

- a canonical-name-keyed registered backend catalog with separate user
  enablement policy, raw health/credential/native evidence, cache age, policy
  assessment, uncertainty, and remediation,
- each configured agent's effective backend and selection source, plus each
  workflow occurrence's effective canonical backend,
- one exact schema per canonical backend name, with allowed values and defaults
  (for example `backend_options.claude_cli.model` and
  `backend_options.codex_sdk.sandbox`).

Only pass entries for backends selected by the chosen workflow; anything else
is rejected. Omit options you do not need — backend-specific configured
defaults are applied automatically and echoed back in the start response.
The compatibility `available` boolean means only `health.status == "ok"`; use
the separate policy and assessment fields for decisions. Discovery never makes
a model call and cannot prove authentication, entitlement, model support, or a
successful turn.

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
`interactive_idle_timeout`, `backend_options`, `backend`.

Start is authoritative for preflight: it reloads workdir config, re-resolves
the exact selection, rejects disabled backends, revalidates options, and freshly
probes selected backends whose `start_probe_policy` is `fresh`, all before
creating session state. Backends marked `not_probed` deliberately defer health
failure to the real turn. In every case, the first real turn remains the
authority for provider-side failures a side-effect-free probe cannot establish.

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
   bounded `timeout_ms` (for example 20000); it returns as soon as new
   events exist or the timeout elapses,
3. after a nonterminal response containing only routine progress or tool
   events, wait at least 20 seconds before the next observation call; do not
   immediately poll again merely because the long-poll returned early. Use a
   tighter cadence only when the user requests it or an actionable event needs
   an immediate follow-up,
4. inspect `status`, `terminal`, `error`, `failure`, and `turn_outcomes` on
   every read/wait response, including responses with `events: []`; stop when
   `terminal` is true. `awaiting_input` is live, not terminal. An older daemon
   may omit this additive view; only then use `agent_collab_status` as the
   compatibility fallback.

Never make one unbounded blocking call. Always pass the cursor from the
previous response, not a guess. Tool events default to compact summaries that
include an absolute event id. If the payload is genuinely needed, fetch only
that event with `cursor: EVENT_ID`, `limit: 1`, and `tool_output: "full"`.
`agent_collab_read_transcript` likewise summarizes tool payloads by default;
pass `tool_output: "full"` for the stored Markdown transcript.

## Errors

If `agent_collab_start` returns `isError` with `invalid_start_options`, the
`details` list contains one entry per problem with a field `path` (for
example `backend_options.claude_cli.model`) and a `message` naming the allowed values.
Fix exactly the named fields and retry; do not guess or drop the options.
Backend failures also include a machine-readable `code`, canonical backend,
check timestamp, and structured remediation when known. A disabled backend must
be enabled in the user config, not project config. If discovery said usable but
the real turn fails, prefer the turn error, request fresh discovery, and
remediate deliberately rather than automatically oscillating between backends.
If a workflow or agent is unknown, call `agent_collab_describe_options` for
the same `workdir` and choose from what it lists. Unknown `session_id`
errors usually mean the id was mistyped or belongs to a different daemon.

For a started session, treat `turn_outcomes` as the authoritative per-turn
history and key entries by `turn_id`, never by array position. A required
sequential or directed turn continues the workflow only when its outcome is
`completed`. Use the structured `failure.code` and canonical `failure.message`
for remediation. Provider identity, partial transcript prose, raw terminal
payloads, an exit-zero status, and the absence of a Python exception do not
prove success. Do not infer `refused` from model prose.
