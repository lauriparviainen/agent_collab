# Sample: slash command palette

Stage 1a mockup — illustrative, not approved. Covers the real palette states
from `make_slash_completion` / `filter_slash_commands` / `_handle_key`.

## 1. Data assumptions

- Terminal: 80 x 24, same running `cross-review` session as `main-session.md`.
- The palette is an overlay docked directly above the input rail (today:
  `_render_slash_completion` at the bottom of the body). Only the bottom of the
  screen is shown; the context/info/body above are unchanged.
- Commands come from `SLASH_COMMANDS` (10 total). Two-row bottom chrome, so the
  palette occupies the overlay row band above the rail + status/hint line.

## 2. Mockups

### 2a. Bare `/` — all commands (windowed, selection at top)

```
────────────────────────────────────────────────────────────────────────────
 / commands
 ▸ /help       commands and keys
   /sessions   pick from daemon sessions
   /session    switch active session
   /new        start an interactive daemon session
   /details    toggle session details
   /follow     jump to tail and follow
   /stop       stop the active session
────────────────────────────────────────────────────────────────────────────
 [referee] /▏                                                    referee note
                          ⠹ running · Tab complete · Enter run · Esc close
```

10 commands, window of 7; `↓` scrolls to reveal `/refresh`, `/ask`, `/quit`
(faithful to `format_slash_completion_lines`' windowing).

### 2b. Partial prefix `/s` — three matches

```
────────────────────────────────────────────────────────────────────────────
 / commands
 ▸ /sessions   pick from daemon sessions
   /session    switch active session
   /stop       stop the active session
────────────────────────────────────────────────────────────────────────────
 [referee] /s▏                                                   referee note
                          ⠹ running · Tab complete · Enter run · Esc close
```

Matches exactly `filter_slash_commands("/s")` -> `/sessions`, `/session`,
`/stop`.

### 2c. Exact `/help` — Enter runs, does not re-complete

```
────────────────────────────────────────────────────────────────────────────
 / commands
 ▸ /help       commands and keys
────────────────────────────────────────────────────────────────────────────
 [referee] /help▏                                                referee note
                               ⠹ running · Enter run /help · Tab keep · Esc
```

Faithful: when input equals the selected command, `slash_completion_matches_input`
is true, so `Enter` submits `/help` instead of inserting a trailing space.

### 2d. Argument form `/ask ` — rail enters argument mode

```
────────────────────────────────────────────────────────────────────────────
 codex    Agreed. wait_events re-reads the session on empty batches.
 referee  note: ship it once tests pass                                  9:22
────────────────────────────────────────────────────────────────────────────
 [referee] /ask ▏                                                  -> ask AGENT
 usage: /ask AGENT message              ⠹ running · type agent then Q · Esc
```

A space in the body makes `make_slash_completion` return `None`, so the palette
closes. **Approved interaction change:** today the completion just vanishes and
the `usage: /ask AGENT message` error only appears on `Enter`; the target shows
the mode chip `-> ask AGENT` and the usage hint live, with `Esc` cancelling back
to referee mode.

### 2e. No match `/xyz`

```
────────────────────────────────────────────────────────────────────────────
 / commands
   no matches
────────────────────────────────────────────────────────────────────────────
 [referee] /xyz▏                                                 referee note
                                        ⠹ running · no command matches · Esc
```

Faithful: `make_slash_completion("/xyz")` returns an empty-match state, and
`format_slash_completion_lines` emits `no matches`.

## 3. Color-token intent

- Palette block (header + rows): solid fill from the David AI gray ramp,
  Grok-style (Stage 1b decision, 2026-07-10) — `--color-gray-floor #1F2124`,
  so the menu reads as one chrome surface, not transparent rows over the
  transcript.
- No palette header (Stage 1b decision, 2026-07-10): the typed `/` in the
  input box is the label and keys live on the status/hint line, so the header
  row is dropped; the Stage 1a mockups' ` / commands` line is superseded.
- Rows: command name in `accent`, description in `muted`.
- Selected row (`▸`): `--color-gray-panel #2E3033` band over the gray fill
  (the "+1 rule"), name in `text`. Today's `selection` style restated in
  David AI tokens.
- `no matches`: `dim`.
- Rail mode chip: `referee note` (`accent`) normally; `-> ask AGENT` (`accent`)
  in argument mode.
- Status/hint line: usage/message left (`muted`, `error` on failure); spinner +
  hint right (`dim`).

## 4. Keyboard behavior

- Typing `/` opens the palette; each keystroke refilters.
- `↑ ↓` move the selection (only while there are matches; with no matches they
  fall back to scrolling the transcript — faithful to `_handle_key`).
- `Tab` inserts the selected command + trailing space (`accept_slash_completion`).
- `Enter` runs the command when input already equals it; otherwise accepts the
  completion.
- `Esc` closes the palette (`slash_completion_dismissed_for`) without clearing
  the typed text; in argument mode `Esc` cancels back to referee mode.
- Status/hint line (precedence rule: slash palette open) varies by sub-state as
  shown; the left message is blank except in argument mode (`usage: …`).

## 5. Removed / simplified

- **Target delta:** restyles the existing completion menu — the header row is
  dropped entirely (today `commands  Tab/Enter accepts  Esc closes`; the typed
  `/` and the hint line carry that information), the block gets the solid gray
  fill, selection is a `--color-gray-panel` band, marker `▸` (today `>`). Same
  windowing and match behavior.
- **Approved interaction change:** directed argument-entry mode for `/ask ` (see
  2d and the parent README).
- Demotes the 19-line `HELP_LINES` overlay: still reachable via `/help`, but the
  palette is the primary discovery path.
