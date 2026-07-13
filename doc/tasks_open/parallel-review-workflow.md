# Parallel review workflow

**Status:** Designed; not started.

**Created:** 2026-07-13

**Issue:** [#19](https://github.com/lauriparviainen/agent_collab/issues/19)

## Context

agent-collab workflows run their agents strictly in sequence. A workflow is a
`[workflows.*].sequence` list of agent ids, and `Referee.run` walks it one turn
at a time (`referee.py`: `sequence = self._sequence()[: max_turns]`, then a
`for turn, agent_name in enumerate(sequence)` loop). Each turn runs one agent to
completion under a single `asyncio.wait_for(..., timeout)` before the next
starts. Every emitted event flows into one monotonic, cursor-ordered stream that
the daemon appends under a lock, writes to one JSONL and one Markdown log, and
serves to every watcher identically (`daemon.py`: `_record_event`,
`wait_events`).

The most valuable agent-collab use is cross-vendor review over MCP. Decision 7
of `doc/tasks_open/cross-agent-review-skills.md` records that a client agent
(Gemini specifically) finds client-side management of *parallel* review sessions
error-prone: it must start two sessions, keep a `{session_id, backend, cursor,
status}` record per reviewer, poll round-robin, and reconcile only after both go
terminal. Issue #19 asks the daemon to own that orchestration: **one**
`agent_collab_start`, **one** `wait_events` loop, N reviewers running
concurrently, their events merged into the single existing cursor stream with
per-agent attribution, and the client watching exactly as it watches any session
today.

This design adds a daemon-orchestrated parallel workflow capability. N=2 (dual
review) and N=3 (triple review) are both first-class: a parallel group is a set
of any size ≥ 2. The design keeps sequential workflows, their config, and the
cursor-based wire contract untouched, and bounds v1 to non-interactive review
fan-out.

## Goals and decisions

1. **Express parallelism as `stages`, not a `parallel` list or a `type` field.**
   A new optional workflow field `stages` is an ordered list whose elements are
   each either a bare agent id (a sequential step of one) or a nested list of
   agent ids (a parallel group that runs concurrently):

   ```toml
   [workflows.dual-review]
   stages = [["claude", "codex"]]            # one parallel stage, two reviewers

   [workflows.review-then-synthesize]
   stages = [["codex", "xai"], "claude"]     # parallel reviews, then a synthesis turn
   ```

   `stages` is chosen over the two weaker alternatives the issue lists. A flat
   `parallel = ["a","b","c"]` field expresses only "all at once" and cannot
   compose the parallel-reviews-then-synthesis shape that review consumers want;
   it would need a *second* field to sequence anything, reinventing `stages`
   badly. A workflow `type = "parallel" | "sequential"` field is redundant — the
   presence of `stages` vs `sequence` already discriminates the shape — and it
   forces the *whole* workflow into one mode, again blocking mixed
   sequence+parallel. `stages` subsumes both: a sequential workflow is exactly a
   `stages` list of singletons, so there is one execution model internally.

   A workflow declares **exactly one** of `sequence` or `stages`; declaring both
   is a config error. `sequence` remains the canonical, untouched sequential
   form (constraint: existing config stays valid). The TOML fallback parser
   already handles this shape: `_parse_toml_value` parses a mixed array of
   strings and nested arrays recursively (`stages = [["a","b"], "c"]` yields
   `[["a","b"], "c"]`), so no fallback-parser change is required (inference,
   verified against `config.py:_parse_toml_value`/`_split_top_level`).

2. **`CURRENT_CONFIG_SCHEMA` bumps to 7 with a no-op stamp migration.** Strictly,
   a bump is not *required* for correctness: `stages` is a new optional field, so
   a schema-6 config that omits it stays valid, and one that adds it still loads
   (nothing rejects unknown-but-handled workflow fields once parsing supports
   them). The bump is a convention choice (inference): v4/v5/v6 each stamped the
   version for an additive optional capability (`backends`, `sessions`,
   `workdir`), and following that keeps `describe_options`/version signalling
   honest and the write-back path uniform. `_migrate_v6_to_v7` stamps only the
   version — no field shape changes — so the existing tomlkit/regex
   `_stamp_schema_version` write-back keeps working and `render_user_config`
   emits `schema_version = 7`.

3. **Project workflows stay `sequence`-only; parallelism comes only from
   built-in and user config.** `config_migrations._filter_project_workflows`
   fail-closed requires `set(values) == {"sequence"}` and drops anything else.
   v1 does **not** loosen that: a project `.agent-collab/config.toml` cannot
   introduce a `stages` workflow. Rationale — a parallel group multiplies
   concurrent provider subprocesses, which is an execution-posture change
   (`doc/tasks_closed/workdir-limits-and-workspace-trust.md`, decisions 3/5:
   execution-relevant settings are user-config-only). Keeping project workflows
   sequence-only means the parallel feature ships with **zero** new trust
   surface. A project `stages` workflow is dropped with the existing "malformed
   project workflow" warning (inference: the current shape check already rejects
   it, so no new code is needed to be safe — only a doc note). Loosening this is
   deferred (Open questions).

4. **The referee unit becomes the *stage*; `max_turns` counts stages, the
   per-agent `timeout` applies per agent.** Internally, `Referee` normalizes both
   forms to `stages: List[List[str]]` (a `sequence` workflow → a list of
   singletons). The loop becomes `for stage in stages[: max_turns]`. `max_turns`
   therefore bounds conversation **depth** (stage count), not width: a
   one-parallel-stage dual review with the default `max_turns=3` runs its single
   stage; `review-then-synthesize` (2 stages) runs both. This preserves the
   meaning `max_turns` has today for sequential workflows (each turn is one
   stage). The per-agent `timeout` is applied **per agent** inside a group — each
   concurrent member runs under its own `asyncio.wait_for(consume(), timeout)`,
   exactly as `_run_agent_turn` does today — **not** per group. A slow reviewer
   consuming the whole group's budget would defeat the point of independent
   parallel opinions; independent per-member timeouts also let a group return the
   fast reviewers' results while one straggler is cut at its own deadline
   (inference; refute path in Open questions if operators want a group wall-clock
   cap instead).

5. **Parallel members get identical prompts over a frozen transcript snapshot.**
   At group start the referee takes one immutable copy of the transcript and
   builds one prompt from it; every member of the group receives that same
   prompt and snapshot. Members must not observe each other's in-progress output
   — they run concurrently, so cross-observation would be nondeterministic and
   would collapse the "independent second opinion" property that decision 1/4 of
   `cross-agent-review-skills.md` depends on. The role text is stage-shaped
   rather than turn-index-shaped: every member of a parallel group gets a
   reviewer role; a subsequent sequential step (e.g. the synthesis turn) sees all
   group outputs already appended to the transcript and gets a synthesis/reviser
   role. This generalizes today's turn-1-lead / turn-2-reviewer / turn-3-reviser
   ladder without changing it for sequential workflows (a singleton stage keeps
   the existing role selection).

6. **One cursor stream; append-order per arrival is sufficient; add an optional
   `agent_id` attribution field to the event.** Confirmed: the single monotonic
   cursor is kept, and interleaving is simply arrival order. `Referee._emit`
   serializes appends under `_emit_lock`, and the daemon appends on the
   single-threaded event loop (`_record_event`), so concurrent members' events
   interleave safely and the cursor stays monotonic with no new synchronization
   (verified against `referee.py:_emit` and `daemon.py:_record_event`). Events
   already carry `source`, which distinguishes cross-*vendor* members
   (`claude`/`codex`/`xai`) — the entire motivating case. But `source` cannot
   distinguish two members of the same provider type, and decision 4 of
   `cross-agent-review-skills.md` warns that antigravity can run a Claude model,
   so attribution must key on the workflow **agent id**, not the provider brand.

   Recommendation: add an optional `agent_id: Optional[str]` to `events.Event`
   and `api_schema.EventModel`, set by the referee for every workflow-emitted
   event (sequential and parallel alike, for uniformity), tolerant of `null` in
   old JSONL. This is the **one** client-visible wire addition and carries a
   compatibility note (goal 13). It is preferred over stuffing attribution into
   `raw`: `raw` is provider-controlled and opaque, and the summary projection
   nulls it for tool events (`daemon.py:_project_event`), so it is unreliable for
   attribution. Group membership is not a new field — it is derivable from the
   `referee`/`status` boundary events the referee emits at stage start/end
   ("stage 1 (parallel): claude, codex"; "codex completed"; "xai timed out"). A
   structured `stage`/`group` index on the event is deferred (Open questions) as
   unnecessary for reconcile-by-reviewer-identity.

7. **Transcript renderers gain a small attribution tweak; JSONL is unchanged.**
   JSONL stays one-event-per-line; `agent_id` is just another key. The Markdown
   renderers (`logging.SessionLogger.write` and `daemon._render_transcript`)
   header each block `## SOURCE \`type\``; when three members interleave this is
   readable for distinct sources but ambiguous for same-source members.
   Recommendation: when `agent_id` is present and differs from `source`, render
   `## SOURCE (agent_id) \`type\``. Renderer-only, no wire impact. (For the
   common cross-vendor case the header is unchanged because `agent_id` matches
   the distinct source semantically — implementation may skip the suffix when
   `agent_id == source`.)

8. **Runtime member failure degrades to partial results with attribution;
   start-time ineligibility fails the whole start; `stop` cancels the whole
   group.** This is the presumed answer, and it is *correct* for a review
   consumer (justified, not merely accepted):
   - **Runtime failure** (a member times out, exits nonzero, or its backend dies
     mid-turn): the member emits an attributed `error` event (its `agent_id`),
     the group continues, and the session reaches `done` as long as **at least
     one** member produced output. Failing the whole session would discard the
     reviews that succeeded — the opposite of what a "second/third opinion"
     caller wants. If **every** member of a group fails, the stage produced
     nothing and the session is `failed`.
   - **Start-time ineligibility** (a member's backend is unavailable or disabled
     at start) keeps the existing all-or-nothing gate: `validate_start_backends`
     already rejects the *start* before any session exists, and `_describe_workflow`
     already reports a workflow ineligible if any member is ineligible. This is
     the right seam — start eligibility stays predictable and matches today's
     behavior; only post-start failures degrade. It also means the caller never
     watches a session that was doomed from t=0.
   - **`agent_collab_stop` mid-group** cancels **all** in-flight member tasks
     (the group's `asyncio` tasks), sets `stopped`, and emits an attributed
     `status` event per cancelled member. Stop means stop; there is no
     "some members keep running" state. This reuses the existing
     `stop_session` → task cancel path (`daemon.py`).

9. **Session status vocabulary is unchanged; per-agent detail rides events and
   the existing `agent_sessions` map; `settings` surfaces the stage structure.**
   No per-agent sub-status is added to `SessionState.status` /
   `SessionStateModel.status`. The existing statuses
   (`running`/`awaiting_input`/`done`/`failed`/`stopped`/`interrupted`) describe
   the *session*, which is exactly what the cursor-watching client consumes.
   Per-member progress is already expressible three ways without a status-contract
   change: (a) the per-event `source`/`agent_id` (goal 6); (b) `referee`/`status`
   stage-boundary events; (c) the existing per-agent `SessionState.agent_sessions`
   map, which already carries `{agent_id: {backend, provider_session_id,
   provider_session_kind}}` and is already opaque on the wire
   (`api_schema.SessionStateModel.agent_sessions`). The only `settings` change is
   additive and inside the already-dynamic `settings` dict:
   `build_session_settings` adds a `stages` representation under
   `settings.workflow` and keeps `settings.workflow.sequence` present as the
   flattened, ordered agent-id list so existing settings consumers (and
   `daemon._session_agent_refs`) keep working. Net api_schema change: **only**
   `EventModel.agent_id` (goal 6) regenerates the public API docs; `SessionStateModel`
   is untouched.

10. **Discovery reports a parallel workflow eligible only when *all* members are
    eligible, and keeps per-member detail.** `options._describe_workflow` already
    accumulates `ineligible` across every agent in `workflow.sequence` and sets
    `start_eligible = not ineligible`, with per-member facts in `effective_agents`
    and cross-provider overrides in `uniform_backend_overrides` (set-based over
    provider types). Feeding a parallel group's flattened members through the
    same accumulation yields exactly all-members-eligible gating with per-member
    detail retained — no behavior change, only that `_describe_workflow` iterates
    the flattened stage members instead of `sequence` and adds a `stages` key to
    its payload beside `sequence` (inference, verified against
    `options.py:_describe_workflow`). All-or-nothing eligibility is the right
    default for a fan-out review: a dual review that silently drops to a solo
    review because one backend is down is a worse surprise than a clear
    "not eligible, backend X unavailable" with per-member reasons the caller can
    act on.

11. **Resource limits: a per-session parallel-group width cap; no new
    cross-session limiter; per-session bookkeeping is unchanged.** A parallel
    group of pathological width would spawn many concurrent provider subprocesses
    under one session, so v1 caps parallel-group width (config-bounded, default
    4 — comfortably above triple review; inference on the exact number). A single
    session already maps to one subprocess today; a global cross-session
    concurrency limiter is **deferred** (Open questions) because the existing
    model already permits N sessions × 1 subprocess and the width cap plus
    operator awareness bounds the new multiplier for v1. No per-agent bookkeeping
    extension is needed: a parallel group writes to the **same** single JSONL and
    Markdown via one `SessionLogger`, so logs and retention (`retention.py`,
    `classify_transcript_paths`) stay keyed by session and need no change;
    provider identity is already per-agent via `agent_sessions`; subprocess
    lifetimes are owned by the runners and the referee's in-memory task set, and
    group cancellation cancels all member tasks. Retention, pruning, and the
    session index are untouched.

12. **Interactive parallel groups are rejected in v1.** Confirmed and recorded:
    starting a workflow that contains a parallel group with `interactive=true` is
    rejected at start with an actionable error. Reviews are non-interactive
    (`cross-agent-review-skills.md` passes `interactive:false`), and
    `post_message` target routing to "a specific agent inside a concurrently
    running group" is unspecified and racy (`turn_active` is a single bool used
    only for interactive queueing; it has no per-member meaning). A parallel-only
    workflow therefore never enters `awaiting_input`. Interactive parallel is
    deferred (Open questions).

13. **Compatibility: sequential workflows and parallel-unaware clients are
    untouched.** Because a workflow is selected by *name* and parallelism lives
    entirely in server-side config, the **start wire shape does not change** —
    `StartSessionRequestModel`/`agent_collab_start` gain no field. Consumer
    inventory:

    | Consumer | Change |
    |---|---|
    | Existing `sequence` workflows (config + execution) | none |
    | `StartSessionRequestModel` / `agent_collab_start` payload | none |
    | `SessionState` / `SessionStateModel` (incl. `agent_sessions`) | none |
    | TUI, `agent-collab watch`, MCP clients unaware of parallelism | none — they read the same cursor-ordered stream; interleaving is just more events, rendered per-source as today |
    | `events.Event` + `api_schema.EventModel` | **additive**: optional `agent_id` (regenerates API docs; compat note) |
    | `config.WorkflowConfig`, `_merge_workflow`, `validate_workflow` | parse/validate `stages`; enforce sequence-xor-stages |
    | `config_migrations` | bump to 7 (no-op stamp); project filter stays sequence-only |
    | `referee.py` (`_sequence`→stage normalization, group execution) | new parallel path; sequential path preserved as stages-of-one |
    | `options.describe_options` / `build_session_settings` | surface `stages`; eligibility already correct |
    | `logging.py` + `daemon._render_transcript` | attribution suffix when `agent_id != source` |

## Configuration shape

Built-in and user config (project config is `sequence`-only, goal 3). A workflow
declares exactly one of `sequence` or `stages`:

```toml
schema_version = 7

# First-class dual review (N=2), enabled by default (both providers are built-in).
[workflows.dual-review]
stages = [["claude", "codex"]]

# First-class triple review (N=3). Start-eligible once a third provider
# (e.g. xai or antigravity) is enabled in user config — consistent with the
# existing opt-in-agent pattern in default_config.toml.
[workflows.triple-review]
stages = [["claude", "codex", "xai"]]

# Composed: parallel reviews, then a single synthesis turn.
[workflows.review-then-synthesize]
stages = [["codex", "xai"], "claude"]
```

- `stages` is an ordered list; each element is a bare agent id (sequential step
  of one) or a nested list of agent ids (a parallel group, size ≥ 2).
- A group of size 1 is accepted and treated as a sequential step; an empty group
  or empty `stages` is a config error (mirrors the existing empty-`sequence`
  rejection).
- `sequence` and `stages` are mutually exclusive; both present is a config error.
- Every referenced agent must exist and be enabled (existing `validate_workflow`
  rules extended over the flattened member set).
- The built-in `dual-review` ships enabled (claude+codex, both built-in-enabled).
  `triple-review` ships as a built-in workflow that becomes start-eligible only
  when a third provider is enabled — this keeps default behavior valid with no
  local `grok`/`agy` setup, exactly as the current opt-in agents do. N=2 and N=3
  are *both first-class in the mechanism* (a parallel group of any size ≥ 2);
  "first-class" is about the config/referee/discovery model, not about shipping
  every provider enabled.

`schema_version` migration: `_migrate_v6_to_v7` stamps the version only.

## Implementation notes

- `config.WorkflowConfig` gains `stages: Optional[List[List[str]]]` (normalized:
  a bare-string element becomes a singleton list; nested list stays as-is).
  `_merge_workflow` parses `stages` and enforces sequence-xor-stages;
  `validate_workflow` validates every flattened member (exists + enabled) and
  rejects empty stages/empty groups. Keep `sequence` handling byte-for-byte as-is.
- `config_migrations`: add `_migrate_v6_to_v7` (stamp-only) to `MIGRATIONS`; set
  `CURRENT_CONFIG_SCHEMA = 7`; update `render_user_config`. Leave
  `_filter_project_workflows` unchanged so project `stages` are dropped with the
  existing warning.
- `referee.py`: replace `_sequence()` with a `_stages()` normalization that maps
  either form to `List[List[str]]`. The main loop iterates `stages[:max_turns]`.
  For a singleton stage, keep the current single-agent path (including the
  turn-index role selection) untouched. For a group of size ≥ 2: freeze one
  transcript snapshot, build one shared prompt (reviewer role), launch one
  `_run_agent_turn` per member via `asyncio.gather(return_exceptions=True)`, each
  wrapped in its own `wait_for(timeout)` exactly as today. Emit `referee`/`status`
  boundary events at group start and per-member completion/timeout. Tag emitted
  events with `agent_id`.
- `events.Event`: add `agent_id: Optional[str] = None`, include it in
  `to_dict`/`to_json` (additive). `api_schema.EventModel`: add the optional
  field to `from_dict`/`to_dict`. Regenerate `doc/daemon_api_doc/` via
  `./agent_collab_dev.sh build`; record the additive-field compat note.
- `daemon.py`: no `SessionState` change. `_record_event` already appends on the
  event loop; confirm the group's concurrent `_emit`s serialize under the
  referee lock (they do). Attribution suffix in `_render_transcript`.
- `options.py`: `_describe_workflow` iterates the flattened stage members and
  adds a `stages` key; `build_session_settings` adds `settings.workflow.stages`
  and keeps a flattened `settings.workflow.sequence`.
- `logging.SessionLogger.write`: attribution suffix when `agent_id` present and
  differs from `source`.
- Enforce the interactive-parallel rejection (goal 12) and the group-width cap
  (goal 11) at start validation (`_prepare_session_start` / `validate_start_backends`).
- Public-repo hygiene: no machine-specific paths, tokens, or transcript contents
  in any of the above; the width cap and workflow names are the only new
  constants.

## Verification

- A `stages` dual-review workflow starts once, runs both members concurrently,
  and merges their events into one cursor stream with per-member `agent_id`;
  a single `wait_events` loop observes both and terminates on session status.
- Triple review (N=3) behaves identically to dual with three concurrent members.
- `review-then-synthesize` runs the parallel group, then a synthesis step that
  sees both members' outputs in the transcript.
- `max_turns` counts stages: a one-stage workflow runs under the default; a
  two-stage workflow runs both; `max_turns=1` on `review-then-synthesize` runs
  only the parallel stage.
- Per-agent `timeout` cuts a slow member at its own deadline while faster members
  still return; the session reaches `done` with an attributed timeout `error`
  event for the straggler.
- All-members-fail yields `failed`; `stop` mid-group cancels all members and
  reaches `stopped` with attributed cancellation events.
- Sequential workflows (`solo-claude`, `cross-review`, `compare`) are byte-for-byte
  unchanged in config, execution, events, and rendering.
- `describe_options` marks a parallel workflow start-eligible only when all
  members are eligible, exposes per-member detail and `stages`; a disabled/
  unavailable member reports the reason.
- A project `stages` workflow is dropped with a sanitized warning; project
  `sequence` workflows still work.
- Interactive + parallel is rejected at start with an actionable message.
- The TOML fallback parser accepts `stages` (both `[[...]]` groups and bare-string
  singletons).
- `agent_id` is optional and null-tolerant when reading old JSONL; the API docs
  regenerate cleanly and the added field carries a compatibility note.
- `./agent_collab_dev.sh test` and `build --check` pass; `git diff --check` passes.

## Open questions

- **Project-config parallel workflows.** Whether to ever let a project define a
  `stages` workflow over globally enabled agents. Deferred because a parallel
  group is an execution-posture change and the fail-closed rule keeps v1's trust
  surface at zero; revisit only with an explicit per-member gate.
- **Group wall-clock cap.** Whether operators want an optional per-group total
  timeout in addition to per-member timeouts (e.g. to bound a whole dual review).
  Deferred; per-member timeouts are the correct default for independent reviews.
- **Structured stage/group index on events.** Whether events need a first-class
  `stage`/`group` field beyond `agent_id` + boundary status events. Deferred as
  unnecessary for reconcile-by-reviewer-identity; add if a consumer needs
  machine-grouped rendering.
- **Global cross-session concurrency limiter.** A daemon-wide cap on total
  concurrent provider subprocesses across all sessions. Deferred to the width cap
  plus operator awareness for v1; revisit if the daemon becomes shared or
  long-running (mirrors the workdir-allowlist note in `daemon-architecture.md`).
- **Interactive parallel groups.** `post_message` routing to a specific member of
  a running group and `awaiting_input` semantics for parallel stages. Deferred;
  v1 reviews are non-interactive.
- **Same-provider members within a group.** Two members of the same provider type
  (e.g. antigravity-running-Claude vs claude) are distinguished by `agent_id`;
  whether the review skills should *warn* when a "cross-vendor" group is
  effectively same-model is a skill-side concern tracked in
  `cross-agent-review-skills.md` (decision 4), not here.
