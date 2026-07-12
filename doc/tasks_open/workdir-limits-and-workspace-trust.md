# Workdir limits and workspace trust

**Status:** Open. Design captured; not yet scheduled (no milestone).

**Created:** 2026-07-13

**Issue:** [#13](https://github.com/lauriparviainen/agent_collab/issues/13)

## Context

A session's `workdir` does two things: it selects which project
`.agent-collab/config.toml` is loaded, and it becomes the subprocess cwd for the
launched agents. Today the daemon accepts it as an arbitrary absolute path with
no validation and no confinement.

The trust model in `SECURITY.md` is intentionally local and small: the daemon
binds to loopback, authenticates with a bearer token from the user config, and
agents run with the invoking user's permissions in the given workdir. Because
the caller is already the user, a hard path allowlist by default would buy
little and would break the product's premise — one global daemon owning sessions
across all projects (the 0.1.0 design). So the goal is **permissive by default,
tightenable in user config**, not restrictive by default.

The sharper issue is not the path string but *config trust*: project config
overrides user config, and project scope does not currently protect agent
execution fields.

## Code-verified assumptions

Checked against the current code (v0.6.0):

- **Workdir is unvalidated.** `SessionManager._prepare_session_start`
  (`daemon.py`) resolves `Path(request.workdir).expanduser().resolve()` with a
  special case mapping `"."` to `default_workdir`. There is no existence check
  and no is-a-directory check, so a bogus path resolves silently and only fails
  later when a subprocess is spawned.
- **Project config overrides user config.** `load_config` (`config.py`) applies
  built-ins, then user config, then project config, "in reverse order [so]
  project values override user values." The project config always comes from
  `workdir`.
- **Project scope strips only three sections.** `migrate_config_data`
  (`config_migrations.py`) drops `[backends.*]`, `[daemon]`, and `[sessions]`
  when `scope == "project"`, each with a warning. It does not touch
  `[agents.*]`.
- **Agent command/args are settable per agent.** `_merge_agent` (`config.py`)
  accepts `agents.<id>.command` and `agents.<id>.args`. Combined with the two
  facts above, a `.agent-collab/config.toml` inside an untrusted repo can
  redefine an already-enabled agent's command/args, which then run with the
  user's permissions when that checkout is used as a session `workdir`. This is
  a workspace-trust gap analogous to editor "workspace trust".

## Goal / scope

Three changes, in value order. #2 is the one that actually matters.

1. **Workdir sanity validation (low risk).** Reject a non-existent or
   non-directory `workdir` at session start with an actionable error, instead of
   resolving silently and failing confusingly at spawn time. Keep the existing
   `"."` -> `default_workdir` behavior.

2. **Protect agent execution fields from project scope (the fix).** Treat
   `agents.<id>.command` and `agents.<id>.args` from *project* scope as
   untrusted and strip them by default, mirroring the existing `[backends.*]`
   project-scope stripping (same warning style). Legitimate per-project agents
   remain expressible in user config. This closes the arbitrary-command path
   without restricting the workdir path itself.

3. **Opt-in workdir confinement (the config override).** Add a user-config
   `[workdir] allowed_roots = [...]`. Absent or empty preserves today's
   unrestricted behavior; when set, a session `workdir` must resolve under one
   of the listed roots or be rejected. User-config only (like `[daemon]` and
   `[sessions]`), so an untrusted project cannot widen its own confinement.

## Decisions

- Default posture is permissive: no path allowlist unless the user opts in.
  (Inference from the stated local trust model; confirmed direction with the
  maintainer.)
- Strip-by-default for project command/args rather than a trust prompt: the
  daemon is non-interactive, so a prompt has nowhere to live, and stripping
  matches the precedent already set for `[backends.*]`/`[daemon]`/`[sessions]`.

## Open questions

- Should stripping project command/args emit a warning per agent (like
  backends) or a single aggregate warning? Leaning per-agent for parity.
- Do any legitimate flows rely on project config setting command/args today?
  None found in the tracked default/project configs, but worth a wider check
  before implementing.
- Should `allowed_roots` support globs / `~` expansion, or only resolved
  absolute prefixes? Start with resolved absolute prefixes for a tight contract.

## Verification (to define at implementation time)

- Non-existent and non-directory `workdir` rejected at start, with a test.
- Project `.agent-collab/config.toml` cannot change an enabled agent's
  command/args; regression test mirrors the `[backends.*]` stripping test.
- `[workdir] allowed_roots` honored when set, no-op when absent; tests for
  in-root, out-of-root, and unset.
- `SECURITY.md` and the config docs describe the workspace-trust boundary and
  the opt-in restriction.
