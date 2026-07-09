# Stage 5.2 - Calm TUI Cleanup

Status: draft, not approved for implementation.

## Goal

Clean up the agent-collab TUI so it is easier to understand, calmer, and more
purposeful. The first screen should make the current session, available actions,
and input target obvious without feeling busy.

This is a staged task with approval gates:

1. Stage 1a: plain-text markdown sample mockups, reviewed and approved.
2. Stage 1b: ANSI/color screenshots of the approved mockups, reviewed and
   approved.
3. Stage 2: implement the approved direction.

Do not start Stage 1b until the text mockups are approved. Do not start Stage 2
until the ANSI screenshots are approved.

## References

- Grok CLI screenshot reference:
  [assets/grok-cli-reference.svg](assets/grok-cli-reference.svg)
- Screenshot note:
  [assets/README.md](assets/README.md)
- Current TUI implementation:
  [../../../agent_collab/tui.py](../../../agent_collab/tui.py),
  [../../../agent_collab/tui_core.py](../../../agent_collab/tui_core.py)
- David AI design system:
  `/home/devel/projects/david_ai_git/doc/david_ai_design_system`

The Grok CLI reference is layout inspiration only. The agent-collab TUI palette
must come from the David AI design system.

## Resolved Decisions

These were confirmed with the user and now drive the samples. They supersede the
old "Initial Open Questions" (kept at the bottom for history).

- **Sample format:** plain-text markdown mockups first (Stage 1a). ANSI/color
  screenshots come only after the mockups are approved (Stage 1b).
- **First screen:** open the latest session's transcript
  (`select_latest_session_id`). With **zero** sessions, keep today's real
  behavior — open with no active session and a clear empty state pointing at
  `/new` (today's message is `no daemon sessions found; use /new` at
  [tui.py:781](../../../agent_collab/tui.py)); do **not** auto-open a picker.
  Auto-opening an empty picker would be a new interaction — out of scope unless
  explicitly requested.
- **Command help:** Grok-style. Because this tool is used occasionally, not
  daily, users will not remember commands, so discovery must be built in:
  - a command palette that is prominent but compact (a few rows, not a wall of
    text), surfaced while typing `/`;
  - a bottom hint line whose contents change with the current state
    (input mode, overlay open, scrollback vs following, read-only);
  - the exhaustive list stays behind `/help`.
- **Session info to surface:** model, task, workdir, and backend should be
  legible for the active session (today's header shows session id, status,
  workflow, and workdir but not model or backend). Note: model and backend are
  **per agent** (`settings.agents[*].model` and `.type`), not one session-level
  value — a session can pair `claude` and `codex` on different models. The info
  line summarizes participants (e.g. `claude:opus-4.8 · codex:gpt-5`) and
  collapses to a single entry for one-agent sessions.
  - **Inline vs `/details` (reconciled):** the info line shows, per agent,
    `name:model` always; backend (`.type`) is appended inline only when it
    differs from that agent's configured default, and the full per-agent block
    (`format_session_details`) stays behind `/details`. Both this README and
    `samples/README.md` must state this identically.
  - **Truncation priority** when the line overflows the width (drop right-most
    first): workflow -> workdir/project -> secondary agents (keep the lead
    agent) -> task (ellipsize last, never drop entirely).

## Design System Inputs

Use the David AI dark, warm-charcoal system as the source of truth:

- `--page #0D0C0A`: app floor.
- `--terminal #0A0906`: darkest inset for terminal/log material.
- `--floor #211C15`: grouping band.
- `--panel #322A20`: default standalone surface.
- `--raised #423629`: nested raised tile.
- `--text #F6EFE2`: primary text.
- `--muted #C2B6A3`: secondary text.
- `--dim #8C8170`: tertiary/caption text.
- `--teal #30AB92`: one primary accent.
- `--hairline rgba(255,255,255,.09)`: section separators.

### Terminal color mapping

Terminal color support is limited, and setting background fills overrides the
user's terminal theme, so this design leans on **foreground color + weight**,
and reserves background fills for the two bands that need contrast (the user
message band and the selected palette row). Every color degrades gracefully:
truecolor -> xterm-256 -> the 8-color source labels the current TUI already
uses.

| Token intent          | David AI  | xterm-256 | 8-color fallback     |
| --------------------- | --------- | --------- | -------------------- |
| page (default bg)     | `#0D0C0A` | 232       | terminal default bg  |
| terminal inset bg     | `#0A0906` | 233       | default bg           |
| panel / rail bg       | `#322A20` | 235       | default bg           |
| raised band bg        | `#423629` | 237       | `A_REVERSE`          |
| primary text          | `#F6EFE2` | 255       | default fg           |
| muted text            | `#C2B6A3` | 250       | default fg           |
| dim / caption         | `#8C8170` | 245       | `A_DIM`              |
| accent (one only)     | `#30AB92` | 36        | `COLOR_CYAN`         |
| hairline separator    | white .09 | 236/238   | `A_DIM` dashes       |

Source labels (`human`, `referee`, `claude`, `codex`, `tool`, `error`) keep
distinct hues today. The calm direction reduces reliance on six colors: prefer
one accent plus the aligned source label for identity, and let secondary agents
read in muted/dim tones. Stage 1a mockups should state, per source, whether a
hue is retained or flattened.

## Target Layout

Adapted from the Grok reference, top to bottom. Regions map to the current
render in [tui.py](../../../agent_collab/tui.py) so Stage 2 has a clear delta.

1. **Context line (top).** Left: branch + project/workdir. Right: session
   status and a compact usage/activity readout. Quiet, `dim`.
2. **Session info line.** Task, per-agent `name:model` (backend appended only
   when it differs from that agent's default), and workflow for the active
   session — see the reconciled inline-vs-`/details` rule and truncation
   priority under Resolved Decisions. This is the new surface the user asked
   for. Multi-agent sessions summarize participants
   (`claude:opus-4.8 · codex:gpt-5`); `/details` expands the full per-agent
   block from `format_session_details`.
3. **Transcript body.** Readable prose, not over-framed cards. Source labels
   left-aligned in a narrow gutter; user/referee messages get a subtle
   `raised` band with a timestamp; thinking/tool/status metadata stays `dim`.
4. **Command palette (contextual).** Appears near the input while typing `/`:
   a few rows, selected row highlighted (`raised`), `command` + short
   description columns. Prominent but compact.
5. **Input rail.** A single focused prompt line. Right side shows the input
   **mode / target** (referee note vs `#agent` directed vs `/new` wizard step),
   the analogue of Grok's `Grok Build · auto` mode chip.
6. **Message slot.** A dedicated line (or a left segment of the hint line) for
   transient feedback that today lives in `_render_status_line`: `sent note`,
   `queued for X`, errors, `refreshed`, read-only notices. This must stay
   distinct from the static hint text — the message updates on actions, the
   hints update on state. Messages are transient (clear on next keystroke/turn);
   hints are ambient.
7. **Bottom hint line (contextual).** Static key affordances for the current
   state. Because states overlap, resolve the hint by this **precedence** (first
   match wins): new-session wizard -> session picker open -> slash palette open
   -> `/details` overlay -> read-only/terminal session -> scrollback (not
   following) -> default (following). Examples:
   `Enter send · Tab complete · Esc close` (palette);
   `up/down move · Enter open · Esc close` (picker);
   `up/down scroll · End follow · q quit` (scrollback);
   `read-only` (terminal).

### `/details` disposition

Today `/details` renders a right-side panel only at width >= 100 and is silently
hidden below that even when `details_visible` is true ([tui.py](../../../agent_collab/tui.py)
`_details_width`). Target: keep the side panel at wide widths, and below the
threshold render details as a scrollable overlay (like the picker/help overlay)
so the toggle is never a no-op. The narrow fallback sample must show this.

## Current TUI -> Target Delta

Grounding for Stage 2, from [tui.py](../../../agent_collab/tui.py):

- Header (`_render_header`) is one dense space-joined string
  (`agent-collab id status workflow workdir [tags]`). Split into the quiet
  context line + the session info line; add model and backend.
- Status line (`_render_status_line`) mixes the transient message with the
  scroll/activity mode. Split into a distinct **message slot** (transient
  feedback) and the static **hint line** (state affordances) — see layout
  regions 6 and 7.
- Slash completion (`_render_slash_completion`) already renders a compact
  menu near the input with a selected row — this is close to the target
  palette; restyle to David AI tokens rather than rebuild.
- The large `HELP_LINES` overlay stays reachable via `/help` but is no longer
  the primary discovery path.
- Separators (`"-" * width` in `chrome` dim) become hairline-styled rules used
  sparingly.
- Behavior (commands, event model, poller, input dispatch) stays unchanged
  unless an approved sample requires a specific interaction change.

## Stage 1a - Text Mockups For Approval

Deliver static plain-text markdown mockups under `samples/`, one file per view.
The **authoritative file list, widths, and per-sample template live in
[samples/README.md](samples/README.md)** — it is the source of truth and
already covers the review-expanded states (main session, palette states,
picker, new-session wizard, first-launch/zero-session, `/details` wide + narrow,
directed `#agent` input, awaiting-input, error + read-only rejection, narrow and
sub-minimum terminal).

Each sample must call out (per that template): data assumptions, layout
structure, David AI color-token intent, keyboard behavior (both the transient
message and the contextual bottom hint, naming the precedence rule), and what
was removed or simplified from the current TUI.

Approval checkpoint: review the mockups with the user and adjust until the
direction is approved. Only then start Stage 1b.

## Stage 1b - ANSI Screenshots For Approval

After the mockups are approved, render the hero screens (at minimum the main
session view, the command palette, and the narrow fallback) as real colored
terminal captures using the David AI palette and the terminal mapping above.
Store alongside the mockups. Review and approve before implementation.

## Stage 2 - Implementation

After the screenshots are approved:

- Refactor TUI rendering around the approved layout.
- Keep command/event behavior unchanged unless the approved samples require a
  specific interaction change.
- Add focused tests for formatting helpers, command palette behavior, session
  picker behavior, the contextual bottom-hint precedence selection, the
  message-slot vs hint split, the `/details` wide-panel vs narrow-overlay
  fallback, and narrow-terminal rendering.
- Run the full test suite before closing.

## Review Notes

This plan was reviewed by a solo-codex agent-collab session
(`daemon-80958c73c2e04baa`, 2026-07-09) before Stage 1a. That pass corrected the
zero-session first-screen behavior, reconciled the inline-vs-`/details` model and
backend rule, added the message-slot / hint-precedence split, flagged the
`/details` narrow-width no-op, and expanded the sample set and per-sample data
assumptions. Those are folded into the sections above.

## Initial Open Questions (resolved)

Kept for history. Where a first-pass answer below was later corrected in review,
the authoritative version is in "Resolved Decisions" above — trust that section.

- Should Stage 1 samples be plain text, generated ANSI, or both?
  -> Text mockups first, ANSI after mockup approval.
- Should the first screen prioritize the latest session or a picker?
  -> Latest session. (First-pass answer said "picker when none exist";
  **corrected** in review — with zero sessions the TUI shows an empty state
  pointing at `/new`, not a picker.)
- Which status items belong in the top line vs the bottom input rail?
  -> Top: quiet context + session info (task · per-agent `name:model` ·
  workflow). Bottom: input mode/target on the rail, a transient message slot,
  and a state-dependent hint line (see the reconciled backend rule and hint
  precedence above).
- How much slash-command help should remain visible by default?
  -> A compact contextual palette while typing `/`, plus changing bottom hints;
  the full list moves behind `/help`.
