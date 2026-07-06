# Stage 2: Local daemon and session manager

## Purpose

Introduce a long-running process that owns sessions and lets multiple clients join the same collaboration.

This stage changes ownership of live state but should reuse the existing `Referee`, runners, event model, and log writer.

## User experience

Start the daemon:

```bash
agent-collab serve --host 127.0.0.1 --port 8765
```

Start a mock session through the daemon API:

```bash
agent-collab start --mock --workdir /repo "task"
```

Watch it:

```bash
agent-collab watch SESSION_ID
```

## Modules

Add:

```text
agent_collab/daemon.py
agent_collab/server_http.py
agent_collab/client.py
```

`daemon.py`:

- Defines `SessionState`.
- Defines `SessionManager`.
- Owns background tasks.
- Stores session metadata and status.
- Provides cursor-based event reads.
- Provides long-poll `wait_events`.

`server_http.py`:

- Exposes daemon operations over local HTTP.
- Converts request/response payloads to JSON.
- Keeps transport concerns out of the session manager.

`client.py`:

- Small Python client used by CLI and MCP.
- Uses stdlib HTTP first.

## Session manager API

Suggested internal API:

```python
class SessionManager:
    async def start_session(self, request: StartSessionRequest) -> SessionState: ...
    async def stop_session(self, session_id: str) -> None: ...
    def list_sessions(self) -> list[SessionState]: ...
    def get_session(self, session_id: str) -> SessionState: ...
    def read_events(self, session_id: str, cursor: int) -> EventBatch: ...
    async def wait_events(self, session_id: str, cursor: int, timeout_ms: int) -> EventBatch: ...
```

## HTTP API

Minimum endpoints:

```text
POST /sessions
GET  /sessions
GET  /sessions/{session_id}
GET  /sessions/{session_id}/events?cursor=N
GET  /sessions/{session_id}/events/wait?cursor=N&timeout_ms=30000
GET  /sessions/{session_id}/transcript
POST /sessions/{session_id}/stop
```

`POST /sessions` request:

```json
{
  "task": "string",
  "mode": "claude-leads",
  "workdir": "/repo",
  "max_turns": 3,
  "timeout": 900,
  "mock": false,
  "dry_run": false
}
```

Response:

```json
{
  "session_id": "session-id",
  "status": "running",
  "jsonl_path": "...",
  "markdown_path": "..."
}
```

## Event delivery

The session manager needs both durable logs and live notification.

Implementation approach:

- Keep `SessionLogger` for JSONL and Markdown.
- Add a live in-memory list of event dicts per session.
- Add an `asyncio.Condition` per session.
- Every emitted event appends to memory and notifies watchers.
- `wait_events` waits on the condition with timeout.

## Referee integration

The existing `Referee` accepts a printer callback but writes logs internally. For daemon mode, it should accept an event sink or logger abstraction so the daemon can append to memory and write logs in one place.

Recommended small refactor:

```python
class EventSink:
    async def emit(self, event: Event) -> None: ...
```

The one-shot CLI sink prints and logs.

The daemon sink logs, stores in memory, and notifies watchers.

## Tests

Add tests for:

- Starting a mock session through `SessionManager`.
- Reading event batches by cursor.
- `wait_events` returning when a new event arrives.
- Stop request transitions a running session to `stopped`.
- Logs still appear under `WORKDIR/.agent-collab/sessions/`.

## Acceptance criteria

- Daemon can run a mock session to completion.
- Multiple clients can read the same session events by cursor.
- CLI one-shot still works.
- No MCP changes are required in this stage.
