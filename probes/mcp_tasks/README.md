# MCP Tasks client compatibility probe

This is a deliberately tiny **Streamable HTTP** MCP server used to determine
whether Claude Code or Codex actually implements protocol-level MCP Tasks. It
is a manual compatibility harness, not part of the normal integration-test
runner. It listens only on localhost, at:

```text
http://127.0.0.1:48623/mcp
```

It is not part of agent-collab. It has one read-only tool,
`delayed_echo(seconds, message)`, and two mutually exclusive protocol modes:

- `legacy`: MCP `2025-11-25` experimental Tasks. The tool advertises
  `execution.taskSupport: required`. A supporting client adds `params.task`,
  receives a task handle immediately, polls `tasks/get`, and retrieves the
  final `CallToolResult` with `tasks/result`.
- `extension`: a provisional probe for the newer
  `io.modelcontextprotocol/tasks` extension. A supporting client uses the
  modern discovery/per-request capability flow, accepts `resultType: task`,
  and polls `tasks/get`. Its internal lifecycle control passes, but this mode
  has not yet been validated against an independent conforming client, so a
  negative client result is preliminary.

There is no authentication. The server binds only to `127.0.0.1`, and the
Claude/Codex configurations below need no credentials, headers, or environment
variables.

The server emits the complete exchange to stderr and to a durable log:

- TCP open/close and remote/local addresses
- wall-clock and monotonic timestamps
- request method, path, protocol headers, and all other headers
- complete request and response JSON bodies
- response status and byte count
- task creation, completion, polling, result retrieval, and cancellation
- connection exceptions and tracebacks

Authorization and cookie header values are redacted. Everything else is
logged, so use this only as a local diagnostic server. Generated
`probe-*.log` files are intentionally ignored; summarize durable findings in
the matching task document instead of committing raw client logs.

## Start the server

Start with legacy mode because its complete `2025-11-25` lifecycle has a
passing positive control:

```bash
cd probes/mcp_tasks
./run_server.sh legacy
```

The default log is `probe-legacy-48623.log`. It is appended across restarts;
each run begins with a separator. To select a different port or log:

```bash
./run_server.sh legacy 8910 /tmp/mcp-tasks-legacy.log
```

Watch the wire log from another terminal:

```bash
tail -F probes/mcp_tasks/probe-legacy-48623.log
```

Use a different port when running the extension mode concurrently:

```bash
./run_server.sh extension 48624
```

Run its internal lifecycle control against a separate extension server:

```bash
./run_server.sh extension 48626
# In another terminal:
python3 ./self_test_extension.py 48626
```

This checks the probe's discovery, request capability, task creation, polling,
and completed-result shapes. It is not an independent MCP implementation, so
it does not replace the future positive-control requirement below.

## Add it to Codex

Codex must be restarted after changing its MCP configuration:

```bash
codex mcp add tasksprobe --url http://127.0.0.1:48623/mcp
```

After restarting Codex, ask:

```text
Call delayed_echo from tasksprobe with seconds=2 and message="hello from Codex".
Report exactly what the tool returns.
```

For a disposable `codex exec` run that does not modify persistent
configuration:

```bash
./run_codex.sh 2
```

Remove the persistent server when finished:

```bash
codex mcp remove tasksprobe
```

## Add it to Claude Code

Add the Streamable HTTP server to the current project:

```bash
claude mcp add --transport http --scope local \
  tasksprobe http://127.0.0.1:48623/mcp
```

Restart Claude Code, then ask:

```text
Call mcp__tasksprobe__delayed_echo with seconds=2 and
message="hello from Claude". Report exactly what the tool returns.
```

For a disposable `claude -p` run:

```bash
./run_claude.sh 2
```

Remove the persistent server when finished:

```bash
claude mcp remove --scope local tasksprobe
```

## How to interpret the legacy log

The server accepts initialization only when the client requests MCP
`2025-11-25`. Tasks were introduced in that protocol version, so a client that
requests an older version is rejected with:

```text
LEGACY PROTOCOL UNSUPPORTED
```

That is already a negative result for MCP `2025-11-25` Tasks support. The
strict probe rejects the older request instead of continuing a mixed-version
compatibility session, so later tool behavior cannot be mistaken for a
successfully negotiated Tasks session.

Support is proven only if the log shows this sequence:

1. `initialize` and a negotiated protocol version of `2025-11-25`
2. `tools/list`
3. `tools/call` containing a `params.task` object
4. `LEGACY TASK CREATED`
5. one or more `tasks/get` calls
6. `tasks/result`

For a successfully negotiated `2025-11-25` session, the decisive negative
signature is:

```text
LEGACY TASK UNSUPPORTED: tools/call omitted params.task
```

Because the tool declares task support as `required`, calling it normally is
not a valid fallback. Merely parsing the `execution` field or containing task
types in the client binary is not support.

Run a positive control against a legacy server on port 48625:

```bash
./run_server.sh legacy 48625
# In another terminal:
python3 ./self_test_legacy.py 48625
```

The control requests `2025-11-25`, creates a task, polls `tasks/get`, and
retrieves `tasks/result`. A `PASS` confirms that the server-side lifecycle is
reachable by a conforming client.

## How to interpret the extension log

Support is proven if the client:

1. calls `server/discover`
2. includes `io.modelcontextprotocol/tasks` in its per-request client
   capabilities
3. accepts the server's `resultType: task` response
4. polls `tasks/get` until the completed result is present

An `initialize` request in extension-only mode demonstrates that the client
is using the legacy lifecycle for this connection. A tool request without the
extension capability produces:

```text
EXTENSION UNSUPPORTED: client capability is absent
```

Test CLI, IDE, and desktop surfaces separately. Shared MCP configuration does
not prove that their host loops implement the same task behavior.
