# Sample: first launch (zero sessions)

Stage 1a mockup — illustrative, not approved.

## 1. Data assumptions

- Terminal: 80 x 24.
- No daemon sessions exist. `run_tui` finds none, so there is **no active
  session** (the real behavior — not a picker; see the parent README's
  First-screen decision). `run_tui` seeds the message `no daemon sessions
  found; use /new`.
- Active overlay: none.

## 2. Mockup (80 x 24)

```
 agent-collab
 no active session
────────────────────────────────────────────────────────────────────────────

        No daemon sessions yet.

        /new       start a supervised session
        /sessions  browse the daemon (empty right now)
        /help      commands and keys



────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                       no session
 no daemon sessions found; use /new              /new start · /help · q quit
```

## 3. Color-token intent

- Context line: `dim`, just `agent-collab` (no activity — there is no session).
- Session-info line: single `dim` `no active session` placeholder.
- Body: a calm empty state. Command names (`/new`, `/sessions`, `/help`) in
  `accent`; their descriptions in `muted`. No source gutter.
- Input rail: prompt `[referee]` in `dim` (disabled tone); mode chip
  `no session` in `dim` (`format_activity_indicator(None)` returns
  `no session`).
- Status/hint line: message `no daemon sessions found; use /new` in `muted`
  (the exact real startup string); hint right in `dim`.

## 4. Keyboard behavior

- `/new` opens the new-session wizard (see `new-session-flow.md`); `/sessions`
  opens the picker (its own empty state); `/help` opens the full command
  overlay — these commands work with no active session.
- Plain text and `#agent` input are rejected with `no active session` in the
  message slot (matches `_post_referee_message`'s guard).
- Status/hint line: message `no daemon sessions found; use /new`; hint
  (precedence rule: no active session) `/new start · /help · q quit`.

## 5. Removed / simplified

- **Target delta:** replaces today's bare startup message (the same string
  tucked into the status line) with a purposeful empty-state body naming the
  three ways forward. The message-slot string itself is kept **faithful**
  (`no daemon sessions found; use /new`).
- Behavior is otherwise **faithful**: no session is auto-selected and no picker
  is force-opened; commands vs plain-text/`#agent` rejection match the code.
