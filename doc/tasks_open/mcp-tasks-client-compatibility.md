# Re-evaluate MCP Tasks client compatibility

**Status:** Open — monitor client and protocol support; no agent-collab
implementation is planned until the relevant task flow is independently
validated and supported by at least one target client.

**Created:** 2026-07-25.

**Issue:** [#53](https://github.com/lauriparviainen/agent_collab/issues/53)

## Context

The cancelled `sse-collection-transport-evaluation` established that response
streaming cannot provide a cross-client non-blocking collection primitive.
Claude Code can move a sufficiently long streamed MCP call into its own
background task, but Codex remains blocked for the duration of the call.

Protocol-level MCP Tasks are a better conceptual fit: the requestor explicitly
asks for deferred execution, receives a task identifier, polls task state, and
retrieves the result later. This could eventually let
`agent_collab_wait_result` return control without depending on transport
timing or undocumented client backgrounding.

Two task designs are relevant:

1. **MCP `2025-11-25` task-augmented requests.** A server advertises
   `capabilities.tasks.requests.tools.call`; a tool declares
   `execution.taskSupport`; the client adds `params.task` to `tools/call`,
   receives `CreateTaskResult`, polls `tasks/get`, and retrieves the original
   tool result through `tasks/result`. Tasks are experimental in this protocol
   version.
2. **The newer `io.modelcontextprotocol/tasks` extension.** This uses extension
   discovery and per-request capability metadata. Its probe mode is
   provisional and must be checked against an independent conforming
   implementation before a negative client result is treated as definitive.

The tracked manual compatibility harness lives in `probes/mcp_tasks/`. It is
not part of the hermetic or automatic integration-test suites and may launch
credentialed vendor CLIs when its client runners are invoked. Raw wire logs are
ignored; durable results belong in this document.

## Findings on 2026-07-25

### Codex

Codex did not interoperate with either task design.

| Client | Probe | Result |
| --- | --- | --- |
| Codex CLI 0.145.0 and Codex client 0.146.0-alpha.3 | MCP `2025-11-25` compatibility probe | Codex initialized with `2025-06-18`. When the server offered `2025-11-25`, Codex continued far enough to list and call the required-task tool but omitted `params.task`; it never called `tasks/get` or `tasks/result`. This is supplemental evidence because the client had offered the older version. |
| Codex client 0.146.0-alpha.3.1 | Strict MCP `2025-11-25` probe | Codex again requested `2025-06-18`; the corrected server rejected the unsupported version during initialization, before tool discovery. |
| Codex CLI 0.145.0 and Codex client 0.146.0-alpha.3 | `io.modelcontextprotocol/tasks` extension probe | Codex used legacy `initialize` with `2025-06-18`, never sent `server/discover`, and never declared the Tasks extension capability. The extension probe's internal lifecycle control passes, but it still needs an independent positive control, so this is preliminary rather than a conformance claim. |

The strict legacy result is decisive for the tested versions. MCP Tasks were
introduced in `2025-11-25`, while each Codex client offered `2025-06-18`. The
probe's positive-control client negotiated `2025-11-25`, created a task,
polled `tasks/get`, and retrieved `tasks/result`, proving that the server-side
legacy lifecycle is reachable.

The extension probe was audited again against the current `2026-07-28` draft
and Tasks extension shapes. That review corrected missing `resultType` fields
on complete results and removed an obsolete advertised protocol version. Its
tracked internal control now completes discovery, task creation, polling, and
completed-result retrieval. This validates the probe's own lifecycle, but not
interoperability with an independent implementation.

### Claude Code

Neither task design has been tested with Claude Code yet. Claude Code's own
automatic backgrounding of an ordinary long MCP call is a separate
client-specific feature and is not evidence of protocol-level MCP Tasks
support.

Testing Claude Code against both probe modes is required before making any
Claude compatibility claim.

## Decisions

- Keep agent-collab's existing bounded watch-and-harvest guidance.
- Do not add MCP Tasks to the production server while target-client support is
  absent or unproven.
- Preserve the manual wire-level probe so client updates can be evaluated
  without rebuilding the harness.
- Treat the legacy `2025-11-25` probe as validated by its positive control.
- Treat extension-mode negatives as provisional until that mode has an
  independent positive control or is rebuilt using an official SDK example.
  Its tracked internal lifecycle control only guards the probe's own wire flow.
- Record exact client versions and HTTP-level evidence for every rerun; do not
  infer support from model narration, type definitions, or a client accepting
  unknown response fields.

## Next evaluation

1. Validate the extension mode against an independent conforming client, and
   recheck its schema whenever the draft protocol changes.
2. Test current Claude Code in both legacy and extension modes:
   - CLI print mode;
   - terminal or editor interactive mode if their MCP host differs;
   - verify task creation, `tasks/get`, result retrieval, and whether the model
     is actually free while the task runs.
3. Re-run both modes after a Codex release advertises `2025-11-25`, the Tasks
   extension, or public release notes claim Tasks support.
4. If a client supports Tasks, test operational behavior that matters to
   agent-collab:
   - task visibility to the model and user;
   - polling and result delivery;
   - cancellation;
   - reconnect and client restart behavior;
   - multiple concurrent tasks;
   - task TTL and cleanup;
   - behavior when a delegated session becomes `awaiting_input`.
5. Only then decide whether to map `agent_collab_wait_result`,
   `agent_collab_wait_events`, or a new collection tool onto MCP Tasks.

## Probe commands

From `probes/mcp_tasks/`:

```bash
# Validated legacy server
./run_server.sh legacy 48624

# Positive control, normally against a second legacy server on 48625
python3 ./self_test_legacy.py 48625

# Disposable vendor-client runs
PROBE_PORT=48624 ./run_codex.sh 2
PROBE_PORT=48624 ./run_claude.sh 2

# Provisional extension server
./run_server.sh extension 48624

# Internal extension lifecycle control (use two terminals)
./run_server.sh extension 48626
python3 ./self_test_extension.py 48626
```

Persistent client configurations require restarting the client after changing
the probe mode.

## Done when

- The extension probe has an independent positive control.
- Claude Code has dated, versioned results for both task designs.
- Codex is retested when its advertised protocol or extension capabilities
  change.
- A client that claims support completes the full task lifecycle at the wire
  level and demonstrates that the calling model is not blocked.
- The task records an explicit adopt-or-decline decision for agent-collab.
