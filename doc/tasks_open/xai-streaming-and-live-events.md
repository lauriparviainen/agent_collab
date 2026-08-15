# xAI live streaming and event fidelity

**Status:** Open. Planning reviewed 2026-08-16 (four independent
read-only reviews: facts, live UX, compatibility, harvest/TUI). Implement
against the decisions below; recapture current Grok `streaming-json`
fixtures before claiming tool fidelity.

**Created:** 2026-08-16.

**Issue:** [#62](https://github.com/lauriparviainen/agent_collab/issues/62).

**Predecessor:** [stage-5.1.1-xai-provider.md](../tasks_closed/stage-5.1.1-xai-provider.md)
landed `xai_cli` on `streaming-json` and `xai_sdk` as message-only
`chat.sample()`. [backend-turn-outcomes.md](../tasks_closed/backend-turn-outcomes.md)
owns terminal classification. [sdk-session-control.md](sdk-session-control.md)
(#20) owns interrupt, tool gating, and restart-safe resume — this task does
not flip those flags.

## Purpose

Make `xai_cli` show **Codex-like** live answer text and dim `source="tool"`
rows. Event `raw` will not match Codex. Older Grok CLIs and the audited
`xai-sdk` 1.17 series must keep working.

This is a display/fidelity task, not a new Grok capability. Current Grok
(`streaming-json` since 0.2.116) already emits `tool_call` /
`tool_call_update`. Agent-collab drops them. Codex looks different
because it maps JSONL into `source="tool"` events immediately.

Today a long `xai_cli` turn can sit silent for many minutes and then dump one
message at the end. That is not because Grok only has an end-of-process JSON
object. Headless `streaming-json` is already an NDJSON stream. Local choices
hide it: `XaiStreamingParser` coalesces every `text` delta until `end`/`error`;
thought is verbose-only; and the 0.2.93 fixtures never saw `tool_call`
records, so the parser still maps none.

A measured dual-review watch (~12.5 minutes of heartbeats, then one dump)
was mostly **think + untyped tools**, then one dumped answer. Live `text`
flushes (Stage A) fix “the answer arrives as one blob.” They do **not** by
themselves make a long review feel like Codex. Thought visibility (TUI) and
Stage B tool mapping are what fill the silent middle.

`xai_sdk` has the same end-of-turn shape for a different reason: the runner
calls `chat.sample()` and emits `response.content` once. The installed 1.17.0
SDK already exposes `async for response, chunk in chat.stream()`.

## Goal

- **CLI:** keep `--output-format streaming-json`. Flush live `message`
  chunks. Map current-Grok `tool_call` / `tool_call_update` into the same
  `source="tool"` event types Codex uses (`tool_call`, `command`,
  `file_change`) so the TUI draws dim `*-tool` rows during the turn.
- **SDK:** prefer `chat.stream()` on 1.17, with a `sample()` fallback. Do not
  bump the `xai-sdk>=1.17,<1.18` pin in this task.
- **Compatibility:** older Grok that only emits `thought` / `text` / `end`
  (legacy `EndTurn`) and SDKs that only have `sample()` keep working.
- **Harvest:** `wait_result` answers stay the full turn text, never the last
  streamed fragment. The TUI/markdown archive must not reprint the whole
  answer after the live chunks.

## Verified facts (2026-08-16)

Keep these separate from inference. Re-check the installed `grok` and
`xai-sdk` before implementation; record the versions in the same change as
any new fixture.

### Grok Build CLI

Installed locally: **Grok Build 1.0.4** (`d846eb93d9`). `grok --help` lists
four `--output-format` values:

| Format | When it emits | Use in this task |
| --- | --- | --- |
| `plain` | human text | no |
| `json` | one object at process end | no — this is the end-only dump |
| `streaming-json` | NDJSON ACP session updates | **keep as the shipped command** |
| `streaming-messages-json` | Anthropic Messages `stream-json` | **do not switch the default** |

`--include-partial-messages` emits Messages `stream_event` framing
(`message_start`, `content_block_delta`, …). It **only** affects
`streaming-messages-json`; Grok warns and ignores it on `streaming-json`.
Those deltas are coarser than token-level (tool input is one
`input_json_delta`). Public docs still omit the fourth format; the installed
help and `~/.grok/docs/user-guide/14-headless-mode.md` document it.

Changelog **0.2.116** (2026-07-30): `streaming-json` gained `tool_call`,
`tool_call_update`, and `usage`. Current documented `streaming-json` types:

- `text`, `thought`
- `tool_call`, `tool_call_update`
- `usage` (per model response)
- `plan`, `available_commands`
- `end` (always last), `error`
- treat the list as non-exhaustive (`max_turns_reached`, `auto_compact_*`)

Observed 0.2.93 fixtures (still in-tree) are only `thought` / `text` / `end`
with legacy `EndTurn`. A real tool turn on that version emitted **no** typed
action record. Current stop-reason handling already accepts `end_turn` and
legacy `EndTurn` (#56).

Shipped argv stays:

```text
grok --no-auto-update --output-format streaming-json -p
```

`xai_cli.event_fidelity` is `message_first`. `parse_xai_line` (stateless
fixture mapper) already turns each `text` line into a message.
`XaiStreamingParser` (the runner) buffers those lines until `end`.

### xAI Python SDK

This repo pins `xai-sdk>=1.17,<1.18`. The durable venv has **1.17.0**.

1.17 already has:

```python
async def stream(self) -> AsyncIterator[tuple[Response, Chunk]]:
    ...
    async for chunk in stream:
        response.process_chunk(chunk)
        yield response, Chunk(chunk, index)
```

`Chunk.content`, `Chunk.reasoning_content`, and `Chunk.tool_calls` exist.
`Response` accumulates across chunks. The current runner uses `chat.sample()`
only.

**1.18.0** released 2026-08-13. Release notes: accept `xhigh` reasoning
effort and add `grok-4.6` to `ChatModel`. Protobuf remains `<7`. Streaming
is not new in 1.18.

`xai_sdk` `no_local_effects` is a **versioned software capability** pinned to
series `1.17.` and `chat.create` keys `{model, store_messages,
previous_response_id, reasoning_effort}` only. Forbidden keys include
`tools`, `include`, `search_parameters`, `max_turns`. Importing
`xai_sdk.tools` revokes the capability. Unexpected `response.tool_calls`
already fail closed as `provider_output_invalid`.

Docs for agentic tools recommend `chat.stream()` plus
`include=["verbose_streaming"]`. That `include` key is forbidden on this
backend. This task does **not** enable server-side or client-side tools.

### Shared harvest contract

`Referee._find_turn_answer` returns:

1. the turn's last `message` with `raw.final` true, else
2. the turn's last non-empty `message`.

Codex already marks a final response. Incremental xAI `message` events
without a final marker would make `wait_result.answers` the last fragment.
Events are append-only; there is no in-place rewrite.

Documented MCP watch is `wait_events` with `view="digest"` and
`types=["message","error"]`. Digest drops `raw` and caps text at 200
characters. Status and `tool_call` events are filtered out of that loop.
[sse-collection-transport-evaluation](../tasks_closed/sse-collection-transport-evaluation.md)
measured `xai_cli` emitting nothing for ~12.5 minutes, then the whole answer
at once, so the digest watch degenerated to heartbeats.

## Design decisions

### 1. Stay on `streaming-json`

Do not change the default to `streaming-messages-json` or add
`--include-partial-messages`.

Reasons: older CLIs lack the fourth format; it is a second schema; Messages
`init`/`result` lines omit and placeholder fields; `streaming-json` already
streams `text`/`thought` and, since 0.2.116, tools and usage. Switching
formats would rewrite the parser for no live-progress gain.

`streaming-messages-json` stays a recorded alternative, not a fallback and
not a user option in this task.

### 2. No version-gated argv

Do not parse `grok --version` to pick flags. The same command works from the
0.2.93 fixture generation through 1.0.4. Unknown NDJSON `type` values stay
ignored (verbose status only). Invalid JSON still fails closed on the
streaming parser.

Optional: record the probed CLI version on health/settings. Not a start gate.

### 3. Incremental new-text chunks; harvest via `raw.full_text`

The TUI (`format_transcript_event`) and markdown transcript each render
**one gutter-labeled block per event**. There is no sibling collapse.
Style is by `source`, not `type`. A trailing second `message` whose `text`
is the whole answer would reprint the review in the TUI, the archive, and
the digest (the digest of that replay is just chunk 1 again, capped at 200
characters). Growing snapshots are worse (quadratic prefixes). Codex
`raw.final` is collect-then-emit with emit-time dedup, not a streaming
template — do not analogize a reprint to Codex.

Keep coalescing so tiny `text` records do not become thousands of lines.
Flush a `message` whose `text` is **new text only** when:

- accumulated new text reaches ~200 characters (one digest line), or
- a semantic boundary arrives (`tool_call`, `tool_call_update`, `usage`,
  `end`, `error`, or thought→text), or
- `finish()` at EOF.

Do **not** treat “8 deltas” as the headline rule. Token-sized deltas would
spin `wait_events` (each wake can be a caller LLM turn). An idle timer
needs a runner tick the line parser does not have; until one exists,
thought/tool/`usage`/`end` are the ticks that also flush pending text.

**Answer integrity (required).** `_find_turn_answer` skips empty
`event.text` and today’s restore ledger stores `event.text` only (it
does not keep `raw`). `usage` / `tool_*` are **not** last-of-turn
boundaries (`usage.stopReason` can be `tool_use`). After a 200-char or
tool flush, `end` often arrives with an empty text buffer. You cannot
emit an empty `final` message (harvest skips it; TUI still prints a
blank row) and you cannot emit a second full-text `message` (reprint).

Therefore:

- Every incremental `message` carries **running** `raw.full_text` (the
  concatenation so far). `event.text` stays **new text only**.
- `raw.final` is set only on the `end` / `error` / `finish()` flush when
  there is leftover new text. Never mark a `usage` or tool flush `final`.
- Harvest (the one allowed referee/daemon edit): if the chosen last or
  final **message** has a non-empty `raw.full_text`, use that — **do not
  require `final` to read `full_text`**. Otherwise keep `event.text`.
  Restore must copy `full_text` into `entry["text"]` when the event is
  seen (the ledger does not retain `raw`). Sibling messages are never
  concatenated. Codex/Claude omit `full_text`; they still harvest
  `event.text`.
- One-chunk turns: `event.text` already equals the full answer;
  `full_text` may be omitted.
- Keep `raw["delta_count"]` on coalesced flushes
  (`test_runner_coalesces_text_deltas_and_keeps_session_event`).

Do not add a reprint `message`. Do not emit a harvestable empty
`message`.

Implement flush / `full_text` **only** in `XaiStreamingParser` and the
SDK stream loop. Leave `parse_xai_line` as the 0.2.93 mapper.

### 4. Thought (TUI heartbeat only)

- Keep thought out of `type="message"` so it cannot become the harvested
  answer (including thought-only turns that then `EndTurn`).
- Default TUI must not dump full chain-of-thought as ordinary `xai` body
  text (`format_transcript_event` ignores `type`, so a thought `status`
  looks like the answer). Always-on thought is a **coarse heartbeat**:
  first thought → one `status` line such as `thinking…`; later thought
  stays quiet or updates a `thinking ·` prefix. Full thought prose only
  when `verbose`.
- Implement that only in `XaiStreamingParser`. Do not change
  `parse_xai_line`’s verbose-gated thought mapping (0.2.93 fixture tests
  drive the stateless mapper).

### 4b. Tool calls — Codex-like display on `xai_cli`

This is a first-class goal, not an afterthought. The TUI only special-cases
`source="tool"` (one dim summary row). Codex reaches that path by mapping
JSONL into `tool_call` / `command` / `file_change`. Grok 1.0.4 already
emits ACP-shaped records; we must map them in `XaiStreamingParser`.

**When to emit.** On `type=="tool_call"` and `type=="tool_call_update"`,
flush pending text first, then emit a `source="tool"` event. Do this in
Stage B against the documented 1.0.4 shape (hermetic fixtures may be
labeled documented-shape). A live recapture still happens in the same
stage to confirm field names and `kind` values before flipping
`event_fidelity`.

**What to map (documented 1.0.4 / ACP fields only).**

| Grok field | Use |
| --- | --- |
| `toolCallId` | correlate start and update; coalesce key |
| `toolName`, `title` | TUI first-line name (`title` else `toolName` else `"tool"`) |
| `kind` | event **type** (see below) |
| `status` | in_progress vs completed; coalesce updates |
| `rawInput` | compact args on the TUI line (limit ~120) |
| `rawOutput`, `content`, `locations` | kept on `raw`; never invent a diff |

**Kind → event type.** Official Grok 1.0.4 docs show the field `kind`
and **one** value (`read`). The other tokens below are ACP `ToolKind`
(`read`, `edit`, `delete`, `move`, `search`, `execute`, `think`,
`fetch`, default `other`) — **inferred, not Grok-verified**. Recapture
before flipping `typed`. Persist `kind` from the start record; updates
omit it.

| `kind` (case-insensitive) | Event `type` | Why |
| --- | --- | --- |
| `execute` | `command` | shell / terminal |
| `edit`, `delete`, `move` | `file_change` | path mutation |
| `read`, `search`, `fetch`, `think`, `other`, missing/unknown | `tool_call` | default; do not guess |

ACP also documents `think` and `other`; they fall through to `tool_call`.
Do **not** fold `kind=think` into the Stage A thought heartbeat (that is
a different `streaming-json` `type`). Do **not** use Codex
`looks_like_*` haystack heuristics: on a Grok record `type` is already
`tool_call`, so those predicates match every tool line. Do **not**
invent `file_change` from a name like `read_file`. Do **not** synthesize
a patch/`diff` if Grok did not send one.

**`event.text` (required).** The TUI shows only the first line of
`event.text`. Daemon `_tool_identity` is **not** in scope for this task
(it looks for `name`/`input`, not Grok camelCase). So the parser must
put a complete one-line summary in `text`:

```text
name = title or toolName or "tool"
args = compact_json(rawInput, limit=120)   # omit when rawInput is empty
text = name
     | f"{name} {args}"                    # when args present
     | f"{text} · {status}"                # close row only (completed|failed)
```

Do **not** copy `content` / `rawOutput` / file bytes into `text` (that
becomes a huge `+N lines` dim dump). Optional and honest: copy
`name`/`input` **aliases** onto `raw` so digest uses `toolName` instead
of a sentence-y ACP `title`. Do not add `diff`/`patch` keys.

**Coalescing (required).** Documented `tool_call_update` often has only
`toolCallId`, `status`, `content`, `rawOutput`, `locations` — no
`title`/`kind`/`rawInput`. Naive mapping would emit a completed row
named `tool` with empty args.

1. Keep per-`toolCallId` state: `kind`, `title`, `toolName`, `rawInput`,
   last status, whether an **open** row was emitted.
2. Flush pending **text** on every `tool_call` / `tool_call_update`,
   even updates that are then dropped.
3. Treat `pending` and `in_progress` as one **open** phase: at most one
   open row. Prefer the first record that already has
   `title`/`toolName`/`rawInput`. If the first record is bare `pending`
   and a later `in_progress` adds `rawInput`, emit that second open row.
4. Emit one **close** row on `completed` or `failed`, using **merged**
   start fields. Same mapped `type` as the open row.
5. Drop identical-status updates (streaming `content` spam).
6. Missing `toolCallId`: emit once as `tool_call`, do not crash. Update
   with no prior start: treat as start from whatever fields are present.
7. Append-only: two rows per tool is correct; do not rewrite the open
   event.

A documented-shape test must show that an update with no title still
renders like `Read {"path":"src/main.rs"} · completed`.

**Older Grok.** 0.2.93 tool turns emit no `tool_call` records. The
existing fixture must keep producing **no** `source="tool"` events. That
is degrade, not a parser failure.

**`parse_xai_line`.** Stays the stateless 0.2.93 mapper and does **not**
guess tools. Mapping lives only in `XaiStreamingParser` so old fixture
tests that call `parse_xai_line` stay green.

**Fidelity claim.** Keep `event_fidelity = "message_first"` until a
current-Grok capture shows the kind table matches live output. Then flip
to `typed`. A partial mapping (tools as `tool_call` only, kinds missing)
stays `message_first`. Do not map permission-prompt-ish records; leave
them unknown for #40.

**`usage`.** Verbose `status` only. Never run `_classify_end_reason` on
a `usage` line (`usage.stopReason` can be `tool_use`). `end.stopReason`
is the only turn terminal.

**Unknown types.** Ignored unless verbose. Unknown ≠ invalid JSON.
Invalid JSON on the runner parser still `ValueError` →
`provider_output_invalid`.

**What will not match Codex.** Identical `raw` payloads; file diffs when
Grok omits them; `xai_sdk` local tools (decision 6). The visible
parity is: live dim tool rows + live answer text.

### 5. MCP watch: fragments vs findings

The default `types=["message","error"]` loop will start seeing mid-turn
prose once chunk messages exist. Those events are **fragments**, not
findings. Callers must not rebuild the answer from them (`wait_result`
still harvests `raw.full_text` / the final message). During thought,
filtered status still **wakes** `wait_events` and can return an empty
batch — that means “filtered,” not idle. Pace ~20s once a fragment stream
is flowing.

`#20` owns `mcp-guidance.md` Watch/delegate and the `mcp_tools.py`
initialize / `wait_events` description strings (approval stop state).
This task must **not** rewrite those blobs.

- Default recipe stays `types=["message","error"]`.
- At most **one additive sentence** in guidance if that file is not
  already mid-edit: mid-turn `message` events are fragments; harvest
  with `wait_result`; an empty digest batch can mean filtered-out.
- Put the optional live-supervision list
  `["message","error","tool_call","command","file_change"]` in the
  `xai_cli` README and implementation-notes, not as a competing
  initialize-string rewrite. That list also admits one referee
  `type="command"` “preparing …” line; acceptable. Keep `status` out.

### 6. SDK stream without widening the audit

`XaiConversation.run(prompt)` stays `async -> Response`. Do not put
`emit` on that Protocol. `_FakeConversation.run()` and every hermetic
Chat fake that only implements `sample()` must keep working.

Live chunks must be emitted **during** `chat.stream()`, via an internal
callback the runner installs. A buffer drained only after `run()` returns
is the same end-of-turn dump as `sample()`. `run()` still returns the
last accumulated `Response` so `run_turn` can keep today’s id /
`finish_reason` / unexpected-`tool_calls` logic. **Do not** also emit
`iter_xai_response_events(response)` after a streamed turn — that
reprints the full answer. `sample()`-only fakes keep the current
one-message path. `getattr(chat, "stream", None)` on production 1.17
`Chat` is always true; the useful fallback is a fake that only has
`sample()`. Do not write the string literals `include` / `tools` /
`xai_sdk.tools` in `backend.py` even in comments.

Cancel: `stream()` is an async generator over `GetCompletionChunk`, not
one `GetCompletion` future. Do not copy the `shield(sample_task)` loop
verbatim. Drain the chunk iterator to completion (or an equivalent that
does not abandon the gRPC stream), capture `response.id` from the last
accumulated `Response` if the RPC finished, then reset as now. Keep
`test_cancellation_performs_exactly_one_reset`.

`chunk.reasoning_content` follows the CLI thought rule (coarse status by
default, full only when verbose).

`no_local_effects` is a **source-text** audit, not only a kwargs
allowlist. `audit_production_source` fails if `backend.py` contains
forbidden **string literals** (`include`, `tools`, …), the substring
`xai_sdk.tools`, or a new sibling module not on the allowed-import list.
Keep denylist commentary in `sandbox.py` only. Introspect `stream` via
attribute access. Do not widen `production_chat_kwargs` or
`AUDITED_CHAT_CREATE_KEYS`. Do not import `xai_sdk.tools`. Unexpected
`tool_calls` still fail closed.

Do **not** bump to 1.18 in this task. 1.18 (2026-08-13) only adds `xhigh`
and `grok-4.6` on `ChatModel`; protobuf remains `<7`; streaming is not
new. `AUDITED_PACKAGE_SERIES = "1.17."` already accepts 1.17.1 if that
is what is installed. A 1.18 pin is a separate re-audit.

Refresh `sdk-introspection.json` to record 1.17 `chat_stream`. The
2026-07-10 capture omitted it; that is not evidence that `stream()` is
new.

`event_fidelity` stays `message_only`. Continuity, identity kind
`response`, and close-deletes-stored-completions stay unchanged.

### 7. No new user options

No `stream=true` flag, no output-format override, no opt-in for live
flushes. Stream when the provider can; fall back when it cannot.

### 8. Out of scope

- ACP (`grok agent stdio`) and any bidirectional control channel
- `interrupt`, `tool_gate`, `resume` (#20)
- Enabling xAI server-side or client-side tools on `xai_sdk`
- Permission-prompt log enrichment (#40)
- Switching the CLI to `streaming-messages-json`
- Event-log mutation / growing one message in place
- Changing TUI rendering to concatenate sibling messages
- `xai-sdk` 1.18 pin bump and protobuf-7 gate retirement

## Compatibility matrix

| Provider surface | Live progress | Full answer | Terminals |
| --- | --- | --- | --- |
| Grok ≤0.2.115 `thought`/`text`/`end` (`EndTurn`) | chunked `text` + coarse thought heartbeat | last chunk `raw.final` + `raw.full_text` | existing #56 mapping |
| Grok ≥0.2.116 / 1.0.4 with tools + `usage` | above + tool events (Stage B) | same | same; `usage` is not terminal |
| Grok that rejects `streaming-json` | start still succeeds if `grok` exists | n/a | **turn/parser** fail-closed, not probe |
| `xai-sdk` 1.17 with `stream()` | chunked `content` | last chunk `raw.full_text` from accumulated `response.content` | existing finish-reason map |
| Fake / older SDK without `stream()` | today's single message | that message marked `final` | same |
| `xai-sdk` 1.18 installed against the 1.17 pin | not this task | not this task | series drift already revokes `no_local_effects` |

## Staged implementation

Re-verify installed `grok --help` / `grok --version` and `xai-sdk`
introspection in the same change as new fixtures.

### Stage A — CLI live text (no tool-fidelity claim)

This stage makes **answer prose** live. It does not fix a think+tools
review that emits no `text` for many minutes.

1. Change **only** `XaiStreamingParser` for flush, coarse thought
   heartbeat, `raw.final`, and `raw.full_text`.
2. Teach `_find_turn_answer` and `_derive_restored_answers` the
   `raw.full_text` preference in decision 3. Add hermetic referee and
   restore tests (Codex-style `final` without `full_text` still harvests
   `event.text`).
3. Keep 0.2.93 fixtures passing via `parse_xai_line`. Also drive
   `XaiStreamingParser` over `streaming-json-reasoning.ndjson` and
   `streaming-json-tooluse.ndjson`: still `EndTurn`, still no
   `source="tool"`.
4. Add hermetic tests for: 200-char / semantic flush + last-chunk
   `raw.full_text`; two short deltas still one message at `end`;
   single-chunk no extra event; thought-only + `EndTurn` completes with
   no harvested message; EOF `finish()`; unknown types ignored and
   non-terminal (including synthetic `usage` before mapping); `usage` is
   not a terminal; existing cancel / incomplete / `end_turn` / `EndTurn`
   / protocol-conflict outcomes unchanged; invalid JSON still
   `ValueError`.
5. MCP fragment sentence + initialize instructions (decision 5).
6. Update `xai_cli` README: live chunks, coarse thought, still
   `message_first` until Stage B.

### Stage B — CLI tool rows (Codex-like display)

1. Map documented 1.0.4 `tool_call` / `tool_call_update` in
   `XaiStreamingParser` per decision 4b. Hermetic fixtures may be
   documented-shape (from the Grok user-guide example), clearly labeled
   as not a live capture.
2. Recapture a disposable-workspace tool turn and a reasoning turn on
   current Grok (redact ids/prose; keep field names, `kind` values, and
   record boundaries). Adjust the kind table if live `kind` tokens
   differ. Keep the 0.2.93 tool-use fixture as "old CLI, no typed
   action" — `XaiStreamingParser` over that file still emits no
   `source="tool"`.
3. Tests: read → `tool_call`; execute → `command`; edit → `file_change`;
   unknown kind → `tool_call`; documented update with no title still
   renders merged start fields + ` · completed`; `failed` close row;
   `pending` then `in_progress` does not double-open when the first
   record already has args; identical-status updates coalesced; no
   invented `diff`/`patch` on `raw`; 0.2.93 fixture still has zero
   `source="tool"`.
4. Flip `event_fidelity` to `typed` only after the live capture matches
   the kind table. Otherwise stay `message_first`. Mapping itself does
   not wait on credentials.
5. Document the optional
   `types=["message","error","tool_call","command","file_change"]` watch
   recipe (decision 5).

### Stage C — SDK `stream()` with `sample()` fallback

1. Introspect `chat.stream` on the Chat instance. Do not import
   `xai_sdk.tools`. Do not add forbidden string literals to `backend.py`.
2. Keep `XaiConversation.run() -> Response`. Add a FakeChat **with**
   `stream()` for chunked content / reasoning heartbeat / `full_text`;
   keep the existing FakeChat **without** `stream()` as the required
   fallback. Assert `chat.create` kwargs remain only audited keys.
3. Cancel still owns the chunk iterator; still one `reset()`; `close()`
   still deletes stored completions.
4. Unexpected `tool_calls` still `provider_output_invalid`.
5. Refresh `sdk-introspection.json` to record 1.17 `chat_stream`. Do not
   treat that as a pin change. Do not treat 1.18 as in-scope.

Stage A can land alone. Stage B **mapping** lands on documented-shape
hermetic fixtures. Only the **`typed` flip** waits on a live recapture
(no secrets, no real session ids). C is independent of B.

## Collision with other work

This branch also carries #20 (`sdk-session-control`). `PROTOCOL_VERSION`
is already 2 and `VALID_TYPES` already includes approval events. This
task must stay additive on the xAI parsers and the production stream
loop.

### Do not touch

- `agent_collab/sandbox/worker_codec.py` (`PROTOCOL_VERSION`, frames)
- `VALID_TYPES` / `LIVE_WAIT_STATUSES` (except using existing
  `message` / `status` / `tool_call`)
- `BackendCapabilities` on either xAI backend (`interrupt` / `tool_gate`
  / `resume` stay false)
- `_PersistentXaiConversation.close()` and `delete_stored_completion`
- `production_chat_kwargs`, `AUDITED_*`, `FORBIDDEN_*`,
  `AUDITED_PACKAGE_SERIES`
- `permission_mode` defaults and cancel copy (#40 is still open
  remediation; do not absorb it)
- `SUCCESS_STOP_REASONS` and friends (#56)
- `XaiConversation` Protocol shape: keep `async def run(self, prompt)`
  returning a response
- `parse_xai_line` thought/text/end/error behavior and the 0.2.93
  fixture tests that call it
- Config schema, `options.toml`, and `config_migrations/`
- `mcp-guidance.md` Watch/delegate and `mcp_tools.py` initialize /
  `wait_events` descriptions beyond one additive fragment sentence
  (#20 owns those strings)

The allowed referee/daemon edit is **only** the `raw.full_text`
preference in decision 3.

`xai_sdk` never takes the worker path (`no_local_effects`). Stage C does
not need interrupt hooks. If both tasks land together, review the
combined diff for parser/runner conflicts only.

## Verification

Hermetic (required for each landed stage):

```bash
python3 -m unittest tests.backends.xai_cli.test_backend \
  tests.backends.xai_sdk.test_backend
```

Also keep green, because they pin terminals and fixture-backed runners:

```bash
python3 -m unittest tests.test_runners tests.backends.xai_cli.test_sandbox
```

After Stage A’s harvest extension:

```bash
python3 -m unittest tests.test_daemon tests.backends.codex_sdk.test_backend
```

(the Codex final-marker tests plus restore-ledger tests must still
harvest `event.text` when `full_text` is absent).

After Stage C, always run the sandbox audit — it will fail if anyone
adds `include` or `xai_sdk.tools` to production source:

```bash
python3 -m unittest tests.backends.xai_sdk.test_sandbox
```

Credentialed (operator, not the ordinary gate):

```bash
./agent_collab_dev.sh integration-test xai_cli --strict
./agent_collab_dev.sh integration-test xai_sdk --strict
```

Live CLI check, when authorized: a turn that streams answer prose must
emit at least one `message` before `end`; `wait_result.answers[].text`
must equal the full assistant prose, not the last chunk; TUI/markdown
must not contain a second copy of that full prose. A think+tools review
is allowed to stay quiet on `message` until Stage B if Grok emits no
`text` yet — do not treat that as a Stage A failure.

Do not run the complete `./agent_collab_dev.sh test` suite inside a nested
agent-harness sandbox.

## Documentation to update when implementing

- `agent_collab/backends/xai_cli/README.md`
- `agent_collab/backends/xai_sdk/README.md`
- `agent_collab/mcp-guidance.md` (fragments vs findings; optional
  `tool_call` watch)
- initialize `instructions` in `agent_collab/mcp_tools.py`
- `doc/implementation-notes.md` (xAI streaming paragraph)
- `tests/fixtures/xai/README.md` (new captures)
- `CHANGELOG.md` with the issue number

No config schema migration. No new `options.toml` fields.

## Open questions

1. **Whether to dim `type=="status"` in the TUI.** Source-agnostic and
   small, but it is TUI work and out of the default Stage A slice. The
   coarse `thinking…` heartbeat must be readable even without it.
2. **Whether Stage B can claim `typed`.** Only after a current-Grok
   capture confirms the kind table. Documented-shape fixtures are enough
   to implement the mapping; they are not enough to advertise `typed`.
3. **1.18 pin (follow-up, not this task).** Separate audit: protobuf 7
   gate, `AUDITED_PACKAGE_SERIES`, whether `xhigh` / ChatModel aliases
   need SDK-side mapping beyond today's string `reasoning_effort`.
   1.17 `ReasoningEffort` is `none|low|medium|high` only.

## Decisions already settled

- Default format remains `streaming-json`. No
  `streaming-messages-json`, no `--include-partial-messages`.
- No `xai-sdk` 1.18 bump here. No provider tools. No `include` on
  `chat.create`.
- Append-only events. No in-place rewrite, no growing snapshots, no TUI
  sibling-concat, no second full-text `message`.
- Last chunk carries `raw.final` + `raw.full_text`; harvest may read
  that one field. Sibling messages are never concatenated.
- Default thought is a coarse heartbeat, not full CoT. Thought is never
  `type="message"`.
- Flush on ~200 characters + semantic boundary, not on every token
  delta.
- Stage A = live answer text. Stage B = Codex-like tool rows from
  documented `tool_call` records (kind table + live recapture before
  claiming `typed`). Stage C = SDK `stream()` inside existing `run()`,
  still no provider tools.
- Older CLI/SDK degrade; they do not error just because new types or
  `stream()` are absent.
- `parse_xai_line` stays the 0.2.93 fixture mapper. Invalid JSON still
  fails closed only on the runner parser.
- #20 capabilities stay false. #40 and #56 are not reopened.
- No new user options and no config migration.

## Review adjudication (2026-08-16, accuracy/safety pass)

A second read-only review (verdict: needs-fix) found: (1) the kind table
is ACP-inferred; only `read` is in Grok’s user guide; (2) harvest that
requires `raw.final` fails when `end` arrives after a mid-turn flush
emptied the buffer — last fragment would become the answer; (3) Stage C
must not also emit `iter_xai_response_events`; (4) MCP initialize/watch
strings are #20-owned. Decision 3 now puts running `full_text` on every
chunk and reads it without requiring `final`. Decision 5 no longer
rewrites MCP blobs. Purpose no longer claims identical Codex streams.

## Review adjudication (2026-08-16, tool-display pass)

A later read-only review of decision 4b (Codex-like tool rows) called the
design right but the spec incomplete: documented `tool_call_update`
omits `title`/`kind`/`rawInput`; the TUI only shows `event.text`;
`types=["message","error","tool_call"]` hides `command`/`file_change`.
The coalesce-and-merge rules, `event.text` formula, optional watch list,
and “mapping vs `typed` flip” split above are that review’s required
edits. Verdict was good-but-gaps; those gaps are now closed in the
plan.

## Review adjudication (2026-08-16, first pass)

Four read-only reviews (facts, live UX, compatibility, harvest/TUI)
agreed on the spine and rejected a trailing full-text `message` as a
TUI/archive/digest reprint. Compatibility asked not to change
`_find_turn_answer` at all; live UX required a harvest path that is not
`event.text` of a tail chunk. The compromise is the single optional
`raw.full_text` field on a `raw.final` message — backward compatible for
every backend that does not set it, and the only referee edit this task
is allowed. Full always-on thought was rejected because the TUI styles
by source. An 8-delta flush was demoted because it would spin MCP watch
loops. Stage A was narrowed so it is not sold as the fix for the
measured 12.5-minute think+tools silence.
