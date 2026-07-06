# Stage 4.75: Daemonize server and typed session options

## Purpose

Make `agent-collab` usable as a long-lived local service that survives the Codex or Claude session that configures it.

This stage also makes MCP-started sessions self-describing and validated. Agents need to know which session options are accepted before they call `agent_collab_start`, and they need clear feedback when they pass an option that does not apply to Claude, Codex, the selected mode, or the configured local commands.

## Target shape

```text
agent-collab daemon start
  starts a background local server
  writes PID/state/logs under WORKDIR/.agent-collab/data/
  exposes the same HTTP and MCP endpoints

agent-collab serve
  remains foreground-only for debugging and development
```

The current foreground server stays available. Daemonization is additive.

## User experience

Start a project-local daemon:

```bash
agent-collab daemon start --workdir /repo
```

Inspect it:

```bash
agent-collab daemon status --workdir /repo
agent-collab daemon logs --workdir /repo --tail 100
```

Stop or restart it:

```bash
agent-collab daemon stop --workdir /repo
agent-collab daemon restart --workdir /repo
```

Existing clients continue to work:

```bash
agent-collab start --workdir /repo "Task"
agent-collab list
agent-collab watch
```

MCP clients continue to use:

```text
http://127.0.0.1:8765/mcp
```

## Project Data Directory

Add a project-local data root:

```text
WORKDIR/.agent-collab/data/
```

This directory is runtime state and must stay git-ignored. The repository `.gitignore` should include:

```text
.agent-collab/data/
```

Initial layout:

```text
WORKDIR/.agent-collab/data/
  daemon/
    pid
    state.json
    daemon.log
    daemon.stderr.log
  sessions/
    SESSION_ID.jsonl
    SESSION_ID.md
```

Compatibility:

- Existing `WORKDIR/.agent-collab/sessions/` logs remain readable.
- New daemon-owned sessions should default to `WORKDIR/.agent-collab/data/sessions/`.
- `agent-collab watch --workdir /repo SESSION_ID` should search the daemon data session directory first, then the legacy sessions directory.
- API responses must keep returning absolute `jsonl_path` and `markdown_path`.

## Daemon Lifecycle

Add a small daemon supervisor in the CLI using standard-library primitives.

Suggested commands:

```text
agent-collab daemon start
agent-collab daemon status
agent-collab daemon stop
agent-collab daemon restart
agent-collab daemon logs
```

Implementation requirements:

- Start a detached Python child process running the existing server code.
- Bind to `127.0.0.1` by default.
- Refuse to start when a live daemon PID/state already exists for the same workdir and port.
- Detect and clean stale PID files.
- Write daemon PID, host, port, workdir, data dir, started-at timestamp, and server URL to `state.json`.
- Redirect daemon stdout/stderr to files under `data/daemon/`.
- Keep `agent-collab serve` foreground-only; do not make `serve` fork itself.

Stop behavior:

- Send graceful terminate first.
- Wait for a short grace period.
- Kill if still running.
- Remove stale PID/state only after confirming process exit or staleness.

Out of scope for this stage:

- systemd/launchd service files.
- Multi-user remote daemon deployment.
- Public network binding.
- Auth beyond any already planned Stage 5 hardening.

## Server Logging

Daemon mode should separate operational logs from transcripts.

Operational logs:

```text
WORKDIR/.agent-collab/data/daemon/daemon.log
WORKDIR/.agent-collab/data/daemon/daemon.stderr.log
```

Session logs:

```text
WORKDIR/.agent-collab/data/sessions/SESSION_ID.jsonl
WORKDIR/.agent-collab/data/sessions/SESSION_ID.md
```

Logging rules:

- Log startup, shutdown, requests, MCP tool calls, session lifecycle, and request errors.
- Do not dump full transcript events into daemon operational logs by default.
- Transcript content belongs in session JSONL/Markdown logs and event read APIs.
- Keep enough log context to debug MCP client registration and failed start requests.

Add retention later through Stage 5 hardening. Do not delete logs automatically in this stage.

## Agent Guidance

MCP clients need explicit guidance before starting sessions. Add or update MCP instructions so agents understand the workflow:

```text
1. Call agent_collab_describe_options before starting a session when you need non-default model or reasoning settings.
2. Use agent_collab_start with task, mode, workdir, max_turns, timeout, and typed agent options.
3. Use agent_collab_wait_events with a cursor for watches; do not make one indefinitely blocking call.
4. If a start request returns isError, fix the invalid option and retry instead of guessing.
```

Add a new MCP tool:

```text
agent_collab_describe_options
```

Purpose:

- Return available modes.
- Return configured agent IDs and types.
- Return which agent types are used by each mode.
- Return accepted `codex_options` and `claude_options` schema.
- Return defaults and allowed values when known.
- Return examples for common starts.

The existing `agent_collab_start` tool description should mention this tool.

## Start Request Shape

Extend the session start payload. Keep existing fields stable:

```json
{
  "task": "Review this repository",
  "mode": "codex-leads",
  "workdir": "/repo",
  "max_turns": 3,
  "timeout": 900,
  "mock": false,
  "dry_run": false,
  "codex_options": {},
  "claude_options": {}
}
```

`codex_options` and `claude_options` are intentionally separate because the CLIs expose different concepts and accepted values.

### Codex Options

Initial shape:

```json
{
  "model": "gpt-5-codex",
  "profile": "readonly",
  "thinking_level": "medium",
  "sandbox": "workspace-write",
  "approval_policy": "on-request",
  "search": false
}
```

Validation:

- `model`: string; allowed values may come from config.
- `profile`: string; must match a configured Codex profile when configured.
- `thinking_level`: enum mapped to Codex `model_reasoning_effort`; expected values are `minimal`, `low`, `medium`, `high`, or `xhigh`.
- `reasoning_effort`: provider-specific alias for `thinking_level`, kept for compatibility.
- `sandbox`: enum matching Codex CLI values the project chooses to expose.
- `approval_policy`: enum matching Codex CLI values the project chooses to expose.
- `search`: boolean.
- Unknown keys are rejected.

Mapping must be explicit. Do not blindly append arbitrary option keys to a shell command.

### Claude Options

Initial shape:

```json
{
  "model": "opus",
  "permission_mode": "default",
  "thinking_level": "high"
}
```

Validation:

- `model`: string; allowed values may come from config.
- `permission_mode`: enum when supported by the configured Claude command.
- `thinking_level`: enum mapped to Claude Code `--effort`; expected values are `low`, `medium`, `high`, `xhigh`, or `max`.
- `thinking_budget_tokens`: advanced raw-token compatibility field, bounded by configured min/max if enabled; do not combine with `thinking_level`.
- Unknown keys are rejected.

Mapping must be explicit. Do not blindly append arbitrary option keys to a shell command.

### Mode-Aware Validation

Validation should consider the selected mode:

- If a mode does not include any Codex agent, non-empty `codex_options` should be rejected.
- If a mode does not include any Claude agent, non-empty `claude_options` should be rejected.
- If a mode includes multiple Codex or Claude agent IDs and options need to differ by agent, add a later explicit per-agent shape instead of overloading the type-level shape.

Possible future per-agent shape:

```json
{
  "agent_options": {
    "codex_review": {"model": "gpt-5-codex"},
    "claude_writer": {"model": "sonnet"}
  }
}
```

Do not implement this future shape unless it is needed.

## Option Configuration

Agent config should advertise which options are accepted.

Example:

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

Built-in defaults can expose a conservative subset. Project config can tighten allowed values.

The option schema returned to agents should be derived from the effective config plus built-in defaults.

## Validation Feedback

Invalid start requests should fail before any subprocess is launched.

For MCP:

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\n  \"error\": \"invalid_start_options\",\n  \"details\": [\n    {\"path\": \"codex_options.thinking_level\", \"message\": \"unsupported value 'maximum'; expected one of: minimal, low, medium, high, xhigh\"}\n  ]\n}"
    }
  ],
  "isError": true
}
```

For CLI:

```text
ERROR invalid_start_options
codex_options.thinking_level: unsupported value 'maximum'; expected one of: minimal, low, medium, high, xhigh
```

Requirements:

- Include a machine-readable error code.
- Include JSON-path-like field paths.
- Include the accepted values when known.
- Return all validation errors in one response when practical.
- Do not start a session when validation fails.

## Implementation Steps

1. Add `.agent-collab/data/` to `.gitignore`.
2. Add a `DataPaths` helper that resolves project data, daemon, and session directories.
3. Update daemon-owned session log defaults to the data session directory.
4. Preserve legacy session log lookup in `watch`.
5. Add daemon CLI commands and PID/state/log file handling.
6. Add start request option dataclasses or typed dictionaries for `codex_options` and `claude_options`.
7. Add option schema derivation from config and built-in defaults.
8. Add validation before `SessionManager.start_session` creates state or subprocess tasks.
9. Add `agent_collab_describe_options`.
10. Update `agent_collab_start` MCP input schema and descriptions.
11. Update README, `doc/daemon-architecture.md`, and `doc/agent-configuration.md`.

## Tests

Add focused tests for:

- Data path resolution under `WORKDIR/.agent-collab/data/`.
- `.agent-collab/data/sessions` used for daemon-owned session logs.
- Legacy `.agent-collab/sessions` still readable by `watch`.
- Daemon start writes PID/state/log paths.
- Daemon start refuses a live duplicate and cleans stale PID state.
- Daemon stop terminates the process and updates/removes state.
- `agent_collab_describe_options` returns modes, agent types, and schemas.
- Valid `codex_options` and `claude_options` pass validation.
- Unknown option keys are rejected.
- Wrong option types are rejected.
- Unsupported enum values are rejected with accepted values.
- Mode-inapplicable options are rejected.
- MCP start failures return `isError: true`.
- CLI start failures print actionable field-path errors.

## Acceptance Criteria

- A user can start `agent-collab` as a background project-local daemon without tying it to a Codex terminal session.
- The daemon writes PID/state/operational logs under git-ignored `WORKDIR/.agent-collab/data/`.
- Daemon-owned session logs are stored under the project data directory and remain visible through CLI and MCP.
- MCP agents can discover supported start options before starting a session.
- `agent_collab_start` validates typed Claude/Codex options before launching any subprocess.
- Invalid options produce clear, actionable feedback to both MCP and CLI callers.
- Existing foreground `serve`, one-shot CLI, stdio MCP adapter, direct HTTP MCP endpoint, and plain `watch` continue to work.
