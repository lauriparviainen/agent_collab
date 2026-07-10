# Stage 5.4: Daemon robustness and code health

**Status:** Open. H1-H3 are resolved and verified; H4-H6, M1-M5, and the
low-priority code-health items remain open. Findings originated in a full-repo
review at v0.2 (2026-07-10).

## Purpose

Fix concrete robustness, contract-integrity, and code-health defects found in
review, plus close the tooling gap (no CI, no lint/type config). This stage
adds no new features. Items already tracked by
[stage-5-hardening.md](stage-5-hardening.md) (auth options, workdir allowlist,
log pruning) and [sdk-session-control.md](sdk-session-control.md) (resume,
interrupt, tool gate) are out of scope here.

## High priority

### H1. Cap HTTP request bodies and headers

**Resolved (2026-07-11).** The HTTP server now enforces a fixed 16 MiB
(`16 * 1024 * 1024` bytes) request-body limit before calling `readexactly`,
bounding the daemon's raw body allocation per connection while leaving ample
room for large prompts, MCP JSON-RPC requests, logs, and source excerpts.

- Oversized bodies receive a structured `413 Payload Too Large` response before
  any body bytes are read.
- `Content-Length` accepts only non-negative ASCII decimal digits. Empty,
  signed, fractional, comma-joined, and otherwise malformed values receive a
  structured `400` response.
- Duplicate `Content-Length` headers are rejected with `400` instead of being
  silently overwritten.
- Extremely long numeric lengths are compared as normalized decimal strings,
  avoiding conversion of attacker-controlled thousands-digit integers.
- A body shorter than its declared length receives a structured `400 incomplete
  request body` response instead of falling through to a 500.
- Request headers are independently bounded to 100 fields and 64 KiB total,
  with a structured `431` response, so many small headers cannot bypass the
  body-memory bound.
- Header names reject whitespace before the colon and other invalid token
  characters. Unsupported `Transfer-Encoding` framing is rejected explicitly
  instead of being treated as an empty body.

Tests cover the configured 16 MiB value, the exact boundary, immediate
oversized rejection, huge numeric lengths, malformed and duplicate headers,
incomplete bodies, header count/byte ceilings, invalid header whitespace,
unsupported transfer encoding, overlong individual request/header lines, and
full structured 413/431 wire responses. The full hermetic suite passes (517
tests), and `./agent_collab.sh setup --check` passes. Iterative Antigravity
reviews using Gemini 3.1 Pro first identified the aggregate-header allocation
gap, then confirmed its remediation and the final overlong-line coverage with
an explicit ship-ready verdict.

### H2. Move blocking work off the event loop

**Resolved and independently reviewed (2026-07-10).** The original defect was
that config loading, fresh backend health probes, and transcript/event file I/O
ran synchronously on the daemon's single asyncio event loop. One slow operation
could therefore freeze unrelated clients, including `watch` long-polls.

Implemented:

- `SessionManager.start_session` runs `_prepare_session_start` through
  `asyncio.to_thread`, covering config load, option normalization, and fresh
  backend health probes before session state is created.
- `describe_options_async`, `read_events_async`, `_load_restored_events`, and
  `read_transcript_async` keep config and file I/O off the event loop.
- `wait_events` uses the async event-read/projection path for its final result.
- HTTP and in-daemon MCP adapters both call the same async manager methods.
- Event-loop-owned session state and event lists are snapshotted before
  projection work crosses into a worker thread; concurrent restored-event loads
  do not overwrite a cache populated by another reader.

Verification:

- `test_slow_start_probe_does_not_block_sessions_read` proves a slow fresh
  health probe does not block a concurrent `/sessions` read.
- `test_restored_event_and_transcript_reads_do_not_block_loop` proves slow JSONL
  event and transcript reads leave the event loop responsive.
- The latest full hermetic suite passes (517 tests), and the
  `./agent_collab.sh setup --check` validation passes.
- A follow-up Antigravity review using Claude Opus 4.6 Thinking found no
  blocking defects and marked the change ready to land.

### H3. Supervisor PID-reuse and double-start races

**Resolved (2026-07-10).** The supervisor now persists a process identity using
the OS process start time plus argv/command and verifies it before treating a
live PID as the daemon. Identity handling distinguishes a match, a definite
mismatch, and unavailable evidence:

- `daemon stop` refuses to signal an unverifiable PID and preserves state for a
  safe retry; it removes stale state without signaling on a definite mismatch.
- Identity is checked before `SIGTERM`, throughout the grace period, and again
  before `SIGKILL`, so a PID recycled during shutdown is not escalated.
- `daemon status` does not report a recycled PID as the daemon, and `daemon
  start` refuses to start a second daemon when a live PID cannot be safely
  classified.
- A private `start.lock` uses a non-blocking exclusive `flock` across the full
  check, spawn, readiness, and state-write transaction. The lock file is kept
  in place so concurrent callers always contend on the same inode.

Verification covers lock contention, exact and legacy-argv identity matching,
refusal to signal a recycled PID, preservation under unavailable identity
evidence, and the identity recheck before `SIGKILL`. The latest full hermetic
suite passes (517 tests), and `./agent_collab.sh setup --check` passes. Two
iterative Antigravity reviews using Claude Opus 4.6 Thinking found no blocking
issues; the final review confirmed the cross-source identity fallback and
marked H3 ship-ready for a cautious maintainer.

### H4. Stop leaking internal exception text to clients

The top-level handler returns `{"error": str(exc)}` with status 500
(`server_http.py:99-101`), exposing internal paths and error strings. Log the
detail server-side; return a generic message.

### H5. Formalize informal backend gating attributes

`options.py` reads `block_on_unavailable`, `checks_credentials`,
`event_fidelity`, and `provider_session_id_kind` via `getattr(..., default)`
(`options.py:403-404`, `options.py:605-610`, `options.py:722-724`), but none
are declared in the `AgentBackend` protocol (`backends/base.py`) or checked by
`_validate_backend_contract` (`backends/__init__.py`). A backend that omits
`block_on_unavailable` silently defaults to not gated.

- Declare these attributes in the protocol.
- Validate them at registration so omission is a startup error, not a silent
  policy change.

### H6. Close SDK streams on cancellation consistently

`codex_sdk` and `xai_sdk` close their streams in `finally`; `claude_sdk`
(`backends/claude_sdk/backend.py:144-176`) and `antigravity_sdk` do not. A
mid-turn stop unwinds the SDK connection nondeterministically. This is also
groundwork for the interrupt semantics in
[sdk-session-control.md](sdk-session-control.md).

## Medium priority

### M1. Capture `provider_session_id` uniformly in CLI backends

Only `xai_cli` emits the uniform provider-session bookkeeping event.
`claude_cli/parser.py:86-88` extracts the record containing `session_id` and
discards it; `codex_cli` and `antigravity_cli` never capture one. Capture what
each CLI already prints, using the shared `provider_session_event` helper.

### M2. Add CI and static tooling

There is no `.github/workflows`, no ruff/mypy/pyright configuration.

- Add a minimal CI job: `python -m unittest discover -s tests -t .`,
  `./agent_collab.sh setup --check`, and a linter.
- Add ruff (lint + format check) config to `pyproject.toml`; adopt a type
  checker incrementally if desired.

### M3. Even out backend test coverage

`claude_cli`, `codex_cli`, and `antigravity_cli` have one hermetic test each
(manifest only), versus 11-27 for the SDK suites and 18 for `xai_cli`.
`build_command`/`normalize_options` for the three most-used CLI backends is
essentially untested. Also uncovered in `runners.py`: the
command-not-found error event (`runners.py:134-136`), non-oversized stderr
error emission, and `_is_noisy_stderr` filtering.

### M4. `Event.create` silently relabels invalid inputs

`events.py:36-40` maps an unknown `source` to `"error"` and unknown `type` to
`"status"` with no logging, masking backend normalization bugs. Log (or fail
loudly in tests) when coercion happens.

### M5. Stderr misattribution for renamed agents

`runners.py:338-339` attributes provider stderr by runner *name*, but
`SubprocessRunner` is constructed with `agent.id`. A Claude agent with id
`reviewer` gets stderr attributed to `"tool"`. Attribute by provider type, not
display id.

## Low priority (code health)

- Deduplicate `_canonical_reasoning` (copied verbatim in `xai_cli/backend.py`
  and `xai_sdk/backend.py`) into `backends/common/options.py`.
- Deduplicate the SDK `settings_summary` boilerplate (4 copies) and the CLI
  `create_runner`/`command_preview` boilerplate (4 copies) into
  `backends/common/` helpers.
- Define loopback detection once: `client.py:238-247` and
  `server_http.py:346-361` independently implement the same trust-boundary
  check.
- Add a shared helper for the `BackendUnavailable` error event repeated in all
  four SDK runners.
- `_schedule_notify` (`daemon.py:567-574`) spawns one asyncio task per recorded
  event; coalesce notifications.
- Make the supervisor readiness timeout (`daemon_supervisor.py:234`, fixed
  3.0s) configurable for slow cold starts.
- Comment the intentional asymmetry that `/health` bypasses auth while the
  supervisor readiness probe uses authenticated `/sessions`, so a refactor does
  not lose the token check.
- Consider `[project.optional-dependencies]` per provider SDK (with an `all`
  extra) in `pyproject.toml` so the default install does not pull all four
  vendor SDKs; weigh against the current first-class-SDK install story.

## Acceptance criteria

- Oversized and malformed request bodies, headers, and framing get structured
  4xx responses; the daemon's request memory is bounded per connection.
- No health probe, config load, or event/transcript read blocks concurrent
  daemon requests.
- `daemon stop` never signals a PID it cannot attribute to the daemon; double
  `daemon start` cannot race two daemons onto one port.
- 500 responses carry no internal exception text.
- Backend registration fails loudly when a contract attribute is missing.
- Every SDK runner closes its stream on cancellation.
- CI runs hermetic tests, `setup --check`, and lint on every push.
- New tests cover each fixed defect; existing CLI, log, and MCP workflows keep
  working.
