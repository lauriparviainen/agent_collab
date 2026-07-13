# Sample: session picker

Stage 1a mockup — illustrative, not approved. The **behavior** (sorting latest
first, current session pre-selected, Enter activates) is faithful to
`make_session_picker` / `format_session_picker_lines`; the **strings/styling are
a target delta** (see section 5).

## 1. Data assumptions

- Opened via `/sessions` from the running session in `main-session.md`.
- Four daemon sessions exist; the active one is pre-selected.
- The picker replaces the transcript body (it is not a side panel).
- Rendered at 100-col below; an 80-col frame and the empty-picker state follow.

## 2. Mockups

### 2a. Picker (100 x 24)

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
──────────────────────────────────────────────────────────────────────────────────────────────
 sessions · ↑↓ choose · Enter switch · Esc close

    session                   status          workflow       updated    workdir
 ▸  daemon-80958c73c2e04baa   running         cross-review   09:22:41   ~/projects/agent_collab
    daemon-1f7c02aa9b3d4e18   awaiting_input  solo-codex     09:15:03   ~/projects/agent_collab
    daemon-6b9911c4e0a24f7d   done            compare        08:47:58   ~/projects/example_project
    daemon-3c02d7f5a1884b6e   failed          solo-claude    08:31:12   ~/projects/agent_collab
──────────────────────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                                          picking
 4 sessions                                             ↑↓ choose · Enter switch · Esc close
```

`status` shows the raw session status (`running`, `awaiting_input`, `done`,
`failed`) — the same values `format_session_picker_lines` renders today.

### 2b. Picker (80 x 24) — narrow columns

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────────────
 sessions · ↑↓ choose · Enter switch · Esc close

    session                   status          workflow       updated
 ▸  daemon-80958c73c2e04baa   running         cross-review   09:22
    daemon-1f7c02aa9b3d4e18   awaiting_input  solo-codex     09:15
    daemon-6b9911c4e0a24f7d   done            compare        08:47
    daemon-3c02d7f5a1884b6e   failed          solo-claude    08:31
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                          picking
 4 sessions                              ↑↓ choose · Enter switch · Esc close
```

At 80 cols the `workdir` column drops first, then the timestamp shortens to
`HH:MM`; session id, status, and workflow are kept.

### 2c. Empty picker

```
────────────────────────────────────────────────────────────────────────────
 sessions · Esc close

    no daemon sessions found — /new to start one
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                          picking
 0 sessions                                            /new start · Esc close
```

Faithful to `format_session_picker_lines`' empty branch (`no daemon sessions
found`), restyled.

## 3. Color-token intent

- Title row (`sessions · …`): `dim`.
- Column header: `dim`, `muted` weight.
- Rows: session id in `text`; status in a calmed hue — `running` `accent`,
  `awaiting_input` `muted`, `done` `dim`, `failed` `error` red; workflow and
  workdir in `muted`.
- Selected row (`▸`): `raised` background band (same fill as the palette
  selection), id in `text`.
- Rail: inert while picking; mode chip `picking` in `dim`.
- Status/hint line: count left (`4 sessions`), hint right (`dim`).

## 4. Keyboard behavior

- `↑ ↓` (and `j` / `k`) move the selection; `PgUp` / `PgDn` jump by a page.
- `Enter` activates the selected session (`activate_session`); `Esc` closes the
  picker and returns to the prior transcript.
- Status/hint line: message `4 sessions`; hint (precedence rule: session picker
  open) `↑↓ choose · Enter switch · Esc close`.

## 5. Removed / simplified

- **Target delta (strings/styling only):** lowercase title/columns (today
  `sessions`, `enter switches  esc closes`, uppercase `SESSION_ID`/`STATUS`/…),
  a single `raised` selection band instead of the reversed-bold `selection`
  style, marker `▸` (today `>`), and a defined narrow-column drop order. Status
  values themselves stay the raw strings.
- **Faithful:** sorting, pre-selection, activation, and the empty-state string
  are unchanged.
