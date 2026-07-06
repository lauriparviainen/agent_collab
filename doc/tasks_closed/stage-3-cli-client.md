# Stage 3: CLI client commands

## Purpose

Make the CLI a client of the daemon while keeping one-shot mode available.

At the end of this stage, a human can start the daemon, start sessions, watch live output, inspect status, and stop sessions from the terminal.

## Commands

```bash
agent-collab serve --host 127.0.0.1 --port 8765
agent-collab start --mock --workdir /repo "task"
agent-collab watch SESSION_ID
agent-collab status SESSION_ID
agent-collab list
agent-collab stop SESSION_ID
```

Keep one-shot compatibility:

```bash
agent-collab --mock --workdir /repo "task"
```

## Configuration

Add daemon connection flags:

```text
--server-url http://127.0.0.1:8765
```

Optional environment variable:

```text
AGENT_COLLAB_SERVER=http://127.0.0.1:8765
```

The flag wins over the environment variable.

## Watch behavior

`watch SESSION_ID` should:

1. Call `GET /sessions/{session_id}/events?cursor=0`.
2. Print the returned events.
3. Repeatedly call `wait_events` with the latest cursor.
4. Stop when the session reaches `done`, `failed`, or `stopped`, unless `--follow` is explicit.

Direct file watching from Stage 1 should remain available:

```bash
agent-collab watch /path/to/session.jsonl
```

## Start behavior

`start` should print the session id and log paths:

```text
session_id: 20260706-...
status: running
jsonl: /repo/.agent-collab/sessions/...
markdown: /repo/.agent-collab/sessions/...
```

Add a convenience option:

```bash
agent-collab start --watch --mock --workdir /repo "task"
```

This starts the session and immediately enters watch mode.

## Tests

Add tests for:

- CLI parsing for all subcommands.
- `start --watch` calls start then watch.
- Status/list/stop client calls.
- File watch still works without daemon.

Integration tests can use a mock daemon or run the real local daemon on an ephemeral port.

## Acceptance criteria

- Human workflow works without MCP:

```bash
agent-collab serve
agent-collab start --watch --mock --workdir . "task"
```

- One-shot CLI behavior still works.
- Watch output matches the original terminal transcript style.
