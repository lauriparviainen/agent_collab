# Cross-agent review skills

**Status:** Implemented; automated verification and live solo-provider smoke complete,
dual/preflight smoke pending.

**Created:** 2026-07-13

**Issue:** [#18](https://github.com/lauriparviainen/agent_collab/issues/18)

## Context

The most useful agent-collab setup is MCP: a coding agent asks another
vendor's agent to review its work. Today the MCP surface teaches the
*protocol* (tool descriptions, `agent_collab_guidance`), but nothing teaches
a coding agent the *workflow*: when to start a review, how to scope it, how
to drive the event loop, and how to triage what comes back. Users must
rediscover all of it.

The SKILL.md format (a folder holding a `SKILL.md` with name/description
frontmatter) has become a cross-vendor standard: Claude Code loads skills
from `~/.claude/skills/` and plugin marketplaces, Codex from
`~/.agents/skills/` (user) and `.agents/skills/` (repo) with
`.codex-plugin/plugin.json` packaging — Codex custom prompts are deprecated
in favor of skills — and Antigravity uses the same folders-with-SKILL.md
concept. One shipped skill set can therefore serve every supported agent;
the per-vendor difference reduces to the install destination.

This design was reviewed before implementation with all four CLI backends
through agent-collab itself (Claude Opus, Codex, Gemini 3.1 Pro, Grok
Build); their consensus and dissents are folded into the decisions below.

## Goals and decisions

1. **Two thin skills, not one with a mode argument.** Skill descriptions are
   the router: a single `solo|dual` skill blurs description-based triggering
   and forces the agent to infer the mode. Dual's reconcile step is distinct
   behavior, not a parameter.
   - `skills/agent-collab-solo-review/` — start one other-vendor reviewer on
     the current diff, triage, report.
   - `skills/agent-collab-dual-review/` — two vendors in parallel, then
     reconcile: same-location findings are high-confidence; disagreements
     are adjudicated by reading the code, never by majority vote.
   - Keep the `agent-collab-` name prefix: the copy fallback installs into a
     flat namespace where a user's own `review` skill would collide.
   - Descriptions state the cost tradeoff (one provider turn vs two plus
     reconcile) and the trigger phrases users actually type ("second
     opinion", "cross-vendor review", "have another model review my diff").

2. **The recipe lives in the daemon, not in the skill.** The full review
   recipe becomes an `agent_collab_guidance` topic (extending
   `agent_collab/mcp-guidance.md`), and the skills fetch it at runtime. One
   canonical source, versioned and served by the daemon; the skills stay
   thin (trigger, preflight, orchestration order, output format) and do not
   rot when the daemon evolves. This also avoids fragile cross-directory
   `references/` paths under per-vendor skill packaging. (Unanimous
   recommendation from all four consulted backends.)

3. **Recipe content.** The guidance topic must contain, mechanically rather
   than as prose:
   - *Scope first:* resolve the absolute workdir; define "current diff"
     precisely (working tree + staged + untracked vs `HEAD`, or an explicit
     base ref); build a one-path-per-line changed-file list including
     renames and deletions. The file list is the primary scope, not a hard
     visibility wall: reviewers may follow direct dependencies of a finding
     but must not run repository-wide searches.
   - *Prompt template:* explicit file list; findings need `file:line`, a
     concrete failure scenario, and severity (high/medium only); read-only;
     no stylistic rewrites. Findings without a resolvable `file:line` are
     dropped, not surfaced.
   - *Event loop as pseudocode:* seed the cursor with `read_events`, loop
     bounded `agent_collab_wait_events` (~20s), always advance to the
     returned cursor, back off between routine batches, and terminate on
     terminal session status — never on an empty batch. Reviews pass
     `interactive: false` so sessions cannot park in `awaiting_input`.
   - *Parallel-session hygiene (dual):* start both sessions before watching
     either; keep one `{session_id, backend, cursor, status}` record per
     reviewer; poll round-robin; reconcile only after both are terminal;
     prefix every reported finding with `[<session_id> <backend>]`.
   - *Triage checklist:* open each cited `file:line` and confirm the
     scenario against real code before surfacing; unconfirmed findings are
     downgraded or dropped; reviewers are advisory — never auto-apply.
   - *Backend quirk matrix, marked advisory/dated:* antigravity `mode=plan`;
     xai `permission_mode=auto` (plan cancels silently headless); codex
     needs the explicit file list and no broad greps. Allowed option values
     and models are read from `agent_collab_describe_options` at runtime —
     the schema is authoritative; the matrix records only what the schema
     cannot say.
   - *Honest boundaries:* prompt-level "read-only" is behavioral, not a
     security boundary; use backend sandbox/mode options where they exist.

4. **Backend selection by underlying model, not backend name.** The skills
   pick reviewers from what `agent_collab_describe_options` reports as
   enabled and start-eligible. Owner decision (2026-07-14): when the user has
   not already named the reviewer model(s), show eligible model/backend pairs
   and ask; never silently choose the strongest or cheapest. Ask for a backend
   only when the selected model maps to multiple eligible backends. Before the
   provider call, show the workflow, selected model(s), canonical backend(s),
   and effective default/override options and obtain explicit confirmation;
   minor defaults do not each require a separate choice.
   Antigravity can run Claude models, so a genuine second opinion must
   compare the *model*, or dual-review silently becomes
   Claude-reviewing-Claude.

5. **Preflight or fail with remediation.** The skills ship instructions but
   not the daemon. Before starting, verify the `agent_collab_*` tools are
   connected and the daemon responds; on failure, emit the exact README
   remediation (MCP registration command, `agent-collab daemon start`)
   instead of failing opaquely.

6. **Distribution.**
   - Top-level `skills/` directory in this repo (not `.claude/skills/`,
     which holds this repo's own contributor conventions).
   - `.claude-plugin/marketplace.json` so Claude Code users install with
     `/plugin marketplace add lauriparviainen/agent_collab`; the plugin
     description states the daemon + MCP registration prerequisite.
   - `.codex-plugin/plugin.json` for Codex plugin packaging.
   - README "Skills" section with a per-agent copy-destination table
     (`~/.claude/skills/`, `~/.agents/skills/`, `~/.gemini/config/skills/`,
     `~/.grok/skills/`) as the universal fallback.
   - Core protocol invariants (absolute workdir, discovery first, bounded
     cursor polling, guidance pointer) also go into the MCP server
     `instructions`, which some agents (Codex) read automatically before
     ever deciding to call a guidance tool; the first ~500 characters must
     stand alone.
   - Install stays out of other tools' config directories: no automatic
     copying into `~/.claude/` or `~/.agents/` by `./agent_collab.sh`.
     Explicit `./agent_collab.sh skills install|uninstall [client]` commands
     manage both review skills independently, refuse to overwrite foreign or
     locally modified copies, and keep their ownership state under
     `~/.agent-collab/`; omitting the client selects all supported clients.

7. **The dual skill builds on the daemon-side parallel workflow (#19).**
   Gemini reported client-side parallel session management is error-prone
   for it and asked for a daemon-orchestrated parallel workflow (one
   session, N concurrent reviewers, merged attributed event stream).
   Decision (2026-07-13): implement
   [#19](https://github.com/lauriparviainen/agent_collab/issues/19) first —
   design in the task document `parallel-review-workflow` — so the dual
   (and triple) review skill is a single `agent_collab_start` of a flat
   `parallel` workflow plus one watch loop, and the client-side
   parallel-session hygiene rules in the recipe reduce to per-agent
   attribution handling. The dependency shipped on 2026-07-13; this task is no
   longer blocked.

## Implementation notes

- Extend `agent_collab/mcp-guidance.md` with the review-recipe topic; the
  guidance tool already serves topics from this document, and it ships in
  the wheel (`[tool.setuptools.package-data]`).
- MCP server `instructions` changes touch the server/MCP layer; keep them
  within the existing instruction budget and verify against the hermetic
  MCP tests.
- Skill frontmatter descriptions carry the trigger phrases; keep them under
  the length that marketplaces truncate.
- Verify skill loading in at least Claude Code (plugin + copy) before
  release; other vendors' loading is documented best-effort with the copy
  table.
- The quirk matrix duplicates knowledge currently held in this repo's
  private memory and task docs; the guidance topic becomes its public,
  canonical home.

## Implementation verification

Implemented 2026-07-14 with two top-level skills, the daemon-served
`review-recipe` guidance topic, expanded MCP initialization instructions,
Claude marketplace and Codex plugin metadata, and README installation guidance.
An explicit checkout CLI installs, upgrades, and safely uninstalls both skills
for Claude Code, Codex, Antigravity, Grok, or all four without coupling those
writes to the main runtime installer. Managed state is persisted after each
completed destination so a later client failure cannot strand an earlier
upgrade as an apparent local modification. Preflight remediation covers native
MCP configuration for all four clients.
Both skills and the Codex manifest pass the bundled validators. An isolated
Claude home installed the repository marketplace and reported both skills in
the plugin inventory. In an isolated staged worktree, the supported Python 3.12
environment passes the full development gate with 829 hermetic tests, and
`build --check` passes. A live installed solo skill selected Gemini 3.1 Pro
High through `antigravity_cli`, completed one read-only provider turn, and
confirmed the global Antigravity skill destination. The resulting
partial-upgrade and missing Antigravity/Grok MCP-remediation findings were fixed
and covered by regression tests.

## Verification

- `agent_collab_guidance` returns the review-recipe topic, and the hermetic
  guidance/MCP tests cover it.
- `./agent_collab_dev.sh test` and `build --check` pass.
- Manual: install the skills into Claude Code via the marketplace manifest
  and via plain copy; run solo review and dual review against a real diff
  in this repo; confirm preflight failure output with the daemon stopped.
- Completed 2026-07-14: installed-skill solo review against a real diff through
  Gemini 3.1 Pro High on `antigravity_cli`.
- Remaining: live dual review, daemon-stopped preflight, and plain-copy loading
  checks.
- Dual review reconciliation labels agreements and disagreements and cites
  `[session_id backend]` per finding.
- README documents the skills, prerequisites, and the per-agent install
  table.

## Resolved questions

- Resolved 2026-07-14: ship `.codex-plugin/plugin.json` in the first iteration;
  its current manifest validates locally, while copy installation remains the
  documented universal fallback until a repo marketplace is deliberately
  added.
- Resolved 2026-07-14: solo review has no cheapest/strongest default. Ask for
  the reviewer model when it is unclear from the request, then confirm the
  effective backend and options before the provider call.
