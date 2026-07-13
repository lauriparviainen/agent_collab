# Agent configuration

## Purpose

`agent-collab` has its own config file for registering which agents are available and how to run them.

This is separate from provider configuration such as Codex's:

```text
agent-collab config -> external agents available to the referee
Codex config        -> how Codex itself runs, loads MCP servers, and applies sandbox policy
```

The referee does not assume there is exactly one Claude command and one Codex command. Users may have several variants:

- Claude Code default.
- Codex default.
- Codex with a read-only profile.
- Codex with a higher reasoning profile.
- Mock agents for tests.
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
directory. For safety, it may only rename an existing agent and define workflows
whose agents are already enabled by built-in or global user config. All
execution-relevant agent fields — including type, command, args, enablement,
environment, cwd, timeout, backend, options, and backend-specific settings — are
global-user-only. Explicit start options remain highest precedence.

Agent commands should be changed in the global `agent-collab` config, not
through project config or dedicated Claude/Codex path flags.

Built-in defaults are stored in [agent_collab/default_config.toml](../agent_collab/default_config.toml), so the base agent commands, option defaults, and built-in workflows are inspectable without reading Python code.

Config files declare a top-level `schema_version` (currently `6`; a missing version means `1`). Known old shapes are migrated in memory by `agent_collab/config_migrations.py` before validation; unknown fields are still rejected afterwards. Inspect the effective merged config with `agent-collab config show --workdir PROJECT`.

## Example

```toml
schema_version = 6

[agents.claude]
type = "claude"
command = "claude"
args = ["-p", "--output-format", "stream-json", "--verbose"]
enabled = true

[agents.codex]
type = "codex"
command = "codex"
args = ["exec", "--json"]
enabled = true

[agents.codex_readonly]
type = "codex"
command = "codex"
args = ["exec", "--json", "--profile", "readonly"]
enabled = true

[agents.mock_claude]
type = "mock"
name = "claude"
enabled = false

[agents.mock_codex]
type = "mock"
name = "codex"
enabled = false

[workflows.solo-claude]
sequence = ["claude"]

[workflows.solo-codex]
sequence = ["codex"]

[workflows.cross-review]
sequence = ["claude", "codex", "claude"]

[workflows.compare]
sequence = ["claude", "codex"]

[workflows.readonly-review]
sequence = ["codex_readonly", "claude", "codex_readonly"]
```

## Agent fields

Agent fields belong in the global user config. A project copy of an existing
agent table may set only `name`; other fields are ignored with a sanitized
warning. A project-only agent table is ignored entirely.

Required fields:

```toml
type = "codex"
enabled = true
```

Required for the `cli` backend (subprocess execution):

```toml
command = "codex"
args = ["exec", "--json"]
```

`command` is required only for the `cli` backend; other backends (e.g. `sdk`)
run in-process and need no command.

Optional fields:

```toml
name = "display-name"
env = { KEY = "VALUE" }
cwd = "/path/override"
timeout = 900
backend = "cli"          # execution mechanism; default "cli"
```

Avoid adding broad permission fields at first. Permission policy should remain explicit in the underlying agent command or profile.

## Backends

An agent's `type` (the *provider*: `claude`, `codex`, `antigravity`, `xai`) is separate
from its `backend` (the *execution mechanism*). The registry is keyed by
`(type, backend)`:

| provider (`type`) | `cli`                | `sdk`                           |
| ----------------- | -------------------- | ------------------------------- |
| `claude`          | ✅ (default)          | ✅ (`claude-agent-sdk`, typed)   |
| `codex`           | ✅ (default)          | ✅ (`openai-codex`, message-first) |
| `antigravity`     | ✅ (`agy`, plain text) | ✅ (`google-antigravity`, typed) |
| `xai`             | ✅ (`grok`, streaming JSON) | ✅ (`xai-sdk`, message-only remote chat) |

- `cli` runs the agent as a subprocess and parses its stdout. It is the default
  backend and runs the provider CLI.
- `sdk` runs the provider's Python SDK in-process. The SDKs install with the
  project (Python ≥ 3.10), but every SDK import is lazy, so a missing wheel is an
  unavailable backend rather than an import error, and the default `cli` backend
  is unaffected. Credentials are never managed by agent-collab (provide the
  provider's own auth in the environment).

Resolution is most-specific-wins: `start-request backend > agents.<id>.backend >
default "cli"`. A `--backend NAME` / `"backend"` start override applies uniformly
to every selected agent and is rejected before any session state when any
selected agent's type does not register that backend. Explicit options are
rejected when their named backend is not selected or cannot honor them, with a
`backend_options.<provider>_<backend>.<key>` field path.

Each backend package declares its MCP/session options in `options.toml` and
normalizes only its own `backend_options` entry. Static backend configuration
stays directly under its backend-specific agent section. CLI backends may infer values
from configured argv; SDK backends do not inherit CLI-only argv values. The
start response and runner use the same per-agent normalized map.

Every backend this stage reports `resume`, `interrupt`, and `tool_gate` as
`false` — capabilities are honest runtime facts, never inferred from the provider
brand. `agent_collab_describe_options` exposes, per agent type, the registered
backend ids, the default, live availability/health, capability flags, and an
`option_schema` for each backend, so the selection is discoverable before
starting. `mock` agents ignore backend selection and reject a `backend` field.

### User backend policy

The user config may independently disable a canonical backend for every agent:

```toml
[backends.claude_cli]
enabled = true

[backends.antigravity_sdk]
enabled = false
```

This section is accepted only from `$AGENT_COLLAB_HOME/config.toml`. A project
`[backends.*]` section is ignored with a migration warning, so project
precedence cannot undo daemon-user policy. Missing entries mean enabled.
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

Each backend's colocated manifest is the shipped source of accepted values and
defaults. A backend-specific agent section may set concrete session defaults. MCP
exposes the effective schemas through `agent_collab_describe_options`. Unknown
keys, wrong types, unsupported values, unselected backends, and invalid
cross-field combinations are rejected with actionable paths.

Example option rules:

```toml
[agents.codex_cli]
type = "codex"
backend = "cli"
command = "codex"
args = ["exec", "--json"]

[agents.codex_cli.options]
model = "gpt-5.6-sol"
thinking_level = "high"
sandbox = "workspace-write"
approval_policy = "on-request"
search = true

[agents.claude_sdk]
type = "claude"
backend = "sdk"

[agents.claude_sdk.options]
model = "opus"
permission_mode = "default"
thinking_level = "high"
```

`backend_options.antigravity_cli` accepts `model` and `mode`; the SDK entry
accepts only `model`. Vertex, project, and location are static SDK configuration
under `agents.antigravity_sdk`, not MCP-call options. A backend entry is rejected
when it is not selected by the workflow. Configured session defaults live under
`agents.<id>.options`; MCP values override them for that session.

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
agent-collab start --workflow solo-antigravity --backend sdk --backend-options '{"antigravity_sdk":{"model":"Gemini 3.1 Pro (High)"}}' "Task"
agent-collab start --workflow solo-xai --backend-options '{"xai_cli":{"model":"grok-build"}}' "Task"
agent-collab start --workflow solo-xai --backend sdk --backend-options '{"xai_sdk":{"model":"grok-4.5","thinking_level":"low"}}' "Task"
```

The option-to-command mapping is explicit. Unknown option keys are never appended as arbitrary shell flags.

Prefer `thinking_level` across providers:

- Codex `thinking_level` accepts `minimal`, `low`, `medium`, `high`, or `xhigh` and maps to the Codex config override `model_reasoning_effort`.
- Claude `thinking_level` accepts `low`, `medium`, `high`, `xhigh`, or `max` and maps to Claude Code `--effort`.
- xAI CLI accepts `low`, `medium`, `high`, or model-specific `xhigh`; the SDK accepts `none`, `low`, `medium`, or `high`. Both map one effective value to `reasoning_effort`.
- Codex `reasoning_effort` is kept as a provider-specific alias for compatibility. Claude `thinking_budget_tokens` is kept for advanced raw-token configurations, but should not be combined with `thinking_level`.

## Agent types

Start with a small set:

```text
claude
codex
antigravity   (opt-in, disabled by default)
xai           (opt-in, disabled by default)
mock
```

`type` controls event parsing and prompt handling. `backend` controls the
execution mechanism (see [Backends](#backends)).

`command` and `args` control process launch.

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

- `cli` runs `agy -p` in print mode. Requires the `agy` CLI installed and a
  Google **OAuth sign-in cached under `~/.gemini/`**. Print mode emits **plain
  text only** (no JSON, no per-event markers), so its transcript fidelity is
  intentionally **message-only** — each non-empty output line is one
  `antigravity` message event; there is no tool/command/file-change structure.
  The default `args` include `--mode accept-edits` so `-p` does not stall on the
  interactive request-review approval prompt. Choose `sdk` for structured events.
- `sdk` runs the `google-antigravity` SDK in-process (installed with the project,
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
[agents.antigravity]
type = "antigravity"
command = "agy"
args = ["-p", "--mode", "accept-edits"]
backend = "cli"        # or "sdk" for the google-antigravity SDK
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
[agents.xai]
type = "xai"
command = "grok"
args = ["--no-auto-update", "--output-format", "streaming-json", "-p"]
backend = "cli"
enabled = true
```

### `mock`

Uses the existing mock runner and does not launch a subprocess.

Useful for tests and demos.

### Future: `command-jsonl`

Generic command that emits JSONL events. This is a future extension point for other agents.

The first version can preserve raw events and print compact verbose output for unknown shapes.

## Workflow fields

A workflow names an orchestration pattern: the ordered agent sequence a session runs. Workflows reference agent IDs:

```toml
[workflows.my-workflow]
sequence = ["agent_a", "agent_b", "agent_a"]
```

This removes hardcoded orchestration logic from the referee. Built-in workflows (`solo-claude`, `solo-codex`, `cross-review`, `compare`) still exist when no config file is present; `cross-review` is the default. Workflow names should describe the orchestration, not who "leads". The old `[modes.*]` sections are rejected with a hint.

Project workflows are safe shared composition: each referenced agent must
already exist and be enabled in built-in or global user config. A workflow that
references a project-only, unknown, or globally disabled agent is ignored with
a warning.

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
- Keep execution-relevant agent settings in global user config; project config
  is limited to display names and safe workflow composition.
- Print the command prefix before launching an agent.
- Do not print full prompts in process metadata unless verbose logging is requested.
- Keep recursive-agent guardrails in generated prompts.
- Keep `--workdir` as the session root even when an agent has a custom command path.

## Guarantees

- Users can define multiple agents of the same type.
- Workflows can reference configured agent IDs.
- Missing config falls back to built-in `claude` and `codex` defaults.
- Mock mode remains easy for tests.
- Existing CLI flags still work as overrides.
