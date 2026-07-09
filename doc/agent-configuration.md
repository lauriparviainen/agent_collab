# Agent configuration

## Purpose

`agent-collab` needs its own config file for registering which agents are available and how to run them.

This is separate from Codex configuration:

```text
agent-collab config -> external agents available to the referee
Codex config        -> how Codex itself runs, loads MCP servers, and applies sandbox policy
```

The referee should not assume there is exactly one Claude command and one Codex command. Users may have several variants:

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

Project config wins over user config and comes from the session `workdir`, never the caller's shell directory. CLI flags win over both.
Agent commands should be changed in `agent-collab` config, not through dedicated Claude/Codex path flags.

Built-in defaults are stored in [agent_collab/default_config.toml](../agent_collab/default_config.toml), so the base agent commands, option defaults, and built-in workflows are inspectable without reading Python code.

Config files declare a top-level `schema_version` (currently `2`; a missing version means `1`). Known old shapes are migrated in memory by `agent_collab/config_migrations.py` before validation; unknown fields are still rejected afterwards. Inspect the effective merged config with `agent-collab config show --workdir PROJECT`.

## Example

```toml
schema_version = 2

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

An agent's `type` (the *provider*: `claude`, `codex`, `antigravity`) is separate
from its `backend` (the *execution mechanism*). The registry is keyed by
`(type, backend)`:

| provider (`type`) | `cli`                | `sdk`                           |
| ----------------- | -------------------- | ------------------------------- |
| `claude`          | ✅ (default)          | ✅ (`claude-agent-sdk`, typed)   |
| `codex`           | ✅ (default)          | ✅ (`openai-codex`, message-first) |
| `antigravity`     | ✅ (`agy`, plain text) | ✅ (`google-antigravity`, typed) |

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
selected agent's type does not register that backend. Explicitly-requested typed
options are also rejected before start when the resolved backend cannot honour
them (a cli-only option on `sdk`, or an sdk-only option on `cli`), with a
`<type>_options.<key>` field path.

Every backend this stage reports `resume`, `interrupt`, and `tool_gate` as
`false` — capabilities are honest runtime facts, never inferred from the provider
brand. `agent_collab_describe_options` exposes, per agent type, the registered
backend ids, the default, live availability/health, and capability flags, so the
selection is discoverable before starting. `mock` agents ignore backend selection
and reject a `backend` field.

## Start options

MCP and CLI callers can pass typed per-agent-type start options. The server validates these options before creating session state or launching a subprocess.

The request shape keeps Codex and Claude options separate because their CLIs expose different controls:

```json
{
  "codex_options": {
    "model": "gpt-5-codex",
    "thinking_level": "medium",
    "sandbox": "workspace-write",
    "approval_policy": "on-request"
  },
  "claude_options": {
    "model": "opus",
    "permission_mode": "default",
    "thinking_level": "high"
  }
}
```

Config can advertise accepted values and defaults, and the MCP layer exposes the effective schema through `agent_collab_describe_options`. Defaults are applied when callers omit an option. Unknown keys, wrong types, unsupported values, and options that do not apply to the selected workflow are rejected with actionable field-path errors. The start response echoes the effective settings, including a prompt-free `command_preview` per agent.

Example option rules:

```toml
[agents.codex.options]
model.allowed = ["gpt-5-codex", "gpt-5"]
thinking_level.default = "high"
thinking_level.allowed = ["minimal", "low", "medium", "high", "xhigh"]
sandbox.allowed = ["read-only", "workspace-write"]
approval_policy.allowed = ["on-request", "never"]
search.allowed = [true, false]

[agents.claude.options]
model.default = "opus"
model.allowed = ["sonnet", "opus"]
permission_mode.default = "default"
permission_mode.allowed = ["default", "acceptEdits"]
thinking_level.default = "high"
thinking_level.allowed = ["low", "medium", "high", "xhigh", "max"]
thinking_budget_tokens.min = 0
thinking_budget_tokens.max = 32768
```

Antigravity agents accept `antigravity_options` (`model`, and `mode` for the
`cli` backend only — one of `default`, `accept-edits`, `plan`). `mode` maps to
`agy --mode`; on the `sdk` backend `mode` is rejected until a faithful SDK
equivalent is confirmed. `antigravity_options` are rejected when the selected
workflow has no Antigravity agent.

CLI callers can pass JSON option objects and select a backend:

```bash
agent-collab start --codex-options '{"thinking_level":"medium"}' --claude-options '{"model":"opus","thinking_level":"high"}' "Task"
agent-collab start --workflow solo-antigravity --backend sdk --antigravity-options '{"model":"gemini-3-pro"}' "Task"
```

The option-to-command mapping is explicit. Unknown option keys are never appended as arbitrary shell flags.

Prefer `thinking_level` for both built-in agent types:

- Codex `thinking_level` accepts `minimal`, `low`, `medium`, `high`, or `xhigh` and maps to the Codex config override `model_reasoning_effort`.
- Claude `thinking_level` accepts `low`, `medium`, `high`, `xhigh`, or `max` and maps to Claude Code `--effort`.
- Codex `reasoning_effort` is kept as a provider-specific alias for compatibility. Claude `thinking_budget_tokens` is kept for advanced raw-token configurations, but should not be combined with `thinking_level`.

## Agent types

Start with a small set:

```text
claude
codex
antigravity   (opt-in, disabled by default)
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
  degrading to message-only if a turn has no tool calls. `antigravity_options.mode`
  is cli-only and rejected on `sdk`.

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

## CLI commands

Add later:

```bash
agent-collab config init
agent-collab agents list
agent-collab agents doctor
```

`agents doctor` should check:

- Agent is enabled.
- Command exists on `PATH` or at the configured absolute path.
- Required output parser exists for `type`.
- Workflow references only known enabled agents.

## Implementation notes

Use Python 3.11 `tomllib` when available. The current host uses Python 3.9, so the prototype needs either:

- a tiny minimal TOML reader for the subset above,
- optional `tomli` for Python < 3.11,
- or JSON config until the package formally requires Python 3.11.

Given the project goal is Python 3.11+, the clean long-term design is TOML with `tomllib`.

## Safety considerations

- Config registration is not permission approval.
- Do not automatically trust arbitrary project config in shared directories.
- Print the command prefix before launching an agent.
- Do not print full prompts in process metadata unless verbose logging is requested.
- Keep recursive-agent guardrails in generated prompts.
- Keep `--workdir` as the session root even when an agent has a custom command path.

## Acceptance criteria

- Users can define multiple agents of the same type.
- Workflows can reference configured agent IDs.
- Missing config falls back to built-in `claude` and `codex` defaults.
- Mock mode remains easy for tests.
- Existing CLI flags still work as overrides.
