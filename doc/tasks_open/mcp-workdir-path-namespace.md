# MCP Streamable HTTP workdir path-namespace bridging

**Status:** Design direction frozen for implementation planning as **G+C**
(client-declared request-scoped mapping + dual identity). Second Codex review
blocking items are **folded into normative rules** below. Exact header/JSON
identifier spellings remain illustrative until code freeze. **Do not implement
until hermetic tests and schema names are locked in a coding PR.**

**Created:** 2026-08-07

**Updated:** 2026-08-08 — G+C preferred; second Codex approve-with-changes
items folded into algorithm, Decisions, security, and MVP (not only a
checklist).

**Issue:** [#57](https://github.com/lauriparviainen/agent_collab/issues/57)

## Design maturity

This document is design-only: no production behavior change until a deliberate
implementation lands with tests. Prefer **Decisions** and the normative
sections (algorithm, contract, security, exemptions) over critique-log
history if anything conflicts.

## Context

### How workdir works today

- MCP `agent_collab_describe_options` and `agent_collab_start` require an
  absolute `workdir`.
- The daemon resolves it with `expanduser().resolve()`, rejects missing or
  non-directory paths, optionally enforces user-global
  `[workdir].restrict_workdir_roots`, loads
  `WORKDIR/.agent-collab/config.toml`, and uses that path as provider
  subprocess cwd and outer Bubblewrap workspace designation.
- Preferred MCP transport is Streamable HTTP `POST /mcp` (`url` + `headers`;
  no process `args`).
- Session state stores a single workdir string (daemon path). Status, list,
  transcripts, and provider output speak that identity.
- Review skills freeze an absolute workdir from the caller environment and
  embed `Workdir: …` in the provider prompt.

### Failure mode

| Actor | Path identity |
| --- | --- |
| Agent / IDE in a devcontainer | e.g. `/workspaces/project` |
| Host agent-collab daemon | e.g. `/home/user/src/project` (or missing) |

Client-absolute paths fail with `workdir does not exist`, or bind a wrong tree
if a coincidental host path exists. Provider output is daemon-namespace even
when the calling agent’s IDE tools are client-namespace.

### Product constraint

The live host daemon must **not** be a registry of every attaching
devcontainer layout. Topology is **client-instance** knowledge. Concurrent
clients often share `/workspaces` while mapping to different host trees; a
process-global map table cannot disambiguate them.

Client declares remapping (or already passes a host-visible path). Daemon
validates/executes on host paths only and discloses both identities so the
agent can reverse-map.

### Outer sandbox note

Outer read-only is typically host-wide RO plus designated workspace identity
on the session workdir. Wrong mapping risks **wrong-project execution**, not
a classic single-bind escape.

### Non-goals (MVP)

- Mount-table / inode auto-discovery.
- Project-config workdir maps or roots.
- **Any** user-global daemon `path_maps` (not even as ambient convenience).
- Task-text or transcript body rewrite.
- Remote daemon without shared filesystem.
- Dedicated `agent_collab_resolve_workdir` tool.
- Env vars as the protocol contract.

## Goals

1. Daemon authoritative for cwd, project config, sandbox workspace, allowlist.
2. Client owns topology declaration for non-host namespaces.
3. Calling agent path-aware via structured dual identity.
4. Streamable HTTP first-class (`url` + `headers`).
5. No global multi-client map collisions (request-scoped context + session
   snapshot).
6. Explicit, fail-closed; maps are not permission grants.
7. MCP + REST share one normalizer; CLI has explicit bridge flags when needed.
8. Describe→start cannot silently change namespace under the agent’s feet.

## Option space

| Option | Verdict |
| --- | --- |
| A Silent rewrite only | Reject as sole design |
| B Instructions only | Reject as sole design |
| C Dual-identity responses | Accept (disclosure) |
| D Stdio-only remap | Reject |
| E Identical bind paths | Ops mitigation only |
| F Host `workdir` + optional client path | Secondary fallback |
| G Client-declared request-scoped maps | **Primary** (with C) |
| H Global daemon `path_maps` | **Out of MVP entirely** |

## Preferred shape: G + C

### Principles

1. Client declares topology; daemon validates and runs on host path only.
2. Mapping context is **request-scoped** (headers / CLI flags on each call),
   not connection-scoped (Streamable HTTP POSTs close).
3. MVP: one exact `client_root` ↔ `daemon_root` pair.
4. Headers present ⇒ map wins for inputs under `client_root`; workdir must
   stay under `client_root`.
5. Headers absent ⇒ **identity only** (no ambient global maps).
6. Top-level `workdir` after normalize always means daemon path.
7. `path_namespace` is the machine-readable contract; reverse map is
   caller-side.
8. Prefer repo-relative paths in review file lists.
9. Active mapped **start** must prove the same namespace describe returned
   (`expected_namespace_id`).

---

## Normative: PathNamespaceContext

### Sources (authenticated requests only)

| Transport | How context is supplied |
| --- | --- |
| Streamable HTTP MCP | Headers on **every** authenticated request that needs map apply or compatibility (not only options/start) |
| REST | Same headers on the same class of endpoints |
| CLI | `--client-root` / `--daemon-root` (illustrative names) when CLI runs outside the daemon namespace |
| Internal/scheduler | **No** client maps; see Internal exemptions |

### Header rules (illustrative names)

```http
Agent-Collab-Client-Root: /workspaces/project
Agent-Collab-Daemon-Root: /home/alice/src/project
```

- Both present together, or neither. Exactly one of the pair ⇒ **400** (or
  equivalent tool error) after auth; do not partially apply.
- Duplicate conflicting values for the same header ⇒ **400**.
- Cap total mapping-header bytes; reject control characters / NUL.
- Do not log Authorization or mapping header values at default log level.
- Auth is evaluated before map parse; unauthenticated callers never see map
  diagnostics that leak host layout beyond existing public errors.

### Canonical forms

| Field | Canonical form |
| --- | --- |
| `client_root` | Absolute **lexical** POSIX path: no empty segments, no `.` / `..`, no trailing slash except `/`, component-normalized. Not host-resolved (may not exist on daemon). |
| `daemon_root` | Header spelling expanded if needed, then **`Path.resolve()`** on the host. Must exist and be a directory **before** any workdir join. Reject if resolve fails or not a dir. |
| Mapping identity | Pair `(canonical_client_root, resolved_daemon_root)`. |
| `namespace_id` | Non-secret digest of that pair (e.g. sha256 of a fixed encoding). Used as an **identifier / receipt**, not as the sole equality authority. |
| Compatibility | Exact equality of the two canonical roots (string forms after the rules above). Digest mismatch implies incompatibility; equality of roots is the comparison. |

`namespace_id` and `mapping_matches_request` are **advisory routing metadata**.
They are **not** authentication, authorization, session ownership, or proof
that a client-side mount exists.

### Normalization algorithm (MCP + REST describe/start)

```text
parse_context(request):
  if neither root header: return InactiveContext
  if only one: fail closed (invalid mapping headers)
  client_root = lexical_canonicalize(Client-Root)
  daemon_root = strict_resolve_existing_dir(Daemon-Root)
  namespace_id = digest(client_root, daemon_root)
  return ActiveContext(client_root, daemon_root, namespace_id)

normalize_workdir(input, context) -> NormalizedWorkdir:
  require absolute input; reject ".." / "." segments; strip trailing slash
  if context is Inactive:
    daemon_workdir = strict_resolve_existing_dir(input)  # today's rules;
                                                         # no expanduser tricks
                                                         # beyond today's workdir
    return {
      active: false,
      client_root: null,
      daemon_root: null,
      client_workdir: null,
      daemon_workdir,
      namespace_id: null,
      input_workdir: input  # optional echo; may omit if redundant
    }
  # Active:
  require input under client_root (component boundary)
  suffix = relative parts after client_root
  daemon_candidate = join(daemon_root.parts + suffix)   # Path parts, not concat
  daemon_workdir = resolve(daemon_candidate)
  require daemon_workdir equals or under context.daemon_root
  require exist + is_dir
  validate_workdir_allowed(daemon_workdir)
  return {
    active: true,
    client_root, daemon_root,  # daemon_root already resolved
    client_workdir: input,     # caller's path as submitted (under client_root)
    daemon_workdir,
    namespace_id,
    input_workdir: input       # same as client_workdir when active; keep one
                               # field in schema freeze if preferred
  }
```

**No `expanduser` on already-absolute mapped paths** beyond whatever today’s
workdir path does for bare identity inputs. Mapped `daemon_root` must be an
absolute host path the operator/client can spell without relying on daemon
home expansion for security-sensitive roots.

**TOCTOU / symlink races** after resolve are the same same-user class as
today’s workdir resolution; document, do not claim `resolve()` eliminates them.

Never rewrite task text or transcript bodies.

### Describe → start precondition (normative)

When the request carries an **active** mapping context:

1. `describe_options` returns `path_namespace` including `namespace_id`.
2. `start` **must** include tool/body field `expected_namespace_id` equal to
   that `namespace_id` (illustrative name).
3. Server recomputes context from **this request’s** headers, normalizes
   workdir, and **fails closed before project-config load or provider launch**
   if:
   - headers absent/invalid while `expected_namespace_id` is set, or
   - computed `namespace_id` ≠ `expected_namespace_id`, or
   - `expected_namespace_id` is missing while mapping headers are present.

When the request has **no** mapping headers (identity mode):

- `expected_namespace_id` must be absent or null.
- If the client sends `expected_namespace_id` without mapping headers ⇒ fail
  closed.

This enforces the skill “freeze namespace after describe” at the API boundary
so headers cannot silently change between describe and start.

CLI: when `--client-root` / `--daemon-root` are set, start requires the same
expected id (from a prior describe or a deterministic local recompute the CLI
prints).

### Internal / scheduler exemptions (normative)

Trusted internal starts (e.g. usage-window scheduler empty workdir) that today
bypass ordinary workdir policy:

- **Do not** parse client mapping headers or CLI map flags.
- **Do not** require `expected_namespace_id`.
- Produce an **inactive** `path_namespace` snapshot (or omit dual fields with
  the same meaning as inactive).
- Must remain **unreachable** through public REST/MCP session-create shapes.
- Document the exemption next to the existing internal workdir exemption in
  daemon architecture notes at implementation time.

---

## Agent-visible contract

### Field meanings

| Field | Meaning |
| --- | --- |
| `workdir` (top-level / session) | Always **daemon** absolute path after normalize |
| `path_namespace.active` | Whether a client map applied for origin or request |
| `path_namespace.namespace_id` | Digest of origin mapping pair (null if inactive) |
| `path_namespace.client_root` | Lexical client root (null if inactive) |
| `path_namespace.daemon_root` | **Resolved** daemon root (null if inactive) |
| `path_namespace.client_workdir` | Caller’s workdir in client namespace when active; null if inactive |
| `path_namespace.daemon_workdir` | Same as top-level `workdir` |
| `path_namespace.request_namespace_id` | Digest of **this request’s** map if any; always present on responses that evaluate compatibility (null if request inactive) |
| `path_namespace.mapping_matches_request` | Whether origin mapping pair equals request mapping pair (see matrix). **Not** “client can open this path.” |

Drop free-form `guidance` strings from JSON; prose lives in mcp-guidance.

### Response sketch (active describe)

```json
{
  "workdir": "/home/alice/src/project/packages/core",
  "path_namespace": {
    "version": 1,
    "active": true,
    "namespace_id": "sha256:<digest>",
    "client_root": "/workspaces/project",
    "client_workdir": "/workspaces/project/packages/core",
    "daemon_root": "/home/alice/src/project",
    "daemon_workdir": "/home/alice/src/project/packages/core",
    "request_namespace_id": "sha256:<digest>",
    "mapping_matches_request": true
  }
}
```

### Start (active)

Tool args include:

```json
{
  "workdir": "/workspaces/project",
  "expected_namespace_id": "sha256:<digest from describe>"
}
```

Response includes the same `path_namespace` shape; session persists a
**versioned origin snapshot** (active or inactive).

### Where `path_namespace` is emitted (MVP)

| Surface | Rule |
| --- | --- |
| `describe_options` | Always full object (active or inactive) |
| `start` | Always; persist origin snapshot on **every** new session |
| `status`, `list_sessions` | Echo origin snapshot; compute `request_namespace_id` + `mapping_matches_request` from this request’s headers |
| `wait_result` | Echo origin snapshot + mapping match vs this request |
| `read_events` / `wait_events` | Include origin snapshot (or compact reference equal to full origin fields) on every authenticated response that returns session events |
| `read_transcript` | Include origin snapshot in the response envelope (do **not** rewrite transcript body text) |
| Transcript / event **bodies** | Never path-rewritten |

If a client cannot send mapping headers on a read, treat request context as
inactive: `request_namespace_id: null`, and `mapping_matches_request` is true
only when the **session origin** is also inactive; if session origin is
active, `mapping_matches_request` is **false** (caller must not reverse-map
as if it owned the origin client root).

### Compatibility matrix

| Session origin | Request context | `mapping_matches_request` | Agent reverse-map using origin client_root? |
| --- | --- | --- | --- |
| inactive | inactive | true | N/A (already daemon paths) |
| inactive | active | false | No — request map is not the session origin |
| active | inactive | false | No — missing request map; report daemon paths only |
| active | active, same pair | true | Yes, under origin/daemon roots |
| active | active, different pair | false | No — different client instance |
| legacy session (no snapshot fields) | any | treat origin as **inactive** (conservative migration) | No dual reverse-map |

### Persistence

- Every **new** session stores a versioned `path_namespace` origin snapshot
  (including `active: false` identity sessions).
- Legacy index records without snapshot fields migrate as **inactive**
  identity sessions.
- Session reads never re-normalize the stored daemon workdir from new maps;
  they only compare request context to the snapshot.

### Multi-client `/workspaces`

Two clients may both use client_root `/workspaces/project` with different
daemon_roots. Each request uses only its headers. `namespace_id` differs.
`mapping_matches_request` prevents treating another client’s session client
paths as local.

---

## Agent / skill obligations

1. `describe_options` with caller-visible absolute path (and mapping headers
   from MCP client config).
2. Freeze `namespace_id`, `client_workdir`, `daemon_workdir`, roots.
3. Confirm active mapping matches the expected workspace before paid start.
4. `start` with **client** `workdir` + `expected_namespace_id` from describe.
5. Never pass returned daemon path as start `workdir` while client map headers
   are configured.
6. Put `daemon_workdir` in provider `Workdir:` lines.
7. Prefer repo-relative file lists and citations.
8. Reverse-map only component-boundary paths under `daemon_root`, and only
   when `mapping_matches_request` is true.
9. No raw whole-transcript substring replacement.
10. If `mapping_matches_request` is false, report daemon paths and tell the
    user to use the originating client namespace.

Education channels: structured fields primary; skills hard-step; mcp-guidance;
one-line tool property hint; initialize.instructions pointer only.

---

## REST and CLI parity

- One normalizer: `workdir + PathNamespaceContext + optional expected_namespace_id`.
- REST: same mapping headers on describe/start **and** on session-read
  endpoints that compute `mapping_matches_request`.
- CLI host-local: identity mode.
- CLI container→host: `--client-root` / `--daemon-root` + expected id as above.
- Env is not the protocol.

---

## Secondary: host path without maps (F)

If the environment already exposes a host-visible path, pass it as `workdir`
with no mapping headers. Optional later: explicit `client_workdir` tool field
for dual identity without rewrite. F does not replace G when the model only
knows container paths.

## Global maps (H)

**Not in MVP. Not ambient. Not documented as a recommended mode.**  
Future work only via explicit profile selection design (phase 2+), never silent
no-header rewriting.

## Ops (E)

Identical absolute bind mounts remain a valid zero-protocol mitigation.

---

## Security (normative)

1. Mapping headers only after successful auth.
2. Maps are routing, not grants; final path must pass exist + dir +
   `restrict_workdir_roots` with today’s meaning.
3. Project config never defines maps or roots policy.
4. Allowlist on final resolved daemon path **before** project config load.
5. Component-boundary match; Path-part join; reject `..`; mandatory
   containment under **resolved** `daemon_root`.
6. Execution surfaces use only `daemon_workdir`.
7. No Authorization / mapping header values in default logs.
8. Cap header size; reject partial/duplicate map headers.
9. **Repository-controlled MCP configuration must not receive or embed the
   daemon bearer token without explicit workspace trust.** Topology headers
   alone are not a second principal; the dangerous pair is
   **token + attacker-controlled URL/headers** in untrusted project config.
   Prefer user-level or trusted-container MCP config. Non-loopback access
   requires the existing token model and should use a trusted tunnel or TLS
   as already implied by daemon deployment guidance.
10. `namespace_id` / `mapping_matches_request` are not authz controls.
11. Symlink/TOCTOU: same-user residual risk as today’s resolve; containment
    uses resolved roots.

---

## Phasing

### MVP (includes folded freeze items)

- Paired mapping headers + parse/fail rules.
- Shared normalize + containment.
- `expected_namespace_id` on active start.
- Full `path_namespace` on describe/start/status/list/wait_result/
  read_events/wait_events/read_transcript envelope.
- Origin snapshot on every new session; legacy → inactive.
- Compatibility matrix as above.
- Internal exemption rules.
- CLI root flags + expected id.
- Skills + mcp-guidance.
- Hermetic tests: partial/duplicate headers; auth-before-parse; under-root
  join; symlink daemon_root reverse-map; expected_namespace_id mismatch;
  no-header identity; two clients same client_root different daemon_root;
  legacy migration; log redaction; project cannot set maps; internal start
  inactive.

### Phase 2

- Multi-root client maps (longest prefix, ambiguity reject).
- Optional predeclared daemon profile IDs (no free-form daemon_root in client).
- Durable MCP session id binding namespace.
- Resolve tool only if needed.

---

## Decisions (normative)

1. Preferred shape is **G + C**.
2. Topology is client-instance knowledge; no global multi-client map registry.
3. **MVP has zero global daemon path_maps**; no mapping headers ⇒ identity.
4. MVP mapping surface: paired Streamable HTTP headers (illustrative names)
   plus CLI flags; both roots required together.
5. `client_root` lexical; `daemon_root` strictly resolved exist+dir; that pair
   is mapping identity; `namespace_id` is its digest.
6. Top-level `workdir` always daemon path after normalize.
7. `path_namespace` always on describe/start; full origin snapshot on every
   new session; echoed on status/list/wait_result and event/transcript
   **envelopes**; bodies never rewritten.
8. Field `mapping_matches_request` (not “compatible” as filesystem proof);
   `request_namespace_id` always present on responses that evaluate match
   (null if request inactive).
9. Active mapped start requires `expected_namespace_id` matching this
   request’s computed id; fail before project config / provider launch.
10. Shared normalizer for MCP and REST; CLI uses explicit flags when needed.
11. No task/transcript body rewrite; no resolve tool in MVP.
12. Mandatory containment under resolved daemon_root.
13. Internal/scheduler starts: inactive namespace, no client maps, not
    public-API reachable.
14. Token + topology must not live in untrusted repository MCP config without
    workspace trust; `namespace_id` is not auth.
15. Implementation deferred to a coding PR with tests; design rules above are
    the intended contract.

## Open questions (cosmetic / naming only)

1. Final header spellings (`Agent-Collab-*` vs `X-Agent-Collab-*`).
2. Final JSON key spellings if bikeshed requires rename (semantics fixed).
3. Whether to keep both `input_workdir` and `client_workdir` or only
   `client_workdir` when active.
4. Exact CLI flag spellings.
5. Digest encoding string format for `namespace_id`.

## Acceptance criteria (design phase)

- [x] Client-owned topology and multi-client overlap addressed.
- [x] Streamable HTTP without process args.
- [x] Dual-identity contract + skill obligations.
- [x] Security vs allowlist and project trust.
- [x] Global maps out of MVP; no-header identity unconditional.
- [x] Describe→start expected namespace precondition specified.
- [x] Canonical root / symlink reverse-map rules specified.
- [x] Compatibility matrix + persistence + legacy migration specified.
- [x] Propagation to event/transcript envelopes specified.
- [x] Internal exemption specified.
- [x] IDE/token trust normative.
- [x] Codex recommendation + second review recorded and folded.
- [x] Implementation still deferred to a tested coding change.

## Acceptance criteria (implementation phase — draft)

- All MVP hermetic tests listed under Phasing.
- Schema and OpenAPI/MCP tool schemas match Decisions.
- Skills and mcp-guidance updated.
- Config show / docs state: no global path_maps in product.

## Verification

- Adversarial critiques 2026-08-07 (risk analysis).
- Product pushback 2026-08-08 (client owns topology).
- Codex recommendation `daemon-4924afa840404662`.
- Codex second review `daemon-83bd6f327f0241b9` (Approve with changes) —
  blocking items folded into this revision.

## Implementation plan

1. PathNamespaceContext parse (headers + CLI) with fail-closed partials.
2. Shared normalize + containment + allowlist ordering.
3. `expected_namespace_id` on start.
4. Session snapshot for every session; legacy migrate inactive.
5. Emit path_namespace on all MVP surfaces including event/transcript
   envelopes.
6. Internal exemption wiring.
7. Docs, skills, tests.

---

## Critique log

### 2026-08-07 — adversarial reviews

Retained risks: dual identity must persist; maps ≠ grants; no transcript
rewrite; project cannot set policy; wrong map ⇒ wrong project under allowlist.

Superseded: “user-config maps only / ban headers” as primary; ambient
identity-wins under global maps.

### 2026-08-08 — product pushback

Daemon must not learn every container layout; client declares remap; global
maps collide on shared client prefixes.

### 2026-08-08 — Codex recommendation (`daemon-4924afa840404662`)

G+C; request-scoped headers; dual identity; demote global maps; namespace_id;
wait_result awareness; REST/CLI parity.

### 2026-08-08 — Codex second review (`daemon-83bd6f327f0241b9`)

**Approve with changes** → changes **folded** in this revision:

| Blocking item | Folded as |
| --- | --- |
| Describe→start freeze | `expected_namespace_id` required when map active |
| Canonical / symlink roots | lexical client_root; strict resolve daemon_root; reverse-map uses resolved root |
| Compatibility + persistence | matrix + every-session snapshot + legacy inactive |
| Propagation | path_namespace on event/transcript envelopes in MVP |
| Global maps vs no-header identity | global maps out of MVP entirely |
| IDE token trust | normative security decision 9 / 14 |
| Internal exemption | dedicated section |

Non-blocking nits also folded where cheap: rename to
`mapping_matches_request`; no expanduser on absolute mapped roots; TOCTOU
note; header partial/duplicate fail-closed.
