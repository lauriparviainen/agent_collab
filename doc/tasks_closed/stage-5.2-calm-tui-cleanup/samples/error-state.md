# Sample: error / read-only states

Stage 1a mockup — illustrative, not approved. Faithful to the read-only guards
in `activate_session` / `_post_referee_message` / `_dispatch` and to
`format_activity_indicator`'s `read-only <status>` string.

Note on rail state: `_submit_input` clears the rail *before* dispatch
([tui.py](../../../../agent_collab/tui.py) `_submit_input`), so every rejection
below shows an **empty rail** with the guard text in the message slot — the
submitted text is already gone.

## 1. Data assumptions

- Terminal: 80 x 24.
- States shown: a `failed` terminal session (read-only) with a rejected note,
  `/stop` on an already-terminal session, a running-but-non-interactive session,
  and a client/poll error.

## 2. Mockups

### 2a. Failed session (terminal, read-only) + rejected note

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────────────
 codex    Patch applied; running the suite.
 error    codex exited with code 1: pytest failed (3 failures)
 referee  status: session failed
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                         read-only
 session is read-only (failed)                       read-only failed · q quit
```

The user typed a note and pressed `Enter`; the rail cleared, then
`_post_referee_message` rejected it with `session is read-only (failed)` (the
session is terminal). The `error` source line is the failure event.

### 2b. `/stop` on an already-terminal session

```
 [referee] ▏                                                         read-only
 session already failed                              read-only failed · q quit
```

`/stop` cleared the rail, then `_dispatch` returned `session already failed`.

### 2c. Running but non-interactive (read-only input)

```
 main  ~/projects/agent_collab
 compare the backends · claude:opus-4.8 · codex:gpt-5 · compare
────────────────────────────────────────────────────────────────────────────
 codex    Comparing the cli and sdk backends…
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                         read-only
 referee input is available only for live interactive sessions   ⠹ running · q
```

The session is `running` (spinner shows) but was not started `interactive`, so a
submitted note is rejected with `READ_ONLY_INPUT_MESSAGE`. Mode chip `read-only`.

### 2d. Client / poll error (session still running)

```
────────────────────────────────────────────────────────────────────────────
 referee  note: ship it once tests pass                                  9:22
 claude   ◆ thinking…
────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                     referee note
 cannot reach daemon: [Errno 111] Connection refused    ⠹ running · Enter send · q
```

On a poll/client exception the poller stops and the slot shows `str(exc)`, but
the **session status is unchanged** — it is still `running`, so the activity
keeps its spinner and the hint stays the default form (not `read-only`). Exact
error text is the client's.

## 3. Color-token intent

- `error` source line: `error` red (retained hue — errors keep their color even
  as other sources calm down).
- `read-only <status>` activity and mode chip: `dim`.
- Message slot: `session already <status>` in `muted`; hard errors
  (`session is read-only`, `READ_ONLY_INPUT_MESSAGE`, connection failure) in
  `error` red.
- Session-info line: unchanged; for a `failed` session the status reads from the
  read-only activity/chip, and `/details` shows the `error:` field.

## 4. Keyboard behavior

- On terminal sessions the rail accepts text, but `Enter` clears it and the
  guard message appears; `q` / `Ctrl-C` quit; `/sessions`, `/new`, `/refresh`
  still work.
- Status/hint line: message is the guard/error string; hint is
  `read-only <status> · q quit` for a terminal session (precedence: read-only /
  terminal). For 2c and 2d the session is not terminal, so the hint keeps the
  running/default form even though 2c's input stays rejected.

## 5. Removed / simplified

- **Faithful:** all four guard strings (`session is read-only (<status>)`,
  `READ_ONLY_INPUT_MESSAGE`, `session already <status>`, `str(exc)`), the
  rail-clears-on-submit behavior, and the `read-only <status>` activity string
  match the code.
- **Target delta:** errors are routed to the dedicated message slot with `error`
  red instead of sharing the old status line; the `error` source hue is one of
  the few colors deliberately kept.
