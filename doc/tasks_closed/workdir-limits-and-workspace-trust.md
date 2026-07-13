# Workdir limits and workspace trust

**Status:** Completed 2026-07-13.

**Created:** 2026-07-13

**Issue:** [#13](https://github.com/lauriparviainen/agent_collab/issues/13)

## Context

A session's `workdir` selects the project `.agent-collab/config.toml` and is
also the default subprocess cwd. Those are separate trust concerns:

- the path must be usable as a cwd and may optionally be confined by global
  user policy;
- configuration read from a checkout must not gain code-execution authority
  merely because the checkout is used as a workdir.

The daemon is deliberately permissive about paths by default so one global
daemon can serve many projects. User-global config must nevertheless be able to
constrain workdirs and explicitly allow an exceptional directory outside a
usual project root. Project config must never be able to change those policies.

Project agent fields are a broader execution boundary than `command` and
`args`. Environment variables, cwd, backend selection, backend configuration,
dynamic options, enablement, type, and timeout can all change execution posture
or resource use. New project-only agents can also package those fields under a
new id and then reference the agent from a project workflow.

## Goals and decisions

1. **Workdir sanity checks.** Session start and workdir-scoped option discovery
   reject a missing path or a non-directory with an actionable error. These
   checks are not bypassable because such a path cannot be used as a process
   cwd.

2. **User-global workdir confinement.** Add
   `[workdir].restrict_workdir_roots` to the user config. A missing or empty
   list means unrestricted. When populated, the resolved workdir must equal or
   be below one entry. An entry may be a broad project root or one specific
   exceptional directory, providing the requested global override. Project
   config cannot set this section.

3. **Fail-closed project agent filtering.** Project config may change only
   `agents.<existing-id>.name`. Project-only agent tables are dropped. All
   execution-relevant fields are ignored, including unknown fields that would
   otherwise become backend configuration.

4. **Safe project workflows.** A project workflow is accepted only when every
   referenced agent id already exists and is enabled in built-in or user
   config. Workflows referencing a dropped, disabled, or unknown agent are
   dropped with a warning rather than making otherwise-valid configuration
   fail to load.

5. **No project-config trust exception.** Execution-relevant agent settings are
   accepted only from built-ins and global user config. Users who need custom or
   permissive agent behavior define it globally; there is no per-workspace
   escape hatch to weaken this rule.

6. **Keep trusted inputs trusted.** User config and explicit start-time backend
   options remain authoritative.

7. **Visible diagnostics.** Ignored project sections, agents, workflows, and
   fields produce sanitized warnings. Start responses expose those warnings in
   effective settings, and option discovery exposes them without echoing the
   ignored values.

8. **Correct the documented boundary.** `workdir` is a config root and default
   cwd, not an operating-system sandbox. Security and configuration docs must
   say so explicitly.

## Configuration shape

User-global configuration:

```toml
[workdir]
restrict_workdir_roots = ["~/projects", "/path/to/one/exception"]
```

Missing or empty `restrict_workdir_roots` means unrestricted. Populated paths
expand `~`, must be absolute after expansion, and are resolved before
comparison. A project `[workdir]` section is ignored.

## Verification

- Missing and non-directory workdirs are rejected by start and describe.
- Missing or empty confinement remains permissive; in-root and exact
  exceptional paths are accepted; out-of-root paths are rejected.
- Project config cannot change `type`, `command`, `args`, `enabled`, `env`,
  `cwd`, `timeout`, `backend`, `options`, or backend-specific fields for a
  globally known agent id.
- Project-only agents and workflows that depend on them are dropped.
- Safe project workflows over built-in/user agents still work.
- Explicit start options and user agent overrides are unaffected.
- The fallback TOML parser accepts the workdir policy section.
- Start and describe return sanitized ignored-override warnings.
- Security and configuration documentation describe the workdir policy and the
  non-overridable project-agent boundary.

Completed 2026-07-13:

- `./agent_collab_dev.sh test` — Ruff checks and 707 hermetic tests passed.
- `./agent_collab_dev.sh build --check` — effective config and generated API
  artifacts verified.
- `git diff --check` — passed.
- Independent re-review by Grok Build and Gemini 3.1 Pro High found no remaining
  release blockers. Gemini's first pass identified two error/malformed-input
  issues; both were fixed and accepted on re-review.

Issue #13 is resolved by this implementation. The broader visibility-flip work
remains tracked by issue #14 and `go-public-checklist.md`.
