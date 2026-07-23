# agent-collab MCP guidance

Guidance for agents using the agent-collab MCP tools. Fetch one topic with
`agent_collab_guidance` and `topic` set to one of: `overview`, `delegate`,
`start`, `watch`, `options`, `errors`, `workflows`, `review-recipe`. No topic
returns this whole document; `overview` returns only the overview section.

Everything here is MCP-only: it describes MCP tool calls, never CLI commands
or local filesystem paths. Read a transcript with `agent_collab_read_transcript`.

## Overview

agent-collab runs supervised collaboration sessions between coding agents
(Claude Code and Codex by default; other providers can be configured). One
global daemon owns all sessions across any number of projects.

A session is one supervised run of a task:

- it has a `session_id`, a `status` (`running`, `awaiting_input`, `done`,
  `failed`, `stopped`, `interrupted`), a `task`, a `workflow`, and a `workdir`,
- its events append to a JSONL log mirrored as a Markdown transcript under
  the global data root (`~/.agent-collab/data/sessions`),
- it survives daemon restarts in a persistent session index; sessions running
  when the daemon died are marked `interrupted`.

The session `workdir` matters twice: project config is loaded from
`WORKDIR/.agent-collab/config.toml` and layered over the user config and
built-in defaults, and agent subprocesses run with `workdir` as their working
directory. Always pass an explicit absolute `workdir`; MCP start and
describe-options calls require it. The daemon's own location and the caller's
shell directory never affect a session's config.

## Delegate

Run another agent as a subagent and collect its result over MCP alone:

1. `agent_collab_describe_options` for the absolute `workdir`; confirm the
   models, backends, and effective options with the user before a paid start.
2. `agent_collab_start` — `solo` with `members` picks the agent; add
   `interactive: true` for a back-and-forth.
3. Collect with `agent_collab_wait_result`: it blocks until the session
   settles, then returns each agent's latest completed-turn `answer` with
   `status`, `failure`, and `cursor`. Use one long bounded `timeout_ms`
   (60000–120000), or short calls that re-poll immediately on a heartbeat
   (`settled: false`); no pacing delay, the block is server-side. `settled`
   with status `awaiting_input` means you may post a follow-up.
4. Read `answers`. Each carries an `event_id`; re-fetch it at full fidelity
   with `agent_collab_read_events` (`cursor: event_id`, `limit: 1`,
   `tool_output: "full"`), or read the whole thread with
   `agent_collab_read_transcript`.
5. Follow up (interactive only): `agent_collab_post_message` with `text`, then
   `wait_result` again. `target` picks one agent; in a solo session an
   untargeted post routes to the sole agent. Follow-up cost depends on the
   agent's `settings.agents.<id>.capabilities.continuity`: when true, the turn
   continues the provider thread natively (only new events sent); when false
   (every backend today), it re-sends the task and a recent window, costing like
   a fresh turn.
6. End with `agent_collab_stop`, or let the session close on its
   `interactive_idle_timeout` (raise it for long conversations).

`wait_events` is only for streaming live events; for a delegated outcome use
`wait_result`.

## Workflows

A `workflow` names the orchestration pattern a session runs: an ordered
sequence of agent turns or one concurrent review group. Built-in workflows:

- `solo` — one agent turn (Claude by default; pick any enabled agent with
  `members`),
- `cross-review` — Claude, then Codex review, then Claude revision
  (the default),
- `dual-review` — Claude and Codex independently, in parallel.

Projects and users can define more under `[workflows.*]` in config. Call
`agent_collab_describe_options` to list the workflows for a given `workdir`,
including each workflow's ordered member list, optional `parallel` list, and
agent types. Parallel workflows are non-interactive.

## Options

Call `agent_collab_describe_options` with the intended absolute `workdir`
before selecting a workflow or starting. Use `health_refresh: "cached"`
normally, or `"fresh"` when a newer advisory snapshot matters.

`model_refresh` (`"none" | "cached" | "fresh"`, default `"cached"`) controls
the per-backend **model catalog**. It deliberately adds `"none"` beyond the
`health_refresh` modes because catalog probes run the provider's model-listing
command, which can require live auth and incur cost: `"none"` and `"cached"`
are strictly local and never contact a provider, while `"fresh"` runs live
listing commands — treat it like other paid actions and confirm with the user
first (repeated `"fresh"` calls are additionally bounded by a per-backend
minimum re-probe interval). Serve precedence is: fresh successful catalog,
then the last-known-good cached catalog (flagged `stale`), then the static
`suggested` fallback. Each backend entry carries `model_catalog` (observation
status, timestamps, discovered models) and `effective.option_schema`, whose
`model.suggested` merges `[configured default] + [discovered catalog] +
[static fallback]` with order-preserving dedup. The configured default model
is warn-only: an authoritative (`ok` + complete, not stale) catalog that omits
it adds the non-fatal `configured_default_not_in_catalog` reason code, but the
default is always passed through unchanged — catalog naming may differ from
option values, and the provider's first-turn error remains the authority.

It returns:

- a `backends` catalog keyed by backend name (`claude_cli`, `codex_cli`, …)
  where each entry is fully self-describing: user enablement policy, raw
  health/credential/native evidence, cache age, policy assessment,
  uncertainty, remediation, and one exact option schema with allowed values
  and defaults (for example `backend_options.claude_cli.model` and
  `backend_options.codex_sdk.sandbox`),
- each configured agent's effective backend and selection source, plus each
  workflow occurrence's effective backend,
- each workflow's member slots under `workflows[].member_selection`: the slot
  name (the configured member id), its default, and the enabled agents
  eligible to fill it, plus `distinct_members` for parallel shapes.

Backends are isolated peers — `claude_cli` and `claude_sdk` are different
backends that happen to share a vendor; never treat one as a variant or
fallback of the other. Pass `backend_options` entries only for backends the
chosen workflow selects; anything else is rejected. Omit options you do not
need — configured defaults apply automatically and are echoed back in the
start response. Use the policy and assessment fields for decisions. Discovery
never makes a model call and cannot prove authentication, entitlement, model
support, or a successful turn.

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
`interactive_idle_timeout`, `backend_options`, `backend`, `members`. For a
quick smoke test without provider calls, start
`{"task": "...", "mock": true, "max_turns": 1}`.

`members` runs a named workflow shape with different agents: it maps a slot
(the configured member id from `member_selection.slots`) to any enabled agent,
so `{"workflow": "dual-review", "members": {"codex_cli": "xai_cli"}}` reviews
with Claude and xAI without config changes. A sequence slot reprises
(`cross-review`'s lead fills both of its positions); parallel members must
stay distinct. Invalid selections are rejected with `members.<slot>` field
paths. `members` chooses agents; `backend` and `backend_options` stay
orthogonal transport and option choices.

Start is authoritative for preflight: it reloads workdir config, re-resolves
the exact selection, rejects disabled backends, revalidates options, and
freshly probes selected backends whose `start_probe_policy` is `fresh`, all
before creating session state. Backends marked `not_probed` deliberately defer
health failure to the real turn, and `mock` or `dry_run` starts skip health
probes entirely. In every case the first real turn remains the authority for
provider-side failures a side-effect-free probe cannot establish.

The response confirms what the server is about to run; check it before
watching. `settings` defaults to a compact view:

- `workflow` and `settings.workflow.sequence` — the effective ordered members,
- `settings.workflow.parallel` — the concurrent group (null when sequential),
- `settings.agents.<id>` — the effective typed options per agent (model,
  thinking level, permission/sandbox settings where applicable) plus `backend`
  and `capabilities`,
- `jsonl_path` / `markdown_path` — where logs are written.

Pass `detail: "full"` on start or status to also get
`settings.agents.<id>.command_preview` (the exact subprocess command prefix,
without the task prompt) and `.backend_summary` (the backend's own summary of
the normalized options). A setting missing from the response was not
configured; nothing is invented. `agent_collab_list_sessions` and
`agent_collab_status` return the same `settings` block (list is always
compact).

Interactive sessions may move to `awaiting_input` after the planned workflow
finishes. `agent_collab_post_message` (`text`, optional `target`): a targeted
post — or an untargeted post in a solo session — runs a directed turn you
collect with `agent_collab_wait_result` (see Delegate); an untargeted post to a
multi-agent session is recorded only, with no turn. Messages are accepted only
for live sessions started with `interactive: true`.

## Watch

This is the live-streaming path (progress, tool calls, messages). To collect
only a session's outcome, use `agent_collab_wait_result` (see Delegate); the
>= 20s pacing rule below applies to `wait_events` streaming, not to
`wait_result`.

Read events incrementally with a cursor:

1. call `agent_collab_read_events` with `cursor: 0` for existing events and
   the next cursor,
2. loop `agent_collab_wait_events` with the last returned `cursor` and a
   bounded `timeout_ms` (for example 20000); it returns as soon as new events
   exist or the timeout elapses,
3. after a nonterminal response with only routine progress or tool events,
   wait at least 20 seconds before the next observation call, even when the
   long-poll returned early; poll tighter only when the user asks for it or
   an actionable event needs an immediate follow-up,
4. inspect `status`, `terminal`, `error`, `failure`, and `turn_outcomes` on
   every response, including ones with `events: []`; stop when `terminal` is
   true. `awaiting_input` is live, not terminal. If the daemon predates this
   additive view, fall back to `agent_collab_status`.

Never make one unbounded blocking call. Always pass the cursor from the
previous response, not a guess. Tool events default to compact summaries with
an absolute event id; if a payload is genuinely needed, fetch that one event
with `cursor: EVENT_ID`, `limit: 1`, and `tool_output: "full"`.
`agent_collab_read_transcript` likewise summarizes tool payloads unless
`tool_output: "full"` is passed.

## Errors

An `isError` start with `invalid_start_options` lists one `details` entry per
problem: a field `path` (for example `backend_options.claude_cli.model`) and a
`message` naming the allowed values. Fix exactly the named fields and retry;
do not guess or drop the options. Backend failures add a machine-readable
`code`, the backend name, a check timestamp, and structured remediation when
known. A disabled backend must be enabled in the user config, not project
config. If discovery said usable but the real turn fails, prefer the turn
error, request fresh discovery, and remediate deliberately instead of
oscillating between backends. For an unknown workflow or agent, call
`agent_collab_describe_options` for the same `workdir` and choose from what it
lists. Unknown `session_id` errors usually mean a mistyped id or a different
daemon.

For a started session, `turn_outcomes` is the authoritative per-turn history;
key entries by `turn_id`, never by array position. A required sequential or
directed turn continues the workflow only when its outcome is `completed`. Use
the structured `failure.code` and canonical `failure.message` for remediation.
Provider identity, partial transcript prose, raw terminal payloads, an
exit-zero status, and the absence of a Python exception do not prove success.
Do not infer `refused` from model prose.

## Review recipe

Use this recipe for solo and parallel cross-model review skills. The option
schema returned by `agent_collab_describe_options` is authoritative; never
invent model or option values.

### 1. Freeze the scope

1. Resolve the repository to an absolute `workdir`.
2. Choose and state exactly one base: the default current diff (working tree,
   staged changes, and untracked files against `HEAD`) or an explicit base ref
   supplied by the user.
3. Parse `git diff --name-status -z <base>` plus
   `git ls-files --others --exclude-standard -z`. Build a de-duplicated,
   one-path-per-line list:
   - modified, added, or copied: destination path,
   - deleted: deleted path,
   - renamed: source and destination as separate paths,
   - untracked: untracked path.
4. Freeze that list before starting reviewers. It is the primary scope, not a
   hard visibility wall: a reviewer may open a direct dependency needed to
   prove a finding, but must not run repository-wide searches.

### 2. Select and confirm reviewers

1. Call `agent_collab_describe_options` with the absolute workdir.
2. Use only enabled, `start_eligible` workflows. Identify each reviewer by
   agent id, configured model, and backend. Backend or provider names alone
   do not prove model diversity; Antigravity can run a Claude model.
3. Honor models named by the user. If a reviewer model is unclear, show each
   eligible member's configured model plus schema-allowed model overrides and
   ask. Ask for a backend only when the selected model is ambiguous across
   eligible backends. Do not silently pick the strongest or cheapest.
4. Before the paid start, show the workflow, agent ids, models, backends,
   effective configured defaults, and overrides, and ask for explicit
   confirmation. Defaults need no separate choice.

### 3. Build the prompt

Use this template, filling every placeholder:

```text
Review the current diff read-only.
Workdir: <absolute-workdir>
Base: <HEAD-or-explicit-ref>
Changed files (one path per line):
<changed-file-list>

Focus on correctness, security, regressions, and missing tests. Stay within
the listed files except for a direct dependency needed to prove a finding; do
not run repository-wide searches. Report only high- or medium-severity
findings. Every finding must include severity, a resolvable file:line, and a
concrete failure scenario. For a deleted file, cite the base-side line. Do not
propose stylistic rewrites. Do not edit files. If there are no qualifying
findings, say so.
```

Prompt-level read-only instructions are behavioral, not a security boundary.
The shipped backend defaults already enforce a read-only posture where the
provider supports one (`claude_cli` `permission_mode=default`, `codex_cli`
`sandbox=read-only`, `antigravity_cli` `mode=plan`, `xai_cli`
`sandbox=read-only`); verify with `agent_collab_describe_options` that no
configured override loosens it, and include the effective value in the
pre-start confirmation.

### 4. Start and watch

Pass `interactive: false` so a review cannot park in `awaiting_input`. For dual
review, make one start call for a two-member `parallel` workflow; the daemon
starts both reviewers over one frozen prompt and emits one attributed stream.

This recipe streams with `wait_events` to triage findings as they arrive;
`agent_collab_wait_result` returns only the settled outcome and is the simpler
collector when you just need each reviewer's final answer.

```text
session = agent_collab_start(..., interactive=false)
batch = agent_collab_read_events(session_id=session.session_id, cursor=0)
cursor = batch.cursor
consume(batch.events)

while not batch.terminal:
    batch = agent_collab_wait_events(
        session_id=session.session_id,
        cursor=cursor,
        timeout_ms=20000,
    )
    cursor = batch.cursor                 # always advance to returned cursor
    consume(batch.events)
    inspect(batch.status, batch.failure, batch.turn_outcomes)
    if batch is routine and nonterminal:
        wait at least 20 seconds before the next observation call
```

Terminate only when `terminal` is true, never because `events` is empty. For a
parallel workflow, key member events and outcomes by `agent_id`, reconcile
only after terminal status, and map each member to its backend. Prefix every
surfaced reviewer finding with `[<session_id> <canonical_backend>]`.

### 5. Triage and reconcile

For every candidate finding:

1. Reject it if severity is not high/medium or `file:line` does not resolve.
2. Open the cited location and trace the concrete scenario through the real
   code and relevant tests. For a deletion, inspect the base blob and diff.
3. Keep it only when confirmed. Downgrade high to medium when impact is real
   but narrower than claimed; drop unconfirmed claims.
4. Never auto-apply a reviewer suggestion.

For dual review, label same/overlapping-location findings with the same
failure scenario as `Agreement`; agreement raises confidence but is not proof.
Label conflicts and single-reviewer findings as `Disagreement`, then
adjudicate by reading code and tests, never by majority vote.

### Advisory backend quirks (2026-07-15)

| Provider | Behavioral guidance not expressed by the schema |
| --- | --- |
| Antigravity | `mode=plan` (the shipped default) is the read-only review mode; do not switch to `accept-edits` for a review. |
| xAI | Keep the shipped default `permission_mode=bypassPermissions` with `sandbox=read-only`; the sandbox is the safety boundary. Do not override to `auto` or `plan` for a headless run: both can end the turn as `provider_turn_cancelled` — `auto` raises a permission prompt for commands its classifier will not auto-approve (chained `;` pipelines) that no one can answer headlessly and that cancels the turn after 15s. |
| Codex | Include the explicit file list and prohibit broad repository greps. |

Re-read allowed values, defaults, and models from
`agent_collab_describe_options` at runtime. If this dated matrix conflicts
with the schema, the schema wins.
