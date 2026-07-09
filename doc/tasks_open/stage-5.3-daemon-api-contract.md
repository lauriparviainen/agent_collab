# Stage 5.3 - Daemon HTTP API Contract + Loopback Auth

Status: **Approved for implementation** (2026-07-09). Ships as **two PRs, A
first** (both live in this doc). Grounding verified against the code on
2026-07-09 — all references accurate. All prior open questions are resolved; see
[Resolved Decisions](#resolved-decisions).

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
  and must be updated together. In particular `HttpClientToolBackend`
  ([mcp_tools.py](../../agent_collab/mcp_tools.py)) feeds client return values
  straight into the MCP `content()` JSON serializer, so it must convert DTOs back
  to dicts (`.to_dict()`) or MCP-over-HTTP responses break.)

## Current State (grounding)

The API contract has no schema and is redefined in several places:

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
- **Where shapes are defined today** (no single source; the *start* payload alone
  lives in four places):
  1. server request validation — `_required_str` / `_query_int` /
     `_decode_json_object` + the `StartSessionRequest` dataclass
     ([server_http.py](../../agent_collab/server_http.py) `_dispatch`);
  2. server responses — `.to_dict()` on session-state / event-batch objects
     (no declared schema);
  3. client — `AgentCollabClient` methods re-encode the same routes;
  4. **MCP** — [mcp_tools.py](../../agent_collab/mcp_tools.py) re-encodes the
     *start* payload again (`_start_payload` +
     `SessionManagerToolBackend.start_session`), on top of the
     `agent_collab_start` `inputSchema` in `TOOLS`. So the start shape is
     effectively quadruplicated, not triplicated.
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
  carries **non-user** fields (`verbose`, `color`, `log_dir`, `session_id`,
  `resolved_backends`, `collab_config` — [daemon.py](../../agent_collab/daemon.py)
  ~L44) — the API request DTO must expose only the wire fields, not those.
- Server: `_dispatch` builds/returns these DTOs; the ad-hoc `_required_str` etc.
  become DTO `from_dict` validation.
- Client: methods return typed DTOs, not raw dicts — **this is the deliverable
  the TUI consumes** (`client.get_session(id) -> SessionStateModel`). Note this
  is a Python-API change for `AgentCollabClient` callers (see Current State).

### What does NOT go into a static schema

- **`/options` stays the runtime authority.** `describe_options` merges
  configured agents, workflow agent types, backend health, and CLI-arg-inferred
  defaults per workdir ([options.py](../../agent_collab/options.py)
  `describe_options` / `_backend_option_schemas`). A static contract may only say
  `backend_options` is an object; its backend-qualified fields/defaults — plus `agents`, `workflows`,
  `workflow_agent_types`, and `backends` — all come from `/options` at runtime.
  The TUI must call `/options`, not bake the option schema in.
- **`wait_events`** is a normal `GET` returning `EventBatch`; its long-poll
  timing (`timeout_ms`) is prose, not something OpenAPI captures usefully.
- **`/mcp`** is JSON-RPC-in-HTTP, not a REST resource; document it as an opaque
  authenticated endpoint, separate from the CLI daemon API. Its tool inputs
  already have `inputSchema` in `mcp_tools.py`.

**Decision — deferred, not in this task.** The stdlib generator for
`doc/http-api.md` (+ `openapi.json`) from the DTOs + a route registry is *not*
built here; the DTOs + contract test are the real win and ship first. It can be
added later if it turns out cheap. Until then, `daemon-architecture.md` points at
the DTOs as the source of truth (see Deliverables A).

### Versioning

Add an explicit API version so the TUI detects mismatch cleanly instead of via
defensive `if`s. **Decision:** a version field in `GET /health` **plus** an
`X-Agent-Collab-API: 1` response header on every REST response (no route churn,
no `/v1` prefix). The client asserts a compatible **major** on connect and
surfaces a clear error on mismatch rather than shape-guessing.

### Error envelope

Formalize the existing `{ "error": ..., "details": [...] }` shape
([client.py](../../agent_collab/client.py) `_format_error_payload`,
[options.py](../../agent_collab/options.py) `StartOptionsError.to_dict`) as one
`ErrorModel` used by every non-2xx **REST** response — including the
transport-level HTTP errors `/mcp` raises for a bad `Origin`/method/protocol
version, which already render through the same `{ "error": ... }` envelope
([server_http.py](../../agent_collab/server_http.py) `_handle_connection`).
`ErrorModel`'s `details` field is **optional**: `StartOptionsError.to_dict`
emits `{ "error", "details" }`, but the `/mcp` transport errors emit
`{ "error" }` only (no `details`, verified 2026-07-09) — so `from_dict` must
tolerate a missing `details` and `to_dict` must omit it when empty. Scope it
explicitly: `ErrorModel` does **not** cover the JSON-RPC error objects that
live inside a `200`/`202` `/mcp` body (`jsonrpc_error`,
[mcp_tools.py](../../agent_collab/mcp_tools.py)) — those keep their JSON-RPC
shape. So the client has exactly one REST error path.

### Deliverables A

- `agent_collab/api_schema.py` — shared typed request/response DTOs (the single
  source; primary deliverable).
- `server_http.py` and `client.py` refactored to use them (kill the duplication);
  client methods return typed DTOs. **Decision — wrap ALL REST routes** (the full
  list in Current State: `health`, `options`, sessions CRUD, `events`,
  `events/wait`, `messages`, `transcript`, `stop`), not just the ones the TUI/CLI
  use today, so the client is uniformly typed with no dict/DTO split. The DTO
  `from_dict` validation must subsume **all** the ad-hoc server helpers:
  `_required_str`, `_query_int`, `_query_required_str`, `_optional_payload`, and
  `_decode_json_object` ([server_http.py](../../agent_collab/server_http.py)
  `:256`/`:271`/`:263`/`:278`/`:244`). Update `HttpClientToolBackend`
  ([mcp_tools.py](../../agent_collab/mcp_tools.py)) to convert those DTOs back to
  dicts (`.to_dict()`) before they reach the MCP `content()` serializer.
- Make the `/options` **request** DTO require a non-blank `workdir` (the server
  already requires it for both `POST` and `GET /options`). This also closes
  `client.describe_options()`'s current no-payload path, which sends `{}` and the
  server `400`s.
- **Contract test** (the real safety net): every route has a server handler and a
  client method; example payloads round-trip through the DTOs; the start payload
  DTO stays in sync with the MCP `agent_collab_start` `inputSchema`.
- Reconcile the *start payload* definition so HTTP **and MCP** validation
  reference one place — this explicitly includes `mcp_tools.py` `_start_payload`,
  `SessionManagerToolBackend.start_session`, and the `TOOLS` `inputSchema`, not
  just `server_http.py` (options details still resolved via `/options`).
- **Deferred (not this task):** the generated `doc/http-api.md` (+ `openapi.json`)
  for the REST-shaped routes and its regen-and-diff CI check. Revisit only if
  cheap.
- Update [daemon-architecture.md](daemon-architecture.md): replace the
  "Suggested endpoints" prose (verified stale on 2026-07-09 — it omits
  `POST /sessions/{id}/messages`; [daemon-architecture.md](daemon-architecture.md)
  ~L122) with a pointer to the DTOs as the source of truth.

### Progress: slice 1 landed (DTOs + contract test)

**Done (2026-07-09, committed `85324eb`):** `agent_collab/api_schema.py` (DTOs,
`API_VERSION` / `API_VERSION_HEADER`, `NON_USER_START_FIELDS`, `Route`/`ROUTES`
full REST registry + `SERVER_ONLY_ROUTES`) and `tests/test_api_schema.py`
(route/model + start-payload-quadruplication + live-wire-fidelity contract
tests). One small additive production change: `AgentCollabClient.health()`.
Reviewed by solo-codex (`daemon-311afbb1340349e0`); its BLOCKING finding
(`GET /options` missing from the registry) was fixed by adding it as a
server-only route with a `SERVER_ONLY_ROUTES` guard.

### Progress: slice 2 landed (server + MCP wired onto the DTOs + versioning)

**Done (2026-07-09):** request validation now flows through the DTOs, not ad-hoc
helpers. Full suite green (279) + an end-to-end socket round-trip check.

- `StartSessionRequest.from_wire` ([daemon.py](../../agent_collab/daemon.py)) is
  the single wire→request construction; the HTTP server (`POST /sessions`) and
  the in-daemon MCP backend (`SessionManagerToolBackend.start_session`) both use
  it (collapses the 4th start-payload copy).
- `server_http._dispatch` validates via `OptionsRequestModel` /
  `PostMessageRequestModel` / `from_wire` through a `_parse()` seam that maps the
  DTO's `ValueError` → `HttpError(400)` (keeps the REST error contract; the
  server tests call `_dispatch` directly and assert `HttpError`). The old
  `_required_str` / `_query_required_str` / `_optional_payload` helpers are gone.
- Versioning: `GET /health` returns `HealthModel(..., api_version=API_VERSION)`;
  every response carries `X-Agent-Collab-API`; the client asserts a compatible
  major per request (`_assert_compatible_api`, tolerant of a missing/garbage
  header, rejects a mismatch).
- Deliberate unifications (were forward-risks): whitespace-only `task` is now
  rejected on `/mcp` (via `from_wire`); `backend: null` is accepted and a
  non-null non-string `backend` rejected identically on REST **and** `/mcp`
  (`_start_payload` aligned with the DTO — the review caught that `_start_payload`
  still rejected `null` on the public `/mcp` path); `POST /messages` validates
  `source`/`target` up front (matches the MCP path; a bad message on an unknown
  session is now `400` before `404`). `int()/float()` replace the MCP
  `_int_arg`/`_float_arg` custom messages. No existing test asserted those old
  MCP-only edges.

**Client return types are still dicts** (cli/tui consume dicts). Reviewed by
solo-codex (`daemon-a147a57d9ff143cf`); verdict commit-safe, its one actionable
non-blocking finding (`/mcp` `backend: null`) fixed + tested above.

### Remaining Workstream A work (later slices)

- **Typed client return values ("slice 3")** — `AgentCollabClient` methods return
  DTOs (`get_session -> SessionStateModel`, …), the deliverable the TUI consumes,
  plus updating `HttpClientToolBackend` to `.to_dict()` the results. **Scheduled
  into [stage-5.2 Stage 2](stage-5.2-calm-tui-cleanup/README.md#stage-2---implementation)**
  (it lists this as an explicit build item) so the doomed dict-based
  `tui.py`/`cli.py` call sites are migrated once, not churned twice.
- **Table-driven `_dispatch` off `ROUTES`** — closes reverse route-completeness
  (server dispatch ⊆ `ROUTES`) fully; the current test only proves the forward
  direction.
- **Malformed numeric/null request fields → `400`, not `500`.** `_parse` maps
  only `ValueError`; a null/ill-typed `max_turns`/`timeout` reaches `int(None)` →
  `TypeError` → generic `500` (pre-existing behavior, not a slice-2 regression).
  If the contract wants all malformed request fields to be `400`, have the DTO
  `from_dict` raise `ValueError` on those conversions.
- **`doc/http-api.md` generator** — still deferred.

## Workstream B - Loopback Auth (rotating shared-secret token)

### Design

- **Mint:** the **serving process** (not the supervisor) generates a random
  token at startup (`secrets.token_urlsafe(32)`). Pass its path explicitly into
  [run_server](../../agent_collab/server_http.py) / `serve` so direct
  `agent-collab serve` and the supervisor-spawned daemon behave identically and
  do not drift.
- **Share (atomically, before serving):** add a `token_path` to `GlobalDataPaths`
  ([paths.py](../../agent_collab/paths.py)) alongside `pid_path` / `state_path`,
  so the path is not reconstructed ad hoc. Write the token there via a perms-safe
  helper — create at `0600` (write to a temp file, `chmod 0600`, `os.replace`)
  **before** the server starts accepting protected traffic. Ensure the daemon dir
  is `0700` — note `ensure_dirs`' `mkdir(parents=True, exist_ok=True)` will **not**
  tighten an already-loose dir, so `chmod` it explicitly. Also, the serving
  process does not create the daemon dir today (only the supervisor's
  `ensure_dirs` does — direct `agent-collab serve` → `run_server` never makes it),
  so the mint path must `ensure_dirs` + `chmod` itself to behave identically in
  both launch modes. A stale token file must never be trusted (see readiness).
- **Send:** client reads the token file and sends `Authorization: Bearer
  <token>`; `AGENT_COLLAB_TOKEN` env var overrides (for manual/remote clients).
  Reading the local token file only makes sense for the loopback default; when
  `AGENT_COLLAB_SERVER` points at a non-local daemon, the env override is the
  intended source (a local file would not match that daemon's token).
- **Enforce:** daemon rejects requests without a valid token with `401` +
  `ErrorModel` (also add `401` to `_http_reason`, which omits it today). `GET
  /health` stays **open** and unauthenticated for probes — document that it
  exposes only status + session count. `/mcp` token is **mandatory**, on top of
  its existing (currently optional) `Origin` / protocol-version checks.
- **Readiness (tighten the handshake):** `_wait_for_ready` today only opens a TCP
  socket ([daemon_supervisor.py](../../agent_collab/daemon_supervisor.py)
  `_wait_for_ready`). With auth it must prove the server accepts **this lifetime's
  fresh token** — not merely that the token file exists (a leftover file from a
  prior daemon would satisfy mere existence). **Contradiction to avoid:** because
  `GET /health` stays open (see Enforce, above), an authenticated `/health`
  round-trip would
  succeed with *any* token — or none — and prove nothing about token acceptance.
  Readiness must therefore either hit a **protected** route (e.g. `GET /sessions`)
  with the freshly-read token and require `200`, or `/health` must *optionally*
  validate a presented token (`401` on mismatch) while still answering tokenless
  probes. **Decision:** the protected-route probe (`GET /sessions` with the fresh
  token, require `200`) — keeps `/health` a pure, tokenless liveness check. Fail
  startup if the fresh token is not accepted.
- **Rotate:** **Decision — per-daemon-lifetime only.** Each start mints a new
  token, atomically superseding the old file; no rotation timer in this task. The
  client's `401` re-read-and-retry-once path (below) still applies — it covers the
  restart-while-client-lives case. Periodic timer-based rotation is explicitly
  **deferred** (out of scope here); if added later it reuses the same client
  re-read path.
- **UX:** on `401` the client re-reads the token file once and retries; if still
  `401`, error `daemon token mismatch; restart the daemon`.

### Threat model (be honest)

Loopback binding + `0600` protects against **other local users** and
non-owner processes reading the port. It is **not** a boundary against
same-user processes (which can read the token file) — that is out of scope and
should be stated in the docs. This is parity with the referenced local-token
tools, not a stronger claim.

### Deliverables B

- Add `token_path` to `GlobalDataPaths`. Perms-safe write helper (temp file +
  `chmod 0600` + `os.replace`); daemon dir `0700` (`chmod` even when it already
  exists, and have the serving process ensure/chmod it since direct `serve` does
  not); apply it to the token, and give `state.json` / `pid` the same treatment
  (both are default-perms today via `write_text` / `ensure_dirs`).
- Token mint (atomic, pre-serve) + Bearer enforcement in
  [server_http.py](../../agent_collab/server_http.py); explicit token-path arg
  threaded through `run_server` / `serve`; `_http_reason` gains `401`.
- `/health` stays open (documented); `/mcp` token mandatory.
- Client reads/attaches the token, honors `AGENT_COLLAB_TOKEN`, and does the
  `401` re-read-and-retry-once.
- Supervisor readiness (`_wait_for_ready`) upgraded to a **protected-route probe
  (e.g. authenticated `GET /sessions`) proving the fresh token is accepted** — not
  a bare socket accept, file existence, or an open-`/health` hit (see the
  readiness contradiction above).
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
`0600` pre-serve write, readiness that rejects a stale token — then specified as
an authenticated `/health` round-trip, **superseded by the third pass below**
which replaced it with a protected-route probe — dir `0700`, `run_server`
token-path arg, mandatory `/mcp` token, `401`
reason); note `GET`-vs-`POST /options` and that the typed-client swap is a
Python-API change; and split A and B into separate PRs.

Third pass (`daemon-7f5d3418265d4157`, 2026-07-09, solo-codex read-only). Folded
in: resolved the readiness contradiction (an authenticated **open**-`/health`
round-trip proves nothing about the token — readiness now probes a **protected**
route, e.g. `GET /sessions`, with the fresh token); flagged that
`HttpClientToolBackend` must convert DTOs back to dicts for MCP `content()`;
called out the *start* payload as **quadruplicated** (adds `mcp_tools.py`
`_start_payload` / `SessionManagerToolBackend.start_session` / `TOOLS`) and named
`mcp_tools.py` explicitly in scope; scoped `ErrorModel` to REST + `/mcp`
transport errors only (not JSON-RPC error bodies); added `token_path` to
`GlobalDataPaths`; noted `0700` must `chmod` an already-loose dir and that direct
`serve` never creates the daemon dir; added `color` to the non-user field list;
required `workdir` in the `/options` request DTO (fixing
`client.describe_options()`'s no-payload `400`); generalized the `/options`
runtime-authority wording around backend-qualified options; and noted the local token file
only applies to the loopback default.

Fourth pass (finalization, 2026-07-09). Grounding re-verified against the code —
all references confirmed accurate (routes, `daemon.py` fields, option-block
names, `_wait_for_ready` socket-only, `paths.py` lacking `token_path`, stale
"Suggested endpoints" prose, no token file in `runtime-layout.md`). Folded in two
implementer notes: the DTO `from_dict` must also subsume `_query_required_str`
and `_optional_payload` (not just `_required_str`/`_query_int`/
`_decode_json_object`), and `ErrorModel.details` is **optional** (the `/mcp`
transport errors emit `{ "error" }` with no `details`). All Open Questions
resolved into [Resolved Decisions](#resolved-decisions): A = DTOs + test only
(generator deferred); wrap **all** REST routes; version via health field +
header; `Bearer` with open `/health` + mandatory `/mcp`; per-lifetime rotation;
protected-route readiness probe; keep A+B in one doc as two PRs. Status flipped to
**approved for implementation**.

## Resolved Decisions

All open questions were resolved on 2026-07-09; the doc body above reflects them.
Recorded here for traceability:

- **A scope:** typed DTOs + contract test **only**. The generated
  `doc/http-api.md` (+ `openapi.json`) and its CI regen-diff are **deferred** (not
  this task) — revisit only if cheap. See [Approach](#what-does-not-go-into-a-static-schema)
  / Deliverables A.
- **Scope of the typed client:** wrap **all** REST routes with typed DTOs (no
  dict/DTO split), not just the TUI/CLI-used subset. See Deliverables A.
- **Versioning:** `GET /health` version field **+** `X-Agent-Collab-API: 1`
  response header; **no `/v1` prefix**. Client asserts a compatible major on
  connect. See Versioning.
- **Auth transport:** `Authorization: Bearer <token>`. `/health` stays **open**
  and tokenless; `/mcp` token is **mandatory**. See Enforce.
- **Rotation:** **per-daemon-lifetime only** (new token each start, atomic
  supersede). Periodic timer rotation is deferred. The client's
  `401`-re-read-and-retry-once still applies. See Rotate / UX.
- **Readiness probe:** **protected-route probe** — `GET /sessions` with the fresh
  token, require `200`; keeps `/health` a pure liveness check. Fail startup if the
  fresh token is not accepted. See Readiness.
- **Task split:** **keep A and B in this one doc**, ship as **two PRs, A first**.
  Spin B into its own `stage-5.x` doc only if it grows during implementation.

The **R1 research items** (liveness/heartbeat signal, stuck-vs-slow diagnosis)
remain a separate, non-blocking findings-note deliverable — not a gate on A or B.
