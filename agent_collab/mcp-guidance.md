# agent-collab MCP guidance

Guidance for agents using the agent-collab MCP tools. Fetch one topic with
`agent_collab_guidance` and `topic` set to one of: `overview`, `delegate`,
`start`, `watch`, `interrupt`, `resume`, `options`, `errors`, `workflows`, `review-recipe`.
No topic returns this whole document; `overview` returns only the overview section.

Everything here is MCP-only: it describes MCP tool calls, never CLI commands
or local filesystem paths. Read with `agent_collab_read_transcript` and `agent_collab_read_events`.

## Overview

One global daemon owns supervised multi-agent sessions. A session has
`session_id`, `status` (`running`, `awaiting_input`, `awaiting_approval`,
`done`, `failed`, `stopped`, `interrupted`), `task`, `workflow`, and
`workdir`. Read with `agent_collab_read_events` / `agent_collab_read_transcript`.

`workdir` applies twice: project config from `WORKDIR/.agent-collab/config.toml`
layered over user config and defaults, and as the agents' cwd.

Per-backend `continuity` / `resume` / `interrupt` / `tool_gate` live in
`describe_options` `backends.*.static.capabilities` and
`settings.agents.<id>.capabilities`. Session `resumable` / `interruptible` /
`continuity` are AND-reduced across selected backends. `tool_gate` is
per-agent, not session-AND'd — never wait for `awaiting_approval` when that
agent's `tool_gate` is false. Start-time `resumable=false` is expected
(empty capture). Resume and interrupt: topics `resume` and `interrupt`.

## Delegate

Run another agent as a subagent and collect its result over MCP alone:

1. `agent_collab_describe_options` for the workdir; confirm with the user
   before a paid start (Options).
2. `agent_collab_start` — `solo` with `members` picks the agent; add
   `interactive: true` for a back-and-forth.
3. Watch with `agent_collab_wait_events` (cursor / `view` / `types`: Watch).
   Stop states: Watch (`terminal`, `awaiting_input`, `awaiting_approval`).
4. Harvest or park with `agent_collab_wait_result`:
   - `settled` + terminal: harvest `answers` (preview-bounded; re-fetch via
     `event_id` — Watch). A non-`done` terminal also carries `events_tail`.
     Read `turn_outcomes` and `failure` when an agent has no answer.
   - `settled` + `awaiting_input`: follow-up-ready; you may `post_message`.
   - `settled` + `awaiting_approval`: park, not harvest (`terminal=false`,
     `pending_approvals`). One `agent_collab_approval` per `request_id`
     (`approve` or `deny`). Digest drops `request_id`; take it from the
     park payload. Remaining requests stay parked. There is no
     `wait_approval` or `list_approvals`. Read the decision response
     `outcome`, not just the absence of an error: an `approve` whose
     decision frame cannot reach the agent comes back `outcome:
     "auto_denied"`, `status: "delivery_failed"`, and the tool was denied.
     `pending_approvals_omitted > 0` means more requests are parked than
     the payload budget could list; resolve the listed ones and re-poll
     `wait_result` for the rest.
   - `settled: false`: heartbeat — re-poll immediately, no 20s pace.
   `timeout_ms: 0` is an instant peek. Default `timeout_ms` is 45000; do
   not exceed it (clients kill near 60 s). Use `wait_result` alone only
   when you need nothing but the outcome.
5. Follow-up (interactive only): `agent_collab_post_message`, then collect
   with `agent_collab_wait_result`, not a watch loop — status stays
   `awaiting_input` for the whole directed turn. `target` picks one agent;
   solo untargeted posts route to the sole agent. Follow-up cost depends
   on whether that agent's runner still holds provider context, not on the
   flag alone: `settings.agents.<id>.capabilities.continuity` is the
   advertised both-path-proven claim, and every CLI backend continues its
   provider thread inside one live session while that flag stays `false`,
   so a CLI follow-up after a completed turn costs a delta, not a re-sent
   task. Steer in-flight with `agent_collab_interrupt` (Interrupt), then
   `post_message`.
6. End with `agent_collab_stop`, or let `interactive_idle_timeout` close it.
   `terminal: true` is not always the end of the thread: `stopped` and
   `interrupted` may still be reopened with `agent_collab_resume` (Resume).

## Start

Always pass an explicit absolute `workdir` on `agent_collab_describe_options`
and `agent_collab_start`. Daemon location and caller cwd never apply.

Omitted `workflow` is `cross-review` (Claude → Codex → Claude: three paid
turns). Pass `solo` for one agent.

```json
{
  "task": "Review project B",
  "workdir": "/home/user/projects/project-b",
  "workflow": "solo"
}
```

Optional: `max_turns`, `timeout`, `mock`, `dry_run`, `interactive`,
`interactive_idle_timeout`, `approval_deadline`, `backend_options`, `backend`,
`members`, `sandbox`. Top-level `sandbox` is agent-collab's outer filesystem
policy (`read-only` or `none`), distinct from provider-native values in
`backend_options`. All shipped backends support outer `read-only`; others
fail closed. The shipped default is `read-only`, and disabling it requires an
explicit `sandbox: "none"`. `mock: true` skips health probes and provider
calls — a smoke test, not a control-loop proof of interrupt, resume, or
`tool_gate`.

`members` maps a workflow slot to any enabled agent, e.g.
`{"workflow": "dual-review", "members": {"codex_cli": "xai_cli"}}`. Sequence
slots reprise; parallel members must stay distinct. Invalid selections
reject with `members.<slot>` field paths. `members` chooses agents;
`backend` / `backend_options` stay orthogonal. On `invalid_start_options`,
fix the named field paths (Errors).

Start reloads workdir config, re-resolves selection, rejects disabled
backends, revalidates options, and freshly probes selected backends whose
`start_probe_policy` is `fresh` before creating state. `not_probed` defers
health failure to the real turn. The first real turn remains the authority.

The response `settings` (compact; `detail: "full"` adds `command_preview`
and `backend_summary`) include `workflow` / `settings.workflow.sequence` /
`settings.workflow.parallel`, `settings.agents.<id>` (typed options,
`backend`, `capabilities`, `outer_sandbox`), and `settings.sandbox`. A
missing setting was not configured. `agent_collab_list_sessions` and
`agent_collab_status` return the same block (list is always compact).

`agent_collab_post_message` is accepted only on live `interactive: true`
sessions. Collect a directed turn as in Delegate.

## Watch

Follow a session with bounded `agent_collab_wait_events` polls so you stay
steerable (the user's message reaches you when the call returns).

Schema defaults are `cursor: 0`, `view: "events"` (full raw),
`timeout_ms: 30000`. Watchers must pass the returned `cursor` (never a
guess), `view: "digest"`, `types: ["message", "error", "approval_request",
"approval_resolved"]`, and `timeout_ms` 20000–30000 (never above 45000;
clients kill near 60 s). The wait returns on new events or timeout.

`types` filters the returned batch only; the cursor still advances over
every scanned event and the wait still wakes on any event or status change.
Never stop on an empty batch — it can mean filtered-out events, not idle.
Inspect `status`, `terminal`, `error`, `failure`, and `turn_outcomes` every
response, including `events: []`.

Stop on `terminal`, `awaiting_input`, or `awaiting_approval`. Then
`agent_collab_wait_result` for harvest vs park (`pending_approvals` on
`awaiting_approval`). `timeout_ms` is the poll bound, not the approval
deadline.

Pace only after an *early* return of routine progress — wait ~20 s. A
full-duration block already paced the loop.

`view: "digest"` drops `raw`, caps `text`, stamps `event_id`. `tool_output`
has no effect on digest. Digest text is a scent: do not rebuild a result
from it. Re-fetch one event with `agent_collab_read_events` (`cursor:
EVENT_ID`, `limit: 1`, `tool_output: "full"`; no `types` on a re-fetch —
`limit` counts scanned events). An unfiltered events batch omits
`event_id` (id = request cursor plus index).

## Interrupt

`agent_collab_interrupt` parks a live interactive in-flight turn at
`awaiting_input` so `post_message` can steer. It abandons every remaining
planned workflow stage: the session then continues only as directed turns and
ends `done` when the input loop closes. It also denies every pending tool
approval (count in `interrupt.approvals_denied`). It is not idempotent: a
second call is a `conflict`, as is any session that is not live, not
interactive, or has no in-flight turn. `agent_collab_stop` ends the session; do
not use stop to keep an *in-flight turn* alive. Stop on an already-parked
session is the route to a resumable session (topic `resume`).

Check per-agent `settings.agents.<id>.capabilities.interrupt` for every
in-flight agent. Session `interruptible` is the AND of selected backends, not
of the in-flight set. Any in-flight agent with `interrupt=false` (or a missing
capabilities dict/key) fails closed: `code=unsupported` naming the blocking
agent id(s). Status, pending approvals, and in-flight turns stay unchanged;
no abort is issued.

`fallback_cancelled` is only an issued abort that missed ACK (or advertised
interrupt that did not issue). It is not the unsupported path.

Session status `interrupted` means the daemon died (restore of a live
session). Operator abort is a turn outcome `interrupted` /
`local_turn_interrupted` and does not make the session resumable after reload.

## Resume

`agent_collab_resume` needs two things at once. Session status must be
`stopped` or `interrupted` (topic `interrupt` owns what `interrupted` means);
`done` and `failed` are `ineligible` and cannot be reopened. And
`last_turn_status` must be `completed` for every started agent — read it from
`agent_sessions.<agent_id>.last_turn_status` on `agent_collab_status`.
Operator-interrupted turns (`last_turn_status=interrupted`) are ineligible
even with `interrupt_acknowledged`.

To reach an eligible state deliberately: start `interactive: true`, let a turn
complete and park at `awaiting_input`, then `agent_collab_stop` — a stopped
parked session is resume-eligible. An `interactive: false` session (including
the review recipe) that ran every planned stage ends `done` and cannot be
reopened; one stopped or reloaded mid-workflow keeps its remaining stages.

Start-time `resumable=false` is expected: capture is empty until a completed
descriptor exists. Per-agent `settings.agents.<id>.capabilities.resume` is
the advertisement; session `resumable` reports descriptor readiness on a
live or resume-eligible session and is false on `done` / `failed`. It does
not by itself decide the call — a live session can still project `resumable`
and be refused as `conflict`.

A live session is a `conflict`. Daemon restore alone never starts a paid turn,
but `agent_collab_resume` can: reopening a session parked in the input loop
costs nothing and returns at `awaiting_input`, while reopening one with planned
stages left runs them as paid turns — confirm the cost with the user first, as
for a start. A quarantined descriptor cannot be repaired in place; start a new
session. Cursor/transcript continue after a successful resume; the original
task is not re-emitted — after resume collect with `agent_collab_wait_result`,
then `agent_collab_post_message`.

## Options

Call `agent_collab_describe_options` with the intended absolute `workdir`
before selecting a workflow or starting. Confirm the models, backends, and
effective options with the user before a paid start. `health_refresh:
"cached"` normally, or `"fresh"` when a newer advisory snapshot matters.

`model_refresh` (`"none" | "cached" | "fresh"`, default `"cached"`) controls
the per-backend model catalog. `"none"` and `"cached"` are local and never
contact a provider. `"fresh"` runs live listing commands — confirm with the
user first (repeated `"fresh"` is bounded by a per-backend re-probe
interval). Serve order: fresh successful catalog, then last-known-good
cached (`stale`), then static `suggested` fallback. Each backend carries
`model_catalog` and `effective.option_schema`, whose `model.suggested`
merges `[configured default] + [discovered catalog] + [static fallback]`
with order-preserving dedup. An authoritative catalog that omits the
configured default adds `configured_default_not_in_catalog` (warn-only);
the default still passes through.

It returns a `backends` catalog keyed by name (`claude_cli`, `codex_cli`,
…) — enablement, health/credential evidence, cache age, policy, uncertainty,
remediation, and one option schema — plus each agent's effective backend,
each workflow occurrence's backend, and `workflows[].member_selection`
(slot name, default, eligible agents, `distinct_members`).

Backends are isolated peers — `claude_cli` and `claude_sdk` are different
backends that share a vendor; never treat one as a variant or fallback of
the other. Pass `backend_options` only for backends the chosen workflow
selects; anything else is rejected. Omit unused options; configured
defaults apply and echo in the start response. Discovery never makes a
model call and cannot prove authentication, entitlement, or a successful
turn.

## Workflows

Built-in `workflow` names: `solo` (one agent; Claude by default, or
`members`), `cross-review` (Claude → Codex review → Claude revision),
`dual-review` (Claude and Codex in parallel). Projects/users may add
`[workflows.*]`. `agent_collab_describe_options` lists each workdir's
workflows, ordered members, optional `parallel` list, and agent types.

Parallel workflows fail closed if `interactive` is true
(`invalid_start_options` path `interactive`). Pass `interactive: false` or
omit it. `interactive: true` is required for `post_message` / interrupt park.

## Errors

An `isError` start with `invalid_start_options` lists one `details` entry per
problem: a field `path` (for example `backend_options.claude_cli.model`) and a
`message` naming the allowed values. Fix exactly the named fields and retry;
do not guess or drop the options. Backend failures add a `code`, backend name,
timestamp, and structured remediation. Enable a disabled backend in user
config, not project. Prefer the real-turn error over discovery. Unknown
workflow/agent: `agent_collab_describe_options` for the same `workdir`.
Unknown `session_id`: mistyped id or a different daemon.

`agent_collab_resume` codes: `conflict`, `ineligible`, `incompatible`,
`quarantined`, `not_found`. Do not retry a quarantined resume in place.
`agent_collab_interrupt` codes: `conflict`, `not_found`, `unsupported`.
`agent_collab_approval` codes: `not_found`, `conflict`, `stale`.

`turn_outcomes` is authoritative per-turn history; key by `turn_id`, never
array position. A required sequential or directed turn continues only when
`completed`. Use `failure.code` and canonical `failure.message`. Provider
identity, transcript prose, raw payloads, exit-zero, and no Python exception
do not prove success. Do not infer `refused` from model prose.

## Review recipe

Use this for solo and parallel cross-model review. The option schema from
`agent_collab_describe_options` is authoritative; never invent option values.

### 1. Freeze the scope
Resolve an absolute `workdir`. Choose one base: current diff (working tree,
staged, untracked vs `HEAD`) or an explicit user base ref. Parse
`git diff --name-status -z <base>` plus
`git ls-files --others --exclude-standard -z`. De-duplicate, one path per
line: modified/added/copied → destination; deleted → deleted path;
renamed → source and destination separately; untracked → untracked path.
Freeze before starting reviewers. Scope is primary, not a hard wall: a
reviewer may open a direct dependency to prove a finding, not repo-wide
searches.
### 2. Select and confirm reviewers
Call `agent_collab_describe_options` with the absolute workdir. Use only
enabled, `start_eligible` workflows. Identify each reviewer by agent id,
configured model, and backend — names alone do not prove model diversity;
Antigravity can run a Claude model. Honor user-named models; if unclear,
show eligible models/overrides and ask. Ask for a backend only when the
model is ambiguous across backends. Do not silently pick strongest or
cheapest. Before a paid start, show workflow, agent ids, models, backends,
defaults, and overrides, and get explicit confirmation.
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

Prompt-level read-only is behavioral, not a security boundary. Shipped
defaults already enforce it where supported (`claude_cli`
`permission_mode=default`, `codex_cli` `sandbox=read-only`,
`antigravity_cli` `mode=plan`, `xai_cli` `sandbox=read-only`); verify with
`agent_collab_describe_options` that no override loosens it.
### 4. Start and collect
Pass `interactive: false` so a review cannot park in `awaiting_input`. For
dual review, one start for a two-member `parallel` workflow. Then follow
Delegate (Watch for `agent_collab_wait_events` cursor / `view` / `types`).
Prefix every surfaced reviewer finding with `[<session_id> <canonical_backend>]`.
For a parallel workflow, key member events and outcomes by `agent_id`,
reconcile only after terminal, and map each member to its backend.
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

### Backend quirks
| Provider | Behavioral guidance not expressed by the schema |
| --- | --- |
| Antigravity | `mode=plan` (the shipped default) is the read-only review mode; do not switch to `accept-edits` for a review. |
| xAI | Keep the shipped default `permission_mode=bypassPermissions` with `sandbox=read-only`; the sandbox is the safety boundary. Do not override to `auto` or `plan` for a headless run: both can end the turn as `provider_turn_cancelled` — `auto` raises a permission prompt for commands its classifier will not auto-approve (chained `;` pipelines) that no one can answer headlessly and that cancels the turn after 15s. |
| Codex | Include the explicit file list and prohibit broad repository greps. |

Re-read allowed values, defaults, and models from
`agent_collab_describe_options` at runtime. If this matrix conflicts with the
schema, the schema wins.
