# Sample: directed input (#agent and /ask)

Stage 1a mockup — illustrative, not approved. Faithful to `parse_input` (the
`#agent message` and `/ask AGENT message` directed forms) and
`_post_referee_message` (posts as `referee` with a `target`, then surfaces
`asked <t>` / `queued for <t>`).

## 1. Data assumptions

- Terminal: 80 x 24, running `cross-review` session (agents `claude`, `codex`).
- Directed input posts `source="referee"`, `target=<agent>`; the response's
  `resolved_target` / `queued` drive the message slot.

## 2. Mockups

### 2a. Argument-entry mode (typing `#codex ` with no message yet)

```
────────────────────────────────────────────────────────────────────────────
 referee  note: ship it once tests pass                                  9:22
 claude   ◆ thinking…
────────────────────────────────────────────────────────────────────────────
 [referee] #codex ▏                                                    -> codex
 message codex directly                       ⠹ running · Enter send · Esc clear
```

**Approved interaction change:** the mode chip shows `-> codex` and the message
slot shows a live hint while the message is still empty. Today the rail gives no
directed-mode signal until `Enter` (and `#codex` with no message returns
`usage: #AGENT message`).

### 2b. Sent — `#codex fix the poller race`

```
────────────────────────────────────────────────────────────────────────────
 claude   ◆ thinking…
 referee  fix the poller race                                            9:24
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                     referee note
 asked codex                                  ⠹ running · Enter send · / cmds · q
```

The note posts as a `referee` line targeted at `codex`; the message slot shows
`asked codex` (from `resolved_target`).

### 2c. Queued (target busy)

If codex is mid-turn, the same send is queued and the slot reads instead:

```
 queued for codex                             ⠹ running · Enter send · / cmds · q
```

Faithful to `_post_referee_message`'s `queued for {resolved}` vs `asked
{resolved}` branch.

### 2d. `/ask` form — `/ask codex compare the two options`

Identical dispatch to `#codex …` (both become a directed turn); the message slot
reads `asked codex`. While typing `/ask codex ` with no message, the rail is in
the same argument-entry mode as 2a (mode chip `-> codex`).

### 2e. Unknown / ambiguous target (after send — rail cleared)

```
 [referee] ▏                                                     referee note
 unknown target 'reviewer'; valid agent ids: claude, codex   ⠹ running · Enter send · q
```

`#reviewer take a look` was submitted; the rail cleared, the daemon rejected the
target, and `_post_referee_message` put `str(exc)` in the slot. The exact daemon
strings are `unknown target 'reviewer'; valid agent ids: claude, codex`
([daemon.py](../../../agent_collab/daemon.py) `_resolve_message_target`) and, for
an ambiguous type (two `claude` agents), `ambiguous agent type 'claude'; valid
agent ids: claude-a, claude-b`. The rail is empty because submit clears it before
dispatch.

## 3. Color-token intent

- Rail mode chip: `-> codex` in `accent` (directed) vs the plain `referee note`
  in `accent` for undirected.
- Message slot: `asked codex` / `queued for codex` in `accent`; target errors
  (`unknown target …`, `ambiguous agent type …`) in `error` red.
- Transcript `referee` line: `muted` (as in `main-session.md`).
- Status/hint line hint: `dim`.

## 4. Keyboard behavior

- `#<agent> <message>` or `/ask <agent> <message>` + `Enter` sends a directed
  turn.
- **Approved interaction change:** while the message is still empty the rail is
  in argument-entry mode (mode chip + usage hint); `Esc` clears back to referee
  mode.
- Status/hint line: message is the result (`asked codex` / `queued for codex` /
  error); hint (precedence rule: default / following) `⠹ running · Enter send ·
  / cmds · q`.

## 5. Removed / simplified

- **Faithful:** the directed dispatch, the `referee`-with-target posting, and the
  `asked` / `queued for` results are unchanged.
- **Approved interaction change:** argument-entry mode (chip + live usage hint,
  `Esc` cancel), shared with the `/ask ` palette state in
  `slash-command-palette.md` 2d.
