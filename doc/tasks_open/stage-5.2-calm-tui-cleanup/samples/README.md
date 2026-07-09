# Samples

Stage 1a delivers plain-text markdown mockups here, one file per view, before
any implementation. Stage 1b adds ANSI/color captures of the approved mockups
(see the parent [README](../README.md) for the staging and approval gates).

## Files (Stage 1a)

- `main-session.md`
- `slash-command-palette.md` — cover the real palette states: bare `/`, a
  partial prefix (`/s`), an exact match (`/help`), an argument form (`/ask `),
  and no-match. See the palette rules in the parent README.
- `session-picker.md`
- `new-session-flow.md` — the `/new` wizard steps (task -> workflow -> workdir).
- `first-launch.md` — no active session / zero sessions, empty state pointing
  at `/new` (matches `run_tui`'s real zero-session behavior, not a picker).
- `details-visible.md` — `/details` as the wide side panel and as the narrow
  overlay fallback.
- `directed-input.md` — `#agent` directed input mode on the rail.
- `awaiting-input.md`
- `error-state.md` — plus a read-only/terminal session with a rejected input
  attempt.
- `narrow-terminal.md` — and the sub-minimum frame (below the
  `height < 5 or width < 20` cutoff).

Show the same underlying session content across files. Render main-session,
details, and the picker at both 80-col and ~100-col (they carry real responsive
risk); simple states may use a single representative width. The narrow fallback
is ~48-col, plus one sub-minimum frame.

## Per-sample template

Each file must contain these five sections:

1. **Data assumptions** — pin the state the mockup depicts so approval blesses a
   reachable state, not an impossible one: session status, workflow + agent
   sequence, agents with their types/models, interactive flag, terminal
   width×height, follow vs scrollback, and which overlay is active
   (none / palette / picker / details / new wizard).
2. **Mockup** — the terminal frame in a fenced code block, box-drawn, at the
   stated width. Two-row bottom chrome: the input rail, then one status/hint
   line (transient message left, hint + activity right). A third row appears
   only for an open overlay or a high-severity error.
3. **Color-token intent** — which David AI token each region uses (see the
   terminal mapping in the parent README), including per-source label treatment.
4. **Keyboard behavior** — keys active in this state, and the exact strings on
   the status/hint line (left message + right hint), naming which precedence
   rule selected the hint.
5. **Removed / simplified** — what this drops or calms versus the current TUI
   (reference the region in `tui.py`). Tag every divergence from current
   behavior as **faithful**, **target delta** (restyle only), or **approved
   interaction change** (per the parent README's Approved Interaction Changes) —
   never call a behavior change "faithful".

## Anchor mockup (main session, ~80 col)

Reference frame the other samples should stay consistent with. Illustrative
only — not approved.

```
 main ~/projects/agent_collab
 test the poller · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────
 codex     Reworked the poller so wait_events drives the cursor.
 referee   note: check the terminal-state early return          9:22
 claude    Following. The epoch guard already covers that path.

           ◆ thinking…
────────────────────────────────────────────────────────────────────
 [referee] ▏                                           referee note
 sent note                        ⠹ running · Enter send · / cmds · q
```

Regions: line 1 context (`dim`), line 2 session info
(task · per-agent `name:model` · workflow, `muted` with `accent` on the agent
names), body with source gutter, hairline rules, input rail with mode chip
(`referee note`) on the right, then the two-row bottom chrome's status/hint line
— transient message left (`sent note`), spinner + hint right
(`⠹ running · …`). Backend (`.type`) is appended inline per agent only when it
differs from that agent's default; the full per-agent block stays behind
`/details`. The spinner is the braille orbit (ASCII dot-pulse fallback), and
activity appears here only — not in the top context line.
