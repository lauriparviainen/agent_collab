# Stage 4.5: Interactive TUI shell

## Purpose

Add an interactive terminal UI for observing and managing daemon-owned
collaboration sessions.

The plain `watch` command stays as the reliable transcript mode for pipes,
logs, SSH, and simple terminals. The TUI is the richer human console:

- open the latest session by default,
- switch between sessions without leaving the UI,
- start a new session from inside the UI,
- inspect effective session details,
- prepare the UI surface for referee input and directed agent questions.

The actual live referee input channel is split into
[Stage 4.6: Interactive referee input](stage-4.6-interactive-referee.md)
because it requires daemon and referee-loop changes. This stage should still
ship a useful TUI on top of the existing daemon API.

## User Experience

Open the most recently updated daemon session:

```bash
agent-collab tui
```

Open a specific session:

```bash
agent-collab tui SESSION_ID
```

Target a non-default daemon:

```bash
agent-collab tui --server-url http://127.0.0.1:8765 SESSION_ID
```

The TUI should feel closer to Codex CLI and Claude Code CLI than to a passive
log tail. It opens directly into the active transcript, keeps an input line at
the bottom, and treats slash commands as the main control surface.

## Layout

Initial layout:

```text
agent-collab  SESSION_ID  STATUS  WORKFLOW  WORKDIR           [details]
----------------------------------------------------------------------------
scrollable transcript pane                     | optional details pane
HUMAN    ...
REFEREE  ...
CLAUDE   ...
CODEX    ...
TOOL     ...
ERROR    ...
----------------------------------------------------------------------------
[referee] type /help, /new, /session, /details                 following
q/Ctrl-C quit  arrows/page keys scroll  End follow
```

The details pane is toggled, not always visible. Narrow terminals should keep
the transcript and input line usable by hiding details.

Transcript lines use the same speaker labels as plain watch:

```text
HUMAN
REFEREE
CLAUDE
CODEX
TOOL
ERROR
```

## Command Grammar

Commands are entered in the bottom input line.

```text
/help                    show key bindings and commands
/sessions                open a session picker from daemon list_sessions
/session SESSION_ID      switch active session directly
/new                     start a new-session wizard
/details                 toggle the session details pane
/follow                  jump to tail and resume auto-follow
/refresh                 re-read the active session from cursor 0
/stop                    stop the active daemon session
/quit                    exit the TUI

#AGENT message           handled by Stage 4.6 directed agent questions
plain text               handled by Stage 4.6 referee notes
```

Stage 4.5 parsed plain text and `#AGENT` input while Stage 4.6 was still
pending. Before Stage 4.6 landed, those paths showed a clear inline message
such as:

```text
referee input requires stage 4.6; this session is currently read-only
```

Agent names must come from the active session settings or
`agent-collab describe_options` data, not from hardcoded `claude` and `codex`
assumptions. `#AGENT` should first match an enabled agent id. If there is no id
match, it may match an agent type only when exactly one enabled agent of that
type exists in the active session. Ambiguous type matches should be rejected
with a message that lists the valid agent ids.

## Session Details

The details pane should use the existing `status` response and the persisted
`settings` block:

- `session_id`,
- `status`,
- `workflow`,
- workflow sequence,
- `workdir`,
- `created_at` / `updated_at` / `ended_at`,
- `max_turns`, `timeout`, `mock`, `dry_run`,
- `jsonl_path` and `markdown_path`,
- per-agent type, model, thinking level, permission or sandbox settings,
- prompt-free `command_preview`.

Missing settings should be omitted rather than invented.

## Existing API Coverage

This stage should not need daemon behavior changes.

Use existing client calls:

- open latest: `list_sessions`, choose the most recently updated session,
- switch session: `get_session`, `read_events`,
- follow transcript: `wait_events` with cursors and bounded timeouts,
- details pane: `get_session`,
- new-session wizard: `describe_options` and `start_session`,
- stop command: `stop_session`.

The long-poll call must not block keyboard handling. Use a worker thread or an
async task that feeds event batches into a small UI queue. The render loop
drains that queue and updates transcript state.

## Implementation Approach

Start with stdlib `curses`:

- no new dependency,
- works on Linux terminals,
- enough for fixed header/footer, scrollback, input line, and an optional
  details pane.

Keep curses-specific code thin. Put the core behavior in pure helpers:

- transcript formatting,
- scroll and auto-follow state,
- command parsing,
- session picker state,
- details formatting,
- poller state and cursor handling.

If input editing, overlays, resizing, and details panes become too much manual
terminal work, revisit `Textual` as a later dependency-backed implementation.

## Behavior

- Open the latest daemon session when no `SESSION_ID` is passed.
- Read existing events from cursor 0 when a session becomes active.
- Long-poll for new events using the last returned cursor.
- Auto-follow while at the bottom.
- Manual scrolling disables auto-follow until the user returns to the bottom or
  runs `/follow`.
- Header status should refresh periodically or after every event batch.
- `/sessions` should show a compact picker with status, workflow, updated time,
  and workdir.
- `/new` should prompt for task first, then workflow and workdir. Advanced
  typed agent options can be deferred unless the wizard can keep them simple
  and validated through `describe_options`.
- Terminal statuses, including `interrupted`, should open read-only.
- Sessions restored from the on-disk index without a live runner should open
  read-only even if their persisted metadata is still viewable.
- Exit cleanly on `/quit`, `q`, or Ctrl-C.

## Out of Scope

- Making referee notes visible to future agent turns.
- Running directed `#AGENT` questions.
- Continuing a completed session.
- Replaying or editing prompts.
- Resuming restored post-restart sessions.
- Mouse support.

Those first three belong to Stage 4.6.

## Tests

Most curses behavior should be tested through pure helpers rather than full
terminal rendering.

Add tests for:

- transcript line formatting,
- details pane formatting from a session `settings` block,
- command parser behavior for slash commands, plain text, and `#AGENT`,
- scroll position update rules,
- auto-follow behavior,
- event cursor handling,
- session switch resetting cursor and transcript state,
- new-session wizard payload construction from selected workflow/workdir/task,
- clean return from the TUI loop when the session is terminal.

## Acceptance Criteria

- `agent-collab tui` opens the most recently updated daemon session.
- `agent-collab tui SESSION_ID` opens a specific daemon session.
- Header and footer remain fixed while transcript content scrolls.
- The UI can watch a mock daemon session to completion.
- `/sessions` and `/session SESSION_ID` switch active sessions without
  restarting the TUI.
- `/details` shows the effective workflow, workdir, logs, and per-agent
  settings from the daemon response.
- `/new` starts a daemon-owned session and switches to it.
- Existing `watch` command behavior remains unchanged.
- Referee input commands were visibly reserved or disabled before Stage 4.6
  made them active.
