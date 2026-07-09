# Stage 5.3 - Daemon HTTP API Contract + Loopback Auth

Status: draft, not approved for implementation.

## Goal

Give the CLI <-> daemon HTTP API a single source of truth so the upcoming TUI
data layer is stable, and add lightweight loopback authentication. The TUI
refactor in [stage-5.2](stage-5.2-calm-tui-cleanup/README.md) should sit on a
**typed, versioned client** — not hand-shaped dicts — so the UI code stays free
of defensive `if key in resp` branches and shape guesses.

Two workstreams. They are **independent and ship as two separate PRs** (A
first); they touch the same files, so coordinate:

- **A. Typed data layer:** shared typed request/response DTOs used by both server
  and client — this is the deliverable the TUI needs. A generated OpenAPI /
  `http-api.md` is **documentation output**, not the core artifact.
- **B. Loopback auth:** a daemon-minted, rotating shared-secret token, shared via
  a `0600` file. No config token, no user accounts.

## Why now

Motivation from the TUI work: the data layer must be stable *before* the TUI is
re-implemented, or the new UI inherits brittle shape-handling. This task is a
precursor to [stage-5.2](stage-5.2-calm-tui-cleanup/README.md) **Stage 2**
(implementation) — land the typed client first so the TUI consumes stable
objects. Workstream B is orthogonal to 5.2 but shares the client/server files.

## Constraints

- **Stdlib-only.** Base install has `dependencies = []`
  ([pyproject.toml](../../pyproject.toml)), Python >= 3.9. No `pydantic`, no
  OpenAPI codegen libraries. Any schema/typed-model/generator work is
  hand-rolled with `dataclasses` + `json`.
- **No change to the wire protocol** beyond auth and an explicit version —
  existing routes and JSON shapes stay compatible. (This is *wire-only*: swapping
  `AgentCollabClient` methods from raw dicts to typed DTOs **is** a Python-API
  change for in-process callers — the TUI, CLI, and `HttpClientToolBackend` —
  and must be updated together.)

## Current State (grounding)

The API contract is triplicated with no schema:

- **Transport:** bespoke HTTP/1.1 + JSON over `127.0.0.1:8765`; client is
  `urllib` ([client.py](../../agent_collab/client.py)), server hand-parses on
  `asyncio.start_server` ([server_http.py](../../agent_collab/server_http.py)
  `_dispatch`).
- **Routes** (implicit in `_dispatch`): `GET /health`, `POST|GET /options`
  (the client only uses `POST /options`; `GET /options` requires a `workdir`
  query and is unused by the client today),
  `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`,
  `GET /sessions/{id}/events`, `GET /sessions/{id}/events/wait`,
  `POST /sessions/{id}/messages`, `GET /sessions/{id}/transcript`,
  `POST /sessions/{id}/stop`, and `POST /mcp`.
- **Where shapes are defined today** (three places, no single source):
  1. server request validation — `_required_str` / `_query_int` /
     `_decode_json_object` + the `StartSessionRequest` dataclass;
  2. server responses — `.to_dict()` on session-state / event-batch objects
     (no declared schema);
  3. client — `AgentCollabClient` methods re-encode the same routes.
- **What already has a real schema:** the MCP tool `inputSchema`s
  ([mcp_tools.py](../../agent_collab/mcp_tools.py) `TOOLS`) and the typed,
  validated, runtime-discoverable start-options
  ([options.py](../../agent_collab/options.py) `validate_start_options` /
  `describe_options`). These overlap the HTTP surface (start payload, options)
  and should be referenced by the new contract, not re-duplicated.
- **Where secrets/state live:** `~/.agent-collab/data/daemon/` holds `pid` and
  `state.json`, written today by the **supervisor**
  ([daemon_supervisor.py](../../agent_collab/daemon_supervisor.py)
  `_build_state` / `_write_state`), not by the serving process. `state.json` is
  written with default perms today (not `0600`).

## Workstream A - Typed Data Layer (single source of truth)

### Approach (shared typed DTOs; OpenAPI is generated docs, not the core)

Because server and client live in one repo, the single source of truth is
**shared typed dataclasses** both sides import — not a separately authored spec.
A hand-rolled stdlib OpenAPI generator is itself a maintenance surface, so it is
demoted to a *documentation output*, and only for the parts that model cleanly.

- New module `agent_collab/api_schema.py`: `dataclass` request/response DTOs —
  response models (`SessionStateModel`, `EventBatchModel`, `ErrorModel`, …) with
  explicit fields and `from_dict` / `to_dict`. Note `SessionState.to_dict()` is
  `asdict()` today (nested `settings`/`capabilities`), and `StartSessionRequest`
  carries **non-user** fields (`resolved_backends`, `collab_config`, `verbose`,
  `log_dir`, `session_id`) — the API request DTO must expose only the wire
  fields, not those.
- Server: `_dispatch` builds/returns these DTOs; the ad-hoc `_required_str` etc.
  become DTO `from_dict` validation.
- Client: methods return typed DTOs, not raw dicts — **this is the deliverable
  the TUI consumes** (`client.get_session(id) -> SessionStateModel`). Note this
  is a Python-API change for `AgentCollabClient` callers (see Current State).

### What does NOT go into a static schema

- **`/options` stays the runtime authority.** `describe_options` merges
  configured agents, workflow agent types, backend health, and CLI-arg-inferred
  defaults per workdir ([options.py](../../agent_collab/options.py)
  `describe_options` / `_schema_for_agent_type`). A static contract may only say
  `codex_options` is an object; allowed fields/defaults come from `/options` at
  runtime. The TUI must call `/options`, not bake the option schema in.
- **`wait_events`** is a normal `GET` returning `EventBatch`; its long-poll
  timing (`timeout_ms`) is prose, not something OpenAPI captures usefully.
- **`/mcp`** is JSON-RPC-in-HTTP, not a REST resource; document it as an opaque
  authenticated endpoint, separate from the CLI daemon API. Its tool inputs
  already have `inputSchema` in `mcp_tools.py`.

Optional (only if cheap): generate `doc/http-api.md` (and maybe `openapi.json`)
from the DTOs + a small route registry for the REST-shaped routes. If the
generator grows complex, drop it — the DTOs + a contract test are the real win.

### Versioning

Add an explicit API version so the TUI detects mismatch cleanly instead of via
defensive `if`s. Options for approval: a `/v1` path prefix, or an
`X-Agent-Collab-API: 1` response header + a version field in `GET /health`.
Recommend the health/version field + header (no route churn) with the client
asserting a compatible major on connect.

### Error envelope

Formalize the existing `{ "error": ..., "details": [...] }` shape
([client.py](../../agent_collab/client.py) `_format_error_payload`,
[options.py](../../agent_collab/options.py) `StartOptionsError.to_dict`) as one
`ErrorModel` used by every non-2xx response, so the client has exactly one error
path.

### Deliverables A

- `agent_collab/api_schema.py` — shared typed request/response DTOs (the single
  source; primary deliverable).
- `server_http.py` and `client.py` refactored to use them (kill the triplication);
  client methods return typed DTOs.
- **Contract test** (the real safety net): every route has a server handler and a
  client method; example payloads round-trip through the DTOs; the start payload
  DTO stays in sync with the MCP `agent_collab_start` `inputSchema`.
- Reconcile the *start payload* definition so HTTP and MCP validation reference
  one place (options details still resolved via `/options`).
- **Optional/secondary:** generated `doc/http-api.md` (+ `openapi.json`) for the
  REST-shaped routes, with a regen-and-diff CI check; drop if the generator gets
  heavy.
- Update [daemon-architecture.md](daemon-architecture.md): replace the
  "Suggested endpoints" prose with a pointer to the DTOs / generated doc.

## Workstream B - Loopback Auth (rotating shared-secret token)

### Design

- **Mint:** the **serving process** (not the supervisor) generates a random
  token at startup (`secrets.token_urlsafe(32)`). Pass its path explicitly into
  [run_server](../../agent_collab/server_http.py) / `serve` so direct
  `agent-collab serve` and the supervisor-spawned daemon behave identically and
  do not drift.
- **Share (atomically, before serving):** write the token to
  `~/.agent-collab/data/daemon/token` via a perms-safe helper — create at `0600`
  (write to a temp file, `chmod 0600`, `os.replace`) **before** the server
  starts accepting protected traffic. Ensure the daemon dir is `0700`. A stale
  token file must never be trusted (see readiness).
- **Send:** client reads the token file and sends `Authorization: Bearer
  <token>`; `AGENT_COLLAB_TOKEN` env var overrides (for manual/remote clients).
- **Enforce:** daemon rejects requests without a valid token with `401` +
  `ErrorModel` (also add `401` to `_http_reason`, which omits it today). `GET
  /health` stays **open** and unauthenticated for probes — document that it
  exposes only status + session count. `/mcp` token is **mandatory**, on top of
  its existing (currently optional) `Origin` / protocol-version checks.
- **Readiness (tighten the handshake):** `_wait_for_ready` today only opens a TCP
  socket. With auth it must prove the server accepts **this lifetime's fresh
  token** — e.g. an authenticated `GET /health` round-trip — not merely that the
  token file exists (a leftover file from a prior daemon would satisfy mere
  existence). Fail startup if the fresh token is not accepted.
- **Rotate:** baseline = one token per daemon lifetime (each start mints a new
  one, atomically superseding the old file). Optional enhancement: periodic
  rotation on a timer with the daemon rewriting the file and the client
  re-reading on `401` and retrying once.
- **UX:** on `401` the client re-reads the token file once and retries; if still
  `401`, error `daemon token mismatch; restart the daemon`.

### Threat model (be honest)

Loopback binding + `0600` protects against **other local users** and
non-owner processes reading the port. It is **not** a boundary against
same-user processes (which can read the token file) — that is out of scope and
should be stated in the docs. This is parity with the referenced local-token
tools, not a stronger claim.

### Deliverables B

- Perms-safe write helper (temp file + `chmod 0600` + `os.replace`); daemon dir
  `0700`; apply it to the token, and give `state.json` / `pid` the same treatment
  (both are default-perms today via `write_text` / `ensure_dirs`).
- Token mint (atomic, pre-serve) + Bearer enforcement in
  [server_http.py](../../agent_collab/server_http.py); explicit token-path arg
  threaded through `run_server` / `serve`; `_http_reason` gains `401`.
- `/health` stays open (documented); `/mcp` token mandatory.
- Client reads/attaches the token, honors `AGENT_COLLAB_TOKEN`, and does the
  `401` re-read-and-retry-once.
- Supervisor readiness (`_wait_for_ready`) upgraded to an **authenticated
  `/health` round-trip proving the fresh token is accepted**, not just socket
  accept or file existence.
- Tests: authorized request ok; missing/wrong token -> `401`; `/health` open;
  stale token file rejected at readiness; token-file re-read after rotation; env
  override; token/state/dir perms are `0600`/`0700`.
- Docs: security note + threat model in
  [daemon-architecture.md](daemon-architecture.md) and the new token file in
  [runtime-layout.md](runtime-layout.md).

## Sequencing

Ship as **two separate PRs / sub-tasks** (they touch the same files but are
independent concerns — one data-shape, one security/process):

1. **A first** (shared typed DTOs + typed client + contract test), so
   [stage-5.2](stage-5.2-calm-tui-cleanup/README.md) Stage 2 builds on the typed
   client. Smallest useful first step: the DTOs + route/model contract test.
2. **B** separately — it has enough perms/atomic-write/readiness edge cases to
   warrant its own task/PR. Smallest useful first step: the token
   path/perms/atomic-write/readiness design + tests *before* broad enforcement.
   (If B grows, split it into its own `stage-5.x` task doc.)

## Research Items

Separate from A/B — observability work surfaced while driving this task.

### R1. Distinguish a long-running agent turn from a stuck subprocess

While using agent-collab to review these docs, a solo-codex session
(`daemon-03b8f55609584f3a`, 2026-07-09) went ~7 minutes with **no new events**
after a broad repo-wide `rg`; from the caller's side it was indistinguishable
from a hang. Stopping it and re-running with a "read only these files, no broad
greps" constraint completed in ~90s. Two open questions to research:

- **Was it stuck or just slow?** Check what the logs can tell us after the fact:
  the per-session JSONL/markdown transcript, `daemon.log` / `daemon.stderr.log`,
  and whether the codex child process was actually running (CPU/IO) vs blocked.
  Determine which signal reliably separates "long tool call still making
  progress" from "wedged subprocess" (e.g. child liveness, last-output
  timestamp, tool-call start with no completion).
- **How does a caller know "all is well" during a long quiet stretch?** The
  MCP/HTTP `wait_events` long-poll returns nothing during a long tool call, so an
  agent polling it (or a human watching) can't tell healthy-but-slow from hung.
  Research a **liveness/heartbeat signal**: e.g. periodic `keepalive` events or a
  `last_activity` / `turn_active` field the daemon emits while a turn is in
  flight, and guidance for callers on expected quiet durations. This connects to
  the event model and the `wait_events` contract in Workstream A, so design it
  alongside the DTOs.

Deliverable: a short findings note (root cause of the stall + a concrete
liveness mechanism proposal), not necessarily code in this task.

## Review Notes

Reviewed by two solo-codex agent-collab sessions on 2026-07-09
(`daemon-3de1d98eb7e24974`; an earlier run, `daemon-03b8f55609584f3a`, was
stopped after it hung on a broad repo grep). Folded in: demote the hand-rolled
OpenAPI generator to optional generated docs and make **shared typed DTOs** the
core of A; keep `/options` the runtime authority (dynamic schema); document
`wait_events`/`/mcp` out of the REST model; tighten B's token handshake (atomic
`0600` pre-serve write, authenticated-`/health` readiness that rejects a stale
token, dir `0700`, `run_server` token-path arg, mandatory `/mcp` token, `401`
reason); note `GET`-vs-`POST /options` and that the typed-client swap is a
Python-API change; and split A and B into separate PRs.

## Open Questions (for approval)

- **A scope:** typed DTOs + contract test only, or also ship the generated
  `doc/http-api.md` (+ `openapi.json`) for the REST routes? (Recommend DTOs +
  test first; docs generator optional.)
- **Versioning:** `/v1` path prefix, or a `health` version field + response
  header (recommended)?
- **Auth transport:** `Authorization: Bearer` (recommended) or a custom header?
  Confirm `/health` stays open and `/mcp` requires the token.
- **Rotation:** per-daemon-lifetime only (recommended baseline), or add periodic
  rotation now?
- **Scope of the typed client:** wrap every route, or only the ones the TUI +
  CLI use today (defer the rest)?
- **Task split:** keep A and B in this one doc (as now), or spin B into its own
  `stage-5.x` task when it's picked up?
