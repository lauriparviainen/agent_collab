# MCP Streamable HTTP workdir path-namespace bridging

**Status:** Preliminary design — preferred shape is G+C (client-declared
request-scoped mapping + dual identity). Second Codex review: **Approve with
changes** — architecture sound enough for implementation *planning*, not yet
precise enough as the coding/API contract. **Do not implement** until residual
freeze items below are closed. Header/JSON names remain illustrative.

**Created:** 2026-08-07

**Updated:** 2026-08-08 — adopted G+C; demoted global path_maps; incorporated
Codex recommendation and second Codex design review.

**Issue:** [#57](https://github.com/lauriparviainen/agent_collab/issues/57)

## Design maturity

This document records design only. Production behavior must not change until
implementation acceptance criteria are refined into hermetic tests and shipped
deliberately.

Prefer the **Decisions** section over older draft wording if anything
conflicts. Earlier adversarial critiques still apply as risk analysis; their
“header maps forbidden in v1” freeze is **superseded** by the Codex redesign
and the product constraint that topology is client-instance knowledge.

## Context

### How workdir works today

- MCP `agent_collab_describe_options` and `agent_collab_start` require an
  absolute `workdir`.
- The daemon resolves it with `expanduser().resolve()`, rejects missing or
  non-directory paths, optionally enforces user-global
  `[workdir].restrict_workdir_roots`, loads
  `WORKDIR/.agent-collab/config.toml`, and uses that path as provider
  subprocess cwd and outer Bubblewrap workspace designation.
- Preferred MCP transport is Streamable HTTP `POST /mcp`. There is **no MCP
  server process** and therefore **no process `args`**. Client configuration
  is effectively `url` + `headers` (+ bearer token).
- Session state stores a **single** workdir string (daemon path). Status,
  list, transcripts, and provider output all speak that identity.
- Review skills freeze an absolute workdir from the **caller** environment and
  embed `Workdir: …` in the provider prompt.

### Failure mode

| Actor | Path identity |
| --- | --- |
| Agent / IDE in a devcontainer | e.g. `/workspaces/project` |
| Host agent-collab daemon | e.g. `/home/user/src/project` (or missing) |

Client-absolute paths fail with `workdir does not exist`, or bind a wrong tree
if a coincidental host path exists. Even after a successful start with a
hand-typed host path, **asymmetric narration** remains: providers and
transcripts emit daemon-host paths while the calling agent’s local tools use
client-namespace paths.

### Product constraint (why global daemon maps are wrong)

A live host daemon must **not** be the registry of every IDE/devcontainer path
layout that might attach to it:

- Topology is **client-instance** knowledge (“in *this* container,
  `/workspaces/foo` is host `/home/…/foo`”).
- Multiple clients commonly share the same client prefix (`/workspaces`) while
  mapping to **different** host trees. A process-global map table cannot
  disambiguate concurrent attachers.
- Operator `~/.agent-collab/config.toml` would become a fragile graveyard of
  layout fiction and would drift out of date as containers change.

The **client** must declare remapping (or already pass a host-visible path).
The **daemon** validates and executes on host paths only, and **discloses**
both identities so the calling agent can reverse-map provider output.

### Outer sandbox note (accuracy)

Linux outer read-only does **not** mean “only the workdir is visible.” The
common posture is host-wide read-only visibility plus designated workspace
operations on the session workdir. A wrong mapping mainly risks
**wrong-project execution** (cwd, project config, agent focus), not a classic
“escape a single bind.”

### Non-goals

- Automatic mount-table / inode guessing.
- Project-config path maps or workdir policy (same trust class as roots).
- User-global daemon `path_maps` as the **primary** multi-client solution.
- Rewriting free-form `task` text or transcript bodies.
- Remote multi-machine daemons without a shared filesystem story.
- A dedicated `agent_collab_resolve_workdir` tool in MVP.
- Server-side reverse rewrite of provider answers.
- Environment variables as the protocol contract (implicit, not HTTP-portable).

## Goals

1. **Daemon remains authoritative** for the filesystem identity used as cwd,
   project config root, sandbox workspace designation, and allowlisting.
2. **Client owns topology declaration** for non-host path namespaces
   (devcontainer / IDE MCP config).
3. **Calling agent is path-aware** when a bridge is active: structured dual
   identity so it can reverse-map daemon paths for IDE actions and know that
   provider output is daemon-namespace.
4. **Streamable HTTP first-class**: use the real HTTP client surface
   (`url` + `headers`); no process `args`.
5. **No global multi-client map collisions**: mapping context is
   request-scoped (and snapshotted on sessions), not a shared daemon table.
6. **Explicit over magic**: fail closed; no mount discovery.
7. **Same trust posture** as workdir limits: maps are not permission grants;
   project config cannot set them.
8. **Transport parity**: MCP and REST share one normalizer; CLI has an
   explicit bridge for container→host cases.

## Option space

| Option | Verdict |
| --- | --- |
| A. Silent server rewrite only | **Reject as sole design** — blinds the agent |
| B. Instructions / tool text only | **Reject as sole design** — models ignore; cannot invent host existence |
| C. Structured dual identity in responses | **Accept** as the disclosure half |
| D. Stdio-adapter-only remap | **Reject** — fights preferred Streamable HTTP |
| E. Identical absolute bind mounts | **Ops mitigation only** — document; not a protocol substitute |
| F. Client always passes host `workdir` + optional `client_workdir` | **Secondary / fallback** — requires agent already knows host path; topology forced into model-controlled tool args every call |
| G. Client-declared, request-scoped maps (e.g. Streamable HTTP headers) | **Accept as primary** (with C) |
| H. Global user-config `path_maps` on daemon | **Demote** — optional single-machine convenience only; not multi-client primary |
| “Identity-wins if host path exists” under ambient global maps | **Reject as primary** — coincidental host paths pick wrong trees |

## Preferred shape: G + C (client-declared map + dual identity)

### Principles

1. **Knowledge vs authority.** Client declares topology; daemon validates and
   runs on the host path only.
2. **Request-scoped mapping context** at the daemon (from authenticated
   request headers or equivalent CLI flags). Do not call it
   “connection-scoped”: Streamable HTTP POST responses close; there is no
   durable connection identity today.
3. **One workspace anchor pair in MVP:** exact `client_root` ↔ `daemon_root`.
   Tool `workdir` under `client_root` maps by appending the relative suffix
   under `daemon_root` (component-wise join).
4. **When mapping headers are present, the map explicitly wins** for inputs
   under `client_root`. Require tool `workdir` to be at or beneath
   `client_root` so agents cannot accidentally re-submit a returned daemon
   path as the next start workdir under an active client map.
5. **When mapping headers are absent,** preserve today’s identity behavior
   (input is already a daemon-namespace path).
6. **Response/session `workdir` always means the daemon path** after
   normalize.
7. **`path_namespace` is the machine-readable bridge contract**, present on
   describe/start and snapshotted on the session; echoed on status/list and
   included with `wait_result` (provider answers live there).
8. **Reverse mapping is caller-side.** No transcript mutation.
9. **Repo-relative paths** in review file lists remain the best cognitive
   simplification.
10. **Do not put topology in global daemon config as the main story.**

### Streamable HTTP configuration (client declares remapping)

Illustrative MCP client config **inside the devcontainer** (or other client
namespace):

```json
{
  "type": "http",
  "url": "http://127.0.0.1:8765/mcp",
  "headers": {
    "Authorization": "Bearer <token>",
    "Agent-Collab-Client-Root": "/workspaces/project",
    "Agent-Collab-Daemon-Root": "/home/alice/src/project"
  }
}
```

Rules (design intent):

- Both mapping headers must be present together, or neither.
- Separate headers avoid inventing JSON-in-header encoding before multi-map
  phase 2.
- Roots and workdirs: absolute; lexically clean; reject `.` / `..`, control
  characters, excessive length, and `client_root = "/"`.
- Match by **path components**, never raw `startswith`.
- After map: `resolve()` on host; require final path **exists**, is a
  directory, and remains **beneath resolved `daemon_root`** (mandatory
  containment).
- Then apply existing `restrict_workdir_roots` on the final daemon path.
- Project config never defines maps.
- Cap header size; do not log Authorization or mapping headers at default
  level.

Static headers on the MCP client make the map **client-instance-scoped** in
practice while remaining **request-scoped** at the daemon (each POST carries
the same headers).

### Multi-client / overlapping `/workspaces`

Mappings must **never** enter shared global daemon map state as the
disambiguator. Two concurrent clients may declare:

```text
/workspaces/project → /home/alice/src/project-a
/workspaces/project → /home/alice/src/project-b
```

Each request normalizes using **only its own headers**. No collision.

Derive a non-secret `namespace_id` from the canonical mapping pair. Snapshot
it and the dual paths on session creation. Status/list/result should report
whether the session namespace is **compatible with the current request’s
namespace**, so a client does not treat another client’s identical-looking
`/workspaces/project` as locally usable.

### Normalization algorithm (shared MCP + REST)

```text
1. Parse optional request PathNamespaceContext (headers or CLI flags).
2. Require absolute workdir input; reject empty / relative / ".." segments.
3. If context active:
     - require workdir at or under client_root (component boundary)
     - daemon_candidate = join(daemon_root parts + relative suffix parts)
     - client_workdir = input
   Else:
     - daemon_candidate = input
     - client_workdir = null
4. daemon_workdir = expanduser().resolve(daemon_candidate)
5. If context active: require daemon_workdir equals or under resolve(daemon_root)
6. exist + is_dir + validate_workdir_allowed(daemon_workdir)
7. Never rewrite task text or transcripts
```

One function used by describe and start on MCP and REST.

### Agent-visible contract

#### Response sketch

```json
{
  "workdir": "/home/alice/src/project/packages/core",
  "path_namespace": {
    "version": 1,
    "active": true,
    "namespace_id": "sha256:<canonical-map-digest>",
    "input_workdir": "/workspaces/project/packages/core",
    "client_root": "/workspaces/project",
    "client_workdir": "/workspaces/project/packages/core",
    "daemon_root": "/home/alice/src/project",
    "daemon_workdir": "/home/alice/src/project/packages/core",
    "compatible_with_request": true
  }
}
```

Inactive (no mapping headers):

```json
"path_namespace": {
  "version": 1,
  "active": false,
  "namespace_id": null,
  "input_workdir": "/home/alice/src/project",
  "client_root": null,
  "client_workdir": null,
  "daemon_root": null,
  "daemon_workdir": "/home/alice/src/project",
  "compatible_with_request": true
}
```

Session read from another client namespace:

```json
"path_namespace": {
  "active": true,
  "namespace_id": "sha256:<session-origin-map>",
  "client_workdir": "/workspaces/project",
  "daemon_workdir": "/home/alice/src/project-a",
  "request_namespace_id": "sha256:<current-client-map>",
  "compatible_with_request": false
}
```

#### Where to emit

| Surface | `path_namespace` |
| --- | --- |
| `describe_options` | Always |
| start response | Always; snapshot on session when active |
| status / list | Echo snapshot; set `compatible_with_request` from current headers |
| `wait_result` | Include (provider answers need reverse-map context) |
| event batches | Optional small flag / reference in phase 2 |
| Transcript text | Never rewrite bodies |

#### How the calling agent is taught

| Channel | Role |
| --- | --- |
| Structured `path_namespace` | **Primary contract** |
| Session + wait_result echo | Long-session re-attach and answer harvest |
| dual/solo-review skills | Hard steps (below) |
| mcp-guidance | Procedural rule |
| Tool `workdir` property description | One-line hint |
| `initialize.instructions` | Short pointer only |

### Agent / skill obligations

1. Call `describe_options` with the caller-visible absolute path.
2. Inspect and freeze `namespace_id`, `client_workdir`, `daemon_workdir`.
3. Confirm an active mapping matches the expected workspace before paid start.
4. Pass the **original client path** to `start`; do **not** pass the returned
   daemon path as the next `workdir` while a client map is active.
5. Put `daemon_workdir` in provider-facing `Workdir:` lines.
6. Prefer repo-relative file lists and request repo-relative citations.
7. Reverse-map only component-boundary paths beneath `daemon_root`.
8. Never raw substring-replace entire transcripts.
9. Reverse-map only when `compatible_with_request` is true.
10. If incompatible, report the daemon path and ask the user to open the
    session from the originating client namespace.

### REST and CLI parity

- Shared normalizer: `input workdir + optional PathNamespaceContext`.
- MCP Streamable HTTP: context from static headers.
- REST: accept the same headers on options/start.
- CLI on the daemon host: no mapping needed.
- CLI in a container talking to the host daemon: explicit paired flags
  (illustrative `--client-root` / `--daemon-root`) populating the same
  context — not env as the protocol.
- Session-read endpoints use request context only for **compatibility**
  checks; they must not re-normalize the session’s persisted daemon workdir.

### Optional secondary: host path without maps (F)

If the client environment can inject a host-visible path (devcontainer
`remoteEnv`, identical bind path, human config), the agent may pass that path
as `workdir` with **no** mapping headers. Optional later: explicit
`client_workdir` tool field for dual identity without rewrite. F is a valid
ops/skill path; it is **not** a substitute for G when the model only knows
container paths.

### Global daemon maps (H) — outside MVP

**MVP: no global daemon path maps.** Absence of request mapping headers always
means identity behavior. Silent ambient maps would contradict that rule and
reintroduce multi-client collisions.

If reintroduced after MVP, require **explicit profile selection** (not silent
application when headers are absent). Never let ambient global maps change
no-header semantics.

### Ops mitigation (E)

Bind the host tree at the same absolute path in the container when possible;
then neither maps nor dual identity are required for start success (dual
identity still helps if any other surface emits the other form).

## Security

### Invariants

1. Parse mapping headers only on **authenticated** MCP/REST requests.
2. Mapping is **routing, not a grant**: final path must pass exist + dir +
   `restrict_workdir_roots` with the same meaning as a raw host path.
3. Project `.agent-collab/config.toml` must never define or weaken mappings or
   workdir roots.
4. Allowlist runs on the **final resolved daemon path** before project config
   load (preserve workdir-trust ordering).
5. Component-boundary matching; join via path parts; reject `..`; mandatory
   post-resolve containment under `daemon_root`.
6. All cwd / project-config / provider / sandbox decisions use only
   `daemon_workdir`.
7. Do not log Authorization or mapping headers at default level.
8. Cap header size and (later) map count.
9. Treat mapping headers as **untrusted routing input** from a token holder —
   same class as choosing a host path, not a second principal.
10. Prefer that bearer token and topology headers live in **user/trusted** MCP
    configuration, not automatically in untrusted repository-committed IDE
    config that also holds the daemon token.

### Threat notes

- Same-user bearer token already allows any allowlisted host workdir. Maps do
  not expand that set if join/containment/allowlist hold.
- Wrong map to a sibling project under the allowlist remains an
  operator/client-config error class; visible describe output and skill
  confirmation mitigate but do not eliminate it.
- Empty `restrict_workdir_roots` makes bridging more dangerous in the same way
  unrestricted host workdirs already are; document enabling roots for
  multi-client hosts.

## Phasing

### MVP

- One exact header pair: client root ↔ daemon root.
- Shared MCP/REST normalization with request PathNamespaceContext.
- Structured dual identity + session snapshot + `namespace_id`.
- Compatibility flag on session reads under a different request namespace.
- CLI paired root flags for container→host CLI use.
- Guidance + dual/solo-review skill hard steps.
- Hermetic tests for boundary join, containment, allowlist, missing mapped
  path, no-header identity, dual-client same client prefix different daemon
  roots, project config cannot set maps.

### Phase 2

- Multiple client-declared maps (multi-root workspaces), longest component
  prefix, ambiguity rejection.
- Optional daemon-predeclared profile IDs selected by header (deployments that
  prohibit free-form host `daemon_root` in client config).
- Optional binding to a future durable MCP session ID rejecting mid-session
  namespace changes.
- Diagnostic resolve tool only if support evidence requires it.
- Optional path_namespace reference on event batches.

## Decisions (current)

1. **Preferred shape is G + C:** client-declared request-scoped mapping +
   structured dual identity.
2. **Topology is client-instance knowledge**; daemon does not own a global
   multi-client map registry as the primary design.
3. **MVP uses paired Streamable HTTP headers** for client_root and
   daemon_root (illustrative names until freeze).
4. **Returned `workdir` always daemon path**; dual identity only in
   `path_namespace`.
5. **`path_namespace` always on describe and start**; snapshotted on session;
   echoed on status/list/wait_result with compatibility.
6. **Shared normalizer** for MCP and REST; CLI uses explicit flags when needed.
7. **No task/transcript rewrite.**
8. **No dedicated resolve tool in MVP.**
9. **No global daemon path_maps in MVP**; no-header always means identity.
10. **Mandatory containment** under resolved daemon_root after map.
11. **Skills must** freeze dual paths, pass client path to start, put daemon
    path in provider Workdir, reverse-map only when compatible.
12. **Implementation out of scope** until residual freeze items are closed.
13. **Second Codex review (2026-08-08):** Approve with changes — residual
    freeze items are binding for coding-contract readiness.

## Open questions

1. Exact header names and whether to use `X-` prefix vs bare `Agent-Collab-*`.
2. Exact JSON field names (`path_namespace` shape freeze), including whether
   `input_workdir` and `client_workdir` both stay or one is removed.
3. Whether inactive describe always includes the full object (recommended: yes).
4. Schema for CLI flag names.
5. Whether phase-2 profile IDs should exist before multi-map free-form roots.
6. Session index persistence shape for every session (active and inactive)
   and legacy migration.
7. Internal/scheduler workdir exemptions: must bypass namespace path without
   becoming REST/MCP-reachable (second Codex: freeze required before coding).
8. **Describe→start precondition:** should start require
   `expected_namespace_id` (or opaque receipt from describe) so headers cannot
   silently change between describe and start? (second Codex: blocking)
9. Propagation: every provider-text surface in MVP vs mandatory prior status
   lookup (second Codex: blocking ambiguity).
10. Rename `compatible_with_request` to something that cannot be read as
    “client FS verified” (e.g. `mapping_matches_request`)?

## Residual freeze items (from second Codex review)

Architecture direction (G+C) may be used for implementation **planning**.
Close these before treating the document as the coding/API contract:

- [ ] Freeze exact header names, JSON schema, field nullability, versioning.
- [ ] Freeze canonicalization: lexical `client_root`; **strictly resolved**
      `daemon_root` (exist + dir); resolved root used for reverse map, persist,
      and hash; compatibility compares canonical pairs (digest is identity, not
      comparison authority).
- [ ] Add describe→start namespace precondition (expected id/receipt) **or**
      explicitly abandon the pre-start freeze guarantee in Decisions.
- [ ] Full compatibility matrix: active/active-same, active/active-different,
      active/no-request-map, inactive/active, inactive/inactive, legacy sessions;
      whether `request_namespace_id` is always present.
- [ ] Persist versioned namespace snapshot for **every** new session (including
      inactive); migrate legacy records conservatively as inactive identity.
- [ ] Resolve REST/session-read header parsing (all reads that need
      compatibility) and event/transcript vs wait_result propagation.
- [ ] **Exclude global maps from MVP** so “no headers ⇒ identity” is
      unconditional; if reintroduced later, require explicit profile selection.
- [ ] Resolve scheduler/internal workdir exemption interaction.
- [ ] Make trusted IDE/user MCP configuration and non-loopback transport
      requirements **normative** (not “prefer”); state that `namespace_id` is
      not auth/ownership.
- [ ] Hermetic tests: partial/duplicate headers, auth-before-parse,
      canonical-root symlinks, symlink escape, compatibility combinations,
      legacy records, context propagation, log redaction, internal starts.

## Acceptance criteria (design phase)

- [x] Preferred shape addresses client-owned topology and multi-client
      `/workspaces` overlap.
- [x] Streamable HTTP configuration story without process args.
- [x] Dual-identity agent contract and skill obligations specified.
- [x] Security invariants vs allowlist and project trust stated.
- [x] Global maps demoted; request-scoped client declaration preferred.
- [x] Codex recommendation incorporated (2026-08-08).
- [x] Second Codex review of this revised document recorded
      (`daemon-83bd6f327f0241b9`).
- [ ] Residual freeze items closed (Approve with changes → coding freeze).
- [x] Implementation deferred until freeze.

## Acceptance criteria (implementation phase — draft)

- Hermetic tests listed under MVP phasing.
- describe/start/status/list/wait_result expose `path_namespace` as designed.
- Header parse only when both roots present; fail closed on partial/invalid.
- REST accepts same headers; CLI flags documented.
- Skills and mcp-guidance updated.
- Logging tests: no auth/map header leakage at default level.

## Verification (design pass)

- Adversarial design critiques (2026-08-07) — risk analysis retained.
- Product pushback: daemon must not learn all container layouts (2026-08-08).
- Independent Codex gpt-5.6-sol high recommendation session
  `daemon-4924afa840404662` — preferred G+C adopted into this document.
- Second Codex design review session `daemon-83bd6f327f0241b9` — Approve with
  changes; residual freeze items recorded.

## Implementation plan (after design freeze in code review)

1. Define PathNamespaceContext parse (headers + CLI flags).
2. Shared normalize in describe/start path (MCP + REST).
3. Session snapshot fields + compatibility on reads.
4. wait_result includes path_namespace.
5. Docs, skills, hermetic tests.
6. No mount auto-discovery; no global-map-primary path.

---

## Critique log

### 2026-08-07 — three independent adversarial reviews

**Then-consensus (partially superseded):** dual identity + rewrite good;
free-form header maps bad for v1; prefer user-config maps; persist dual
identity; top-level fields; no resolve tool; REST parity.

**What remains valid**

- Dual identity must be a session fact, not only a start ornament.
- Maps are not permission grants; join/`..`/allowlist discipline required.
- No transcript rewrite; prefer repo-relative file lists.
- Project config must not set workdir policy.
- Bubblewrap wrong-map blast radius is mainly wrong-project execution.

**What is superseded (2026-08-08)**

- “User-config maps only / ban header maps in v1” as the **primary** product
  answer — conflicts with client-owned topology and multi-client
  `/workspaces` overlap.
- “Identity-wins for existing host paths” as a global-map escape hatch —
  replaced by: no headers ⇒ identity; headers present ⇒ explicit map wins
  under client_root.

### 2026-08-08 — product pushback

Live daemon must not know every attaching devcontainer layout. Client should
declare remapping. Global config maps collide across concurrent clients that
share client prefixes.

### 2026-08-08 — Codex gpt-5.6-sol high (session daemon-4924afa840404662)

**Verdict:** Choose **G + C**. Client instance declares topology on each
authenticated HTTP request; daemon authoritative on resolved host path;
return structured dual identity. MVP: one exact client-root ↔ daemon-root
pair via static Streamable HTTP headers. Supersede global daemon path_maps as
primary design.

**Key points adopted into this document**

- Request-scoped (not connection-scoped) mapping context.
- Separate Client-Root / Daemon-Root headers for MVP readability.
- `namespace_id` + `compatible_with_request` for multi-client session safety.
- When headers present, map wins; require workdir under client_root.
- path_namespace on wait_result; no transcript rewrite.
- REST same headers; CLI paired flags; env not the protocol.
- Reject A/B/D/H-as-primary; F as secondary; E ops-only.
- Mandatory containment under daemon_root; project never sets maps.

**Session logs:** `daemon-4924afa840404662` under the global session data
directory.

### 2026-08-08 — second Codex review of revised document
(session `daemon-83bd6f327f0241b9`, gpt-5.6-sol high)

**Overall verdict: Approve with changes.**

G+C is sound and consistent with workdir trust: client declares topology;
daemon authorizes and executes on resolved daemon path. Direction is sound
enough for **implementation planning**. Document is **not yet precise enough
to freeze as the coding/API contract**.

**Blocking issues recorded into residual freeze items**

1. Describe→start freeze not enforced — need `expected_namespace_id`/receipt
   or drop the pre-start guarantee.
2. Canonical identity / symlink semantics for `daemon_root` underspecified
   (return/persist/hash/reverse-map the **resolved** root).
3. Full compatibility matrix + persistence for inactive and legacy sessions.
4. Request-context propagation incomplete (session reads, events/transcripts
   vs wait_result only).
5. Optional global maps contradict unconditional no-header identity — exclude
   from MVP.
6. IDE MCP config trust requirement too soft (“prefer”) — make normative.
7. Internal/scheduler workdir exemption must be specified, not open.

**Non-blocking**

- Header error codes / duplicate headers; drop or justify `expanduser` on
  mapped absolute paths; TOCTOU as inherited same-user risk; rename
  `compatible_with_request`; CLI flags can be a second delivery slice;
  allowlist-before-project-config ordering is correct.

**Suggested Decision lines (to adopt when closing freeze items)**

- MVP has no global daemon path maps; no headers always means identity.
- Server strictly resolves `daemon_root`; resolved root + lexical
  `client_root` form persisted mapping identity.
- Compatibility = exact equality of canonical mapping pairs; advisory
  routing only — never auth/ownership.
- Active mapped start must present describe’s namespace receipt, or fail
  before project config / provider launch.
- All new sessions persist versioned namespace snapshot (including inactive).
- Every authenticated response exposing session paths or provider text
  carries namespace context, or a mandatory prior lookup is defined.
- Repository-controlled MCP config must not receive the daemon bearer token
  without explicit workspace trust.

Logs: `daemon-83bd6f327f0241b9` under the global session data directory.
