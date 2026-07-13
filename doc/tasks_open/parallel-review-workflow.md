# Parallel review workflow

**Status:** Designed and design-reviewed; not started.

**Implementation dependency:** The shared turn-outcome contract in
`backend-turn-outcomes.md` (#17) lands before this task's referee/runtime slice.
Configuration-only work may proceed after those shared types are frozen, but
must not become externally visible on its own: schema-7 parsing, discovery,
and the built-in `dual-review` workflow ship together with the stage runtime,
or the daemon would advertise a workflow the referee cannot execute (Codex
cross-plan review finding 5).

Design review 2026-07-13: independent read-only reviews by xAI Grok Build and
Gemini 3.1 Pro (High) through agent-collab, both APPROVE WITH CHANGES; all
required changes are folded into this document. Both reviewers independently
caught the same factual error (the TOML fallback parser does not handle
nested arrays — goal 1 correction) and the missing all-members-fail
mechanism (goal 8: `ParallelStageFailed` + produced-output predicate). Grok
additionally required the machine-readable stage-summary event (goal 8), the
`turn_active` suppression for group members, and the pinned rejection site
for interactive parallel starts (goal 12).

Outcome-contract follow-up 2026-07-13: a second two-model review of issue #17
found that this document's message-only produced-output predicate would accept
partial prose from a turn that later failed. Goal 8, the implementation notes,
and verification below now require explicit per-member turn outcomes. A review
is accepted only when the turn completed and also produced review output.

Fresh-eyes review 2026-07-13 (Claude, plus Codex gpt-5.6-sol high through
agent-collab — two rounds, both APPROVE WITH CHANGES, all justified findings
folded): an owner decision bounded v1 to flat parallel fan-out with the calling
agent as orchestrator/reconciler, so nested `stages` was replaced by a flat
`parallel` list (goal 1; `stages` deferred with a mechanical migration path,
which also made the TOML fallback-parser fix unnecessary). The review corrected
two factual errors earlier rounds missed: shipping `triple-review` as a
built-in would fail `validate_config` at every load (now a user-config recipe),
and `daemon._session_agent_refs` reads `settings.agents`, not
`settings.workflow.sequence` (goal 9). Challenged and kept, with rationale now
written down: `ParallelStageFailed` alongside the stage summary, the schema-7
bump, and the `agent_id` field (whose justification was fixed — antigravity
running Claude is a distinct source; the real gap is same-type members). The
width cap became a fixed constant enforced in `validate_workflow`. Codex
additionally required shape-clearing workflow merge semantics, duplicate-member
rejection, truthful `stopped` ordering in `stop_session`, and the
`max_turns < 1` start rejection for parallel workflows; its composed-design
findings (stage-aware roles, transcript-window visibility), moot for v1, are
recorded in the deferred-`stages` open question. No finding was rejected.
Byte-for-byte compatibility claims were reconciled to behavior-level
throughout.

Cross-plan review 2026-07-13 (Codex gpt-5.6-sol high, one round over this
document plus `backend-turn-outcomes.md`): COMPATIBLE WITH CHANGES — the data
models compose; the gaps were interfaces, all folded into both documents:
stop as a registered control cause with a single truthful `stopped` publisher
(goal 8), the stage-level `parallel_stage_no_accepted_member` failure record
(goal 8, defined in #17), per-member #17 supervisors replacing the raw
`wait_for` wording (goal 4), `agent_id` null/ownership semantics (goal 6),
and the ship-together rule for the config slice (header dependency note).

**Created:** 2026-07-13

**Issue:** [#19](https://github.com/lauriparviainen/agent_collab/issues/19)

## Context

agent-collab workflows run their agents strictly in sequence. A workflow is a
`[workflows.*].sequence` list of agent ids, and `Referee.run` walks it one turn
at a time (`referee.py`: `sequence = self._sequence()[: max(0, self.config.max_turns)]`,
then a `for turn, agent_name in enumerate(sequence)` loop). Each turn runs one agent to
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
of any size ≥ 2. The design keeps sequential workflows and their config
behavior-compatible, keeps the cursor-based wire contract intact apart from one
additive optional event field (goal 6), and bounds v1 to non-interactive review
fan-out.

## Goals and decisions

1. **Express parallelism as a flat `parallel` member list; composed `stages`
   are deferred.** A new optional workflow field `parallel` is a flat list of
   agent ids that all run concurrently as one group:

   ```toml
   [workflows.dual-review]
   parallel = ["claude", "codex"]            # one parallel group, two reviewers
   ```

   Decision history, recorded honestly: issue #19 and earlier revisions of this
   document chose a nested `stages` field (ordered list of bare ids or nested
   groups) so that parallel-reviews-then-synthesis could be composed. The
   fresh-eyes review plus an owner decision (2026-07-13) reversed that: **v1
   builds no layered review.** The only named consumer (#18) reconciles reviews
   *client-side* — the calling agent is the orchestrator, and its decision 1
   explicitly forbids delegating adjudication — so a daemon-side synthesis turn
   has no consumer. With composition out of scope, the flat list wins on cost:
   it needs **no TOML fallback-parser change** (a flat string array is exactly
   the `sequence` shape the subset parser already handles, whereas
   `_split_top_level` tracks quotes but not brackets and cannot parse nested
   arrays — found independently by both design reviewers), no stage-shaped role
   generalization, and no redefinition of `max_turns`. A workflow
   `type = "parallel" | "sequential"` field stays rejected — the presence of
   `parallel` vs `sequence` already discriminates the shape.

   Deferring `stages` is safe, not a dead end: if layered review is ever
   needed, a `stages` field supersedes `parallel` with a mechanical schema
   migration (`parallel = [...]` → `stages = [[...]]`) through the existing
   `config_migrations` machinery, and the fallback parser gains bracket-depth
   tracking then (Open questions).

   A workflow declares **exactly one** of `sequence` or `parallel`; declaring
   both in one table is a config error. Merge semantics across config layers
   are shape-clearing (Codex review finding 3: `_merge_workflow` clones the
   existing workflow before applying an override, so without this rule an
   override of built-in `dual-review` with `sequence` would inherit `parallel`
   and trip the XOR): an override that supplies `sequence` clears an inherited
   `parallel` and vice versa, so the merged result always has exactly one
   shape. `sequence` remains the canonical sequential form and
   its behavior is unchanged (constraint: existing config stays valid) — note
   this is a behavior-level promise verified by tests, not a claim that
   `config.py`'s workflow functions go untouched; `WorkflowConfig`,
   `_merge_workflow`, and `validate_workflow` all need surgery to add the
   `parallel` shape (see Implementation notes).

2. **`CURRENT_CONFIG_SCHEMA` bumps to 7 with a no-op stamp migration.** Strictly,
   a bump is not *required* for correctness: `parallel` is a new optional field, so
   a schema-6 config that omits it stays valid, and one that adds it still loads
   (nothing rejects unknown-but-handled workflow fields once parsing supports
   them). The bump is a convention choice (inference): v4/v5/v6 each stamped the
   version for an additive optional capability (`backends`, `sessions`,
   `workdir`), and following that keeps `describe_options`/version signalling
   honest and the write-back path uniform. `_migrate_v6_to_v7` stamps only the
   version — no field shape changes — so the existing tomlkit/regex
   `_stamp_schema_version` write-back keeps working and `render_user_config`
   emits `schema_version = 7`. The stamp also buys a concrete downgrade
   signal: an older agent-collab reading a `parallel` config fails with the
   clear "schema_version 7 is newer than supported" error from
   `migrate_config_data` instead of a confusing
   `unknown field workflows.<id>.parallel`.

3. **Project workflows stay `sequence`-only; parallelism comes only from
   built-in and user config.** `config_migrations._filter_project_workflows`
   fail-closed requires `set(values) == {"sequence"}` and drops anything else.
   v1 does **not** loosen that: a project `.agent-collab/config.toml` cannot
   introduce a `parallel` workflow. Rationale — a parallel group multiplies
   concurrent provider subprocesses, which is an execution-posture change
   (`doc/tasks_closed/workdir-limits-and-workspace-trust.md`, decisions 3/5:
   execution-relevant settings are user-config-only). Keeping project workflows
   sequence-only means the parallel feature ships with **zero** new trust
   surface. A project `parallel` workflow is dropped with the existing "malformed
   project workflow" warning (inference: the current shape check already rejects
   it, so no new code is needed to be safe — only a doc note). Loosening this is
   deferred (Open questions).

4. **The referee unit becomes the *stage*; a `parallel` workflow is one
   group-stage; the per-agent `timeout` applies per agent.** Internally,
   `Referee` normalizes both forms to `stages: List[List[str]]` — a `sequence`
   workflow becomes a list of singletons, a `parallel` workflow becomes a
   single group — so there is one execution model and the loop stays
   `for stage in stages[: max_turns]`, today's loop generalized. `max_turns`
   keeps its sequential meaning unchanged (each turn is one stage); a parallel
   workflow has exactly one stage, so it runs under any `max_turns` ≥ 1
   (the default 3 included) and `max_turns` never bounds group **width**.
   The per-agent `timeout` is applied **per agent** inside the group — each
   concurrent member runs under its own independent issue #17 per-turn
   supervisor and deadline (the raw `asyncio.wait_for` in today's
   `_run_agent_turn` is exactly what #17 replaces with runner/deadline
   arbitration and bounded cleanup) — **not** per group. A slow reviewer
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
   `cross-agent-review-skills.md` depends on. Every member of the group gets
   the same reviewer role text; with layered review out of v1 (goal 1) there is
   no post-group step and therefore no synthesis role. Today's turn-1-lead /
   turn-2-reviewer / turn-3-reviser ladder is untouched for sequential
   workflows (a singleton stage keeps the existing turn-index role selection).

6. **One cursor stream; append-order per arrival is sufficient; add an optional
   `agent_id` attribution field to the event.** Confirmed: the single monotonic
   cursor is kept, and interleaving is simply arrival order. `Referee._emit`
   serializes appends under `_emit_lock`, and the daemon appends on the
   single-threaded event loop (`_record_event`), so concurrent members' events
   interleave safely and the cursor stays monotonic with no new synchronization
   (verified against `referee.py:_emit` and `daemon.py:_record_event`). Events
   already carry `source`, which is the agent *type* and so distinguishes
   cross-vendor members (`claude`/`codex`/`xai`) — the entire motivating case.
   (Antigravity running a Claude model is *not* an attribution gap: its source
   is `antigravity`, distinct from `claude`; that case is a model-identity
   concern for skill-side reviewer *selection*, decision 4 of
   `cross-agent-review-skills.md`.) What `source` cannot distinguish is two
   members of the same provider type — e.g. a user-config group of two
   `claude`-type agents with different models or options, a legitimate
   same-vendor-second-opinion shape — so attribution must key on the workflow
   **agent id**, not the provider brand.

   Recommendation: add an optional `agent_id: Optional[str]` to `events.Event`
   and `api_schema.EventModel`, tolerant of `null` in old JSONL. Ownership and
   null semantics (Codex cross-plan review finding 6): the **referee** assigns
   `agent_id` on member-stream events and per-member boundary events —
   sequential and parallel alike, overwriting any backend-supplied value so a
   provider cannot impersonate another member — while events with no single
   member (the human task, session-level referee status, the group-level stage
   summary) leave it `null`; the stage summary's `members` map is the
   group-level attribution surface. This is the **one** client-visible wire addition and carries a
   compatibility note (goal 13). It is preferred over stuffing attribution into
   `raw`: `raw` is provider-controlled and opaque, and the summary projection
   nulls it for tool events (`daemon.py:_project_event`), so it is unreliable for
   attribution. The zero-wire-change alternative — rejecting same-type duplicate
   members in v1 so `source` alone suffices — was considered and rejected: the
   validation rule is no less code than the additive optional field, it
   forecloses the same-vendor-different-model group, and uniform `agent_id`
   attribution is what issue #19's "merged attributed event stream" promises.
   Group membership is not a new field — it is derivable from the
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
   - **Runtime failure** (a member is cancelled, interrupted, times out, exits
     nonzero, is refused, or its backend dies mid-turn): the shared issue #17
     runner/referee contract records one attributed `TurnOutcomeRecord`, the
     group continues, and the session reaches `done` as long as **at least
     one** member is accepted. Failing the whole session would discard reviews
     that did complete — the opposite of what a "second/third opinion" caller
     wants. If every member is unaccepted, the stage and session are `failed`.

     A member is **accepted** iff both conditions hold:

     1. its authoritative turn outcome is `completed`; and
     2. it emitted at least one non-error `message` event during the stage.

     The second condition remains the review-output predicate; it is not a
     substitute for terminal success. Partial prose from a cancelled,
     interrupted, timed-out, refused, or failed turn never counts. The group
     retains every outcome. When a group ends with zero accepted members,
     `Referee.run` raises the dedicated safe `ParallelStageFailed` orchestration
     signal after recording the outcomes; the issue #17 daemon failure/CAS path
     converts it to `failed` without overwriting another terminal state. The
     resulting `SessionState.failure` is **stage-level**, not turn-shaped
     (Codex cross-plan review finding 3 — zero accepted reviews may have no
     truthful decisive member): code `parallel_stage_no_accepted_member` with
     its canonical safe message (both defined in #17's stable-code table),
     `stage_index` set, and null turn/agent/backend fields; the legacy `error`
     string carries the same canonical message, never generic `str(exc)` text.

     The simpler alternative — always reach `done` and let the stage-summary
     event alone carry the outcome — was considered and rejected (fresh-eyes
     review 2026-07-13): per-member outcome tracking exists for the stage
     summary regardless, so the exception costs one `raise` reusing the
     outcome-aware daemon failure path, and dropping it would leave every
     parallel-unaware consumer (TUI, `list_sessions`, a human) reading `done`
     on a session that produced zero opinions. Issue #17 also removes the old
     asymmetry: a required sequential turn that times out or otherwise does not
     complete fails its session.

     **Degradation is machine-readable, not prose-only** (design review,
     Grok finding 9): at group end the referee emits one structured
     stage-summary `status` event whose `raw` payload is
     `{"stage": N, "parallel": true, "members": {"<agent_id>":
     "completed" | "cancelled" | "interrupted" | "timed_out" |
     "refused" | "failed"}, "accepted_members": ["<agent_id>"]}` (`stage`
     is always 1 in v1 and
     kept for forward compatibility with deferred composed workflows). (Using `raw` here does not
     contradict goal 6's rejection of `raw` for attribution: that rejection is
     about *provider-controlled* `raw`, and the summary projection only nulls
     `raw` for `tool`-source events — a referee-owned `status` event's `raw`
     is trusted and survives projection.) A consumer that needs "did I
     get all N opinions?" checks this single event instead of parsing
     per-member prose; the review skills (#18) are required to check it and
     report degraded groups explicitly. `SessionState.status` stays
     unchanged — `done` means "at least one opinion arrived", and the
     stage summary is the authoritative full-vs-degraded signal.
   - **Start-time ineligibility** (a member's backend is unavailable or disabled
     at start) keeps the existing all-or-nothing gate: `validate_start_backends`
     already rejects the *start* before any session exists, and `_describe_workflow`
     already reports a workflow ineligible if any member is ineligible. This is
     the right seam — start eligibility stays predictable and matches today's
     behavior; only post-start failures degrade. It also means the caller never
     watches a session that was doomed from t=0.
   - **`agent_collab_stop` mid-group** cancels **all** in-flight member tasks.
     Stop is a *registered control cause*, not bare cancellation (Codex
     cross-plan review finding 1): `stop_session` registers the stop with the
     referee through #17's daemon-to-referee stop signal **before** cancelling
     the session task, so every active member supervisor arbitrates its member
     to `interrupted` — bare `task.cancel()` alone would classify them
     `failed`/`referee_cancelled_unexpected` under #17's arbitration rule.
     Stop means stop; there is no "some members keep running" state. Each
     outcome update is paired with a concise attributed boundary event so
     long-poll watchers wake with the updated outcome snapshot; no
     provider-specific cancellation prose is promised. Terminal `stopped` has
     one publisher and is truthful: `stop_session` publishes it only **after**
     cancellation has propagated and the session task has settled (Codex
     round 2, finding 1: today it sets `stopped` *before* `task.cancel()`, so
     a watcher could observe the terminal state while members still run); the
     order becomes register stop cause → cancel → await task → set `stopped`,
     keeping the direct status set for sessions with no live task, and
     `_run_session`'s cancellation handler defers to that ordering.

9. **Session status vocabulary is unchanged; per-agent detail rides events and
   the existing `agent_sessions` map; `settings` surfaces the stage structure.**
   No per-agent sub-status is added to `SessionState.status` /
   `SessionStateModel.status`. The existing statuses
   (`running`/`awaiting_input`/`done`/`failed`/`stopped`/`interrupted`) describe
   the *session*, which is exactly what the cursor-watching client consumes.
   Per-member progress is expressible four ways without a session-status change:
   (a) the per-event `source`/`agent_id` (goal 6); (b) `referee`/`status`
   stage-boundary events; (c) the existing per-agent `SessionState.agent_sessions`
   map, which already carries `{agent_id: {backend, provider_session_id,
   provider_session_kind}}` and is already opaque on the wire
   (`api_schema.SessionStateModel.agent_sessions`); and (d) issue #17's packed
   `SessionState.turn_outcomes`, keyed semantically by deterministic `turn_id`.
   The only `settings` change is
   additive and inside the already-dynamic `settings` dict:
   `build_session_settings` iterates the group members (today it iterates
   `workflow.sequence`) so `settings.agents` covers every member — that map,
   not `settings.workflow.sequence`, is what `daemon._session_agent_refs`
   reads (correction from fresh-eyes review, verified against
   `daemon.py:_session_agent_refs`) — adds a `parallel` representation under
   `settings.workflow`, and keeps `settings.workflow.sequence` present as the
   ordered member list for existing settings consumers. Once issue #17 has
   added the shared failure/outcome/event-batch fields, this task's only
   additional fixed API-schema field is `EventModel.agent_id` (goal 6).

10. **Discovery reports a parallel workflow eligible only when *all* members are
    eligible, and keeps per-member detail.** `options._describe_workflow` already
    accumulates `ineligible` across every agent in `workflow.sequence` and sets
    `start_eligible = not ineligible`, with per-member facts in `effective_agents`
    and cross-provider overrides in `uniform_backend_overrides` (set-based over
    provider types). Feeding a parallel group's flattened members through the
    same accumulation yields exactly all-members-eligible gating with per-member
    detail retained — no behavior change, only that `_describe_workflow` iterates
    the group members instead of `sequence` and adds a `parallel` key to
    its payload beside `sequence` (inference, verified against
    `options.py:_describe_workflow`). All-or-nothing eligibility is the right
    default for a fan-out review: a dual review that silently drops to a solo
    review because one backend is down is a worse surprise than a clear
    "not eligible, backend X unavailable" with per-member reasons the caller can
    act on.

11. **Resource limits: a per-session parallel-group width cap; no new
    cross-session limiter; per-session bookkeeping is unchanged.** A parallel
    group of pathological width would spawn many concurrent provider subprocesses
    under one session, so v1 caps parallel-group width at a fixed constant of 4
    (comfortably above triple review; inference on the exact number). A config
    knob for the cap is deliberately **not** added in v1 — it would be a new
    user-config field with its own validation for a limit nobody has yet asked
    to raise; simplified from "config-bounded" during the fresh-eyes review,
    which also resolves the Implementation-notes claim that the cap is one of
    the only new constants. The cap is enforced in `validate_workflow` at
    config load, like the existing member-exists/enabled rules — a single
    enforcement site, so discovery and start cannot disagree (Codex review
    finding 5: a start-time-only check would let `describe_options` advertise
    an over-wide workflow as start-eligible that then always fails). A single
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
    starting a `parallel` workflow with `interactive=true` is
    rejected at start with an actionable error. Reviews are non-interactive
    (`cross-agent-review-skills.md` passes `interactive:false`), and
    `post_message` target routing to "a specific agent inside a concurrently
    running group" is unspecified and racy (`turn_active` is a single bool used
    only for interactive queueing; it has no per-member meaning). A parallel-only
    workflow therefore never enters `awaiting_input`. Interactive parallel is
    deferred (Open questions).

    The rejection site and message are pinned (design review, Grok finding
    12): validation happens in the daemon's start preparation
    (`_prepare_session_start`), next to the existing start-time backend
    validation, with the error
    `workflow '<id>' is a parallel workflow; interactive sessions are not
    supported — start it with interactive=false`.
    Field-path detail follows the existing validation-error shape so MCP
    callers can fix the named field.

    The same site rejects `max_turns < 1` for a parallel workflow (Codex
    round 2, finding 2): `Referee.run` clamps `max_turns` at zero, so a
    `max_turns=0` start would reach `done` having run zero stages — silently
    violating goal 8's "`done` means at least one opinion arrived" invariant
    without ever raising `ParallelStageFailed`. Sequential workflows keep
    today's clamping behavior unchanged.

13. **Compatibility: sequential workflows and parallel-unaware clients are
    untouched.** Because a workflow is selected by *name* and parallelism lives
    entirely in server-side config, the **start wire shape does not change** —
    `StartSessionRequestModel`/`agent_collab_start` gain no field. Consumer
    inventory:

    | Consumer | Change |
    |---|---|
    | Existing `sequence` workflows (config + execution) | none |
    | `StartSessionRequestModel` / `agent_collab_start` payload | none |
    | `SessionState` / `SessionStateModel` (incl. `agent_sessions`) | no additional #19 field; reuse issue #17's `failure` and `turn_outcomes` |
    | TUI, `agent-collab watch`, MCP clients unaware of parallelism | none — they read the same cursor-ordered stream; interleaving is just more events, rendered per-source as today |
    | `events.Event` + `api_schema.EventModel` | **additive**: optional `agent_id` (regenerates API docs; compat note) |
    | `config.WorkflowConfig`, `_merge_workflow`, `validate_workflow` | parse/validate `parallel` (same `_expect_str_list` shape as `sequence`); enforce sequence-xor-parallel; every `.sequence` call site audited |
    | `config.py` TOML fallback parser (`_split_top_level`) | none — a flat `parallel` array is the shape it already parses; bracket tracking is deferred with `stages` (goal 1) |
    | `config_migrations` | bump to 7 (no-op stamp); project filter stays sequence-only |
    | `referee.py` (`_sequence`→stage normalization, group execution) | new parallel path aggregates issue #17 outcomes + `ParallelStageFailed`; sequential path preserved as stages-of-one |
    | `daemon.py` outcome/`turn_active` wiring | reuse issue #17 outcome persistence and polling wake-ups; parallel members must not toggle the shared `turn_active` bool |
    | `options.describe_options` / `build_session_settings` | surface `parallel`; eligibility already correct |
    | `logging.py` + `daemon._render_transcript` | attribution suffix when `agent_id != source` |

## Configuration shape

Built-in and user config (project config is `sequence`-only, goal 3). A workflow
declares exactly one of `sequence` or `parallel`:

```toml
schema_version = 7

# First-class dual review (N=2), enabled by default (both providers are built-in).
[workflows.dual-review]
parallel = ["claude", "codex"]

# Triple review (N=3): a *user-config* example, added after enabling a third
# provider (e.g. xai or antigravity). Not a shipped built-in — see below.
[workflows.triple-review]
parallel = ["claude", "codex", "xai"]
```

- `parallel` is a flat list of agent ids that run concurrently as one group,
  size ≥ 2, all distinct. A single-member `parallel` is a config error naming
  `sequence` as the fix; an empty `parallel` is a config error (mirrors the
  existing empty-`sequence` rejection). Duplicate member ids are a config
  error (Codex review finding 4: the referee holds one runner per agent id
  and the stage-summary `members` map is keyed by agent id, so a duplicate is
  unrepresentable at runtime; `sequence` keeps allowing repeats — the built-in
  `cross-review` uses claude twice — because sequential turns reuse a runner
  safely). Same-provider parallel members remain possible via two distinct
  configured agent ids of the same type.
- `sequence` and `parallel` are mutually exclusive; both present is a config
  error.
- Every referenced agent must exist and be enabled (existing `validate_workflow`
  rules applied over the member list).
- The built-in `dual-review` ships enabled (claude+codex, both built-in-enabled).
  `triple-review` is **not** a shipped built-in (correction from fresh-eyes
  review — the previous claim that it could ship built-in and merely be
  start-ineligible was wrong): `validate_config` runs `validate_workflow` over
  every workflow at every config load and raises on disabled agents, and
  `default_config.toml`'s own comments record the invariant that no built-in
  workflow references a disabled agent. A built-in `triple-review` over the
  disabled-by-default `xai` would make every `load_config` fail. Instead it is
  the documented user-config recipe above, added alongside enabling a third
  provider — exactly the current opt-in-agent pattern. N=2 and N=3 are *both
  first-class in the mechanism* (a parallel group of any size ≥ 2, up to the
  width cap); "first-class" is about the config/referee/discovery model, not
  about shipping every provider enabled.

`schema_version` migration: `_migrate_v6_to_v7` stamps the version only.

## Implementation notes

- `config.WorkflowConfig` gains `parallel: Optional[List[str]]`.
  `_merge_workflow` parses `parallel` with the existing `_expect_str_list` and
  enforces sequence-xor-parallel per table, with an override supplying one
  shape clearing the inherited other (goal 1); `validate_workflow` validates
  every member (exists + enabled) and rejects empty, single-member, duplicate,
  or over-width-cap `parallel` lists (goals 1/11). `sequence`
  handling is behavior-preserving (goal 1): the accepted shapes, defaults, and
  error messages for `sequence` workflows do not change, though the code paths
  themselves are touched.
- `config_migrations`: add `_migrate_v6_to_v7` (stamp-only) to `MIGRATIONS`; set
  `CURRENT_CONFIG_SCHEMA = 7`; update `render_user_config`. Leave
  `_filter_project_workflows` unchanged so project `parallel` workflows are
  dropped with the existing warning.
- `config.py` fallback parser: no change — a flat `parallel` array is the shape
  `_split_top_level` already handles. Add one regression test that
  `parallel = ["a", "b"]` parses through the fallback path.
- `referee.py`: replace `_sequence()` with a `_stages()` normalization that maps
  a `sequence` workflow to singleton groups and a `parallel` workflow to one
  group, both `List[List[str]]`. The main loop iterates `stages[:max_turns]`.
  For a singleton stage, keep the current single-agent path (including the
  turn-index role selection) untouched. For a group of size ≥ 2: freeze one
  transcript snapshot, build one shared prompt (reviewer role), launch one
  outcome-returning `_run_agent_turn` per member via
  `asyncio.gather(return_exceptions=True)`. Issue #17's per-turn supervisor
  applies each member's timeout and bounded cancellation cleanup. Suppress the
  `turn_active` callback for group members: the shared bool has
  no per-member meaning and concurrent toggling races (design review, Grok
  finding 4); the group path signals stage-level activity once at group
  start/end instead. Track every `TurnOutcomeRecord` plus whether the member
  emitted a review message. Accept only `completed` members satisfying that
  output predicate; emit the structured stage-summary `status` event at group
  end (goal 8); raise `ParallelStageFailed` when a group of size ≥ 2 has zero
  accepted members. Atomically pair each completed outcome update with a
  `referee`/`status` member-boundary event so issue #17 event polling wakes with
  a consistent snapshot. Tag emitted events with `agent_id`.
- `events.Event`: add `agent_id: Optional[str] = None`, include it in
  `to_dict`/`to_json` (additive). `api_schema.EventModel`: add the optional
  field to `from_dict`/`to_dict`. Regenerate `doc/daemon_api_doc/` via
  `./agent_collab_dev.sh build`; record the additive-field compat note.
- `daemon.py`: no additional #19 `SessionState` field after issue #17 adds
  `failure` and `turn_outcomes`. Reuse its atomic outcome/boundary update and
  monotonic terminal transition helper. `_record_event` already appends on the
  event loop; confirm the group's concurrent `_emit`s serialize under the
  referee lock (they do). Attribution suffix in `_render_transcript`.
- `options.py`: `_describe_workflow` iterates the group members and adds a
  `parallel` key; `build_session_settings` adds `settings.workflow.parallel`
  and keeps `settings.workflow.sequence` as the ordered member list.
- `logging.SessionLogger.write`: attribution suffix when `agent_id` present and
  differs from `source`.
- Enforce the interactive-parallel rejection and the `max_turns < 1` rejection
  for parallel workflows (goal 12) at start validation
  (`_prepare_session_start`); the width cap lives in `validate_workflow`
  (goal 11), not at start.
- `daemon.stop_session`: register the stop control cause with the referee via
  issue #17's stop signal, then cancel, await the session task, and only then
  publish terminal `stopped` (goal 8); keep the direct status set when no live
  task exists. Members stopped this way arbitrate to `interrupted`, never
  `referee_cancelled_unexpected`.
- Public-repo hygiene: no machine-specific paths, tokens, or transcript contents
  in any of the above; the width cap and workflow names are the only new
  constants.

## Verification

- A `parallel` dual-review workflow starts once, runs both members concurrently,
  and merges their events into one cursor stream with per-member `agent_id`;
  a single `wait_events` loop observes both and terminates on session status.
- Triple review (N=3) behaves identically to dual with three concurrent members
  (test config enabling a third agent; no shipped built-in is involved).
- A parallel group wider than the fixed width cap (4) or containing duplicate
  member ids is rejected by `validate_workflow` at config load with an
  actionable error, and such a workflow is never advertised by
  `describe_options`.
- A schema-6 user config migrates to 7 via the stamp-only `_migrate_v6_to_v7`
  and the on-disk write-back stamps `schema_version = 7` without other changes.
- `max_turns` semantics are unchanged: a parallel workflow runs its single
  group under any `max_turns` ≥ 1 (including the default); sequential
  workflows truncate exactly as today. Starting a parallel workflow with
  `max_turns < 1` is rejected at start with an actionable error (goal 12).
- Per-agent `timeout` cuts a slow member at its own deadline while faster members
  still return; the session reaches `done` only if at least one other member is
  accepted, while retaining the straggler's `timed_out` turn outcome.
- All-members-fail raises `ParallelStageFailed` and yields `failed` (the
  issue #17 sequential contract likewise fails a required timed-out turn);
  `stop` mid-group records each active member as `interrupted` and reaches
  `stopped` only after every member task has settled (goal 8 stop ordering).
- The group-end stage-summary `status` event carries the machine-readable
  six-value member outcome map plus accepted-member list, and a degraded group
  is detectable from that single event and from `turn_outcomes`.
- The TOML fallback parser parses a flat `parallel` array (regression test; no
  parser change is expected).
- Parallel group members do not toggle the session `turn_active` flag.
- Sequential workflows (`solo-claude`, `cross-review`, `compare`) are unchanged
  at the behavior level (goal 1): config stays valid as-is, execution order and
  role prompts are identical, and rendering is identical for the built-in
  agents (`agent_id == source`, so no suffix). Their events are identical
  except for the additive `agent_id` key (goal 6) — not byte-for-byte.
- `describe_options` marks a parallel workflow start-eligible only when all
  members are eligible, exposes per-member detail and `parallel`; a disabled/
  unavailable member reports the reason.
- A project `parallel` workflow is dropped with a sanitized warning; project
  `sequence` workflows still work.
- A user-config override of a `parallel` workflow with `sequence` (or the
  reverse) clears the inherited shape and validates cleanly (goal 1 merge
  semantics).
- Interactive + parallel is rejected at start with an actionable message.
- `agent_id` is optional and null-tolerant when reading old JSONL; the API docs
  regenerate cleanly and the added field carries a compatibility note.
- `./agent_collab_dev.sh test` and `build --check` pass; `git diff --check` passes.

## Open questions

- **Composed `stages` workflows (layered review).** Deferred by owner decision
  (2026-07-13): v1 ships flat parallel fan-out only, and the *calling* agent is
  the orchestrator/reconciler (#18 decision 1). If daemon-side composition
  (e.g. parallel reviews then a synthesis turn) is ever wanted, introduce a
  nested `stages` field, migrate `parallel = [...]` → `stages = [[...]]`
  mechanically via the schema-migration machinery, extend the TOML fallback
  parser with bracket-depth tracking (it cannot parse nested arrays today),
  and solve two problems Codex flagged in the composed design: role selection
  must become stage-aware (the turn-index ladder makes a post-group singleton
  turn 2, a *reviewer*, never a synthesizer), and a post-group step must be
  guaranteed to see every producing member's output with `agent_id`
  attribution — `_recent_transcript`'s rolling 12-event window can evict a
  reviewer's result and labels entries by `source` only.
- **Project-config parallel workflows.** Whether to ever let a project define a
  `parallel` workflow over globally enabled agents. Deferred because a parallel
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
- **Same-provider members within a group.** Two members of the same provider
  type (e.g. two `claude`-type agents configured with different models) are
  distinguished by `agent_id`; whether the review skills should *warn* when a
  "cross-vendor" group is effectively same-model (e.g. antigravity running a
  Claude model beside a `claude` agent — distinct sources, same model) is a
  skill-side concern tracked in `cross-agent-review-skills.md` (decision 4),
  not here.
