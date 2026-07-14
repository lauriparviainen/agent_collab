# Workflow member selection at session start

**Status:** Closed — implemented, diff dual-reviewed (Gemini 3.1 Pro +
Grok via agent-collab session `daemon-167646ce71424aab`, 2026-07-15, no
high- or medium-severity findings).

**Created:** 2026-07-14 · **Implemented:** 2026-07-15

**Issue:** [#21](https://github.com/lauriparviainen/agent_collab/issues/21)

## Context

A workflow fixes its member agents in config: `dual-review` *is*
`parallel = ["claude", "codex"]`, and `StartSessionRequestModel` named only a
workflow — issue #19 deliberately shipped the parallel feature with **no start
wire change**. Running an existing shape with different members therefore
required a user-config workflow per combination (the documented triple-review
recipe pattern), and the TUI `/new` wizard could only enumerate fixed recipes.
The `solo-*` family (`solo-claude-cli`, `solo-xai-cli`, …) is that
combinatorial pressure made visible: shape × provider × transport as separate
workflow ids.

Owner decision (2026-07-14, TUI wizard review): the natural start flow picks
the workflow **shape first, then the backends** that fill its slots.
Configured members are preselected as defaults so pressing Enter through both
questions reproduces today's behavior, but the questions are always asked.

## Goal

A caller (TUI wizard or MCP/API client) can start a named workflow with an
explicit member selection that fills the workflow's slots, validated exactly
as strictly as config-defined workflows, without defining a throwaway
user-config workflow first.

## Decisions

- **Wire shape (settles the open question):** one additive optional start
  field, `members`, an object mapping a **slot** to an agent id. A slot is
  named by the workflow's configured member id, with duplicate sequence
  positions collapsed into one slot (`config.workflow_member_slots`), so
  `cross-review`'s `[a, b, a]` exposes slots `a` (lead/reviser, reprising)
  and `b` (reviewer) — never three free positions. Absent, empty, or
  identity-only selections are byte-for-byte today's behavior
  (`StartSessionRequestModel.to_dict` omits an empty map).
- **Validation:** `options.resolve_workflow_members` validates the selection
  (slots exist; agents known and enabled via `workflow_member_state`;
  substituted parallel groups stay duplicate-free with unchanged width) and
  returns an effective `WorkflowConfig`. Rejections use the structured
  `invalid_start_options` shape with `members.<slot>` paths, before any
  session state exists. Sequence substitution may collapse to repeated
  members (`[a, a, a]`) because config-defined sequences already allow
  repeats.
- **One enforcement path:** the daemon folds the effective workflow into the
  start's freshly loaded config snapshot
  (`collab_config.workflows[workflow_id]`), so every later step —
  `validate_start_backends`, `normalize_start_options`,
  `build_session_settings`, and execution, which carries the snapshot — sees
  the substituted members with no second code path. A substitution can also
  *fix* a start-ineligible workflow by replacing the disabled member.
- **Trust posture:** the selection comes only off the wire, never from
  config, and may reference only globally enabled agents; #19 goal 3 (project
  config cannot introduce parallel execution or alter what runs) is
  untouched.
- **Discovery:** `describe_options` advertises
  `workflows[].member_selection`: `start_field`, `distinct_members` (parallel
  shapes), and `slots[]` with `slot`, `default`, `default_eligible` (false
  when the configured member's backend is disabled, i.e. the slot *requires*
  substitution), and `eligible_members` (all enabled agent ids).
  `discovery.start.accepts_member_selection` flags the capability.
- **Settings echo (verified):** `settings.workflow.sequence`/`.parallel` and
  `settings.agents` reflect the effective members with no new fields.
- **Surfaces:** MCP `agent_collab_start` accepts `members` (kept in lockstep
  by the start-contract tests); the CLI gains
  `--members '{"slot":"agent"}'`; the TUI wizard asks shape → one
  single-choice list per slot (▸ starts on the configured member, Enter
  assigns and advances, typed ids work) → workdir. Only real substitutions go
  on the wire. A discovery payload without `member_selection` (older daemon)
  skips the wizard step.
- **`solo-*` recipe collapse:** deferred, unchanged from the open question —
  member selection makes one `solo` shape *possible*, but migrating existing
  references is separate work.

## Verification

- `tests/test_options.py::WorkflowMemberSelectionTests` — slot semantics
  (`[a, b, a]` reprise), enablement, distinctness, field paths, discovery
  payloads including a disabled default.
- `tests/test_daemon.py` — substituted `dual-review` runs both effective
  members with attributed events and echoed settings; `cross-review` lead
  substitution reprises; invalid selections reject before session state.
- `tests/test_api_schema.py` — `members` is a wire field on all start
  surfaces (model, MCP schema, `_start_payload`, daemon dataclass); shape
  rules and the omit-when-empty round trip.
- `tests/test_tui_calm.py::NewWizardMemberStepTests` — Enter-through defaults
  sends no `members`; substitution, arrow selection, per-workflow questions,
  legacy-payload skip.

## Open questions

- Whether the built-in `solo-*` recipe family collapses into one `solo` shape
  now that member selection exists, and what migrates existing references.
- Whether ad-hoc parallel member groups need any additional operator signal
  beyond the existing width cap (mirrors the deferred cross-session limiter
  in the parallel-review task document).
