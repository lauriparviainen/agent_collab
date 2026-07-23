# Subagent-style delegation and SDK thread continuity

**Status:** Open — Stages 1–5 shipped (`wait_result`; surface shape; continuity
groundwork; `codex_sdk` continuity; `claude_sdk` continuity); Stages 6–7 open.

**Created:** 2026-07-23.

**Issue:** [#47](https://github.com/lauriparviainen/agent_collab/issues/47)

**Branch:** the entire task — all stages — is developed on the single branch
`stage-1-wait-result`. Do not merge to `main` until every stage is complete;
land the whole task in one merge at the end, not per stage. Each stage still
commits (and its cross-model review runs) on this branch as it lands, so CI and
the review loop cover work incrementally, but the branch stays unmerged until
the task closes.

## Post-implementation task — collection-primitive re-evaluation

After all SDK continuity stages (4–7) have landed, re-evaluate whether
`wait_result` is actually the collection primitive the guidance should
recommend, or whether `wait_events` should stay preferred. This is a guidance
decision, not a surface change. Evidence from live Stage 5 usage (2026-07-24,
Claude Code as the MCP client): `wait_result` is a server-side long-poll, so
the calling agent blocks inside the tool call and is completely unresponsive
to its own user while waiting; worse, this client kills tool calls near 60 s
(timeout_ms of 120000/600000 died client-side), forcing a chatty ~45 s
heartbeat re-poll loop — many calls, still unresponsive between them. If the
re-evaluation keeps or moves preference to `wait_events`, its returned payload
must first be optimized so streaming does not bloat the calling agent's
context — today each batch carries full event objects (source/type/text/raw),
and a long session streams far more tokens than the settled answer justifies.
Candidate directions to weigh, not commitments: a compact event projection for
watch loops (beyond `tool_output: "summary"`), a `timeout_ms=0` instant-peek
form of `wait_result`, guidance recommending 30–45 s bounds for short-cap MCP
clients, and eventually SSE push on `GET /mcp`.

## Context

Delegating a task through the MCP surface today costs an outer agent many
round trips: `describe_options`, `start` (full settings echo), then a
`read_events` + `wait_events` polling loop paced at >= 20 seconds, then
reconstructing the final answer from message events. A native subagent is one
call out, one result back.

Separately, SDK backends have no provider-thread continuity. `claude_sdk`
runs a fresh one-shot `query()` per turn (`ClaudeSdkRunner.run_turn` in
`agent_collab/backends/claude_sdk/backend.py`); the provider session id is
captured into `SessionState.agent_sessions` but deliberately never fed back
("capture only" in `agent_collab/daemon.py`). Continuity is faked by
re-sending guardrails + the full task + a last-12-events transcript window on
every turn (`Referee._prompt_for` / `_recent_transcript` in
`agent_collab/referee.py`) — the main token hotspot; large tool events can
also evict real context from the window. `post_message` inputs are processed
only at turn boundaries, and interactive sessions listen for input only while
parked in `awaiting_input` after planned stages.

Goal: make delegation via MCP feel like a native subagent (start, wait for the
result, optionally keep talking), and make interactive threads real and
token-efficient through provider-side conversation continuity in `claude_sdk`
(the reference implementation; other SDK backends follow later).

Decisions taken at planning time: surface first, backend last. Stage 1 ships
`wait_result`; stage 2 completes the new agent-facing shape (compact views,
delegate guidance, solo message routing); stage 3 ships the backend-neutral
continuity groundwork; stages 4–7 ship per-backend SDK continuity
separately, in the order `codex_sdk`, `claude_sdk`, `antigravity_sdk`,
`xai_sdk`, each flipping the `continuity` capability only for its own
backend and only with proof. Rationale: stages 1–2 are one coherent contract
change over the same surface files, the riskiest work moves last, and
deferring the SDK backend rework avoids colliding with the concurrent #45
Phase 4 work inside the SDK backend packages. Accepted interim: until
continuity lands, follow-up turns keep today's per-turn provider cost (full
task + transcript window re-sent each turn) — the UX improvement arrives
before the cost improvement, and no path gets more expensive than today
(one deliberate stage-2 exception: an untargeted post to a solo interactive
session becomes a directed turn — a documented cost-bearing behavior
change). This task implements continuity only — interrupt, tool gating, and
restart-safe resume stay explicit later stages tracked by issue #20 and the
`sdk-session-control` task document. A cross-model review of this plan
(codex_cli / gpt-5.6-sol, session daemon-0273d3fcb72b4929, 2026-07-23)
returned 11 findings; the accepted fixes — input-accepting settlement
lifecycle, per-turn answer ledger, prompt-snapshot watermarks,
close-vs-active-turn coordination, compact-view scope, per-agent continuity
aggregation, `detail` plumbing, and the codex-stage rebase — are folded
into the stages below.

## Out of scope

- Dynamic model discovery (#45): `model_discovery.py`, the options catalog
  shape, and `describe_options` / `model_refresh` internals and description
  text are under concurrent work and must not be touched.
- Interrupt, `tool_gate`, and restart-safe resume implementation (issue #20).
- Review-skill behavior changes; the skills may adopt `wait_result` later.

## Relation to sdk-session-control (#20)

This task delivers none of #20's three capabilities — `resume`, `interrupt`,
`tool_gate` all stay false. It introduces a deliberately narrower fourth
fact, `continuity` (provider-thread continuation *within one live session*),
because #20's `resume` definition also requires restart-safe explicit resume
across daemon reloads; flipping `resume` on in-session continuity alone
would dilute that definition.

What this task builds is the substrate #20's later stages stand on:

- The per-session conversation adapter holding a live SDK client is the
  handle a future `interrupt()` calls (stop path asks the adapter for a
  provider-side abort before local cancellation).
- A persistent streaming client is the precondition for Claude's
  `can_use_tool` callback — `tool_gate` cannot exist on one-shot `query()`.
- Captured provider ids plus reconnect-by-id after abnormal turn ends are
  the in-session half of resume; #20 adds the other half — fingerprint
  validation, explicit `POST /sessions/{id}/resume` / `agent_collab_resume`
  / CLI verb, and policy reload across restarts.
- The `awaiting_input`-settled `wait_result` pattern gives #20's proposed
  `awaiting_approval` status an established wait/settle mechanism to reuse.

Doc stewardship is part of the contract: the groundwork stage records the
refreshed design in `sdk-session-control.md` (replacing its
`ActiveTurnController` sketch with the smaller adapter contract, per its own
refresh requirement), and each per-backend stage records its verified SDK
facts there. After this task, #20 shrinks to interrupt, tool gating,
restart-safe resume, and their public surfaces — each buildable per backend
on top of the adapters without re-touching the referee.

## Agent-facing text budget

Standing rule for every stage of this task: whenever a stage touches
agent-facing text — MCP tool descriptions in `mcp_tools.py` (`TOOLS`), the
initialize instructions, or any `mcp-guidance.md` topic — review the touched
text and compress it to the fewest tokens that preserve the information. The
audience is an agent, not a human reader: clunky or telegraphic English is
acceptable when it saves tokens and stays unambiguous to a model. Do not drop
contract facts (defaults, bounds, error semantics, safety norms) to get
shorter; drop filler, repetition, and prose politeness. Measure by token
count, not eloquence. Texts not touched by a stage may be trimmed
opportunistically in the guidance-restructure stage, which owns the final
end-to-end pass over all ten-plus tool descriptions and every guidance topic.

## Verified enablers

- Runners are created once per session and reused for all sequential,
  parallel, and directed turns (`Referee.run` calls `self._runners()` once) —
  a stateful runner needs no scheduling change, only a cleanup hook.
- The installed `claude-agent-sdk` (0.2.114 at planning time; re-verified on
  0.2.126 at Stage 5 implementation time — see `sdk-session-control.md` for
  the full verified facts) supports a persistent `ClaudeSDKClient` (connect /
  query / receive_response / interrupt / disconnect) and
  `ClaudeAgentOptions.resume` + `fork_session`. Its reader task is loop-scoped
  (`spawn_detached` uses `loop.create_task`), so a client connected during one
  turn's task survives into the next; an `atexit` child killer reaps orphaned
  CLI subprocesses on ungraceful exit.
- `post_message` enqueues onto `managed.input_queue` before returning;
  `_process_pending_inputs` / `_await_interactive_input` call
  `queue.task_done()` in `finally`; status stays `awaiting_input` during
  directed turns — so the queue's unfinished-task count is a clean "settled"
  signal.
- `SessionStateModel.settings` and `capabilities` are documented-opaque dicts,
  so new capability keys and a compact settings view are additive; the REST
  API major stays 2.

## Stage 1 — `wait_result`: the delegation primitive

A new long-poll that returns only the outcome, collapsing delegation to
`describe_options` -> `start` -> `wait_result`.

Semantics: `wait_result(session_id, timeout_ms)` blocks until the session is
*settled*: status in the terminal set, or `awaiting_input` while the referee
is actively accepting input and none is pending or in flight. Queue
bookkeeping alone is not sufficient (review finding 1): on idle timeout or a
failed directed turn the referee leaves its input loop — `task_done()`
already called — while status stays `awaiting_input` until the terminal
transition, so a waiter could see a false settled, and `post_message` could
accept input that is never consumed (a pre-existing race). Therefore the
referee owns an explicit input-accepting lifecycle: accepting becomes true
when it parks in `_await_interactive_input` and is cleared before unwinding
on idle timeout, failure, or stop; `post_message` rejects with a structured
error while accepting is false; settled := terminal OR (`awaiting_input`
AND accepting AND `unfinished == 0`). On timeout `wait_result` returns a
small heartbeat (`settled: false`, no answers) — callers re-poll
immediately; the >= 20s pacing rule does not apply because the block is
server-side.

Answers derivation: a latest-message-event scan is unreliable (review
finding 2): `codex_sdk` emits its final response before mapping the
remaining thread items, so the newest message event is not necessarily the
answer, and partial output from a failed turn must not masquerade as one.
Instead the referee records a per-turn answer ledger at commit time: for
each turn whose outcome is `completed`, the answer is the message event the
backend marked as final when such a marker exists in the event `raw`,
otherwise the last message event emitted by that agent within that turn's
event span. `answers` = each agent's most recent completed-turn answer;
failed or refused turns contribute nothing. Text is truncated via the
existing `_truncate_text` — a 64 KiB preview plus a truncation notice, so
documented as a preview bound, not a hard payload cap (review finding 11).
For a settled `awaiting_input` session this is the result-so-far,
signalling the caller may post a follow-up.

Changes:

1. `agent_collab/api_schema.py` — new DTOs `AgentAnswerModel`
   (`agent_id`, `text`, `event_id`, `timestamp`), `SessionResultModel`
   (`session_id`, `status`, `terminal`, `settled`, `cursor`, `error`,
   `failure`, `turn_outcomes`, `answers`, `markdown_path`, `jsonl_path`), and
   `WaitResultRequestModel` (`timeout_ms` default 60000; reject < 0 and
   > 600000); route `GET /sessions/{session_id}/result` -> `wait_result` in
   `ROUTES`.
2. `agent_collab/daemon.py` — `_TrackedInputQueue(asyncio.Queue)` with a
   public `unfinished` counter and a notify hook on `task_done()` (wired to
   the session's coalesced `_schedule_notify`), swapped into
   `_ManagedSession` construction; `SessionManager.wait_result()` (deadline
   loop on `managed.condition.wait_for`, same pattern as `wait_events`),
   `_result_settled()`, and the per-turn answer ledger; restored sessions
   work via the existing `_load_restored_events` and settle immediately.
   `referee.py` changes are minimal but real: the input-accepting lifecycle
   signals around `_await_interactive_input` and recording the answer ledger
   entry when an outcome commits.
3. `agent_collab/server_http.py` — `_route_wait_result` following
   `_route_wait_events`; `agent_collab/client.py` — `wait_result()` with the
   scaled HTTP timeout (`max(timeout, timeout_ms / 1000 + 5)`).
4. `agent_collab/mcp_tools.py` — `agent_collab_wait_result` tool schema,
   `ToolBackend` protocol extension in both adapters, dispatch branch. The
   MCP tool is required, not a convenience: a harness without shell access
   (not co-located with the daemon, no background processes — e.g. connected
   over Streamable HTTP with the static token) has no CLI path, so bounded
   `wait_result` calls with heartbeat re-polls are its only collection
   mechanism. The background CLI pattern is an optimization for harnesses
   that have it, never a replacement.
5. `agent_collab/cli.py` — `agent-collab result SESSION_ID [--timeout-ms N]
   [--json]`, following the CLI output marker convention. This command is the
   primary collection path for harness-hosted agents: run as a background
   process, it lets the agent stay fully active and be woken by its harness's
   background-task notification when the process exits with the result.
   Therefore, unlike the MCP tool, it loops on `wait_result` internally —
   absorbing timeout heartbeats inside the process — and exits only when the
   session is settled (the session's own `timeout` and
   `interactive_idle_timeout` bound the worst case). `--timeout-ms` is an
   optional total bound with a distinct exit code on expiry; `--json` prints
   the settled `SessionResultModel` on stdout for machine consumption.
6. Regenerate `doc/daemon_api_doc/` via `./agent_collab_dev.sh build`.

## Stage 2 — surface shape: compact views, guidance, message routing

1. `detail` on start/status, **defaulting to `"compact"`** with `"full"` as
   the opt-in — same efficient-by-default philosophy as the existing
   `tool_output: "summary"` event projection. An additive wire field on
   `StartSessionRequestModel` (plus `WIRE_FIELDS`), and a small request model
   for `GET /sessions/{session_id}`. Scope of compaction, narrowed by review
   findings 4 and 5: every top-level `SessionStateModel` field keeps being
   emitted — including `capabilities` and `agent_sessions` — so the typed
   response contract is untouched; compact slims only the *content* of the
   documented-runtime-defined `settings` dict. The compact `settings` keeps
   workflow shape, interactive flags, warnings, and per-agent
   `{type, backend, capabilities, options}` where `options` is the full
   normalized effective option map (model, permission/sandbox mode,
   thinking/reasoning — everything cost- or permission-relevant, required by
   the paid-action confirmation norm); it drops only `command_preview` and
   the duplicative `backend_summary` (both retrievable with
   `detail: "full"`). Compaction is a response-view concern only: persisted
   `SessionState.settings` stays full fidelity, mirroring events (full
   storage, projected reads). Plumbing (review finding 10): `detail` flows
   through the `ToolBackend` protocol and both adapters, `client.py`
   methods, `SessionManager.get_session`/`list_sessions` signatures, and the
   MCP start/status input schemas — covered by the lockstep contract tests
   (`WIRE_FIELDS` <-> MCP input schema <-> `_start_payload`) updated in the
   same commit. Internal consumers that render the dropped fields (CLI start
   output, TUI detail views) pass `detail: "full"` explicitly in the same
   commit; `list_sessions` returns compact views, a free token win for
   session tables. Client-visible default change — CHANGELOG entry required;
   external readers of `command_preview`/`backend_summary` switch to
   `detail: "full"`.
2. Guidance restructure (`agent_collab/mcp-guidance.md` plus the guidance
   plumbing in `mcp_tools.py`) — a new short `delegate` topic (describe ->
   confirm -> start -> `wait_result` -> read `answers` -> optional
   `post_message(target)` + `wait_result` follow-ups -> stop or idle
   timeout; note `interactive_idle_timeout` for long conversations).
   Guidance principle: the audience is an MCP client that may be remote,
   shell-less, or on another OS — `mcp-guidance.md` describes MCP usage
   only, never CLI commands or local filesystem paths as steps; the
   transcript path is `read_transcript`. Collection is
   `agent_collab_wait_result`: one long bounded call, or short calls with
   immediate heartbeat re-polls; `wait_events` remains only for live
   streaming. CLI commands and local-access patterns (backgrounding
   `agent-collab result` for wake-on-exit, reading `markdown_path` from
   disk) belong in project docs — README, doc/, and the Claude Code skills —
   never in `mcp-guidance.md`. Rewrite the initialize instructions to lead
   with the
   delegate flow; scope the >= 20s pacing rule to the `wait_events`
   streaming path only; make the `overview` topic return only its own
   section (no-topic keeps the full document); add a one-line additive note
   to the review recipe. Do not
   touch the `describe_options` description or the Options topic's
   `model_refresh` prose (#45 owns those). Describe follow-up-turn cost
   honestly for the interim: each follow-up re-sends the task and recent
   window until continuity lands; the continuity stage then adds one line
   pointing at the `continuity` capability.
3. Untargeted message routing (`agent_collab/referee.py`
   `_process_input_item`): when the session has exactly one enabled agent,
   route untargeted messages to it (today `target=None` is appended but runs
   no turn), so `post_message` behaves like a direct message in solo
   interactive sessions and the delegate loop needs no `target` bookkeeping.
   Honest classification (review finding 8): this is a cost-bearing behavior
   change, not an additive tweak — an existing client posting an untargeted
   note to a solo interactive session now triggers a provider turn. It is
   the one deliberate exception to "no path gets more expensive"; CHANGELOG
   entry required. Blast radius is small: the shipped review skills never
   call `post_message`.

## Stage 3 — shared continuity groundwork (backend-neutral)

Runner contract (`agent_collab/runners.py`): add defaulted methods to
`AgentRunner` so CLI runners stay untouched —

- `conversation_active() -> bool` (default false): the runner holds
  provider-side context the next `run_turn` will continue;
- `async close()` (default no-op, idempotent): release any client or
  subprocess held across turns.

Lifecycle: the backend owns what is inside the runner; the referee owns
lifecycle — a new `finally` in `Referee.run()` closes every runner, bounded
and shielded, reusing the existing bounded-cancel/reaper-adoption pattern.
The daemon needs no new hook: stop cancels the session task and the `finally`
runs; daemon exit is covered by the SDK's atexit cleanup. Close-vs-turn
coordination (review finding 6): the referee's bounded cancel can adopt a
non-cooperative `run_turn` task as a reaper that outlives the turn, so
`close()` must be concurrency-safe against an in-flight or adopted turn —
the conversation adapter serializes `close()` against `run()` internally,
and close errors or timeouts never alter an already-committed session
outcome. Groundwork tests must cover a cancellation-ignoring `run_turn`
followed by a bounded `close()`.

Referee delta prompts (`agent_collab/referee.py`): per-agent watermarks with
prompt-snapshot semantics (review finding 3): an agent's watermark advances
to the transcript length captured when its prompt was *built* — never to
completion-time length, which in a parallel stage or during a mid-turn post
would silently skip peer events the provider never saw. The delta for the
next turn is `transcript[watermark:]` minus the agent's own events (its
provider context already holds its own output), with the existing
provider-session filtering; no silent cap — if any bound is applied, the
watermark advances only over events actually included. At the two
sequential call sites (the `run()` stage loop and `_process_input_item`),
when `runner.conversation_active()` is true, send a continuation prompt — a
role note, "NEW EVENTS SINCE YOUR LAST TURN:", the delta, plus the directed
question when present — with no guardrails, task, or full window re-send.
Turn 1, CLI and mock runners, and parallel stages keep the current prompt
byte-for-byte.

Capabilities: add `continuity: bool = False` to `BackendCapabilities`
(`agent_collab/backends/base.py`) and its `to_dict()`; extend
`summarize_session_capabilities` (`agent_collab/backends/__init__.py`). No
backend flips it in this stage — the groundwork ships with zero behavior
change, proven with stub runners only; each per-backend stage below flips
`continuity` for its backend alone, and only with hermetic plus credentialed
proof. Session-level aggregation (review finding 9): the reducer is
conservative — the session `capabilities` dict reports `continuity: true`
only when *every* selected agent's backend has it; the per-agent flag is
visible in `settings.agents.<id>.capabilities` so a caller can target
follow-ups at the agents that hold provider context in a mixed workflow.
`resume`, `interrupt`, and `tool_gate` stay false everywhere under the
strict definitions in the `sdk-session-control` task document. No REST or
config schema change.

Guidance: add the one-line note to the `delegate` topic that
`capabilities.continuity: true` means follow-up turns continue the provider
thread natively.

The per-backend stages share one shape: verify the installed SDK's actual
session/thread facts and record them in the `sdk-session-control` task
document (the #20 re-verify rule); replace the backend's per-turn provider
context with a per-session conversation adapter behind an injectable seam
(`active()` / `run(prompt)` / `note_session_id()` / `reset()` / `close()` —
reset keeps the captured provider id for reconnect, close drops
everything); on abnormal turn ends reset and reconnect via the provider's
native resume if the verified API supports it, else fail the turn
structurally — never a silent fresh provider session; flip
`BackendCapabilities(continuity=True)` and add
`settings_summary["conversation"] = "persistent"` only when the hermetic
fake-conversation suite and a credentialed provider-memory integration test
both pass. A backend whose SDK cannot prove native continuity keeps the
capability false with the finding recorded — a valid, closable outcome.

## Stage 4 — `codex_sdk` continuity

First backend, per decision. Rebase on the actually pinned SDK before
implementation (review finding 7): the backend collects a `TurnResult` from
`thread.run()` inside an `AsyncCodex` context — there is no consumable
stream — and the plan's version citation must be re-verified against the
installed `openai-codex` package and its runtime at implementation time.
Questions to answer and record: whether one open `AsyncCodex` client holds a
thread across several `run()` calls; whether a thread can be reopened by
persisted id after the client closed (the reconnect path); how an in-flight
`run()` behaves under cancellation and what cleanup the client needs.
The conversation adapter owns the `AsyncCodex` client and
thread in `agent_collab/backends/codex_sdk/backend.py`; the backend keeps
resolving the local `codex` executable through `CodexConfig(codex_bin=...)`
as today. "Starting a new thread with the same transcript is not thread
resume" (#20) — if reopen-by-id is unsupported in the pinned SDK, reset
degrades to structured turn failure and `continuity` still qualifies only if
in-client thread reuse across turns is proven.

Answer-ledger dependency carried from Stage 1 (grok-gemini-review of the
Stage 1 diff, session daemon-a5fea5bc303c46c9, 2026-07-23): `iter_codex_turn_events`
emits the collected `final_response` message **first**, then maps the remaining
thread items (which can include further `agentMessage`s). Stage 1's answer
derivation — both the live `Referee._find_turn_answer` and the restored
`SessionManager._derive_restored_answers` — selects a message marked
`raw["final"]` when present, otherwise the last message in the turn span. No
backend sets that marker yet, so for a `codex_sdk` turn that emits a trailing
non-final `agentMessage`, `wait_result` currently returns that trailing message
rather than `final_response`. Fix in this stage by marking the emitted
`final_response` event with `raw["final"] = True` (the ledger already honors it
and a hermetic answer-selection test covers the precedence); this is the only
correct place to express codex's emit order and was deliberately deferred out of
the surface-only Stage 1.

## Stage 5 — `claude_sdk` continuity

**Shipped.** SDK facts re-verified on the implementation-time
`claude-agent-sdk` 0.2.126 (latest release at the time; no bump available —
full connect/query/resume/cancellation facts recorded in
`sdk-session-control.md`). The per-turn `MessageStreamFactory` seam in
`agent_collab/backends/claude_sdk/backend.py` was replaced with the
conversation adapter holding one persistent `ClaudeSDKClient` per runner;
reconnect after reset uses `ClaudeAgentOptions(resume=<sid>,
fork_session=False)` and a rejected resume fails the turn structurally; the
lazy SDK import stays inside the factory (`BackendUnavailable` on
ImportError); `build_claude_agent_options` grew an optional resume-session
parameter emitted only on reconnect. The `run_turn` message loop (event
mapping, provider-session capture, evidence accumulation) stayed as it was.
`claude_sdk` flips `continuity=true` with hermetic fake-conversation coverage
plus the credentialed provider-memory integration test
(`integration_tests/backends/claude_sdk/test_live.py`), which also asserts the
follow-up turn used the Stage 3 delta prompt and a stable provider session id.

## Stage 6 — `antigravity_sdk` continuity

Verify the installed `google-antigravity` API for reopening a conversation
by `conversation_id` and for multi-turn use of one live agent context. Known
blocker: the SDK's bundled native runtime requires glibc >= 2.36 and the
current development host has 2.34, so the backend probes `unavailable`
locally. The hermetic conversation-seam work can land against verified
source facts, but `continuity` stays false until the credentialed
provider-memory test passes on a compatible host — a skipped provider keeps
the capability false.

## Stage 7 — `xai_sdk` continuity assessment

`xai_sdk` is remote message-only chat (`provider_session_id_kind:
"response"`) and currently disabled in user config. Assess whether the
installed `xai-sdk` offers provider-held conversation state at all —
client-side history replay is transcript continuity, not native continuity,
and does not qualify. Expected outcome: record the finding and keep
`continuity` false unless real provider-side state is proven; the
delta-prompt path then simply never activates for this backend.

## Tracking and docs

- Rewrite the `sdk-session-control` task document: record the refreshed
  design (verified 0.2.114 facts above), replace the `ActiveTurnController`
  / streaming-runner sketch with the smaller `conversation_active()` +
  `close()` contract and backend-internal conversation adapter, and move
  interrupt, restart-safe resume, and tool gating to explicit later stages;
  comment the refresh on issue #20.
- Keep `doc/implementation-notes.md` and `doc/daemon-architecture.md` deltas
  minimal and accurate; add a `CHANGELOG.md` entry referencing #47.

## Testing

Hermetic (`tests/`, no SDK imports — CI has no vendor SDKs and runs Python
3.10/3.11):

- `tests/test_daemon.py`: `wait_result` on done; timeout heartbeat;
  interactive session settles while parked and accepting; unsettled during a
  directed turn and settles after `task_done` (post_message -> wait_result
  ordering); accepting lifecycle — no false settled during the idle-timeout
  unwind window, and `post_message` is rejected with a structured error once
  accepting clears; answers come only from the completed-turn ledger (a
  failed turn's partial output is excluded); restored session from JSONL;
  per-agent attribution on a parallel workflow; (stage 2) compact settings
  echo.
- `tests/test_api_schema.py`: DTO round-trips, `timeout_ms` bounds,
  route-registry invariants, live-wire fidelity for `/result`; (stage 2)
  start-payload lockstep tests.
- `tests/test_server_http.py`, `tests/test_mcp_server.py` (tool listed,
  dispatch, guidance topic, initialize-instruction pins),
  `tests/test_sessions_cli.py` (`result` command: loops through heartbeats
  until settled; `--timeout-ms` expiry exits with the distinct code;
  `--json` emits exactly the settled result on stdout).
- Per SDK-backend stage, `tests/backends/<backend>/test_backend.py` with a
  fake conversation: one connect across two turns; provider id fed back;
  abnormal end -> a single bounded `reset()`, next connect resumes the
  captured id; resume/reopen rejected -> structured failure with no fresh
  provider session; `close()` idempotent; `conversation_active()`
  transitions; the resume id present in provider options only when provided.
- `tests/test_referee.py` (stub-runner pattern): (stage 2) an untargeted
  input in a solo interactive session runs a turn of the single agent, and
  multi-agent sessions keep the append-only behavior; (stage 3) continuation
  prompt contains no `TASK:` and only post-watermark events; turn-1 and
  stateless prompts are byte-identical to today; watermark equals the
  prompt-snapshot length and the delta excludes the agent's own events;
  runners closed on completion, stop, and failure; a hanging `close()` does
  not hang teardown; `close()` racing a cancellation-ignoring adopted turn
  is serialized by the adapter.
- `tests/backends/test_registry.py`: capability pins and reducer cases for
  `continuity`.
- `tests/test_project_build.py` keeps generated docs current.

Credentialed (`integration_tests/backends/<backend>/`, opt-in, one per
backend that flips `continuity`): a provider-memory test — interactive solo
session, turn 1 states a codeword, a directed turn 2 asks for it without
including it in the prompt; assert the answer and chained provider ids in
`agent_sessions`. A backend whose test cannot run (missing credentials,
incompatible host) keeps `continuity` false.

## Verification

1. `./agent_collab_dev.sh build --check` after doc regeneration; full
   hermetic suite locally and on the CI matrix after push.
2. Credential-free smoke: a `mock` backend session returns answers through
   `agent-collab result` and `agent_collab_wait_result`; an interactive mock
   session parks and settles correctly.
3. Live per-backend check as each SDK stage lands (`codex_sdk` first, then
   `claude_sdk`; `antigravity_sdk` when a compatible host exists; `xai_sdk`
   only if continuity is proven), after installing the package so the daemon
   runs the new code: interactive solo session via MCP with the backend
   substituted through `members` — start -> `wait_result` -> `post_message`
   follow-up -> `wait_result`; confirm the follow-up turn used a delta
   prompt (transcript shows no task re-send) and provider memory held.

## Risks

- Long blocking MCP calls: some harnesses kill long tool calls — guidance
  recommends `timeout_ms` of 60–120 s with immediate re-poll; the heartbeat
  is tiny.
- One idle provider CLI subprocess per SDK agent while a session is parked —
  bounded by `interactive_idle_timeout` and runner `close()`.
- Resume-reconnect depends on provider-side or local session storage; when
  absent the turn fails structurally rather than silently starting fresh.
- Two backends may legitimately never flip `continuity`: `antigravity_sdk`
  is blocked on the dev host's glibc, and `xai_sdk` may have no provider-held
  state at all. Closing those stages with the capability false and the
  finding recorded is a valid outcome, not a failure of the task.
- SDK drift on upgrade: the loop-scoped reader-task behavior is verified on
  0.2.114; the fake-conversation cancellation test guards the contract, and
  the fact must be re-verified on SDK bumps.
- Settlement race when `wait_result` runs concurrently with another caller's
  in-flight `post_message` — documented as caller-ordering responsibility
  (`post_message` enqueues before returning, so sequential callers are
  safe).

## Open questions

- Whether untargeted `post_message` routing should later extend to
  multi-agent interactive sessions (e.g. round-robin or last-addressed) —
  out of scope here; solo-only routing is unambiguous.
- Whether `wait_result` should ever include a bounded tail of recent
  non-message events for debugging failed sessions, or whether pointing at
  `read_events` / the transcript stays sufficient.
