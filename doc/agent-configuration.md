# Agent configuration

## Purpose

`agent-collab` has its own config file for registering which agents are available and how to run them.

This is separate from provider configuration such as Codex's:

```text
agent-collab config -> external agents available to the referee
Codex config        -> how Codex itself runs, loads MCP servers, and applies sandbox policy
```

The schema is backend-first: each `[backends.<canonical>]` section configures
one backend once — enablement, command, environment, and option defaults.
Every **enabled** backend implicitly defines its default agent under the
canonical name (`claude_cli`, `codex_cli`, …), and named options-only
*personae* nest under the backend when one backend should appear as several
agents:

- Claude Code default (`claude_cli`).
- Codex default (`codex_cli`).
- A write-enabled Codex persona (`codex_cli.writer`, differing by options —
  the shipped defaults are read-only, so writing is the opt-in).
- A higher-reasoning persona on the same backend.
- The mock pseudo-backend for tests (`[backends.mock]`).
- Future command-based agents that stream JSONL.

## Config locations

Lookup order (highest precedence first):

```text
explicit session/start options
SESSION_WORKDIR/.agent-collab/config.toml
~/.agent-collab/config.toml   (or $AGENT_COLLAB_HOME/config.toml)
built-in defaults
```

Project config comes from the session `workdir`, never the caller's shell
directory. For safety, it may only rename a derived agent and define workflows
whose agents are already enabled by built-in or global user config. All
execution-relevant settings live on the `[backends.*]` sections — enablement,
command, args, environment, cwd, timeout, options, and backend-specific static
configuration — and those sections are global-user-only. Explicit start
options remain highest precedence.

Agent commands should be changed in the global `agent-collab` config, not
through project config or dedicated Claude/Codex path flags.

Built-in defaults are stored in [agent_collab/default_config.toml](../agent_collab/default_config.toml), so the base backend commands, option defaults, and built-in workflows are inspectable without reading Python code.

Config files declare a top-level `schema_version` (currently `8`; a missing version means `1`). Known old shapes are migrated in memory by `agent_collab/config_migrations.py` before validation; unknown fields are still rejected afterwards. `./agent_collab.sh install` additionally rewrites the user config file on disk to the current schema (see [Migration](#migration-from-the-agents-first-schema)). Inspect the effective merged config with `agent-collab config show --workdir PROJECT`.

## Example

```toml
schema_version = 8

[backends.claude_cli]
enabled = true
command = "claude"
args = ["-p", "--output-format", "stream-json", "--verbose"]

[backends.codex_cli]
enabled = true
command = "codex"
args = ["exec", "--json"]

# Options-only persona; derives agent id "codex_cli.writer". The shipped
# default sandbox is "read-only", so a write-enabled persona is the opt-in.
[backends.codex_cli.agents.writer]
name = "codex-writer"

[backends.codex_cli.agents.writer.options]
sandbox = "workspace-write"

# The mock pseudo-backend; its default agent id is "mock".
[backends.mock]
enabled = false

[workflows.solo]
sequence = ["claude_cli"]

[workflows.cross-review]
sequence = ["claude_cli", "codex_cli", "claude_cli"]

[workflows.dual-review]
parallel = ["claude_cli", "codex_cli"]

[workflows.implement-then-review]
sequence = ["codex_cli.writer", "claude_cli", "codex_cli.writer"]
```

Every enabled backend defines its default agent with the canonical backend
name as the agent id; a disabled backend defines no agents. A nested persona
derives the agent id `<canonical>.<name>`, inherits the backend's command,
environment, and options, and may override `options` and a display `name`
only.

## Workflow shapes

A workflow defines exactly one shape:

- `sequence = [...]` runs one agent at a time in the listed order. Repeated
  members are allowed.
- `parallel = [...]` runs one flat group of two to four distinct agents
  concurrently. Every member receives the same reviewer prompt over a frozen
  transcript snapshot.

Members reference either a canonical backend name (its default agent) or a
persona id (`<canonical>.<name>`). An **unknown** reference is a fatal config
error (it is a typo). A member whose backend is **disabled** keeps the config
loadable: the workflow stays visible in discovery but is start-ineligible with
reason `backend_disabled` until that backend is enabled.

The built-in `dual-review` uses Claude and Codex. The `xai_cli` backend is also
enabled by default, so adding it as a third reviewer is just a global-user
workflow (it becomes start-eligible once the `grok` CLI is installed; enable an
opt-in `sdk` backend first if you would rather use one of those):

```toml
schema_version = 8

[workflows.triple-review]
parallel = ["claude_cli", "codex_cli", "xai_cli"]
```

Parallel workflows are user-config-only: project config may continue to define
`sequence` workflows, but a project `parallel` table is ignored with a sanitized
warning because concurrent fan-out changes execution posture. Parallel sessions
must use `interactive = false` and `max_turns >= 1`. `max_turns` counts stages,
so any positive value runs the parallel workflow's single group; `timeout`
applies independently to each member.

At runtime, each member retains its six-value turn outcome. A member review is
accepted only when its outcome is `completed` and it emitted a non-error message.
The group can finish in a degraded `done` state when at least one review is
accepted. Its final referee status event carries `raw.members` and
`raw.accepted_members`; zero accepted members produces the canonical
`parallel_stage_no_accepted_member` session failure.

### Member selection at start

A session start can fill a named workflow's slots with different agents
without defining a throwaway workflow (#21). The additive `members` start
field maps a **slot** — named by the workflow's configured member id — to the
globally enabled agent that fills it. Duplicate sequence positions collapse
into one slot, so `cross-review`'s `[a, b, a]` exposes slots `a`
(lead/reviser, reprising) and `b` (reviewer); substituting `a` replaces both
of its positions:

```json
{"workflow": "dual-review", "members": {"codex_cli": "xai_cli"}}
```

Selection is validated with the same rules as configured workflows before any
session state exists: members must exist and be enabled, parallel groups keep
duplicate rejection and their configured width. Violations are rejected with
`invalid_start_options` field paths (`members.<slot>`). An absent or empty
field — and a selection that names only configured members — is exactly
today's behavior. The selection is a caller-side start choice: project config
can neither supply nor influence it, so the #19 posture (parallel execution
and agent enablement are user-config-only) is unchanged. The start response's
`settings.workflow` and `settings.agents` echo the effective members.

Discovery advertises each workflow's slots under
`workflows[].member_selection` (`slots[]` with `slot`, `default`,
`default_eligible`, `eligible_members`, plus `distinct_members` for parallel
shapes), the CLI accepts `--members '{"slot":"agent"}'`, and the TUI `/new`
wizard asks for the workflow shape first and then the backends that fill its
slots, with the configured members preselected so pressing Enter through the
questions starts the configured workflow.

## Backend sections

Backend sections belong in the global user config. The section name is the
canonical backend name, `<provider>_<mechanism>` (plus the `mock`
pseudo-backend), so the old per-agent `type` and `backend` keys no longer
exist — both derive from the section name.

Required for a `*_cli` backend (subprocess execution):

```toml
[backends.codex_cli]
enabled = true
command = "codex"
args = ["exec", "--json"]
```

`command` is required only for `cli` backends; `sdk` backends run in-process
and need no command.

Optional fields:

```toml
name = "display-name"    # default agent's display name
env = { KEY = "VALUE" }
cwd = "/path/override"
timeout = 900
```

An `options` sub-table sets session option defaults for the backend (see
[Start options](#start-options)). Any other key is backend-specific static
configuration and must be declared by that backend (for example the Vertex
settings of `antigravity_sdk`); undeclared keys are rejected.

Write posture is an option like any other. The built-in config ships every
backend that has a permission or sandbox control with a read-only default (see
[Default write posture](#default-write-posture)); granting write access is a
deliberate `options` override per backend, persona, or session, never an
implicit provider default.

### Nested personae

Additional agents on one backend are options-only personae:

```toml
[backends.codex_cli.agents.fast]
name = "codex-fast"

[backends.codex_cli.agents.fast.options]
thinking_level = "low"
```

A persona may set only `name` and `options`; it can never select a different
backend, command, or environment, so a persona never changes *what* runs. Its
options are layered over the backend's `options`. The derived agent id is
`<canonical>.<name>` (here `codex_cli.fast`), and personae of a disabled
backend do not exist.

### Display-name overrides

Top-level `[agents.<id>]` sections survive for exactly one purpose: renaming a
derived agent.

```toml
[agents.claude_cli]
name = "claude"
```

Any other key in a top-level `[agents.*]` section is a config error with a
hint to re-run `./agent_collab.sh install` (which migrates old agents-first
files). This name-only shape is also what project config may use: a project
copy of an existing agent table may set only `name`; other fields are ignored
with a sanitized warning, and a project-only agent table is ignored entirely.

## Backends

A canonical backend name combines the *provider* (`claude`, `codex`,
`antigravity`, `xai`) with the *execution mechanism* (`cli`, `sdk`). The
registry is keyed by `(provider, mechanism)`:

| provider          | `cli`                | `sdk`                           |
| ----------------- | -------------------- | ------------------------------- |
| `claude`          | ✅ (default)          | ✅ (`claude-agent-sdk`, typed)   |
| `codex`           | ✅ (default)          | ✅ (`openai-codex`, message-first) |
| `antigravity`     | ✅ (`agy`, plain text) | ✅ (`google-antigravity`, typed) |
| `xai`             | ✅ (`grok`, streaming JSON) | ✅ (`xai-sdk`, message-only remote chat) |

- `cli` runs the agent as a subprocess and parses its stdout. It is the default
  mechanism and runs the provider CLI.
- `sdk` runs the provider's Python SDK in-process. The SDKs install with the
  project (Python ≥ 3.10), but every SDK import is lazy, so a missing wheel is an
  unavailable backend rather than an import error, and the default `cli` backends
  are unaffected. Credentials are never managed by agent-collab (provide the
  provider's own auth in the environment).

An agent's mechanism normally derives from its canonical backend name. A
`--backend NAME` / `"backend"` start override applies uniformly to every
selected agent and is rejected before any session state when any selected
agent's provider does not register that mechanism. Explicit options are
rejected when their named backend is not selected or cannot honor them, with a
`backend_options.<provider>_<mechanism>.<key>` field path.

Each backend package declares its MCP/session options in `options.toml` and
normalizes only its own `backend_options` entry. Static backend configuration
stays directly under its `[backends.<canonical>]` section. CLI backends may
infer values from configured argv; SDK backends do not inherit CLI-only argv
values. The start response and runner use the same per-agent normalized map.

Every backend this stage reports `resume`, `interrupt`, and `tool_gate` as
`false` — capabilities are honest runtime facts, never inferred from the provider
brand. `agent_collab_describe_options` exposes, per provider, the registered
backend ids, the default, live availability/health, capability flags, and an
`option_schema` for each backend, so the selection is discoverable before
starting. The `mock` pseudo-backend ignores backend selection.

### Enablement policy

`enabled` on a backend section is both the user policy bit and agent
enablement: an enabled backend defines its default agent (and personae), a
disabled one defines nothing but keeps its settings for when it is re-enabled.

```toml
[backends.claude_cli]
enabled = true

[backends.antigravity_sdk]
enabled = false
```

This section is accepted only from `$AGENT_COLLAB_HOME/config.toml`. A project
`[backends.*]` section is ignored with a migration warning, so project
precedence cannot undo daemon-user policy. A backend with no section at all is
policy-enabled (relevant for `--backend` overrides) but defines no agents.
`agent-collab config init` generates one explicit section per registered
backend. Disabled backends remain visible in discovery but start rejects them
before health probing or session creation.

## Start options

MCP and CLI callers pass one backend-qualified map. The server validates it
before creating session state or launching a subprocess.

```json
{
  "backend_options": {
    "codex_cli": {
      "model": "gpt-5.6-sol",
      "thinking_level": "medium",
      "sandbox": "workspace-write"
    },
    "claude_sdk": {
      "model": "opus",
      "thinking_level": "high"
    }
  }
}
```

Each backend's colocated manifest is the shipped source of accepted keys and
values (types, allowed values, ranges). Shipped default *values* live in the
built-in config's `[backends.<canonical>.options]` tables in
[agent_collab/default_config.toml](../agent_collab/default_config.toml), so
they are ordinary, inspectable configuration rather than hard-coded manifest
data. They rank below flags configured in `args` and below a user config's
`options` table, and overriding one value never drops the others. A backend
section's `options` table sets concrete session defaults. MCP exposes the
effective schemas — with the shipped defaults overlaid — through
`agent_collab_describe_options`. Unknown keys, wrong types, unsupported
values, unselected backends, and invalid cross-field combinations are rejected
with actionable paths.

The separate `members` start field chooses which *agents* fill the workflow's
slots (see "Member selection at start" above); `backend` and `backend_options`
stay orthogonal transport and option choices for whichever agents end up
selected.

### Default write posture

Shipped defaults lean read-only: agents can call tools and inspect the
repository, but file writes need an explicit opt-in.

| Backend | Shipped default | Write opt-in |
| --- | --- | --- |
| `claude_cli` / `claude_sdk` | `permission_mode = "default"` (headless: write/exec tools are denied instead of prompting; `"plan"` is the strictest read-only mode) | `permission_mode = "acceptEdits"` |
| `codex_cli` / `codex_sdk` | `sandbox = "read-only"` (commands run, writes are blocked) | `sandbox = "workspace-write"` |
| `antigravity_cli` | `mode = "plan"` (read-only, headless-safe); boolean `sandbox` option adds terminal restrictions, opt-in | `mode = "accept-edits"` |
| `xai_cli` | `permission_mode = "bypassPermissions"` + `sandbox = "read-only"` (approval-free inspection, writes blocked) | `sandbox = "workspace"` |
| `antigravity_sdk` | no mode/permission control — follows the provider default | — |
| `xai_sdk` | remote chat only, no tools | — |

The posture is enforced by the provider's own sandbox/mode flag, not by prompt
text. Session `backend_options`, a persona, or a user-config `options` table
can loosen it deliberately.

Example option rules:

```toml
[backends.codex_cli]
enabled = true
command = "codex"
args = ["exec", "--json"]

[backends.codex_cli.options]
model = "gpt-5.6-sol"
thinking_level = "high"
sandbox = "workspace-write"
approval_policy = "on-request"
search = true

[backends.claude_sdk]
enabled = true

[backends.claude_sdk.options]
model = "opus"
permission_mode = "default"
thinking_level = "high"
```

`backend_options.antigravity_cli` accepts `model`, `mode`, and a boolean
`sandbox` (the `agy --sandbox` terminal-restriction flag); the SDK entry
accepts only `model`. Vertex, project, and location are static SDK configuration
under `[backends.antigravity_sdk]`, not MCP-call options. A backend entry is
rejected when it is not selected by the workflow. Configured session defaults
live under `[backends.<canonical>].options`, with a persona's `options` layered
on top for that agent; MCP values override both for that session.

`backend_options.xai_cli` accepts `model`, `permission_mode`, `sandbox`,
`provider_max_turns`, and the reasoning aliases. Its headless defaults are
`permission_mode=bypassPermissions` and `sandbox=read-only`: inspection
commands run without an interactive approval prompt while repository writes
remain blocked. `provider_max_turns` maps to Grok's internal model/tool-loop
limit and is distinct from the top-level agent-collab workflow `max_turns`; it
has no backend default. `backend_options.xai_sdk` accepts only `model` and the
reasoning aliases; pass a model explicitly because no remote API model default
is assumed without a credentialed account check. Its verified/current reasoning
values are `none`, `low`, `medium`, and `high`. The SDK is remote message-only
chat and does not provide the local coding/tool behavior of Grok Build.

CLI callers can pass JSON option objects and select a backend:

```bash
agent-collab start --backend-options '{"codex_cli":{"thinking_level":"medium"},"claude_cli":{"model":"opus"}}' "Task"
agent-collab start --workflow solo --members '{"claude_cli":"antigravity_sdk"}' --backend-options '{"antigravity_sdk":{"model":"Gemini 3.1 Pro (High)"}}' "Task"
agent-collab start --workflow solo --members '{"claude_cli":"xai_cli"}' --backend-options '{"xai_cli":{"model":"grok-build"}}' "Task"
agent-collab start --workflow solo --members '{"claude_cli":"xai_sdk"}' --backend-options '{"xai_sdk":{"model":"grok-4.5","thinking_level":"low"}}' "Task"
```

The option-to-command mapping is explicit. Unknown option keys are never appended as arbitrary shell flags.

Prefer `thinking_level` across providers:

- Codex `thinking_level` accepts `minimal`, `low`, `medium`, `high`, or `xhigh` and maps to the Codex config override `model_reasoning_effort`.
- Claude `thinking_level` accepts `low`, `medium`, `high`, `xhigh`, or `max` and maps to Claude Code `--effort`.
- xAI CLI accepts `low`, `medium`, `high`, or model-specific `xhigh`; the SDK accepts `none`, `low`, `medium`, or `high`. Both map one effective value to `reasoning_effort`.
- Codex `reasoning_effort` is kept as a provider-specific alias for compatibility. Claude `thinking_budget_tokens` is kept for advanced raw-token configurations, but should not be combined with `thinking_level`.

## Providers

Start with a small set:

```text
claude
codex
antigravity   (cli enabled by default; sdk opt-in)
xai           (cli enabled by default; sdk opt-in)
mock
```

The provider (the first half of the canonical backend name) controls event
parsing and prompt handling. The mechanism (the second half) controls
execution (see [Backends](#backends)).

`command` and `args` on the backend section control process launch.

### `claude`

Uses Claude stream JSON parsing.

Default command:

```bash
claude -p --output-format stream-json --verbose "prompt"
```

### `codex`

Uses Codex JSONL parsing.

Default command:

```bash
codex exec --json "prompt"
```

### `antigravity`

Google Antigravity, available on both backends. Disabled by default and opt-in.

- `antigravity_cli` runs `agy -p` in print mode. Requires the `agy` CLI installed and a
  Google **OAuth sign-in cached under `~/.gemini/`**. Print mode emits **plain
  text only** (no JSON, no per-event markers), so its transcript fidelity is
  intentionally **message-only** — each non-empty output line is one
  `antigravity` message event; there is no tool/command/file-change structure.
  The shipped `mode` default is `plan`: read-only, and it does not stall `-p`
  on the interactive request-review approval prompt. Turns that must write
  need `mode = "accept-edits"` (which auto-approves edits — the historical
  default until it deleted files in a nominally read-only review). Choose the
  SDK backend for structured events.
- `antigravity_sdk` runs the `google-antigravity` SDK in-process (installed with the project,
  Python ≥ 3.10, lazy-imported). Needs a **Gemini API key** (`GEMINI_API_KEY` env,
  or `LocalAgentConfig(api_key=...)`, or Vertex/ADC) — the SDK does **not** use the
  `~/.gemini` OAuth. It maps typed SDK events to `tool_call`/`command`/`file_change`,
  degrading to message-only if a turn has no tool calls.
  `backend_options.antigravity_sdk.mode` is unsupported; mode belongs to
  `antigravity_cli` only.

The two backends authenticate differently (OAuth for `cli`, an API key for
`sdk`). Auth is the provider's own concern: agent-collab only passes the
environment through and never manages or logs credentials.

```toml
[backends.antigravity_cli]
enabled = true
command = "agy"
args = ["-p"]

# Or the in-process SDK backend (no command):
[backends.antigravity_sdk]
enabled = true
```

### `xai`

xAI is disabled by default and available through two intentionally asymmetric
backends. `xai_cli` runs Grok Build headlessly with `streaming-json`, attributes
text/thought/end/error records, and captures the Grok session ID. Only
`stopReason=EndTurn` is treated as success; cancelled and other unsuccessful
terminal reasons become structured fatal errors. The observed 0.2.93 stream
exposes no typed tool records. `xai_sdk` uses the remote async chat API, maps only
response content and response identity, requires `XAI_API_KEY`, and does not
enable tools. Both report resume, interrupt, and tool-gate as false.

```toml
[backends.xai_cli]
enabled = true
command = "grok"
args = ["--no-auto-update", "--output-format", "streaming-json", "-p"]
```

### `mock`

A pseudo-backend: `[backends.mock]` defines the agent `mock`, which uses the
existing mock runner and does not launch a subprocess. The session-level
`mock` start flag is independent of this section.

Useful for tests and demos.

### Future: `command-jsonl`

Generic command that emits JSONL events. This is a future extension point for other agents.

The first version can preserve raw events and print compact verbose output for unknown shapes.

## Workflow fields

A workflow names an orchestration pattern: the ordered agent sequence a session runs. Workflows reference canonical backend names or persona ids:

```toml
[workflows.my-workflow]
sequence = ["claude_cli", "codex_cli.writer", "claude_cli"]
```

This removes hardcoded orchestration logic from the referee. Built-in workflows (`solo`, `cross-review`, `dual-review`) still exist when no config file is present; `cross-review` is the default. Workflow names should describe the orchestration, not who "leads". The old `[modes.*]` sections are rejected with a hint.

An unknown member reference is a fatal config error; a member whose backend is
disabled leaves the workflow loadable but start-ineligible with the
discovery-visible reason `backend_disabled`, so disabling a backend never
requires editing every workflow that mentions it.

Project workflows are safe shared composition: each referenced agent must
already exist and be enabled in built-in or global user config. A project
workflow that references a project-only, unknown, or globally disabled agent
is ignored with a warning.

## Migration from the agents-first schema

Configs with `schema_version < 8` used the agents-first shape — top-level
`[agents.<id>]` sections owning `type`, `backend`, `command`, and `options`.
They migrate automatically:

- `./agent_collab.sh install` rewrites the user config file on disk through
  tomlkit, preserving comments and formatting, after writing a
  `config.toml.bak` backup.
- The same migration also runs lazily in memory on every load, so an old file
  keeps loading even before anyone reinstalls. Project configs are never
  rewritten on disk; only their built-in agent references are remapped in
  memory.

What the migration does: each old agent folds into its effective canonical
backend section; one agent per backend becomes the backend section itself
(the default agent), and additional enabled agents that differ only by `name`
or `options` become nested personae. `type = "mock"` agents become
`[backends.mock]`, name-only agent tables stay as top-level display-name
overrides, and workflow member ids are rewritten (built-in `claude` →
`claude_cli`, `codex` → `codex_cli`).

When automatic migration is impossible — two agents on one backend whose
`command`, `args`, `env`, `cwd`, or `timeout` conflict, or an agent that
cannot be expressed as backend + options-only persona — install fails with a
clear error naming the offending section and leaves the config file untouched.
Merge the agents into one `[backends.<canonical>]` section with nested
options-only personae and re-run install. A file already stamped
`schema_version = 8` that still carries execution fields under top-level
`[agents.*]` is rejected at load with the same re-run-install hint.

## Workdir policy

The global user config may optionally confine session workdirs:

```toml
[workdir]
restrict_workdir_roots = ["~/projects", "/path/to/one/exception"]
```

Missing or empty `restrict_workdir_roots` preserves unrestricted behavior. When
populated, a resolved workdir must equal or be below one listed path. An entry
can therefore be a broad normal root or one specific exceptional directory.
Paths must be absolute after `~` expansion. Project config cannot set this
section.

Session start and workdir-scoped option discovery also require the path to
exist and be a directory. `workdir` selects project config and is the default
cwd; it is not an operating-system sandbox, and configured agents may access
other paths according to their provider permissions.

## CLI commands

```bash
agent-collab config show --workdir PROJECT   # effective merged config
agent-collab config init                     # user config with explicit backend policy
agent-collab options --workdir PROJECT      # workflows, backends, health, remediation
```

`options` asks the running daemon for its workdir-scoped discovery snapshot and
covers the doctor duties: it reports whether agents are enabled,
whether commands and SDK wheels resolve, backend health and credential
evidence, and which workflows reference which agents — all without a model
call.

## Implementation notes

Config files are TOML. On Python 3.11+ they are parsed with the standard-library
`tomllib`; on Python 3.10 a bundled minimal parser covers the config subset used
here, so no TOML dependency is required.

## Safety considerations

- Config registration is not permission approval.
- Keep execution-relevant backend settings in global user config; project
  config is limited to display names and safe workflow composition.
- Print the command prefix before launching an agent.
- Do not print full prompts in process metadata unless verbose logging is requested.
- Keep recursive-agent guardrails in generated prompts.
- Keep `--workdir` as the session root even when an agent has a custom command path.

## Guarantees

- Users can run several personae of one backend, differing by options only.
- Workflows can reference canonical backend names and persona ids.
- Missing config falls back to built-in `claude_cli` and `codex_cli` defaults.
- Mock mode remains easy for tests.
- Existing CLI flags still work as overrides.
