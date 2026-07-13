# Workflow member selection at session start

**Status:** Proposed — design not yet reviewed.

**Created:** 2026-07-14

**Issue:** [#21](https://github.com/lauriparviainen/agent_collab/issues/21)

## Context

A workflow fixes its member agents in config: `dual-review` *is*
`parallel = ["claude", "codex"]`, and `StartSessionRequestModel` names only a
workflow — issue #19 deliberately shipped the parallel feature with **no start
wire change**. Running an existing shape with different members therefore
requires a user-config workflow per combination (the documented triple-review
recipe pattern), and the TUI `/new` wizard can only enumerate fixed recipes.
The `solo-*` family (`solo-claude-cli`, `solo-xai-cli`, …) is that
combinatorial pressure made visible: shape × provider × transport as separate
workflow ids.

Owner decision (2026-07-14, TUI wizard review): the natural start flow picks
the workflow **shape first, then the backends** that fill its slots.
Configured members are preselected as defaults so pressing Enter through both
questions reproduces today's behavior, but the questions are always asked.
The interim TUI ships a selectable fixed-recipe list (workflow rows with ▸/✓
selection and inline member annotations); this task is the follow-up that
makes the second question real.

Slot counts for the built-in shapes: solo = 1; `cross-review` = 2 — the shape
is `[a, b, a]`, one lead/reviser plus one reviewer, so slot identity (not
position count) is what the user selects; `dual-review` = 2 independent group
members (up to the width cap of 4 for user-defined parallel shapes).

## Goal

A caller (TUI wizard or MCP/API client) can start a named workflow with an
explicit member selection that fills the workflow's slots, validated exactly
as strictly as config-defined workflows, without defining a throwaway
user-config workflow first.

## Design sketch (to be reviewed)

- **Wire:** an additive optional start field mapping workflow slots to agent
  ids (inference: a `members` map keyed by slot, not a positional list, so
  `[a, b, a]` cannot be broken into three independent positions). Absent
  field ⇒ exactly today's behavior, byte-for-byte request compatibility.
- **Validation:** one enforcement site shared with `validate_workflow` —
  members exist and are enabled; parallel groups keep duplicate rejection and
  the width cap; sequence shapes keep repeat semantics through slot identity.
  Rejection uses the structured field-path error shape
  (`invalid_start_options`) so MCP callers can fix the named field.
- **Trust posture:** member selection is a start-time, caller-side choice
  over *globally enabled* agents only. It must not loosen #19 goal 3:
  project config still cannot introduce parallel execution or reference
  agents beyond the globally enabled set. A parallel group with substituted
  members has the same posture as a user-config parallel workflow.
- **Discovery:** `describe_options` advertises per-slot eligible members
  (enabled agents, with the workflow's configured member as the default) so
  clients can render a real choice; eligibility accumulation reuses the
  existing per-member facts in `effective_agents`.
- **TUI:** after the shape selection, a backends step renders the same
  selectable menu block with the configured members preselected (✓); Enter on
  `continue` accepts the defaults. Solo shapes collapse to a single-choice
  list. (Inference: once this lands, the `solo-*-cli`/`-sdk` recipe family
  can shrink back to one `solo` shape; that cleanup is part of this task's
  open questions, not a commitment.)
- **Settings echo:** the start response's `settings.workflow` /
  `settings.agents` already reflect effective members, so the confirmation
  surface needs no new fields (inference; verify during design review).

## Verification (expected shape)

- Starting `dual-review` with `members` claude + xai runs those two agents in
  one parallel group with attributed events, without a config change.
- Starting `cross-review` with a substituted reviewer keeps `[a, b, a]`
  execution (the lead slot reprises).
- Substitution referencing a disabled or unknown agent is rejected at start
  with a field-path error; parallel width/duplicate rules hold.
- Project config cannot supply or influence member selection.
- Omitting the field leaves every existing request and test unchanged.
- TUI wizard: shape → backends with defaults preselected; Enter-through
  equals today's behavior.

## Open questions

- Exact wire shape: slot-keyed map vs a parallel-only member list plus a
  sequence-only slot map; how slots are named and advertised.
- Interaction with the existing `backend` (uniform transport override) and
  `backend_options` fields — member selection chooses *agents*, transport
  selection stays orthogonal.
- Whether the built-in `solo-*` recipe family collapses into one `solo` shape
  once member selection exists, and what migrates existing references.
- Whether ad-hoc parallel member groups need any additional operator signal
  beyond the existing width cap (mirrors the deferred cross-session limiter
  in the parallel-review task document).
