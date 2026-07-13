# Stage 5.4: Daemon robustness and code health

**Status:** Closed (2026-07-12). All high (H1-H6) and medium (M1-M5) items are
resolved and verified, and every low-priority code-health item is resolved
except per-provider optional dependencies, which is explicitly deferred to
issue #12. Findings originated in a full-repo review at v0.2 (2026-07-10).

**Issue:** [#6](https://github.com/lauriparviainen/agent_collab/issues/6)
(milestone 0.4.1)

**Release gate:** this stage gates the **0.4.1** patch release (0.3.0 shipped
2026-07-11 and 0.4.0 — the permanent daemon token — shipped 2026-07-12; the
remaining scope here is fixes and code health only). When this document's
acceptance criteria hold and it closes to `doc/tasks_closed/`, version 0.4.1
is bumped, tagged, and published following `.claude/skills/release/SKILL.md`.
It becomes 0.5.0 instead only if a behavior or packaging change (for example
per-provider optional dependencies) lands in this stage.

## Purpose

Fix concrete robustness, contract-integrity, and code-health defects found in
review, plus close the tooling gap (no CI, no lint/type config). This stage
adds no new features. Items already tracked by
[stage-5-hardening.md](../tasks_open/stage-5-hardening.md) (auth options, workdir allowlist,
log pruning) and [sdk-session-control.md](../tasks_open/sdk-session-control.md) (resume,
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
tests), and `./agent_collab_dev.sh build --check` passes. Iterative Antigravity
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
  `./agent_collab_dev.sh build --check` validation passes.
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
suite passes (517 tests), and `./agent_collab_dev.sh build --check` passes. Two
iterative Antigravity reviews using Claude Opus 4.6 Thinking found no blocking
issues; the final review confirmed the cross-source identity fallback and
marked H3 ship-ready for a cautious maintainer.

### H4. Stop leaking internal exception text to clients

**Resolved (2026-07-11).** Unexpected failures now keep their exception type
and detail in daemon diagnostics while returning only `internal server error`
to clients.

- REST requests receive a structured HTTP 500 with the generic message.
- HTTP MCP requests receive HTTP 500 with a JSON-RPC `-32603` error envelope
  carrying the original request id and the same generic message.
- Stdio MCP requests likewise retain the original JSON-RPC id, emit `-32603`,
  and log the unexpected exception to stderr. Malformed JSON remains a
  protocol-level `-32700` parse error.
- Explicit `SessionNotFoundError`, `SessionRequestError`, and `McpToolError`
  types separate intentional 404, 400, and MCP tool-validation failures from
  arbitrary `KeyError` and `ValueError` bugs. `StartOptionsError` and typed
  `ClientError` payload contracts remain intact.
- Unexpected MCP tool failures are allowed to reach the HTTP or stdio
  transport boundary instead of being converted to detailed tool content.

Regression coverage exercises complete HTTP bytes for REST and MCP
`RuntimeError`, `ValueError`, and `KeyError` failures, asserts that sensitive
paths are absent from the wire but present in server diagnostics, verifies the
intentional 400/404 contracts, proves HTTP MCP preserves its JSON-RPC id and
error envelope, and directly verifies stdio MCP stdout/stderr behavior. Focused
HTTP/MCP/daemon tests pass, the full hermetic suite passes (523 tests), and
`./agent_collab_dev.sh build --check` passes.

Five iterative read-only Antigravity reviews used Gemini 3.1 Pro (High). The
first two found the broad built-in-exception and MCP tool-content leaks; a later
review caught missing JSON-RPC ids/envelopes after stdio coverage was added.
All findings and the worthwhile stdio test gap were addressed. The final
review verdict is explicit: **SHIP-READY / NO BLOCKERS**.

### H5. Formalize informal backend gating attributes

**Resolved (2026-07-11).** Backend gating and fidelity metadata are now a
required part of the `AgentBackend` contract rather than optional runtime
conventions.

- The protocol declares `block_on_unavailable` and `checks_credentials` as
  booleans, `event_fidelity` as a string, and `provider_session_id_kind` as an
  optional string.
- Registration rejects a missing or non-boolean gating flag, a missing or
  empty/non-string fidelity value, and a missing or invalid provider-session
  kind. An explicitly declared `None` provider-session kind remains valid.
- Validation completes before `_REGISTRY` is mutated, so rejected backends do
  not leave partial entries behind.
- Start gating and option discovery use direct attribute access. The former
  false/`unknown`/`None` `getattr` fallbacks were removed, so omission can no
  longer silently weaken preflight or invent public metadata.
- Built-in backends already supplied valid values; extension and gating test
  doubles were updated to satisfy the same production contract.

Regression tests remove each required attribute in turn and verify registration
fails, cover invalid boolean/string values (including integers masquerading as
booleans), and clean up registry state defensively. The focused backend
contract/config/gating suite passes (91 tests), the full hermetic suite passes
(525 tests), and `./agent_collab_dev.sh build --check` passes. A read-only
Antigravity review using Gemini 3.1 Pro (High) found no blocking or substantive
issues and gave the explicit verdict **SHIP-READY / NO BLOCKERS**.

### H6. Close SDK streams on cancellation consistently

**Resolved (2026-07-11).** All four SDK runners now close their turn stream or
response deterministically from `finally` when consumption completes, fails, or
is cancelled.

- Claude explicitly closes its message iterator; Antigravity closes a
  close-capable `ChatResponse` before leaving the owning agent context. Codex
  and xAI use the same shared cleanup path.
- `close_async_stream` treats `aclose()` as optional and best-effort. Ordinary
  close failures cannot replace a primary SDK error or an in-flight
  `CancelledError`; a new cancellation raised while awaiting close remains a
  `BaseException` and propagates.
- Antigravity retains its existing agent context manager and now guarantees
  child-response cleanup before parent-agent `__aexit__` cleanup.

Each SDK backend has a cancellation/concurrency regression that blocks inside
its async iterator or response, cancels the consumer task, and verifies prompt
closure plus `CancelledError` propagation. Every test runs both successful and
failing `aclose()` variants; the Antigravity test additionally records and
asserts exact `response_closed` then `agent_exited` ordering. These tests fail
without the runner-level cleanup because custom async iterators are not closed
automatically. The focused four-SDK suite passes (79 tests), the full hermetic
suite passes (529 tests), and `./agent_collab_dev.sh build --check` passes.

Four iterative read-only Antigravity reviews used Gemini 3.1 Pro (High). They
identified close-error cancellation masking and the need for explicit coverage
on every SDK backend; both findings and the cleanup-order proof gap were
addressed. The final review verdict is explicit: **SHIP-READY / NO BLOCKERS**.

## Medium priority

### M1. Capture `provider_session_id` uniformly in CLI backends

**Resolved and independently reviewed (2026-07-11).** CLI backends now capture
only identities proven by their current wire formats and emit the shared
provider-session bookkeeping event:

- Claude captures non-empty `session_id` values from `system` and `result`
  records. Its stateful parser emits each identity once while preserving a
  repeated record's separate verbose status event.
- Codex captures non-empty `thread_id` values only from `thread.started`.
- xAI's existing `end.sessionId` capture now uses the same shared helper.
- Antigravity CLI remains explicitly unset because its print-mode output has no
  machine-readable provider identity; prose resembling an id is not guessed.

Each runner binds the configured workflow `agent.id`, including renamed agents.
The original provider record remains available in `Event.raw`, while daemon
persistence, referee prompt filtering, and Claude deduplication consume a
trusted in-process identity marker instead of provider-controlled raw keys. The
marker is excluded from event serialization; the durable uniform identity is
stored in daemon-owned `SessionState.agent_sessions`. This prevents forged raw
keys from creating state, hiding prompt content, or poisoning identity
deduplication. Restored JSONL events serve read/watch APIs only and never enter
the live referee transcript; a future resume feature must reconstruct identity
from daemon-owned session state rather than raw transcript payloads.

Regression coverage includes real Claude/Codex/xAI record shapes, configured
agent attribution, Claude duplicate and verbose behavior, raw-key state and
prompt-filter spoofing, deduplication poisoning, marker serialization, and the
Antigravity no-invention contract. The focused backend/daemon/referee suite
passes (68 tests), the full hermetic suite passes (541 tests),
`./agent_collab_dev.sh build --check` passes, and `git diff --check` passes.

Four concurrent two-reviewer loops used Gemini 3.1 Pro (High) and the highest
Flash model advertised by local option discovery, Gemini 3.5 Flash (High)
(`Flash 4 High` was not advertised). The loops identified and verified
fixes for raw identity spoofing, raw-driven Claude deduplication, trusted-marker
test proof, and xAI configured-agent coverage. Both final reviewers inspected
the current persistence call graph and returned explicit **SHIP-READY / NO
BLOCKERS** verdicts. Three untracked scratch artifacts created during review
were inspected and removed.

### M2. Add CI and static tooling

**Resolved and independently reviewed (2026-07-11).** The repository now has a
least-privilege GitHub Actions CI workflow for every push and pull request,
matrixed across the supported floor, Python 3.10, and primary development
version, Python 3.12. Each matrix job runs Ruff lint,
Ruff format verification, the hermetic unit suite, and `build --check`.

- `actions/checkout` and `actions/setup-python` use immutable full commit SHAs;
  the workflow grants only `contents: read` and does not persist checkout
  credentials.
- Ruff 0.15.20 is pinned in the `dev` optional dependency and the CI install.
  `pyproject.toml` defines a Python 3.10 target, 100-column formatting, LF line
  endings, and the stable `E4`, `E7`, `E9`, and `F` lint baseline.
- The existing Python source and tests were mechanically formatted once to
  establish a repository-wide Ruff format baseline. Small lint-only cleanups
  remove unused bindings/imports, preserve intentional backend-contract
  re-exports explicitly, and replace assigned test lambdas with named helpers.
- `tests/test_ci_tooling.py` fails if the Ruff pin/configuration, supported
  Python matrix, required CI gates, least-privilege settings, or immutable
  action pinning drift or disappear. Every action reference is checked without
  constraining future workflows to an exact action count.
- `./agent_collab_dev.sh test` runs Ruff lint and format verification before unit
  discovery, so the standard local test command enforces the same static gates;
  shell-wrapper regression coverage proves the checks run first and in order.

Focused CI/tooling, setup, and shell-wrapper coverage passes (14 tests). Ruff
lint and format checks pass across all 118 discovered files. The full hermetic
suite passes (543 tests), `./agent_collab_dev.sh build --check` passes, and
`git diff --check` passes.

Three concurrent two-reviewer loops used Gemini 3.1 Pro (High) and Gemini 3.5
Flash (High), the highest Flash option advertised by local discovery because
`Flash 4 High` was not available. The first loop identified a brittle exact
action-count assertion and the missing supported-Python matrix; both were
addressed. One initial claim that Ruff 0.15.20 did not exist was rejected after
direct installation and execution proved the published pin. The second loop
independently reran Ruff, the full suite, and setup validation; both reviewers
returned the explicit verdict **SHIP-READY / NO BLOCKERS**. After the local
test wrapper gained automatic Ruff gates, a third loop verified failure
propagation, argument forwarding, portability, and the command-order regression;
both reviewers again returned **SHIP-READY / NO BLOCKERS**.

### M3. Even out backend test coverage

**Resolved and independently reviewed (2026-07-11).** Current diagnosis found
that M1 had added identity coverage since the original test counts were
recorded, but command normalization remained shallowly tested. The missing
coverage exposed and now fixes two effective-option defects:

- Backend option precedence is now manifest defaults, configured CLI-argument
  inference, backend-specific configured options, then explicit request data.
  Previously manifest defaults overwrote inferred CLI flags, so a configured
  non-default model, mode, or thinking level could be silently replaced.
- Repeated `--flag value`, `--flag=value`, `-c key=value`, `--config key=value`,
  and `--config=key=value` occurrences use the last effective value instead of
  the first. Command construction still removes all owned stale occurrences
  and emits one canonical value while preserving unrelated config entries.
- Validation applies to the final effective merge. An invalid lower-precedence
  CLI value is rejected when effective, but may be repaired by a valid
  configured or requested override that command construction will actually
  substitute.
- Claude thinking-level/token-budget and Codex thinking/reasoning aliases use
  the highest-precedence layer that selected either field. Same-layer conflicts
  fail, while a higher layer cleanly replaces its lower-layer counterpart.

The Claude, Codex, and Antigravity CLI suites now cover inference, precedence,
last-occurrence behavior, invalid effective values, exact command rewriting,
prompt/add-directory placement, command previews, configured cwd/env, renamed
runner identity, and missing-command configuration failures. Shared CLI tests
exercise both flag syntaxes and all Codex config syntaxes.

Wire-level subprocess tests execute real child processes to prove that a
missing executable returns a structured command-not-found error, ordinary
non-oversized stderr becomes an error event without becoming a transport
failure, and known noisy stderr is suppressed normally but emitted as
provider-status events in verbose mode. The focused CLI/runner suite passes (37
tests), the full hermetic suite passes (561 tests), Ruff lint and format checks
pass across 118 files, `./agent_collab_dev.sh build --check` passes, and
`git diff --check` passes.

Two concurrent two-reviewer loops used Gemini 3.1 Pro (High) and Gemini 3.5
Flash (High), the highest Flash model advertised because `Flash 4 High` was not
available. The first loop independently inspected the merge and transport
paths, reran the full suite, distinguished the still-open M5 renamed-stderr
finding from M3, and returned **SHIP-READY / NO BLOCKERS**. A subsequent
parallel full run exposed a PATH-dependent `PermissionError` in the bare-name
missing-command fixture; it now uses a guaranteed-missing absolute path. The
second loop verified that portability fix and the resource-efficient Python
3.10/3.12 CI endpoints; both reviewers again returned **SHIP-READY / NO
BLOCKERS**.

### M4. `Event.create` silently relabels invalid inputs

**Resolved (2026-07-12).** `Event.create` keeps coercing an unknown `source`
to `"error"` and an unknown `type` to `"status"` so malformed backend output
cannot crash a live session, but the coercion is no longer silent: each one
logs a warning through `logging.getLogger("agent_collab.events")` carrying the
original pre-coercion source and type. Warnings are deduplicated per distinct
`(field, value)` pair so a misbehaving backend cannot flood the daemon log; the
dedup set is cleared at a small cap (64) so memory stays bounded even with
dynamically generated invalid values, at the cost of occasional re-logging.

Regression tests (`tests/test_events.py`) cover source and type coercion
warnings, the both-invalid case logging original values, once-per-value
deduplication, the bounded cap continuing to warn past 64 distinct values, and
valid inputs logging nothing.

### M5. Stderr misattribution for renamed agents

**Resolved (2026-07-12).** `SubprocessRunner` now accepts an explicit `source`
validated against `PROVIDER_SOURCES` (an invalid explicit value raises
`ValueError`; omission keeps the legacy name-based fallback), and all four CLI
backends pass `source=self.agent_type`. Verbose noisy provider stderr from a
renamed agent (for example a Claude agent with id `reviewer`) is now attributed
to its provider type instead of `"tool"`. The non-verbose structured-error path
intentionally keeps source `"error"`: it never attributed by name and its
contract is pinned by `test_non_noisy_stderr_is_emitted_as_structured_error`.

Regression tests cover renamed-agent verbose stderr attribution and
construction-time rejection of an invalid source (`tests/test_runners.py`), and
a shared test proves every CLI backend threads its `agent_type` into the runner
for a renamed agent (`tests/backends/common/test_cli.py`).

Both fixes went through two read-only agent-collab review rounds using Gemini
3.1 Pro (High) in plan mode. The first round produced four findings: two were
fixed (the second coercion warning logging the already-coerced source, and the
unthrottled warning flood risk) and two were rejected with evidence (an
`assertNoLogs` Python 3.9 claim — the supported floor is 3.10 — and the
non-verbose stderr `"error"` source, which is the reviewed M3 contract). The
second round verified the fixes and both rejections and returned the explicit
verdict **SHIP-READY / NO BLOCKERS**. The full hermetic suite passes (613
tests) including Ruff lint/format gates, and `./agent_collab_dev.sh build --check`
passes.

### Related fix landed in this stage: installed daemons could not serve MCP guidance

**Resolved (2026-07-12).** `agent_collab_guidance` failed with
`RuntimeError('MCP guidance document is unavailable')` on installed daemons
because `mcp_tools.py` resolved the guidance document at the repo-relative
`doc/mcp-guidance.md`, which does not exist under site-packages. The document
now lives at `agent_collab/mcp-guidance.md`, is declared as package data in
`pyproject.toml`, and is resolved with `Path(__file__).with_name(...)` (the
same convention as `default_config.toml`); documentation links were updated. A
locally built wheel was verified to contain the file. Regression tests pin the
path inside the package (`tests/test_mcp_server.py`) and the package-data
declaration (`tests/test_ci_tooling.py`). Installed daemons pick the fix up on
the next reinstall and restart.

## Low priority (code health)

**Resolved (2026-07-12)** except the final item, which is deferred:

- `_canonical_reasoning` now lives once in `backends/common/options.py` as
  `canonical_reasoning`; the verbatim copies in `xai_cli` and `xai_sdk` are
  gone.
- The SDK `settings_summary` boilerplate (4 copies) collapsed into
  `backends/common/sdk.py:sdk_settings_summary`, and the CLI
  `create_runner`/`command_preview`/`settings_summary` boilerplate into
  `backends/common/cli.py:create_cli_runner`/`cli_command_preview`/
  `cli_settings_summary`. The shared runner constructor also centralizes the
  M5 `source=agent_type` threading. Antigravity keeps its own run-dir-dependent
  `command_preview` by design.
- Loopback trust detection is defined once in `agent_collab/net.py`
  (`is_loopback_host`/`is_loopback_url`); the client token decision and the
  daemon's MCP Origin validation both consume it. DNS names other than
  `localhost` are never trusted. Unit tests pin localhost/literal-loopback
  acceptance and resolver-independent rejection.
- The repeated SDK `BackendUnavailable` error event collapsed into
  `backends/common/sdk.py:backend_unavailable_event` (6 call sites).
- `_schedule_notify` coalesces: a burst of recorded events schedules one
  pending watcher notification instead of one asyncio task per event. Safe
  because events are appended before scheduling and watchers re-check their
  cursor under the condition; a regression test proves one task per burst,
  watcher wake-up, and re-arming after delivery.
- The supervisor readiness timeout is configurable via
  `AGENT_COLLAB_DAEMON_READY_TIMEOUT` (seconds, default 3.0). Invalid or
  non-positive values (including NaN) fail loudly; documented in
  `doc/runtime-layout.md` and covered by tests.
- The intentional `/health`-bypasses-auth asymmetry is now commented at the
  auth check in `server_http.py`, warning refactors not to downgrade the
  supervisor's authenticated `/sessions` readiness probe.
- Per-provider `[project.optional-dependencies]` is **deferred to issue #12**:
  it is a packaging behavior change that would re-gate this release to 0.5.0
  and deserves its own design pass (installer, autostart venv, health-probe
  presentation, CI matrix).

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
- CI runs hermetic tests, `build --check`, and lint on every push.
- New tests cover each fixed defect; existing CLI, log, and MCP workflows keep
  working.
