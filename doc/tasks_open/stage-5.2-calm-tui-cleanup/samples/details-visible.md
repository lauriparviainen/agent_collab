# Sample: details visible

Stage 1a mockup — illustrative, not approved. Covers `/details` as the **wide
side panel** (width >= 100) and the **narrow overlay fallback** (< 100), so the
toggle is never a no-op. `narrow-terminal.md` 2b shows the same narrow overlay in
its own context.

## 1. Data assumptions

- Terminal: 100 x 24, running `cross-review` session, `details_visible = true`.
- At width 100 `_details_width` = `min(48, max(32, 100//3))` = 33, so the
  transcript keeps `100 - 33 - 1 = 66` columns and the panel takes the right 33.
- Panel content is `format_session_details` wrapped to `details_width - 2`.
- The header carries a `[details]` tag (today `_render_header` appends it).

## 2. Mockup (100 x 24, side panel)

```
 main  ~/projects/agent_collab                                                          [details]
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
──────────────────────────────────────────────────────────────────────────────────────────────────
 claude   Looking at _poll_loop — the epoch guard in     │ session_id: daemon-80958c73c2e04baa
          _drain_events already drops stale batches.      │ status: running
 codex    Agreed. wait_events re-reads on empty           │ workflow: cross-review
          batches — the intended keepalive.               │ sequence: claude -> codex -> claude
 referee  note: ship it once tests pass           9:22    │ workdir: ~/projects/agent_collab
 claude   ◆ thinking…                                     │ max_turns: 3
                                                          │ timeout: 900
                                                          │ interactive: true
                                                          │
                                                          │ agent claude: type=claude
                                                          │   model=opus-4.8
                                                          │ agent codex: type=codex model=gpt-5
──────────────────────────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                                          referee note
 opened daemon-80958c73c2e04baa                    ⠹ running · Enter send · / cmds · Esc close
```

The panel shows the `format_session_details` block (`key: value` lines,
`agent <id>: type=… model=…`). The message slot keeps the **prior** message
(here `opened …`): `/details` does not set its own message today — the panel
appearing is the feedback. Wrapping and overflow notes below.

### Wrapping (schematic)

The panel content above is drawn semantically for readability, but today
`_wrap_display_text` is **fixed-width character chunking** with a two-space
continuation indent — at width ~31 a line like `agent codex: type=codex
model=gpt-5` breaks mid-token, not at the field boundary. Treating that as a
**target delta** (Stage 2 could wrap on field boundaries) is the intent;
otherwise the real char-chunked wrap should be drawn.

### Overflow (long details clip)

The side panel does not scroll: today `detail_lines[:body_height]` clips, so a
long block (many agents, long `command_preview`) is cut at the bottom with no
indication. Target options for approval: (a) keep clipping but add a `…` marker
on the last visible row, or (b) let `/details` open the scrollable overlay even
at wide widths. The narrow overlay (below / `narrow-terminal.md` 2b) scrolls.

### Narrow overlay fallback (80 x 24, width < 100)

Below width 100 `_details_width` returns 0, so the side panel is impossible;
`/details` opens a scrollable full-body overlay instead of today's no-op:

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────────────
 details · ↑↓ scroll · Esc close
 session_id: daemon-80958c73c2e04baa
 status: running
 workflow: cross-review
 sequence: claude -> codex -> claude
 workdir: ~/projects/agent_collab
 agent claude: type=claude model=opus-4.8
 agent codex: type=codex model=gpt-5
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                             details
 opened daemon-80958c73c2e04baa               ⠹ running · ↑↓ scroll · Esc close
```

Same content as the wide panel, but scrollable (so long blocks are reachable).
**Approved interaction change** (overlay fallback + `Esc` close).

## 3. Color-token intent

- Header `[details]` tag: `dim`.
- Panel separator column (`│`): `hairline`.
- Panel labels (`session_id:`, `status:`, …): `dim`; values in `muted`; agent
  ids (`claude`, `codex`) in `accent`.
- Transcript half: identical tokens to `main-session.md`.
- Status/hint line: the message keeps the prior action (`/details` sets none);
  spinner + hint right in `dim`.

## 4. Keyboard behavior

- `/details` toggles the panel. **Faithful:** it does not set a message today, so
  the slot keeps the prior one (the panel appearing is the feedback).
- **Approved interaction change:** `Esc` also closes the panel (today `Esc`
  does not; `/details` is the only toggle).
- Wide panel: scroll keys scroll the transcript; the panel itself does not scroll
  (see overflow). Narrow overlay: scroll keys scroll the overlay.
- Status/hint line: message is the prior action; hint (precedence rule:
  `/details` visible) `⠹ running · Enter send · / cmds · Esc close` (wide) or
  `⠹ running · ↑↓ scroll · Esc close` (narrow overlay).

## 5. Removed / simplified

- **Target delta:** the panel separator is a `│` hairline (today an ASCII `|`
  in `chrome` dim), labels/values get calmed hues, and the `[details]` tag reads
  quieter. Panel *content* stays the `format_session_details` output.
- **Approved interaction change:** `Esc` closes the panel.
- **Open question (flagged, not decided):** whether to fix the wide-panel
  clip-with-no-scroll — surfaced above for approval.
