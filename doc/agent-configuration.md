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

Initial lookup order:

```text
WORKDIR/.agent-collab/config.toml
~/.agent-collab/config.toml
built-in defaults
```

Project config should win over user config. CLI flags should win over both.
Agent commands should be changed in `agent-collab` config, not through dedicated Claude/Codex path flags.

## Example

```toml
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

[modes.claude-leads]
sequence = ["claude", "codex", "claude"]

[modes.codex-leads]
sequence = ["codex", "claude", "codex"]

[modes.debate]
sequence = ["claude", "codex", "claude", "codex"]

[modes.readonly-review]
sequence = ["codex_readonly", "claude", "codex_readonly"]
```

## Agent fields

Required fields:

```toml
type = "codex"
enabled = true
```

Required for subprocess agents:

```toml
command = "codex"
args = ["exec", "--json"]
```

Optional fields:

```toml
name = "display-name"
env = { KEY = "VALUE" }
cwd = "/path/override"
timeout = 900
```

Avoid adding broad permission fields at first. Permission policy should remain explicit in the underlying agent command or profile.

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

Config can advertise accepted values and defaults, and the MCP layer exposes the effective schema through `agent_collab_describe_options`. Defaults are applied when callers omit an option. Unknown keys, wrong types, unsupported values, and options that do not apply to the selected mode are rejected with actionable field-path errors.

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

CLI callers can pass JSON option objects:

```bash
agent-collab start --codex-options '{"thinking_level":"medium"}' --claude-options '{"model":"opus","thinking_level":"high"}' "Task"
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
mock
```

`type` controls event parsing and prompt handling.

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

### `mock`

Uses the existing mock runner and does not launch a subprocess.

Useful for tests and demos.

### Future: `command-jsonl`

Generic command that emits JSONL events. This is a future extension point for other agents.

The first version can preserve raw events and print compact verbose output for unknown shapes.

## Mode fields

Modes should reference agent IDs:

```toml
[modes.my-mode]
sequence = ["agent_a", "agent_b", "agent_a"]
```

This removes hardcoded mode logic from the referee. Built-in modes can still exist when no config file is present.

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
- Mode references only known enabled agents.

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
- Modes can reference configured agent IDs.
- Missing config falls back to built-in `claude` and `codex` defaults.
- Mock mode remains easy for tests.
- Existing CLI flags still work as overrides.
