# Stage 4.5: TUI watch

## Purpose

Add a fixed-window terminal UI for watching a daemon-owned collaboration session.

The plain `watch` command stays as the reliable transcript mode for pipes, logs, SSH, and simple terminals. The TUI is an additional view for interactive use.

## User experience

```bash
agent-collab tui SESSION_ID
```

Optional daemon target:

```bash
agent-collab tui --server-url http://127.0.0.1:8765 SESSION_ID
```

## Layout

Initial layout:

```text
agent-collab  SESSION_ID  STATUS  WORKDIR
------------------------------------------------------------
scrollable transcript pane
...
------------------------------------------------------------
q quit  r refresh  arrows/page keys scroll  end follow
```

Transcript lines should use the same speaker labels as plain watch:

```text
HUMAN
REFEREE
CLAUDE
CODEX
TOOL
ERROR
```

## Implementation approach

Start with stdlib `curses`:

- no new dependency,
- works on Linux terminals,
- enough for fixed header/footer and a scrollable transcript pane.

If this becomes too much manual terminal work, revisit `Textual` as a later dependency-backed implementation.

## Behavior

- Connect to the daemon through the existing client.
- Read current events from cursor 0.
- Long-poll for new events with `wait_events`.
- Auto-follow while at the bottom.
- Allow manual scrolling without snapping back until the user returns to the bottom.
- Show session status in the header.
- Exit cleanly on `q` or Ctrl-C.

## Out of scope

- Editing prompts from the TUI.
- Starting new sessions from the TUI.
- Multi-session dashboard.
- Mouse support.

## Tests

Most curses behavior will need light unit tests around formatting/state helpers rather than full terminal rendering.

Add tests for:

- transcript line formatting,
- scroll position update rules,
- event cursor handling,
- clean return from the TUI loop when the session is terminal.

## Acceptance criteria

- `agent-collab tui SESSION_ID` opens a fixed-window view.
- Header and footer remain fixed while transcript content scrolls.
- It can watch a mock daemon session to completion.
- Existing `watch` command continues to work unchanged.
