# Stage 5: Hardening and operations

## Purpose

Make the daemon model safer and more usable after the basic joinable session flow works.

This stage should not add major new collaboration features. It should make the existing design reliable enough for repeated local use.

## Security hardening

Default behavior:

- Bind daemon to `127.0.0.1`.
- Do not expose on a public interface by default.
- Keep `--workdir` explicit.
- Keep command paths configurable.
- Keep max turns and timeouts required or defaulted.
- Keep agent prompts from spawning recursive agents.

Add optional controls:

```text
--auth-token-env AGENT_COLLAB_TOKEN
--allowed-workdir /repo
--allowed-workdir /another/repo
--max-sessions 10
```

If an auth token is configured, CLI and MCP clients must send it.

## Process lifecycle

Add robust session stopping:

- Graceful terminate first.
- Kill after grace period.
- Mark session `stopped`.
- Emit a final `ERROR` or `REFEREE status` event explaining the stop.

Track process metadata:

- PID.
- Start time.
- End time.
- Return code.
- Runner command prefix, without dumping full prompts in process metadata.

## Log retention

Done — implemented and closed via
[session-retention-and-pruning.md](../tasks_closed/session-retention-and-pruning.md)
and issue #5. Completed sessions are retained for 30 days by default, with user-owned
configuration to change or disable automatic pruning and a manual preview/apply
command:

```text
agent-collab sessions prune --dry-run
agent-collab sessions prune --older-than 7d --keep 100 --apply
```

Never prune active sessions or automatically delete transcripts outside the
global managed session directory.

## Observability

Add a daemon status endpoint:

```text
GET /health
```

Response:

```json
{
  "status": "ok",
  "active_sessions": 1,
  "total_sessions": 12
}
```

Add structured daemon logs to stderr. Do not mix daemon logs with MCP stdio protocol output.

## Compatibility

Before removing any old behavior:

- Keep one-shot CLI mode.
- Keep direct JSONL file watch mode.
- Keep MCP tool names stable.
- Keep JSONL event schema stable.

If a schema change is needed, add fields rather than renaming existing fields:

```json
{
  "timestamp": "...",
  "source": "codex",
  "type": "message",
  "text": "...",
  "raw": {},
  "session_id": "...",
  "sequence": 12
}
```

## Tests

Add tests for:

- Stop escalation from terminate to kill.
- Auth required and auth success paths.
- Workdir allowlist rejection.
- Session failure status and final event.
- Log pruning dry-run behavior.

## Acceptance criteria

- Daemon can be left running during local development.
- Sessions can be stopped reliably.
- Auth and workdir limits are available for users who need them.
- Existing CLI, log, and MCP workflows keep working.
