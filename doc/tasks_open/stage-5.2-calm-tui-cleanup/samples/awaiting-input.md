# Sample: awaiting input

Stage 1a mockup — illustrative, not approved. Faithful to the `awaiting_input`
status: `format_activity_indicator` returns the static string `awaiting input`
(no spinner), the poller stays alive (`should_start_poller` is true), and the
session is still read-write for the referee.

## 1. Data assumptions

- Terminal: 80 x 24, session `daemon-80958c73c2e04baa`.
- Raw status: `awaiting_input` (the planned workflow finished; the session is
  parked waiting for referee input before the next turn).
- Interactive: true, so input is accepted (`_session_accepts_input` is true —
  `awaiting_input` is not terminal).

## 2. Mockup (80 x 24)

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────────────
 claude   The epoch guard covers the stale-batch path; I'd add one test for
          the terminal-status early return and call it done.
 codex    Same read. Nothing else outstanding from my side.
 referee  status: workflow complete — awaiting your input


────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                     referee note
 opened daemon-80958c73c2e04baa        awaiting input · Enter send · / cmds · q
```

The right of the status/hint line shows the static `awaiting input` (no spinner
— the session is not `running`). The rail is live: a referee note or `#agent`
turn resumes the session.

## 3. Color-token intent

- Session-info line: same as `main-session.md`.
- `awaiting input` activity: `muted` (calm, not `accent` — it is a resting
  state, not active work), on the right of the status/hint line.
- Transcript `referee` status line: `dim` (a status event, not a note).
- Rail: normal `[referee]` `muted`, mode chip `referee note` `accent`.

## 4. Keyboard behavior

- Input works exactly as in `main-session.md` — `Enter` sends, `#agent`/`​/ask`
  direct, `/` opens the palette; the send resumes the parked session.
- Status/hint line: message `opened daemon-80958c73c2e04baa` (or the last action
  result); hint (precedence rule: default / following — `awaiting_input` is not
  terminal) `awaiting input · Enter send · / cmds · q`.

## 5. Removed / simplified

- **Faithful:** the `awaiting_input` status, its `awaiting input` activity
  string, the live poller, and read-write input all match the code.
- **Target delta:** the resting activity is toned to `muted` and paired with the
  calm hint; no behavior change.
