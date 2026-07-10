# Stage 5.2 - Calm TUI Cleanup

Status: Stage 1a approved; Stage 1b decisions folded in (2026-07-10). Stage 2
implemented 2026-07-10 (five render paths, three approved interaction changes,
slice 3 + brand_color landed first) — pending user acceptance in a real
terminal. Outstanding: the narrow-fallback hero capture was skipped by
request; live acceptance replaces it.

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
- Typed daemon data layer this TUI consumes (do its "slice 3" here — see Stage 2):
  [../stage-5.3-daemon-api-contract.md](../stage-5.3-daemon-api-contract.md)
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
  **per agent** (`settings.agents[*].model` and `settings.agents[*].backend`),
  not one session-level value — a session can pair `claude` and `codex` on
  different models and backends. `.type` is the *provider* (claude/codex/
  antigravity, already carried by the agent name); the *execution backend* is
  `.backend` (`cli`/`sdk`, canonical `<type>_<backend>` e.g. `codex_sdk`), which
  stage 5.1 promoted to a first-class axis alongside the CLI backends. The info
  line summarizes participants (e.g. `claude:opus-4.8 · codex:gpt-5`) and
  collapses to a single entry for one-agent sessions.
  - **Inline vs `/details` (reconciled):** the info line shows, per agent,
    `name:model` always; the execution backend (`.backend`, shown as the bare
    backend id e.g. `sdk`) is appended inline only when it differs from that
    agent's default (`cli`, the registry fallback), and the full per-agent block
    (`format_session_details`, which carries `backend=…` and the canonical
    `<type>_<backend>`) stays behind `/details`. Both this README and
    `samples/README.md` must state this identically.
  - **Truncation priority** when the line overflows the width (drop right-most
    first): workflow -> workdir/project -> secondary agents (keep the lead
    agent) -> task (ellipsize last, never drop entirely).
- **Solid palette block (Stage 1b review, 2026-07-10):** the command palette
  renders on a solid gray fill like the Grok reference menu, using the David
  AI gray ramp — `--color-gray-floor #1F2124`, selected row on
  `--color-gray-panel #2E3033` (the system's "+1 rule") — see the Terminal
  color mapping table.
- **Grok-style input box (Stage 1b review, 2026-07-10):** the input rail is a
  bordered box (rounded box-drawing `╭─╮ │ ╰─╯`) with a `>` prompt inside and
  the mode chip right-aligned inside the box — the `[referee]`/`[new …]`
  bracket prompt is replaced by the chip, which already names the input
  target. The border uses the design system's warm `--border #635441`, per
  its "borders are interactive-only, warm = ours" rule. Bottom chrome becomes
  the 3-row box + the status/hint line (body height shrinks by one row); the
  hairline above the rail is dropped, the box border is the separator.
- **Provider brand colors (Stage 1b review, 2026-07-10):** agent labels and
  info-line agent names take the provider's official brand hue, declared per
  backend package (same for a provider's cli/sdk pair); unknown providers fall
  back to the accent teal — see Provider brand colors.
- **Tool events render as one summary row (2026-07-10):** a `tool` event shows
  a single dim line — tool name + compact args digest + result size (e.g.
  `tool  Read options.py:281 · +50 lines`) — never the full payload inline.
  Display-only projection (**target delta**): the JSONL transcript keeps full
  fidelity. The MCP read-side counterpart (`tool_output` parameter) is owned by
  [stage-5.3](../stage-5.3-daemon-api-contract.md), Remaining Workstream A.

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
and reserves background fills for the surfaces that need contrast: the user
message band (`raised`), and the command palette, which is a **solid gray
block** like the Grok reference menu — but drawn from the David AI gray ramp
(`--color-gray-floor #1F2124` fill, selected row on the next rung
`--color-gray-panel #2E3033`, the system's "+1 rule"), decided 2026-07-10
during Stage 1b review. Gray is the system's colorless tone, so the menu
reads as chrome, not content, while staying inside the design system. Every
color degrades gracefully:
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
| menu fill (palette)   | `#1F2124` | 234       | default bg           |
| menu selected row     | `#2E3033` | 236       | `A_REVERSE`          |
| input box border      | `#635441` | 59        | `A_DIM`              |

Source labels (`human`, `referee`, `claude`, `codex`, `tool`, `error`) keep
distinct hues today. The calm direction reduces reliance on six colors: agent
identity comes from **provider brand colors** (below); the non-agent sources
read in system tones (`referee`/`human` muted on the raised band, `tool` dim,
`error` red). Stage 1a mockups should state, per source, whether a hue is
retained or flattened.

### Provider brand colors

Decided 2026-07-10 during Stage 1b review: each agent takes its provider's
"official" brand hue, used for the gutter label and the info-line agent name.
The color is a **static backend fact**: each backend package declares a
`brand_color` (alongside `capabilities` on the `AgentBackend` protocol in
[../../../agent_collab/backends/base.py](../../../agent_collab/backends/base.py)),
with the **same value for a provider's `cli` and `sdk` backends** — brand
belongs to the provider, and hue must never encode the execution backend.
Unknown/other providers fall back to the David AI accent teal, which keeps
teal readable as "the system's own color" (chrome, referee cursor, spinner —
and any agent it doesn't recognize).

| Provider     | Brand hue        | truecolor | xterm-256 | 8-color fallback |
| ------------ | ---------------- | --------- | --------- | ---------------- |
| claude       | Anthropic coral  | `#D97757` | 173       | `COLOR_RED`      |
| codex        | OpenAI green     | `#10A37F` | 36        | `COLOR_GREEN`    |
| antigravity  | Gemini blue      | `#4285F4` | 69        | `COLOR_BLUE`     |
| _(unknown)_  | accent teal      | `#30AB92` | 37        | `COLOR_CYAN`     |

Note the accent's 256-color cell moves to 37 (from 36 in the base mapping
table) so the fallback teal and OpenAI green stay distinguishable at 256
colors; in 8-color they split as cyan vs green. In truecolor OpenAI green and
the teal are close relatives — accepted, since codex also reads by its gutter
position and name.

### Activity spinner

`format_activity_indicator` cycles ASCII `- \ | /` today (`ACTIVITY_FRAMES`),
whose `|` frame reads as a chrome pipe beside separators. Target: a **braille
orbit** `⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏` when the terminal is UTF-8 capable, with an
**ASCII dot-pulse** fallback (`.`, `..`, `...`) otherwise — the same
truecolor -> 256 -> ASCII graceful-degradation spirit as the color mapping.
Render it as `<frame> running` and separate it from other context fields with
` · `. This is an approved change to `ACTIVITY_FRAMES` /
`format_activity_indicator` (see Approved Interaction Changes).

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
   a few rows on a solid gray band (Grok-style, `--color-gray-floor #1F2124`),
   selected row highlighted (`--color-gray-panel #2E3033`), `command` + short
   description columns. Prominent but compact.
5. **Input box.** A bordered one-line input (Grok-style: rounded box-drawing
   border in the warm interactive `--border #635441`), `>` prompt inside, and
   the input **mode / target** chip right-aligned inside the box (referee note
   vs `#agent` directed vs `/new` wizard step) — the analogue of Grok's
   `Grok Build · auto` mode chip, replacing the old `[referee]` bracket
   prompt.
6. **Status/hint line (contextual).** One line directly below the input box,
   split: the transient **message** on the left (`sent note`, `queued for X`,
   errors, `refreshed`, read-only notices — today's `_render_status_line`
   message) and the state **hint + activity** on the right. The message
   updates on actions; the hint updates on state. Bottom chrome is the 3-row
   input box + this line in the steady state; extra rows are spent only when
   an overlay is open (palette, picker, details) or a high-severity error
   needs its own line. Activity is shown once here (right), not repeated in
   the top context line.

   Resolve the hint by this **precedence** (first match wins): new-session
   wizard -> session picker open -> slash palette open -> `/details` overlay ->
   no active session -> read-only/terminal session -> scrollback (not following)
   -> default (following). Examples:
   `Enter send · Tab complete · Esc close` (palette);
   `↑↓ move · Enter open · Esc close` (picker);
   `/new start · /help commands · q quit` (no session);
   `↑↓ scroll · End follow · q quit` (scrollback);
   `read-only` (terminal).

### `/details` disposition

Today `/details` renders a right-side panel only at width >= 100 and is silently
hidden below that even when `details_visible` is true ([tui.py](../../../agent_collab/tui.py)
`_details_width`). Target: keep the side panel at wide widths, and below the
threshold render details as a scrollable overlay (like the picker/help overlay)
so the toggle is never a no-op. The narrow fallback sample must show this.

## Approved Interaction Changes

These are the only behavior changes in scope; everything else stays byte-for-byte
(commands, event model, poller, dispatch). Each needs a Stage 2 test.

- **Activity spinner:** braille orbit with ASCII dot-pulse fallback, replacing
  `- \ | /` (see Activity spinner above).
- **Directed argument-entry mode:** while the rail holds a directed command that
  still needs its argument (`/ask ` or `#agent` with no message yet), the mode
  chip shows the target (`-> ask AGENT`, `-> codex`) and the message slot shows
  the usage hint; `Esc` cancels back to referee mode. Today completion simply
  vanishes after whitespace and the error only appears on `Enter`.
- **`Esc` closes `/details`:** `Esc` dismisses the details panel/overlay, matching
  how `Esc` already closes the palette and picker. Today `/details` only toggles
  via the command.

Mockups must tag each divergence from current behavior as one of: **faithful**
(matches code), **target delta** (restyle only, no behavior change — e.g.
lowercase picker headers, `▸` marker), or **approved interaction change** (listed
here). Do not label a behavior change "faithful".

## Current TUI -> Target Delta

Grounding for Stage 2, from [tui.py](../../../agent_collab/tui.py):

- Header (`_render_header`) is one dense space-joined string
  (`agent-collab id status workflow workdir [tags]`). Split into the quiet
  context line + the session info line; add model and backend.
- Status line (`_render_status_line`) mixes the transient message with the
  scroll/activity mode. Restructure into one **status/hint line** below the
  input box — message left, state hint + activity right — layout region 6.
  Activity shows here only, not in the top context line.
- Input line (`_render_input_line`) is a bare `[referee] ` prompt row. Becomes
  the Grok-style bordered input box (region 5): `>` prompt, mode chip inside
  right, border in the warm `--border #635441`; body height gives up one row
  for the box.
- Slash completion (`_render_slash_completion`) already renders a compact
  menu near the input with a selected row — this is close to the target
  palette; restyle to David AI tokens rather than rebuild.
- The large `HELP_LINES` overlay stays reachable via `/help` but is no longer
  the primary discovery path.
- Separators (`"-" * width` in `chrome` dim) become hairline-styled rules used
  sparingly.
- Behavior (commands, event model, poller, input dispatch) stays unchanged
  except for the three items under Approved Interaction Changes (spinner,
  directed argument-entry mode, `Esc` closes `/details`).

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

### Scope simplification (2026-07-10, agreed before implementation)

The Stage 1a sample set exists for design approval; it is **not** a list of
distinct things to build. Implementation-wise there are only **five render
paths**:

1. the main session layout (context + session-info lines, gutter body,
   bordered input box, status/hint line);
2. the palette menu block (gray slab docked above the input box);
3. **one** scrollable full-body overlay component, reused by `/help`, the
   session picker, and `/details` below the width threshold — do not build
   three separately-styled overlays;
4. the `/details` wide side panel;
5. narrow / sub-minimum degradation.

Everything else in the samples (first-launch, awaiting-input, error,
read-only rejection, directed input, `/new` wizard steps, sdk-backend chip) is
the main layout with different chip / hint / info-line / message contents —
specified entirely by the hint-precedence list, the mode-chip states, and the
truncation + brand-color rules, and verified by unit tests on the formatting
helpers, not by bespoke screens.

Also resolved to keep scope down:

- **`/details` wide-panel overflow:** keep clipping, add a `…` marker on the
  last visible row (option (a) from `details-visible.md`); the
  scrollable-overlay-at-wide-widths idea is deferred.
- **Stage 1b captures:** only the three hero screens (main session, palette,
  narrow fallback) are rendered and approved; the remaining mockups are
  reference material and get no captures.

After the screenshots are approved:

- Refactor TUI rendering around the approved layout.
- **Consume the typed daemon client in the same pass — this is stage-5.3's
  deferred "slice 3."** [stage-5.3](../stage-5.3-daemon-api-contract.md) landed
  shared typed DTOs (`api_schema`) + a versioned client but deliberately left
  `AgentCollabClient` methods returning raw dicts, so the doomed dict-based
  `tui.py`/`cli.py` call sites are migrated **once here, not churned twice**. In
  this Stage 2: swap the client return types to the DTOs (`get_session ->
  SessionStateModel`, `list_sessions -> SessionListModel`,
  `read_events`/`wait_events` -> `EventBatchModel`, `stop_session` /
  `post_message`, …); migrate the `tui.py` (and `cli.py`) call sites off
  `session["status"]` / `.get(...)` dict access to typed attributes; and update
  `HttpClientToolBackend` ([mcp_tools.py](../../../agent_collab/mcp_tools.py)) to
  `.to_dict()` the client results before the MCP `content()` serializer. See
  stage-5.3 "Remaining Workstream A work".
- Add `brand_color: str` to the `AgentBackend` protocol
  ([backends/base.py](../../../agent_collab/backends/base.py)) and each backend
  package — a static registry fact like `capabilities`, identical across a
  provider's `cli`/`sdk` pair (claude `#D97757`, codex `#10A37F`, antigravity
  `#4285F4`). Surface it through the session settings / `describe_options` so
  the TUI colors labels from backend data instead of a hardcoded provider map,
  falling back to the accent teal for providers it doesn't recognize.
- Keep command/event behavior unchanged unless the approved samples require a
  specific interaction change.
- Add focused tests for formatting helpers, command palette behavior, the
  shared scrollable overlay component (one suite covering `/help`, the picker,
  and `/details` narrow — per the scope simplification above), the contextual
  hint precedence selection, the status/hint line composition (message left +
  hint/activity right), the spinner (braille frames + ASCII fallback
  selection), directed argument-entry mode, `Esc` closing `/details`, the
  `/details` wide-panel clip-with-`…`-marker, and narrow-terminal rendering. Also cover the slice-3 client change:
  each `AgentCollabClient` method returns its `api_schema` DTO, and
  `HttpClientToolBackend` still serializes correctly (DTOs `.to_dict()`ed for
  MCP `content()`).
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
