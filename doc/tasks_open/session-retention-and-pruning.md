# Session retention and pruning

**Status:** Open design and implementation task.

**Created:** 2026-07-12

**Issue:** [#5](https://github.com/lauriparviainen/agent_collab/issues/5)

## Context

The global daemon persists one session record in `data/session-index.json` and
normally writes JSONL and Markdown transcripts under `data/sessions/`. Neither
the index nor the transcript directory is currently bounded. Long-running
installations therefore accumulate private transcript data and consume disk
space indefinitely.

`runtime-layout.md` already calls out the unbounded index. The Stage 5
hardening plan originally proposed only manual `sessions prune` commands and
said never to delete logs by default. This focused task supersedes that part
of the older plan: completed sessions should have a visible, configurable
retention period, defaulting to 30 days.

Retention changes the auditability promise. The README and command help must
state the default plainly, show how to retain sessions longer, and show how to
disable automatic pruning before users rely on the daemon as a durable archive.

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
  Transaction recovery may delete quarantine entries created by this feature.
- Do not compress or archive old sessions; pruning is deletion.

## Configuration

Add built-in defaults with a new config schema version:

```toml
[sessions]
retention_days = 30
cleanup_interval_hours = 24
```

These are daemon-global settings. Only built-in and user config participate;
a project `[sessions]` section must be ignored with a warning or rejected in
the same way as other user-only policy. The exact handling should be consistent
with existing config scope rules and tested explicitly.

Validation rules:

- `retention_days` is a non-negative integer;
- `0` disables automatic pruning;
- `cleanup_interval_hours` is a positive integer;
- booleans are not accepted as integers.

Changing either value takes effect after daemon restart. Live config reload is
out of scope.

## CLI design

Add `sessions` as a public command group and advertise it in root help:

```text
agent-collab sessions prune
agent-collab sessions prune --dry-run
agent-collab sessions prune --apply
agent-collab sessions prune --older-than 7d --apply
agent-collab sessions prune --older-than 30d --keep 100 --apply
```

The no-argument form previews candidates using configured retention. It must
not mutate data and should end with guidance to rerun using `--apply`.
`--dry-run` makes preview intent explicit and is mutually exclusive with
`--apply`.

`--older-than DURATION` overrides `retention_days` for that invocation. Accept
documented whole-number units such as `h`, `d`, and `w`; reject zero, negative,
fractional, ambiguous, or unbounded values. `--keep N` protects the newest `N`
eligible terminal sessions after status filtering, ordered deterministically by
the same effective terminal timestamp and then session ID. `N` must be a
non-negative integer.

The command operates through the authenticated daemon API. It must not mutate
the session index directly while the daemon may be its single writer. If the
daemon is unavailable, return the normal actionable connection error rather
than falling back to unsafe filesystem mutation.

Human output and the typed response should summarize:

- effective cutoff and keep count;
- candidate and pruned session counts;
- managed transcript files removed;
- custom or unsafe transcript files preserved;
- failures;
- bytes reclaimed.

Preview and apply must use the same selection implementation. A preview is a
snapshot, not a deletion guarantee: a later apply recalculates eligibility so
it cannot act on a session that became active or otherwise changed.

## Eligibility and age

Only these terminal statuses are eligible:

- `done`;
- `failed`;
- `stopped`;
- `interrupted`.

`running` and `awaiting_input` are never eligible, regardless of timestamps or
CLI arguments. The manager should also reject any record backed by a live task
as a defense in depth.

Use `ended_at` as the retention timestamp. For legacy terminal records that do
not have `ended_at`, fall back to `updated_at`. If neither timestamp is present
or valid, preserve the record and report it as skipped; guessing from file
mtime or `created_at` could delete data prematurely.

A session is old enough when its effective timestamp is less than or equal to
the UTC cutoff. Inject the clock into selection/scheduling code so boundary
tests do not depend on wall-clock time.

## Filesystem ownership boundary

Automatic and manual pruning may unlink a transcript only when all of these
conditions hold:

1. its recorded path is exactly the expected global managed path
   `data/sessions/<session-id>.jsonl` or
   `data/sessions/<session-id>.md`;
2. the session ID passes the existing ID validation;
3. the file and relevant path components are not symlinks;
4. the target is a regular file or is already absent.

Do not use a broad recursive delete. A mismatched path, symlink, special file,
or transcript in a custom log directory is preserved and reported. Its expired
index record may still be removed: agent-collab is relinquishing discovery of
that session, not claiming ownership of externally placed data. The preview
must make this distinction visible.

## Crash-safe pruning transaction

Treat index removal and managed transcript deletion as one logical operation.
An implementation should use a small transaction manifest and atomic renames
within the global data filesystem:

1. Revalidate selected sessions on the daemon event-loop thread.
2. Validate every candidate transcript against the ownership boundary.
3. Write an owner-only transaction manifest containing session IDs and the
   expected managed paths, never transcript contents.
4. Atomically rename managed transcript files into an internal quarantine
   directory on the same filesystem.
5. Atomically replace the session index without the selected records.
6. Remove the selected sessions from the manager's in-memory registry.
7. Unlink the quarantined files and remove the manifest.

If index replacement fails, restore the renamed files and keep the in-memory
records. On startup, reconcile an incomplete transaction: restore quarantined
files when the index still contains the session, or finish deletion when the
record is absent. Recovery must be idempotent. Failures are logged and included
in manual results, but must not terminate the daemon.

The exact order may change if implementation work proves a safer standard-
library approach, but the externally observable invariants must remain:

- the index never intentionally points to a partially deleted managed
  transcript set;
- retries and startup recovery are safe;
- unrelated paths cannot be deleted.

## Daemon and API design

Put selection and mutation behind `SessionManager`/`SessionIndex` operations;
do not duplicate retention logic in the CLI. `SessionIndex` needs a bulk atomic
replacement/removal operation so pruning does not rewrite once per session.

Add a typed authenticated endpoint, for example:

```text
POST /sessions/prune
```

The request carries preview/apply mode, optional age override, and optional
keep count. The response carries the summary and per-session skip/error details
needed by the CLI. Update the API schema and generated REST documentation
through the normal setup workflow.

The server owns one background retention task. When retention is enabled it:

1. runs once after index restoration and transaction recovery;
2. sleeps for the configured interval;
3. recalculates and applies configured retention;
4. logs a compact result without transcript content;
5. cancels cleanly during server shutdown.

Do not overlap scheduled cleanup runs. Manual and scheduled pruning must
serialize through the same manager lock or event-loop-owned operation.

## Implementation plan

1. Add config schema migration, typed settings, scope filtering, validation,
   built-in defaults, and config rendering/show support.
2. Implement duration parsing and a pure retention selector with injected UTC
   time.
3. Add bulk index removal and the quarantine transaction/recovery layer.
4. Add manager pruning with live-session protection and structured results.
5. Add the authenticated API models, route, client method, and generated docs.
6. Add `agent-collab sessions prune`, root-help inventory, and human/JSON-safe
   output as appropriate to existing CLI conventions.
7. Add daemon startup/periodic scheduling and bounded shutdown cleanup.
8. Update README, runtime layout, Stage 5 hardening notes, CLI help, and
   changelog.

## Verification

Hermetic tests must cover:

- the 30-day default, user override, disable value, invalid values, config
  migration, and project-scope rejection/ignore behavior;
- exact cutoff boundaries and legacy `updated_at` fallback;
- missing/invalid timestamps being preserved;
- every terminal and non-terminal status;
- defense against a terminal-looking record with a live task;
- deterministic `--keep` behavior;
- preview/no-flag behavior versus explicit apply;
- duration parsing and invalid CLI combinations;
- exact managed-path deletion, missing files, custom paths, path traversal,
  symlinks, and special files;
- atomic bulk index update and in-memory registry removal;
- rollback before index commit and recovery before/after index commit;
- idempotent retries and stale quarantine recovery;
- authenticated route and typed client behavior;
- startup run, periodic run, non-overlap, disabled scheduling, failure logging,
  and clean shutdown;
- root and subcommand help plus documented examples.

Run the full repository gate and `setup --check`. A mock daemon smoke should
create terminal fixtures inside an isolated `AGENT_COLLAB_HOME`, preview them,
apply pruning, and prove active and external files remain untouched. Never use
the real user session directory in tests.

## Open questions

- Whether the API response should expose individual candidate session IDs by
  default or only under a verbose/JSON output mode. Session IDs are not secret,
  but compact normal output may be easier to audit.
- Whether a future session-pinning feature should supersede `--keep` for
  durable exemptions. Pinning is not required for this task and should not
  delay basic retention.
