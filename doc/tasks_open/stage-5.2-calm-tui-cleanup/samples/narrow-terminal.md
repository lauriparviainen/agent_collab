# Sample: narrow terminal + fallbacks

Stage 1a mockup — illustrative, not approved. Covers the ~48-col layout, the
narrow `/details` overlay (side panel is suppressed below width 100), and the
sub-minimum "too small" frame.

## 1. Data assumptions

- Same running `cross-review` session as `main-session.md`.
- Three widths: 48 x 20 (narrow), 48 x 20 with `/details` open, and a
  sub-minimum frame (width < 20 or height < 5, the `_render` guard).
- `_details_width` returns 0 below width 100, so `/details` can never be a side
  panel here — the target renders it as a full-body overlay instead of the
  current no-op.

## 2. Mockups

### 2a. Narrow main view (48 x 20, following)

```
 main ~/agent_collab
 review the poller race · claude:opus-4.8
────────────────────────────────────────────────
 claude  epoch guard drops stale batches, so
         the early return is safe.
 codex   wait_events keepalive, not a bug.
 referee note: ship it once tests pass   9:22
 claude  ◆ thinking…
────────────────────────────────────────────────
 [referee] ▏                        referee note
 sent note              ⠹ running · / cmds · q
```

Truncation follows the parent README priority: **workflow drops first**
(`cross-review` gone), then the secondary agent (`codex:gpt-5` gone); the lead
`claude:opus-4.8` and the task survive. No invented abbreviations.

### 2b. Narrow `/details` overlay (48 x 20)

```
 main ~/agent_collab
 review the poller race · claude:opus-4.8
────────────────────────────────────────────────
 details · ↑↓ scroll · Esc close
 session_id: daemon-80958c73c2e04baa
 status: running
 workflow: cross-review
 sequence: claude -> codex -> claude
 workdir: ~/projects/agent_collab
 agent claude: type=claude model=opus-4.8
 agent codex: type=codex model=gpt-5
────────────────────────────────────────────────
 [referee] ▏                             details
 details                    ⠹ running · Esc close
```

The overlay content is the real `format_session_details` block (colon labels,
`agent <id>: type=… model=…`) — **faithful content**, shown as a scrollable
overlay because the width-100 side panel is unavailable here. `Esc` closes it
(**approved interaction change**).

### 2c. Sub-minimum frame (width < 20 or height < 5)

```
 terminal too small
```

Faithful to the `_render` early guard: only this line is drawn until the
terminal grows past 20 x 5. (Note: the guard stops *rendering*, not input —
`_handle_key` still runs, so typing/`q`/`Ctrl-C` are still processed.)

## 3. Color-token intent

- Same token roles as `main-session.md`, only the widths change.
- Details overlay: labels (`session_id:`, `status:`, …) in `dim`; values in
  `muted`; agent ids in `accent`.
- `terminal too small`: `dim`, no accent — deliberately unstyled.

## 4. Keyboard behavior

- Narrow main view behaves like the wide one; only wrapping/truncation differ.
- `/details` overlay: `↑ ↓ / PgUp PgDn` scroll, `Esc` closes; status/hint line
  message `details`, hint (precedence rule: `/details` overlay)
  `⠹ running · Esc close`.
- Sub-minimum frame: keys are still processed (input buffer, `q`, `Ctrl-C`);
  only the draw is suppressed. The real UI returns as soon as the size passes.

## 5. Removed / simplified

- **Approved interaction change:** below width 100, `/details` falls back to a
  scrollable overlay instead of today's silent no-op (`_details_width` returns
  0); `Esc` closes it.
- **Target delta:** explicit truncation order (drop workflow, then secondary
  agents, keep lead `name:model` and task) so narrow widths degrade predictably.
- **Faithful:** the `terminal too small` guard and the details block content
  are unchanged (only the details *placement* becomes an overlay).
