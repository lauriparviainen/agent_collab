# Stage 1: Watch and attach to logs

## Purpose

Add a human attach path before changing session ownership.

This stage gives the user a way to watch any existing session log, including logs created by the current MCP subprocess model.

## User experience

```bash
agent-collab watch /path/to/session.jsonl
```

Optional convenience:

```bash
agent-collab watch --workdir /repo --session-id mcp-1
```

Expected output uses the existing readable terminal labels:

```text
HUMAN   ...
REFEREE ...
CLAUDE  ...
CODEX   ...
TOOL    ...
ERROR   ...
```

## Implementation

Add a module:

```text
agent_collab/watch.py
```

Responsibilities:

- Resolve a JSONL path from either:
  - a direct path,
  - `--workdir` plus `--session-id`.
- Read existing events from the start or from a requested cursor.
- Pretty-print events using `terminal.print_event`.
- Follow the file until interrupted.

Suggested API:

```python
def watch_jsonl(path: Path, follow: bool = True, start_cursor: int = 0) -> None:
    ...
```

## CLI changes

The current CLI uses a single optional `task` argument. Introduce subcommands without breaking one-shot use:

```bash
agent-collab watch SESSION_OR_PATH
```

Keep this working:

```bash
agent-collab --mock "task"
```

Implementation can detect known subcommands first and otherwise fall back to the existing one-shot parser.

## Tests

Add tests for:

- Reading an existing JSONL transcript.
- Cursor behavior.
- Path resolution from `workdir/session_id`.
- Compact behavior on malformed JSONL lines.

## Acceptance criteria

- A session started by the current MCP server can be watched with `agent-collab watch`.
- A session started by one-shot CLI can be watched after completion.
- The command exits cleanly on Ctrl-C.
- No daemon exists yet.
