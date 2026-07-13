# Session retention and pruning

**Status:** Closed 2026-07-12. All seven stages implemented and verified; each
stage was Gemini-reviewed to an explicit SHIP-READY verdict before its commit,
and the daemon flow was smoke-tested end to end in an isolated
`AGENT_COLLAB_HOME` (manual preview/apply over the CLI, disabled-retention
error path, and the startup scheduler run pruning an expired session).

**Created:** 2026-07-12

**Design verified:** 2026-07-12 (post stage 5.4, v0.5.0)

**Issue:** [#5](https://github.com/lauriparviainen/agent_collab/issues/5)

## Context

The global daemon persists one session record per session in
`data/session-index.json` and normally writes JSONL and Markdown transcripts
under `data/sessions/`. Neither the index nor the transcript directory is
currently bounded. Long-running installations therefore accumulate private
transcript data and consume disk space indefinitely.

`runtime-layout.md` already calls out the unbounded index. The Stage 5
hardening plan originally proposed only manual `sessions prune` commands and
said never to delete logs by default. This focused task supersedes that part
of the older plan: completed sessions should have a visible, configurable
retention period, defaulting to 30 days.

Retention changes the auditability promise. The README and command help must
state the default plainly, show how to retain sessions longer, and show how to
disable automatic pruning before users rely on the daemon as a durable archive.

## Code-verified assumptions

The design below rests on these facts, checked against the current code:

- **The daemon is the single index writer.** `SessionIndex` is constructed
  only by `SessionManager` (`daemon.py`), and only
  `SessionManager._persist` writes it. The CLI and typed client never touch
  the file. `SessionIndex._write` already rewrites the whole file atomically
  via `os.replace`, so a bulk removal operation is a natural extension; today
  the class has only `load` and `upsert`.
- **Terminal statuses and timestamps.** `TERMINAL_STATUSES` is exactly
  `{done, failed, stopped, interrupted}`. `SessionManager._set_status` stamps
  `ended_at` (and `updated_at`) on every terminal transition, and the restore
  path stamps `interrupted` plus `ended_at` on any session that was live when
  the daemon died — so `interrupted` is a real, daemon-produced status today.
  Timestamps come from `events.utc_timestamp()` =
  `datetime.now(timezone.utc).isoformat()`, which `datetime.fromisoformat`
  parses on the Python 3.10 floor (no `Z` suffix is involved).
- **The manager is event-loop-confined.** There is no threading lock to
  share; session state lives on the daemon event loop and blocking work is
  pushed through `asyncio.to_thread`. "Serialize pruning" therefore means one
  `asyncio.Lock` around the prune operation, not a manager-wide lock.
- **There is no graceful shutdown hook.** `daemon stop` sends SIGTERM; the
  managed daemon maps it to `KeyboardInterrupt`, which unwinds
  `asyncio.run(...serve_forever())`. Pending tasks are cancelled abruptly.
  The retention task must simply tolerate `asyncio.CancelledError` at every
  await point; there is no place to run bounded shutdown cleanup, and the
  deletion procedure must be safe against a kill at any instant.
- **Missing transcripts are already a tolerated state.**
  `_load_events_from_jsonl` returns `[]` on any `OSError` and
  `_read_full_transcript` returns `""` for a missing file. An index record
  whose transcript files are gone degrades gracefully everywhere today. This
  is what makes convergent deletion (below) safe.
- **User-only config scoping has one mechanism.** `migrate_config_data`
  pops `[backends]` and `[daemon]` from project-scope files with a warning.
  The new `[sessions]` section is scoped the same way: ignored with a warning
  in project config, never rejected. The config schema is currently 4;
  the v3→v4 precedent (a purely additive section) also took a schema bump.
- **The API workflow is fixed.** A new route means: DTOs in
  `api_schema.py`, a `ROUTES` entry, a `_route_<handler>` method in
  `server_http.py`, a typed method in `client.py`, registering the new DTOs
  in `project_build.py`'s hardcoded `_MODELS` / `_REQUEST_MODELS` tuples
  (plus `_FIELD_SCHEMAS` entries for constrained fields) — schema generation
  does not discover models dynamically — and regenerating
  `doc/daemon_api_doc/{openapi.json,http-api.md}` with
  `./agent_collab_dev.sh build`. Contract tests pin routes to client methods
  (`SERVER_ONLY_ROUTES` is the only exemption list). `POST /sessions/prune`
  does not collide with any existing route template, and session IDs like
  `prune` cannot be created over the wire (`session_id` is not a wire field).
- **No daemon-global config load exists.** `load_config` is per-session
  workdir; the only startup config read is `ensure_daemon_token()` in
  `run_server`. Retention settings must be loaded once at daemon startup
  (built-in + user config; there is no project config in play at that point),
  mirroring the token pattern. Changes take effect on daemon restart.
- **Background-task pattern.** `SessionManager._notify_tasks` holds fire-and-
  forget tasks with a done-callback discard; watcher notifications were
  recently coalesced (`notify_pending`). Watchers cannot be blocked waiting
  on a prunable session: `wait_events` only blocks while the status is in
  `LIVE_WAIT_STATUSES`, and eligibility requires a terminal status. A reader
  that races a prune gets `SessionNotFoundError` → 404, which is acceptable.

## Goals

- Retain terminal sessions for 30 days by default.
- Let the user change or disable automatic retention in global user config.
- Provide a safe manual preview and prune command with age and keep-count
  overrides.
- Remove daemon-owned index records and managed transcripts without risking
  active sessions or unrelated files.
- Make cleanup idempotent and recoverable after partial failure or daemon
  restart.
- Keep the daemon responsive and healthy if cleanup fails.

## Non-goals

- Do not clean legacy project-local session directories automatically.
- Do not recursively delete custom or external log directories.
- Do not add transcript retention settings to project config.
- Do not add an MCP pruning tool in this task. Retention is an owner-operated
  administrative action, not an agent workflow action.
- Do not discover and delete arbitrary orphan files that have no index record.
- Do not compress or archive old sessions; pruning is deletion.
- Do not remove index records that fail state restoration
  (`_state_from_record` returning `None`). They are daemon-owned garbage, but
  deleting data the code cannot interpret is the wrong default; count and
  report them in prune results instead, and leave removal to a future task.

## Configuration

Add a `[sessions]` section with a config schema bump to 5:

```toml
[sessions]
retention_days = 30
cleanup_interval_hours = 24
```

Defaults live as typed dataclass defaults in `config.py` (a
`SessionsConfig` on `CollaborationConfig`), not in `default_config.toml` —
the same shape as `daemon_token`, which also has no TOML built-in. The TOML
section only overrides.

These are daemon-global settings. Only built-in and user config participate:
`migrate_config_data` strips a project-scope `[sessions]` section with a
warning, exactly as it does for `[backends]` and `[daemon]` today. The v4→v5
migration is a no-op stamp (the v3→v4 precedent), and
`agent-collab config show` prints the effective values.

Validation rules (reuse `_expect_int`, which already rejects booleans):

- `retention_days` is a non-negative integer; `0` disables automatic pruning;
- `cleanup_interval_hours` is a positive integer;
- unknown fields in `[sessions]` are rejected like other unknown fields.

The daemon reads these once at startup (built-in + user config, next to
`ensure_daemon_token()` in `run_server`); changing them takes effect after
daemon restart. Live config reload is out of scope. Manual
`sessions prune --older-than` works even when `retention_days = 0`.

## CLI design

Add `sessions` as a public command group (registered in `PUBLIC_COMMANDS` and
`_command_handlers()`, with a subparser group like `daemon`):

```text
agent-collab sessions prune
agent-collab sessions prune --dry-run
agent-collab sessions prune --apply
agent-collab sessions prune --older-than 7d --apply
agent-collab sessions prune --older-than 30d --keep 100 --apply
agent-collab sessions prune --json
```

The no-argument form previews candidates using configured retention. It must
not mutate data and should end with guidance to rerun using `--apply`.
`--dry-run` makes preview intent explicit and is mutually exclusive with
`--apply`. Preview works when `retention_days = 0` only with an explicit
`--older-than` (otherwise it reports that automatic retention is disabled).

`--older-than DURATION` overrides `retention_days` for that invocation. Accept
whole-number units `h`, `d`, and `w`; reject zero, negative, fractional,
unitless, or unknown-unit values. Rejecting zero is deliberate: "prune
everything terminal right now" should not be one typo away; `--older-than 1h`
is the explicit near-equivalent. `--keep N` protects the newest `N` terminal
sessions overall — at least `N` terminal sessions always survive a prune
regardless of age — ordered deterministically by the effective terminal
timestamp and then session ID (both descending for "newest"). Only protected
sessions that would otherwise be pruned are reported as kept. `N` must be a
non-negative integer.

The command operates through the authenticated daemon API. It must not mutate
the session index directly, because the daemon is the single index writer. If
the daemon is unavailable, return the normal actionable connection error
rather than falling back to unsafe filesystem mutation.

Human output summarizes: effective cutoff and keep count; one line per
candidate (session ID, status, effective timestamp, files to remove or
preserved-as-external); pruned counts, bytes reclaimed, files preserved,
failures, and skipped/unparseable records. `--json` prints the full typed
response (the existing `events --json` / `options --json` convention).

Preview and apply must use the same selection implementation. A preview is a
snapshot, not a deletion guarantee: a later apply recalculates eligibility so
it cannot act on a session that changed in between.

## Eligibility and age

Only these terminal statuses are eligible: `done`, `failed`, `stopped`,
`interrupted` (all four are producible today; excluding `interrupted` would
make crash-orphaned sessions immortal).

`running` and `awaiting_input` are never eligible, regardless of timestamps or
CLI arguments. The manager also rejects any record backed by a live
`asyncio.Task` (`managed.task is not None and not managed.task.done()`) as
defense in depth.

Use `ended_at` as the retention timestamp. For legacy terminal records without
`ended_at`, fall back to `updated_at`. If neither parses with
`datetime.fromisoformat`, preserve the record and report it as skipped;
guessing from file mtime or `created_at` could delete data prematurely.

A session is old enough when its effective timestamp is less than or equal to
the UTC cutoff. Inject the clock into selection and scheduling code so
boundary tests do not depend on wall-clock time.

## Filesystem ownership boundary

Automatic and manual pruning may unlink a transcript only when all of these
conditions hold:

1. its recorded path is exactly the expected global managed path
   `data/sessions/<session-id>.jsonl` or `data/sessions/<session-id>.md`;
2. the session ID passes the existing ID validation;
3. the file and relevant path components are not symlinks;
4. the target is a regular file or is already absent.

Do not use a broad recursive delete. A mismatched path, symlink, special file,
or transcript in a custom log directory (`--log-dir` sessions record absolute
custom paths) is preserved and reported. Its expired index record is still
removed: agent-collab is relinquishing discovery of that session, not claiming
ownership of externally placed data. The preview must make this distinction
visible.

## Convergent deletion (no transaction manifest)

An earlier draft of this design specified a quarantine directory, an on-disk
transaction manifest, atomic rename of transcripts before index replacement,
rollback on failure, and startup reconciliation of incomplete transactions.
That machinery is deliberately dropped. The invariant it protected — "the
index never points to a partially deleted transcript set" — does not hold
today and does not need to: every reader already degrades gracefully when
transcript files are missing, and re-running selection is itself the recovery
mechanism. A manifest also introduces the one genuinely dangerous behavior in
the old draft: startup code deleting files named by a stale side file instead
of files revalidated against live state.

The replacement is a converging idempotent operation, executed as one
serialized prune run:

1. On the event loop, under the prune `asyncio.Lock`: recalculate selection
   from the in-memory registry (terminal status, effective timestamp vs
   cutoff, keep count, no live task).
2. Validate every candidate transcript path against the ownership boundary.
3. Unlink the validated managed files (`stat` first for bytes-reclaimed
   accounting; `FileNotFoundError` counts as already absent). Do the blocking
   filesystem work in `asyncio.to_thread` on a detached copy of the plan,
   never touching manager state off the loop.
4. Back on the event loop: bulk-remove the successfully processed session IDs
   from the index in one atomic rewrite, and drop them from the in-memory
   registry. A session whose file removal failed (other than already-absent)
   keeps its index record and is reported as a failure.

Crash safety falls out of ordering plus idempotence, with no recovery code:

- Killed after some unlinks, before the index rewrite: the records remain,
  still terminal and still past the cutoff, so the next scheduled or manual
  run re-selects them; the missing files count as already absent and the
  index removal is retried. The interim state (a listed session with empty
  transcript reads) is exactly the already-tolerated missing-file state.
- Killed after the index rewrite: the in-memory registry dies with the
  process and is rebuilt from the index at restart. Nothing dangles.
- Retries are safe by construction because every run revalidates against
  live state and deletes only exact managed paths.

The externally observable invariants remain: retries and restarts are safe,
unrelated paths can never be deleted, and cleanup failures are logged and
reported but never terminate the daemon.

## Daemon and API design

Selection and mutation live behind `SessionManager` / `SessionIndex`
operations; the CLI never duplicates retention logic. `SessionIndex` gains a
bulk removal operation (`remove_many(ids)`) so pruning rewrites the file once,
not once per session; removing an ID absent from the file is a no-op.

Add a typed authenticated endpoint:

```text
POST /sessions/prune
```

The request carries `apply` (bool, default false), optional `older_than`
(duration string, validated server-side with the same parser the CLI uses)
and optional `keep`. The response carries the summary plus per-session
details: session ID, disposition (`pruned`, `preview`, `kept`,
`skipped_no_timestamp`, `preserved_external`, `failed`), effective timestamp,
files removed or preserved, error text for failures, bytes reclaimed, and the
count of unparseable index records. Per-session details are always present
(see resolved questions). Update `api_schema.py`, `server_http.py`,
`client.py`, and regenerate `doc/daemon_api_doc/` via `./agent_collab.sh
setup`.

The server owns one background retention task (stored on the server or
manager and created only when `retention_days > 0`). It:

1. runs once after index restoration (which already runs in the manager
   constructor);
2. sleeps `cleanup_interval_hours`;
3. recalculates and applies configured retention;
4. logs a compact result line without transcript content;
5. tolerates `asyncio.CancelledError` (abrupt teardown is the only shutdown
   path) and catches all other exceptions so a failing run never kills the
   loop or the daemon.

Scheduled and manual pruning serialize through the same prune `asyncio.Lock`,
so runs never overlap.

## Implementation plan

Each stage is independently committable and reviewable; run
`./agent_collab_dev.sh test` and `./agent_collab_dev.sh build --check` before each
commit. CI has no vendor SDKs installed — none of these stages may depend on
provider packages.

**Stage 1 — Config schema v5 and `[sessions]` settings.**
Touches: `agent_collab/config_migrations.py` (schema 5, no-op v4→v5,
project-scope strip of `sessions` with warning), `agent_collab/config.py`
(`SessionsConfig` dataclass with defaults, `KNOWN_TOP_LEVEL_KEYS`, merge and
validation), `agent_collab/default_config.toml` (`schema_version = 5`),
`agent_collab/cli.py` (`config show` prints the effective values).
Tests: `tests/test_config.py` (defaults, user override, `0` disables,
negative/boolean/fractional rejects, unknown `[sessions]` field),
`tests/test_config_migrations.py` (v4→v5 stamp, project-scope warning strip,
user scope preserved).

**Stage 2 — Pure retention selection and duration parsing.**
Touches: new `agent_collab/retention.py` (duration parser for `h`/`d`/`w`;
selection over state dicts with injected `now`, returning per-record
dispositions; managed-path ownership validation helpers).
Tests: new `tests/test_retention.py` (exact cutoff boundary, `updated_at`
fallback, missing/invalid timestamp skip, every terminal and non-terminal
status, deterministic `--keep` ordering and ties, duration parser accepts and
rejects, path validation: exact match, custom dir, traversal, symlink,
special file).

**Stage 3 — Index bulk removal and manager prune operation.**
Touches: `agent_collab/session_index.py` (`remove_many` with one atomic
rewrite), `agent_collab/daemon.py` (`SessionManager.prune_sessions` with the
prune `asyncio.Lock`, live-task defense, threaded unlink of a detached plan,
bulk index and registry removal, structured result dataclass, unparseable-
record count).
Tests: `tests/test_session_index.py` (bulk removal, absent IDs, atomicity),
`tests/test_daemon.py` (preview mutates nothing, apply removes index +
registry + files, live-task and non-terminal defense, custom log_dir files
preserved while the record is removed, already-absent files, unlink failure
keeps the record and reports, rerun after simulated partial failure
converges).

**Stage 4 — Authenticated API route and typed client.**
Touches: `agent_collab/api_schema.py` (request/response DTOs, `ROUTES`
entry), `agent_collab/server_http.py` (`_route_prune_sessions`),
`agent_collab/client.py` (`prune_sessions`),
`agent_collab/project_build.py` (add the new DTOs to `_MODELS` and the
request model to `_REQUEST_MODELS`; add `_FIELD_SCHEMAS` entries for
constrained fields such as `keep >= 0` and the disposition enum — the OpenAPI
generator only emits schemas for models listed there), regenerate
`doc/daemon_api_doc/openapi.json` and `http-api.md` via `./agent_collab.sh
setup`.
Tests: `tests/test_api_schema.py` (DTO validation and contract lockstep),
`tests/test_server_http.py` (401 without token, 400 on invalid
`older_than`/`keep`, preview and apply round-trips).

**Stage 5 — `agent-collab sessions prune` CLI.**
Touches: `agent_collab/cli.py` (`sessions` group in `PUBLIC_COMMANDS` and
`_command_handlers`, prune subparser, `--dry-run`/`--apply` exclusivity,
`--older-than`, `--keep`, `--json`, human summary and per-candidate lines,
apply guidance line on preview).
Tests: `tests/test_cli_help.py` (root inventory, subcommand help),
CLI behavior tests against a stubbed client (flag exclusivity, disabled-
retention preview message, output shape).

**Stage 6 — Daemon scheduler.**
Touches: `agent_collab/server_http.py` (load `[sessions]` at startup next to
`ensure_daemon_token`, create the retention task when enabled, injected
clock/interval for tests), possibly `agent_collab/daemon.py` for task
ownership.
Tests: `tests/test_server_http.py` / `tests/test_daemon.py` (startup run
after restore, periodic rerun with a short injected interval, disabled when
`retention_days = 0`, a failing run logs and continues, overlap prevented by
the shared lock, cancellation tolerated).

**Stage 7 — Documentation and changelog.**
Touches: `README.md` (retention default, how to extend or disable),
`doc/runtime-layout.md` (replace the unbounded-index caveat),
`CHANGELOG.md` (Unreleased entry referencing #5), command help text review.
Tests: none beyond help-text assertions already added in stage 5.

A mock-daemon smoke test (stage 3 or 6) creates terminal fixtures inside an
isolated `AGENT_COLLAB_HOME`, previews, applies, and proves active sessions
and external files remain untouched. Never use the real user session
directory in tests.

## Verification

Hermetic tests must cover:

- the 30-day default, user override, disable value, invalid values, config
  migration, and project-scope ignore-with-warning behavior;
- exact cutoff boundaries and legacy `updated_at` fallback;
- missing/invalid timestamps being preserved and reported;
- every terminal and non-terminal status;
- defense against a terminal-looking record with a live task;
- deterministic `--keep` behavior including ties;
- preview/no-flag behavior versus explicit apply;
- duration parsing and invalid CLI combinations;
- exact managed-path deletion, missing files, custom paths, path traversal,
  symlinks, and special files;
- one-rewrite bulk index update and in-memory registry removal;
- convergence: rerun after a simulated kill between unlink and index rewrite
  completes the removal without recovery code;
- authenticated route, 401/400 behavior, and typed client round-trip;
- startup run, periodic run, non-overlap, disabled scheduling, failure
  logging, and cancellation tolerance;
- root and subcommand help plus documented examples.

Run the full repository gate and `build --check` before every commit.

## Resolved questions

- **Candidate-ID verbosity.** The API response always carries per-session
  details. The CLI needs them anyway to render the preview and to report
  failures and preserved external files, session IDs are not secret, and a
  30-day-retained population keeps the payload small. The human preview
  prints one line per candidate — that listing *is* the audit trail — and
  `--json` exposes the full typed response. No separate verbose mode.
- **`--keep` versus future pinning.** Keep `--keep` as an invocation-scoped
  selection filter. Pinning, if it ever lands, is a durable per-session
  exemption and composes with `--keep` (a pinned session is simply never
  eligible) rather than superseding it. Nothing in this task blocks or
  prefigures pinning.
- **Quarantine transaction.** Dropped in favor of convergent deletion; see
  that section for the reasoning and the preserved invariants.
