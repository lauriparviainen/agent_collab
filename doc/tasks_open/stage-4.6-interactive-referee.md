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

Response should return the accepted event or session state plus cursor metadata
so clients can immediately render the queued message.

Add client support:

```python
AgentCollabClient.post_message(session_id, text, source="referee", target=None)
```

Add MCP support for symmetry:

```text
agent_collab_post_message
```

MCP agents should use the same validation and event path as the TUI.

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

Open question: make `awaiting_input` opt-in through a start option such as
`interactive: true`, or make it the default for sessions started from the TUI.
The safer first implementation is opt-in.

## Referee Loop Changes

Add per-session input queue state owned by `SessionManager`.

The referee needs a queue-aware mode:

1. Before each planned turn, drain queued note messages and append them to the
   transcript/log.
2. If a queued item has `target`, validate that target against enabled agents
   and run exactly one turn for that agent.
3. After the planned sequence, enter an awaiting loop that waits for queued
   input, stop, idle timeout, or cancellation.
4. Do not run overlapping agent turns for one session.
5. Every accepted note/question must be appended to JSONL, Markdown, in-memory
   events, and watcher notifications before any directed agent answer starts.

Update event waiting and status assumptions at the same time. Every
`status == RUNNING` assumption must be audited for `awaiting_input`; at minimum
this includes the `SessionManager.wait_events` long-poll guard/predicate and
the daemon completion transition. `awaiting_input` must be treated as a live
wait status, otherwise TUI follow mode would busy-poll while waiting for input
and directed answers would not stream through the normal bounded long-poll path.

Directed-turn prompts should keep the existing guardrails and current task, but
use a role string that reflects the directed question, for example:

```text
Directed agent: answer the referee's latest question using the current
transcript. Keep the response scoped to that question.
```

## Validation

- Reject messages for terminal, interrupted, or restored sessions.
- Reject empty text.
- Reject unknown `target`.
- Reject target agents that are disabled or unavailable in the session config.
- Reject ambiguous target type aliases when multiple enabled agents share that
  type; require the exact agent id.
- Reject `target` when another directed/planned turn is already active unless
  it is queued explicitly.
- Keep the prompt-free command preview rule unchanged.

## TUI Integration

Once this stage lands, Stage 4.5 reserved input becomes active:

- plain text calls `post_message(..., target=None)`,
- `#AGENT text` and `/ask AGENT text` call `post_message(..., target=AGENT)`,
- the UI shows accepted messages immediately,
- the UI resumes follow mode after send,
- terminal sessions and sessions without a live runner keep input disabled with
  a clear inline message.

## Tests

Add focused headless tests before TUI wiring:

- `post_message` appends a referee event and wakes `wait_events`,
- `wait_events` long-polls while a session is `awaiting_input`,
- a queued note before the next planned turn appears in that agent prompt,
- `#agent`/targeted message runs exactly one turn for that agent,
- unknown and ambiguous targets are rejected with a clear field-path or message,
- `awaiting_input` is non-terminal and remains listed/status-readable,
- `/stop` from `awaiting_input` transitions to `stopped`,
- idle timeout from `awaiting_input` appends a referee status event and
  transitions to `done`,
- daemon restart marks `awaiting_input` sessions `interrupted`,
- sessions restored from the index without a live runner cannot accept new
  messages,
- client and MCP wrappers map to the same manager method.

Mock runners should capture prompts so tests can prove note visibility without
calling real Claude or Codex.

## Acceptance Criteria

- TUI plain text creates a `referee` transcript event visible to watchers.
- That referee note is included in the next agent turn prompt.
- `#AGENT message` or `/ask AGENT message` runs one directed turn for the named
  agent and streams the answer into the same session transcript.
- Completed planned workflows can stay in `awaiting_input` when interactive mode
  is enabled.
- `stop` cleanly terminates an `awaiting_input` session.
- Idle timeout cleanly terminates an `awaiting_input` session as `done` after a
  visible referee status event.
- Terminal statuses, including `interrupted`, reject new input.
- Sessions restored from the index without a live runner reject new input.
- MCP and CLI/TUI use the same daemon message API.

## Risks

- This changes the referee from a closed loop to a queue-driven session owner.
- Mid-turn input cannot be visible until the next prompt; the UI must say
  "queued" rather than imply interruption.
- The current prompt only includes the last 12 transcript events, so important
  referee notes can age out. This may need a later prompt-context policy.
- `awaiting_input` sessions may hold resources and can trigger paid agent turns
  later. Add an idle timeout and make stop obvious.
- Continuing restored sessions requires runner rehydration and is out of scope.
