# Sample: main session

Stage 1a mockup — illustrative, not approved.

## 1. Data assumptions

- Terminal: 80 x 24, following (tailing the transcript).
- Session `daemon-80958c73c2e04baa`, status `running`.
- Workflow `cross-review`, sequence `claude -> codex -> claude`.
- Agents: `claude` (type `claude`, model `opus-4.8`, backend `cli` = default),
  `codex` (type `codex`, model `gpt-5`, backend `cli` = default). Both on their
  default backend, so no backend is shown inline.
- Interactive: true. Active overlay: none.
- Row map (24 rows): 0 context, 1 session info, 2 hairline, 3-20 body,
  21 hairline, 22 input rail, 23 status/hint. Body height = height - 6
  (one row less than today, spent on the session-info line; the bottom is
  two-row chrome).

## 2. Mockup (80 x 24, following)

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────────────
 claude   Looking at _poll_loop — the epoch guard in _drain_events already
          drops stale batches, so the terminal-status early return is safe.
 codex    Agreed. wait_events re-reads the session on empty batches, which is
          the intended keepalive, not a bug.
 referee  note: ship it once tests pass                                  9:22
 claude   ◆ thinking…



────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                     referee note
 sent note                              ⠹ running · Enter send · / cmds · q
```

The right of the status/hint line is `⠹ running` — the braille-orbit spinner
(ASCII dot-pulse `... running` fallback on non-UTF-8 terminals). Activity is
shown here only, not in the top context line. No token/usage figure is shown:
the session does not track one today.

## 3. Color-token intent

- Context line (row 0): `dim`. Branch + project path only; no activity here.
- Session-info line (row 1): `muted`, with each agent name in its **provider
  brand hue** (`claude` Anthropic coral `#D97757`, `codex` OpenAI green
  `#10A37F`; unknown providers fall back to the accent teal — see the parent
  README's Provider brand colors); task and workflow in `muted`. Truncates
  right-to-left per the parent README priority.
- Hairlines (rows 2, 21): `hairline` separator (dim, single rule).
- Body source gutter: label in the source's provider brand hue, weight 600 —
  `claude` coral, `codex` green, `referee` `muted`; the `◆ thinking…` metadata
  in `dim`. Timestamp (`9:22`) in `dim`, right-aligned.
- Input rail (row 22): prompt `[referee]` in `muted`; typed text in `text`; the
  mode chip `referee note` right-aligned in `accent`.
- Status/hint line (row 23): transient message left — `accent` for success
  (`sent note`), `error` red for failures; spinner + hint right in `dim`
  (spinner frame may take `accent`).

## 4. Keyboard behavior

- `Enter` sends the rail contents as a referee note.
- `#agent …` switches the rail to a directed turn (mode chip becomes e.g.
  `-> codex`) — see `directed-input.md`.
- `/` opens the command palette (see `slash-command-palette.md`).
- `↑ ↓ / PgUp PgDn` scroll; `End` re-follows; `q` or `Ctrl-C` quits.
- Status/hint line: message `sent note` (transient, clears on next
  keystroke/turn); hint (precedence rule: default / following)
  `⠹ running · Enter send · / cmds · q`.

## 5. Removed / simplified

- **Target delta:** splits today's dense `_render_header` (one space-joined
  `agent-collab id status workflow workdir [tags]`) into a quiet context line +
  a legible session-info line that adds per-agent model.
- **Target delta:** collapses today's `_render_status_line` into one status/hint
  line (message left, hint + activity right); activity no longer duplicated in
  the header.
- **Target delta:** source gutter uses lowercase calmed labels; today
  `format_transcript_event` emits uppercase 7-wide labels (`CLAUDE `, `CODEX  `).
- **Target delta:** heavy full-width `-` rules (`chrome` dim) become sparse
  hairlines.
- **Approved interaction change:** braille-orbit spinner replaces `- \ | /`.
- Otherwise faithful: commands, event model, poller, and input dispatch are
  untouched.
