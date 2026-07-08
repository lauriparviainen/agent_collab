# Stage 4.6: Interactive referee input

## Purpose

Let a human participate in a live daemon session from the TUI by adding referee
notes and asking a specific agent a directed question.

Stage 4.5 provides the TUI shell. This stage adds the daemon, client, MCP, and
referee-loop capabilities that make the TUI input line meaningful.

## Current Constraint

The current `Referee.run(task)` is a closed batch loop:

1. create an initial `human` task event,
2. run the configured workflow sequence up to `max_turns`,
3. mark the session `done`.

There is no input queue, no API for adding a message to a running session, and
no way to run an additional turn after the planned sequence finishes.

The existing prompt path is still useful: `_prompt_for` renders recent
transcript events as `SOURCE: text`, and `human` plus `referee` are already
valid event sources. If a referee message is appended before an agent turn, the
next prompt can already include it.

The current logging boundary matters: `SessionLogger` is opened and closed
inside `Referee.run()`, while `SessionManager._record_event` only appends to
in-memory events and wakes watchers. Therefore an interactive session must keep
`Referee.run()` on the stack while awaiting input, so referee notes can still be
written through the same JSONL, Markdown, in-memory, and watcher path.

## User Experience

From the TUI input line:

```text
plain text               append a referee note to the active session
#AGENT message           ask one directed question
/ask AGENT message       discoverable equivalent to #AGENT
```

Plain referee notes are visible in the transcript and included in future agent
prompt context. Directed questions append the human/referee question and run one
turn of the named agent against the current transcript.

If a message is submitted while another agent turn is running, the UI should
show it as queued. The running prompt cannot see mid-turn input; queued input is
visible at the next turn boundary.

Queueing is automatic for accepted input. The first implementation should not
add a separate "queue explicitly" API flag: if an interactive session is live
and a turn is already active, the daemon accepts the message, appends the
message event immediately, and processes it at the next safe turn boundary.

`AGENT` resolution:

- exact enabled agent id wins,
- if there is no id match, an agent type can match only when exactly one
  enabled agent of that type exists in the active session,
- ambiguous type matches are rejected and the error lists valid agent ids.

## API Shape

Add an HTTP endpoint:

```text
POST /sessions/{session_id}/messages
```

Request:

```json
{
  "source": "referee",
  "text": "Please compare the two proposals.",
  "target": "claude"
}
```

Fields:

- `source`: `referee` by default; optionally `human` if a future UI wants that
  distinction,
- `text`: required non-empty string,
- `target`: optional agent id. Missing target means note-only. Present target
  means run one directed turn of that agent.

Response should use the same `EventBatch` envelope as `read_events`, with
`session_id`, the new `cursor`, and `events` containing the accepted message
event so clients can immediately render the queued message. The event `raw`
metadata should include enough structured detail for clients and tests, such as
the original `target`, the resolved agent id when present, and whether the item
is queued behind an active turn.

Add client support:

```python
AgentCollabClient.post_message(session_id, text, source="referee", target=None)
```

Add MCP support for symmetry:

```text
agent_collab_post_message
```

MCP agents should use the same validation and event path as the TUI. This is not
just a tool-list addition: wire the tool schema, `TOOL_NAMES`, the `ToolBackend`
protocol, both the session-manager and HTTP-client backends, `handle_tool`
dispatch, and MCP guidance so every surface reaches the same manager method.

## Session Lifecycle

Introduce a non-terminal status:

```text
awaiting_input
```

When the planned workflow sequence finishes, an interactive-capable session
should move to `awaiting_input` instead of `done`. It remains continuable until:

- the user stops it, which sets `stopped`,
- it reaches an idle timeout, which sets `done` after appending a clear
  referee status event,
- the daemon shuts down or restarts, which sets `interrupted` on restore.

After daemon restart, an `awaiting_input` session should follow the same path as
an interrupted running session: the persisted status becomes `interrupted`
unless true runner/session rehydration is implemented. Sessions restored from
the on-disk index have metadata and logs but no live runner (`request is None`),
so the TUI must treat them as read-only.

The idle-timeout path must set that terminal status explicitly. The existing
daemon completion path only transitions `running` to `done`; once a session is
in `awaiting_input`, that guard should not be relied on to finish the session.

Make the first implementation opt-in with a start option:

```json
{
  "interactive": true,
  "interactive_idle_timeout": 600
}
```

`interactive` defaults to `false` for existing CLI, daemon, HTTP, and MCP start
flows so plain `watch` and batch sessions keep their current completion
behavior. The TUI should pass `interactive: true` for sessions it starts through
its `/new` flow. Sessions that were not started as interactive remain
read-only for plain text and `#AGENT`/`/ask` input.

The option must thread through `StartSessionRequest`, `SessionState`
persistence, HTTP `POST /sessions`, `AgentCollabClient.start_session`, MCP
`agent_collab_start`, the TUI new-session payload helper, and the effective
`settings` block. `interactive_idle_timeout` is separate from the existing
per-agent-turn timeout; it controls, in seconds, how long an `awaiting_input`
session remains open with no queued input.

## Referee Loop Changes

Add per-session input queue state owned by `SessionManager`.

The referee needs a queue-aware mode:

1. In interactive mode, `Referee.run()` must not return after the planned
   sequence. It keeps the logger, transcript, runners, and current task alive
   while it waits for manager-owned queued input.
2. Before each planned turn, drain queued note messages and append them to the
   transcript/log.
3. If a queued item has `target`, validate that target against enabled agents
   and run exactly one turn for that agent.
4. After the planned sequence, enter an awaiting loop that waits for queued
   input, stop, idle timeout, or cancellation.
5. Do not run overlapping agent turns for one session.
6. Every accepted note/question must be appended to JSONL, Markdown, in-memory
   events, and watcher notifications before any directed agent answer starts.

`SessionManager` still owns session status. Give the interactive referee path a
small async callback/hook for status transitions so it can set
`awaiting_input` when the planned sequence finishes and set `done` on idle
timeout after appending a visible referee status event. The daemon completion
path should also defensively handle an interactive run returning from
`awaiting_input` without leaving the session non-terminal.

Update event waiting and status assumptions at the same time. Every
`status == RUNNING` assumption must be audited for `awaiting_input`; at minimum
this includes the `SessionManager.wait_events` long-poll guard and predicate,
the daemon completion transition, the daemon `TERMINAL_STATUSES` set, the TUI
`TERMINAL_STATUSES` set, `status_is_terminal`, and `should_start_poller`.
`awaiting_input` must be treated as a live wait status, otherwise TUI follow
mode would busy-poll while waiting for input and directed answers would not
stream through the normal bounded long-poll path.

Directed-turn prompts should keep the existing guardrails and current task, but
they need either a `_directed_prompt_for` helper or a role override in
`_prompt_for`, because the current role is selected only by planned turn index.
The directed prompt should include the just-appended question explicitly instead
of relying only on the last 12 transcript events, for example:

```text
Directed agent: answer the referee's latest question using the current
transcript. Keep the response scoped to that question.

DIRECTED QUESTION:
...
```

In interactive mode, the current end-of-run "final summary" event should move to
the terminal transition. The planned sequence ending is not final when the
session remains in `awaiting_input`.

## Validation

- Reject messages for terminal, interrupted, or restored sessions.
- Reject empty text.
- Reject unknown `target`.
- Reject target agents that are disabled or unavailable in the session config.
- Reject ambiguous target type aliases when multiple enabled agents share that
  type; require the exact agent id.
- Accept and queue messages while another planned or directed turn is active;
  reject only if the session is not live/interactive or the target is invalid.
- Keep the prompt-free command preview rule unchanged.

## TUI Integration

This stage made the Stage 4.5 reserved input active:

- plain text calls `post_message(..., target=None)`,
- `#AGENT text` and `/ask AGENT text` call `post_message(..., target=AGENT)`,
- the UI shows accepted messages immediately,
- the UI resumes follow mode after send,
- terminal sessions and sessions without a live runner keep input disabled with
  a clear inline message.

`/ask` needs to be normalized to the same directed-input shape as `#AGENT`.
That means adding `ask` to the command table and either teaching `parse_input`
to emit `kind="directed"` for `/ask AGENT message` or normalizing it in one
shared dispatch helper before validation and `post_message`.

The input line should also provide slash-command discovery like Codex CLI and
Claude Code CLI. When the user starts typing `/`, show an inline list of
available slash commands with short descriptions. Filter the list as the user
types: `/s` should show only commands starting with `s`, such as `/sessions`
and `/stop`. This completion UI is discoverability only; selecting or pressing
Enter still dispatches through the same command parser and validation path.
Use a deterministic command table with descriptions and a pure helper such as
`filter_slash_commands(prefix)` so completion can be tested without curses.
The current `SLASH_COMMANDS` set can become that table, for example a mapping
keyed by command name, while existing membership checks continue to use command
keys.

Do not add a new public `has_runner` field just for restored sessions. Restored
sessions are already marked terminal (`interrupted`) when they were non-terminal
at daemon restart, so the TUI can keep using terminal status for read-only
behavior. The server-side `request is None` guard still rejects input for any
restored session that lacks a live runner.

## Tests

Add focused headless tests before TUI wiring:

- `post_message` appends a referee event and wakes `wait_events`,
- the returned `EventBatch` contains the accepted event and updated cursor,
- accepted messages are written to JSONL, Markdown, in-memory events, and
  watcher notifications through one path,
- `interactive` and `interactive_idle_timeout` thread through HTTP, client,
  MCP start, session persistence, TUI `/new`, and effective settings,
- `wait_events` long-polls while a session is `awaiting_input`,
- a queued note before the next planned turn appears in that agent prompt,
- `#agent`/targeted message runs exactly one turn for that agent,
- `/ask AGENT message` parses to the same directed-input shape as `#AGENT`,
- unknown and ambiguous targets are rejected with a clear field-path or message,
- `awaiting_input` is non-terminal and remains listed/status-readable,
- `/stop` from `awaiting_input` transitions to `stopped`,
- idle timeout from `awaiting_input` appends a referee status event and
  transitions to `done`,
- daemon restart marks `awaiting_input` sessions `interrupted`,
- sessions restored from the index without a live runner cannot accept new
  messages,
- client and MCP wrappers map to the same manager method,
- slash-command completion lists commands after `/`, filters by prefix such as
  `/s`, and dispatches through the normal parser rather than a separate path,
- the interactive final summary is emitted only when the session actually
  reaches a terminal status, not when it first enters `awaiting_input`.

Current `MockRunner` only summarizes the first prompt line, so tests cannot
prove note visibility through its emitted transcript alone. Add a prompt
capturing test runner/helper, or adjust mock support for tests, before writing
the prompt-visibility tests. If arbitrary non-claude/codex agent ids are tested,
also cover event source labeling rather than relying on the current mock's
claude/codex-only source choice.

## Acceptance Criteria

- TUI plain text creates a `referee` transcript event visible to watchers.
- The message API returns an `EventBatch` containing the accepted event and new
  cursor.
- Accepted referee input is written to JSONL, Markdown, in-memory events, and
  watcher notifications through the same event append path.
- That referee note is included in the next agent turn prompt.
- `#AGENT message` or `/ask AGENT message` runs one directed turn for the named
  agent and streams the answer into the same session transcript.
- `/ask AGENT message` and `#AGENT message` share target resolution and
  validation behavior.
- Completed planned workflows can stay in `awaiting_input` when interactive mode
  is enabled.
- Non-interactive starts keep current batch behavior and finish as `done`.
- `stop` cleanly terminates an `awaiting_input` session.
- Idle timeout cleanly terminates an `awaiting_input` session as `done` after a
  visible referee status event.
- Terminal statuses, including `interrupted`, reject new input.
- Sessions restored from the index without a live runner reject new input.
- MCP and CLI/TUI use the same daemon message API.
- Typing `/` in the TUI shows available slash commands, and typing a prefix
  such as `/s` filters that list to matching commands.

## Risks

- This changes the referee from a closed loop to a queue-driven session owner.
- Mid-turn input cannot be visible until the next prompt; the UI must say
  "queued" rather than imply interruption.
- The logger/status ownership change is the central implementation risk:
  `Referee.run()` must stay alive in interactive mode while `SessionManager`
  remains the source of truth for session status.
- The current prompt only includes the last 12 transcript events, so important
  referee notes can age out. This may need a later prompt-context policy.
- `awaiting_input` sessions may hold resources and can trigger paid agent turns
  later. Add an idle timeout and make stop obvious.
- `interactive` and MCP `post_message` plumbing touch many layers; keep the
  manager method as the single validation and append path.
- Continuing restored sessions requires runner rehydration and is out of scope.
