# Backend-first configuration schema

**Status:** Implemented and verified 2026-07-14. The full gate passes (856
hermetic tests), the maintainer's real config migrated cleanly (backup kept),
the daemon runs on the migrated config with `dual-review` and the solo
workflows start-eligible, and the readiness table shows one row per backend.
A pre-commit cross-model review (Gemini 3.1 Pro via agent-collab, run on the
migrated config itself) found two high-severity migration flaws — omitted
keys hiding execution differences during persona folding, and file-order
dependent backend enablement — both fixed with regression tests before
commit.

**Created:** 2026-07-14.

**Issue:** #25

## Context

The current config model is agents-first: `[agents.<id>]` owns every
execution-relevant setting (provider `type`, `backend` selection, `command`,
`args`, `env`, `cwd`, per-backend `options`), while `[backends.<canonical>]`
carries only an `enabled` policy bit. Because one agent selects exactly one
backend, running both backends of a provider — or several model presets on
one backend — forces users to hand-write one synthetic agent per backend
(`claude_cli`, `claude_sdk`, …) alongside the built-in `claude`/`codex`
agents. The install readiness table (#23, #24) made the resulting duplication
visible: two enabled agents per cli backend with identical probe facts.

Two sharp edges compound this. A workflow that references a disabled agent is
a fatal `ConfigError` at load, so retiring the built-in duplicate agents
requires overriding every built-in workflow in the same edit. And the
duplication is structural: the maintainer's mental model — configure each
*backend* once, with its options and default agent — is not expressible.

The project is pre-1.0 (0.7.1), published 2026-07-14, with no meaningful
install base. This is the cheapest moment for a breaking schema cut.

## Decisions (maintainer, 2026-07-14)

- The schema becomes backend-first. Top-level `[agents.*]` is **removed**,
  not deprecated: "stray" agents unattached to a backend section are no
  longer expressible.
- Additional agents (personae) nest under their backend:
  `[backends.<canonical>.agents.<name>]`. Workflow members reference either a
  backend name (its default agent) or `<canonical>.<name>`.
- Install migrates the previous schema automatically; when automatic
  migration is not possible, install **fails with a clear error** naming the
  offending section. There is no runtime support for the old shape.
- This ships as a pre-1.0 minor release (0.8.0).

## Target schema (v8)

```toml
[backends.claude_cli]
enabled = true                # replaces both agent enablement and policy bit
command = "claude"            # execution settings live on the backend
# env, cwd as today on agents

[backends.claude_cli.options]
permission_mode = "acceptEdits"

[backends.antigravity_cli]
enabled = true
command = "agy"

[backends.antigravity_cli.options]
model = "Gemini 3.5 Flash (High)"

[backends.antigravity_cli.agents.pro]
# persona: inherits the backend config, overrides options
[backends.antigravity_cli.agents.pro.options]
model = "Gemini 3.1 Pro (High)"

[workflows.gemini-dual]
parallel = ["antigravity_cli", "antigravity_cli.pro"]
```

Semantics:

- Every **enabled** backend implicitly defines its default agent with the
  canonical backend name as agent id. Disabled backends define nothing.
- A nested agent derives id `<canonical>.<name>`, inherits the backend's
  command/env/options, and may override `options` (and a display name).
  Nested agents cannot select a different backend, command, or environment —
  personae differ by options only.
- Provider `type` and backend kind derive from the canonical name
  (`claude_cli` → type `claude`, backend `cli`); the separate `type` and
  `backend` keys disappear.
- `mock` becomes a pseudo-backend: `[backends.mock]` (default agent `mock`),
  replacing `type = "mock"` agents. The session-level `mock` start flag is
  unchanged.
- Workflow validation: an **unknown** member reference stays a fatal
  `ConfigError` (it is a typo); a member whose backend is **disabled** makes
  the workflow ineligible with a discovery-visible reason instead of failing
  the whole config.
- Internally, parsing synthesizes the same `AgentConfig` objects the runtime
  already consumes (registry, runners, workflows, discovery, readiness), so
  the change concentrates in `config.py`, migration, and defaults; downstream
  code sees derived agents with backend-first ids.

## Workspace trust

The #13 boundary carries over unchanged in substance: `[backends.*]` is
execution-relevant and therefore user-global only. Project config may still
define workflows composed from globally enabled backends/agents and display
names; a project-level `[backends]` or legacy `[agents]` section is ignored
with the existing sanitized warning mechanism.

## Migration

Implemented with tomlkit in the existing install-time migration path
(`config.toml.bak` backup, comments preserved), bumping the config schema
version to 8:

- `[backends.<canonical>] enabled` policy bits are preserved.
- Each old `[agents.<id>]` folds into its effective canonical backend:
  - the id matches the canonical name, or it is the only enabled agent for
    that backend → its command/args/env/cwd/options become the backend
    section; the backend is enabled iff the agent was;
  - additional enabled agents on the same backend become nested personae
    (`[backends.<canonical>.agents.<id>]`) when they differ from the default
    only by options;
  - `type = "mock"` agents become `[backends.mock]`.
- Workflow member ids are rewritten to the new references (for example
  built-in `claude` → `claude_cli`).
- **Fatal migration errors** (install prints the error and stops; the config
  file is left untouched): two agents on one backend with conflicting
  command/env that both must survive; an agent whose settings cannot be
  expressed as backend + options-only persona; any section the migrator does
  not recognize.
- Built-in `default_config.toml` is rewritten in the new schema: cli backends
  for Claude and Codex enabled, everything else disabled, built-in workflows
  referencing `claude_cli`/`codex_cli`.

## Affected surfaces

- `config.py`: schema parse/merge/validation, derived-agent synthesis,
  workflow eligibility semantics.
- `default_config.toml`, config migration module, user-install flow (fatal
  migration error path).
- Discovery/`describe_options`, install readiness, TUI pickers, REST/MCP
  session settings: consume derived agents; expected ids change
  (`claude` → `claude_cli`).
- Documentation: README configuration section, `doc/agent-configuration.md`,
  `doc/runtime-layout.md`, MCP guidance, development notes.
- Tests: fixture and expectation updates across the hermetic suite; new
  coverage for nested personae, implicit default agents, disabled-backend
  workflow ineligibility, migration success and each fatal migration error.

## Verification

- `./agent_collab_dev.sh test` and `./agent_collab_dev.sh build --check`.
- Migration cases: the maintainer's real pre-change config (seven synthetic
  agents + built-ins + solo workflows) migrates to backend sections plus one
  Gemini persona with no fatal error; a config with conflicting same-backend
  commands fails install with the documented error.
- Post-migration readiness table shows one row per selected backend with no
  duplicates; `dual-review` and the solo workflows remain start-eligible.
- Old-schema config without migration (hand-copied file) fails fast with the
  unsupported-section error naming `[agents.*]`.
