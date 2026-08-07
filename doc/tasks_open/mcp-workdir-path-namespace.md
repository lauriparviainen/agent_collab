# MCP Streamable HTTP workdir path-namespace bridging

**Status:** Preliminary design — preferred shape settled after adversarial
critique. **Do not implement** until an implementing agent re-checks this
document against current code and marks implementation acceptance criteria
ready. Config field names remain illustrative until schema freeze in code.

**Created:** 2026-08-07

**Issue:** [#57](https://github.com/lauriparviainen/agent_collab/issues/57)

## Design maturity

This document records a design pass only. Production behavior must not change
until implementation acceptance criteria below are refined into hermetic tests
and shipped deliberately.

Syntax under Configuration is illustrative. Prefer the **Decisions** section
over earlier draft wording if anything conflicts.

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

### Outer sandbox note (accuracy)

Linux outer read-only does **not** mean “only the workdir is visible.” The
common posture is host-wide read-only visibility plus designated workspace
operations on the session workdir. A wrong map therefore mainly risks
**wrong-project execution** (cwd, project config, agent focus), not a classic
“escape a single bind.”

### Non-goals

- Automatic mount-table / inode guessing.
- Project-config path maps (same trust class as workdir roots).
- Free-form per-request header maps in v1.
- Rewriting free-form `task` text or transcript bodies.
- Remote multi-machine daemons without a shared filesystem story.
- A dedicated `agent_collab_resolve_workdir` tool in v1.
- Server-side reverse rewrite of provider answers.

## Goals

1. **Daemon remains authoritative** for the filesystem identity used as cwd,
   project config root, sandbox workspace designation, and allowlisting.
2. **Calling agent is path-aware** when a bridge is active: structured dual
   identity so it can reverse-map daemon paths for IDE actions and know that
   provider output is daemon-namespace.
3. **Streamable HTTP first-class**: works without process `args`; maps are not
   inventing a non-portable argv surface.
4. **Explicit over magic**: configured maps only; fail closed.
5. **Same trust posture** as workdir limits: maps are not permission grants;
   project config cannot set them.
6. **Transport parity**: MCP and REST/CLI share one normalizer.

## Option space (rejected vs kept)

| Option | Verdict |
| --- | --- |
| A. Silent server rewrite only | **Reject as sole design** — blinds the agent |
| B. Instructions / tool text only | **Reject as sole design** — models ignore; skills keep templating client cwd |
| C. Rewrite + structured dual identity | **Accept (refined)** — preferred |
| D. Stdio-adapter remap only | **Reject** — fights preferred Streamable HTTP |
| E. Identical absolute bind mounts | **Ops mitigation only** — document; not a protocol substitute |
| Free-form HTTP header maps as v1 | **Reject** — see Decisions / Security |
| Dedicated resolve tool as MVP | **Reject** — extra hop; describe is already mandatory |
| Transcript / task text rewrite | **Reject for v1** — forensic and false-positive risk |

## Preferred shape (settled direction): config-backed dual-identity normalization

### Principles

1. **One tree, two names** when a map applies.
2. **Request `workdir` stays a single absolute string** in the caller’s
   namespace (client or host). No dual input fields in v1.
3. **Response/session `workdir` always means the daemon path** after
   normalize (authoritative).
4. **`path_namespace` is the machine-readable bridge contract** (always
   present on describe; present on start; snapshotted on the session when a
   map applied).
5. **Maps live only in user-global config for v1.** Streamable HTTP clients
   keep using `url` + auth headers; they do **not** carry map policy in v1.
6. **Reverse mapping is caller-side** using structured fields (and skills).
   The daemon does not mutate transcripts.
7. **Repo-relative paths in review file lists** are the best cognitive
   simplification; absolute paths in provider answers still need reverse map
   when the bridge is active.

### Configuration (illustrative)

User-global only (`~/.agent-collab/config.toml`):

```toml
[workdir]
restrict_workdir_roots = ["~/projects"]  # existing

[[workdir.path_maps]]
from = "/workspaces"
to = "/home/user/projects"

[[workdir.path_maps]]
from = "/mnt/c/Users/me/src"
to = "/home/user/src"
```

Load rules (design intent):

- Project `[workdir]` remains stripped; project cannot define maps.
- `from` / `to`: non-empty; absolute after `expanduser` on `to` as needed;
  reject relative entries, empty segments, `.` / `..`, and `from = "/"`.
- Component-boundary semantics (path parts), not raw `startswith`.
- Cap map count and path length.
- When `restrict_workdir_roots` is non-empty, each resolved `to` must equal or
  sit under a root (config-load defense in depth).
- Reject or warn on ambiguous overlapping `to` prefixes that make reverse map
  non-unique (prefer reject at load).
- Exact string prefixes in v1 (no automatic Windows/WSL slash or case folding).

**Streamable HTTP client config (v1)** stays auth-only:

```json
{
  "type": "http",
  "url": "http://127.0.0.1:8765/mcp",
  "headers": {
    "Authorization": "Bearer <token>"
  }
}
```

Maps are configured once on the **daemon host**, not as MCP “server args.”
That is intentional: filesystem topology is operator policy, same class as
`restrict_workdir_roots`. Preferred transport has no argv; stuffing host path
rewrites into IDE MCP headers would recreate project-adjacent policy control.

**Ops alternative (E):** bind the host tree at the same absolute path inside
the container when the environment allows it; then maps are unnecessary.

### Normalization algorithm (shared MCP + REST)

Single conceptual function used by describe and start (MCP tools and REST
options/sessions, hence CLI `--workdir`):

```text
1. Require absolute workdir input (after expanduser); reject empty / relative.
2. Lexical hygiene: strip trailing slashes (keep "/"); reject ".." / "." segments
   in the input string before mapping.
3. Identity-wins (recommended): if expanduser().resolve(input) already exists
   as a directory on the host, treat as daemon_workdir with no map
   (client_workdir null). This preserves “I already know the host path” for
   CLI/REST and avoids maps hijacking real host prefixes.
4. Else apply longest path_map with component-boundary prefix match:
     daemon_candidate = join(to_parts + suffix_parts)  # never string concat
     client_workdir = original input
     record maps_applied
   If no map: daemon_candidate = input; client_workdir = null.
5. daemon_workdir = expanduser().resolve(daemon_candidate)
6. Optional hard check: when a map applied, require resolved path equals or is
   under resolve(to) (symlink containment) — decide at implementation freeze.
7. Existing resolve_existing_workdir checks (exist + is_dir) +
   validate_workdir_allowed(daemon_workdir).
8. Never rewrite task text or transcripts.
```

Join must reject `..` in the unmatched suffix. Map-before-resolve is mandatory
for non-existing client paths; identity-wins covers existing host paths.

### Agent-visible contract

#### Response fields (names provisional until code freeze)

Always on **describe_options** (and on **start**):

```json
{
  "workdir": "/home/user/projects/agent_collab",
  "path_namespace": {
    "active": true,
    "input_workdir": "/workspaces/agent_collab",
    "client_workdir": "/workspaces/agent_collab",
    "daemon_workdir": "/home/user/projects/agent_collab",
    "maps_applied": [
      {"from": "/workspaces", "to": "/home/user/projects"}
    ]
  },
  "discovery": {
    "workdir": "/home/user/projects/agent_collab"
  }
}
```

When inactive:

```json
"path_namespace": {
  "active": false,
  "input_workdir": "/home/user/projects/agent_collab",
  "client_workdir": null,
  "daemon_workdir": "/home/user/projects/agent_collab",
  "maps_applied": []
}
```

Rules:

- Top-level `workdir` and `discovery.workdir` **always** equal `daemon_workdir`.
- Do **not** redefine `workdir` to mean “what the client sent.”
- Prefer **no free-form `guidance` essay inside JSON**; prose lives in
  `mcp-guidance.md` and skills.
- When a map applied at start, **snapshot** bridge metadata on the session so
  status/list can re-attach dual identity after map config changes.

#### How the calling agent is taught (ordered by reliability)

| Channel | Role |
| --- | --- |
| Structured `path_namespace` on describe/start | **Primary contract** |
| Session status/list echo of snapshot | Long-session re-attach |
| dual-review / solo-review / review-recipe skills | Mandatory freeze of both paths; daemon path in `Workdir:`; reverse-map citations; prefer **relative** changed-file lists |
| `mcp-guidance` overview / options / review-recipe | Procedural rule |
| Tool property description on `workdir` | One-line hint only |
| `initialize.instructions` | Short pointer at most; already saturated |
| Dynamic tool description suffix when maps exist | Optional weak hint |

**Do not rely on tool description or initialize text alone.**

#### Calling-agent operational rule

1. Call `describe_options` with the absolute workspace path (client path OK if
   maps are configured).
2. Read `path_namespace`. Freeze `client_workdir` and `daemon_workdir` when
   `active`.
3. Pass the **caller** path as start `workdir` (daemon rewrites). Treat returned
   `workdir` as daemon truth.
4. Put **`daemon_workdir`** in provider task `Workdir:` lines; keep changed-file
   lists **repo-relative** when possible.
5. When opening paths from provider output in the IDE, reverse-map using
   `maps_applied` / workdir prefix swap when `active`.
6. Never invent maps client-side.

### Error shapes (intent)

- Unmapped client path missing on host: today’s error + hint if any maps are
  configured (“no path_map matched; add `[[workdir.path_maps]]` or pass a
  host-absolute workdir”). Do not dump all host targets by default.
- Mapped path missing: show input → mapped daemon path.
- Allowlist failure: same class as today on the **daemon** path.

### Stdio adapter

v1: no special remap; it already talks to the daemon. Durable maps live in
user config for all transports. Optional later: adapter flags that only select
predeclared maps (not free-form host `to`).

### Phase 2 (explicitly deferred)

- Constrained header overlay: select among predeclared maps (e.g. client
  prefix or map id), default **off**; never free-form `to=` from IDE config
  without operator opt-in.
- Optional pure `resolve_workdir` debug tool.
- Optional `path_namespace` echo on wait_result for late attach.
- Stdio `--workdir-map` as syntax sugar that only selects predeclared maps.

## Security

### Verdict from adversarial security review

**Conditional accept** of dual identity + user-config maps; **reject free-form
request header maps for v1.**

### Invariants

1. Execution, project config load, sandbox workspace identity, and allowlist
   use **only** resolved `daemon_workdir`.
2. Maps are **not** grants: final path must pass exist + dir +
   `restrict_workdir_roots`.
3. Project cannot define maps; project `[workdir]` strip must cover them.
4. Lexical map before host resolve (except identity-wins for existing host
   directories); join via path parts; reject `..`.
5. When roots are set, validate map `to` under roots at config load.
6. No Authorization or map-header logging at default level.
7. Same-user bearer-token threat model unchanged; wrong maps mainly cause
   wrong-project sessions under the allowlist (operator/config error class).

### Why not Streamable HTTP “server arguments” for maps?

Streamable HTTP’s only portable client knobs are **URL and headers**. Auth
belongs in headers. **Host filesystem topology does not**: it is operator
policy next to `restrict_workdir_roots`, must be visible in
`agent-collab config show`, must be shared by REST/CLI, and must not become
project-adjacent IDE MCP config that a workspace can influence. Teaching the
agent dual identity is done via **tool responses and skills**, not by hiding
maps in headers.

## Decisions (settled after critique)

1. **Preferred shape:** rewrite + structured dual identity (C-lite).
2. **Maps in user-global config only for v1**; no free-form header/query maps.
3. **Returned `workdir` always daemon path**; dual identity only in
   `path_namespace` (names may be adjusted at implement time).
4. **`path_namespace` always present on describe** (active true/false); on
   start always; snapshot on session when active.
5. **Shared normalizer** for MCP and REST/CLI.
6. **No dedicated resolve tool in v1.**
7. **No task/transcript rewrite in v1.**
8. **Identity-wins:** existing host directory inputs are not rewritten by maps.
9. **Host paths always accepted** when no map matches (maps optional).
10. **Skills/recipe must** freeze dual paths, put daemon path in `Workdir:`,
    prefer relative file lists, reverse-map citations when active.
11. **Ops bind-mount identical paths** remain a documented mitigation, not a
    substitute for maps.
12. **Implementation is out of scope for this design issue** until an
    implementer re-validates and lands tests.

## Open questions (remaining)

1. Exact JSON field names (`path_namespace` vs `path_bridge`) and whether
   inactive describe always includes the object (design recommends always).
2. Whether post-resolve containment under `to` is mandatory or optional.
3. Schema_version / config migration details for `path_maps`.
4. Whether session index persistence of `client_workdir` is required in v1 or
   only full SessionState responses.
5. Interaction with any internal workdir exemptions (must be explicit in code
   review).

## Acceptance criteria (design phase)

- [x] Preferred shape settled under Decisions.
- [x] Rejected alternatives listed with reasons.
- [x] Agent-visible dual-identity contract specified.
- [x] Streamable HTTP story specified without process args or v1 header maps.
- [x] Security interaction with roots and project trust stated.
- [x] Adversarial critiques recorded.
- [x] Implementation deferred.

## Acceptance criteria (implementation phase — draft)

Refine before coding:

- Hermetic tests for longest-prefix, boundary match, trailing slashes, `..`
  rejection, identity-wins, allowlist, missing mapped path, project maps
  stripped, REST/MCP parity, no-map bit-identical behavior.
- describe/start expose `path_namespace`; session echoes snapshot when active.
- Guidance + dual/solo-review skills updated.
- Config show surfaces path maps.
- Logging policy tests for auth/header non-leakage.

## Verification (this design pass)

- Independent adversarial design critiques (product/UX and protocol).
- Independent security-focused critique.
- Consistency check against workdir trust task and Streamable HTTP MCP surface.

## Implementation plan (after design freeze in code review)

1. Add user-config `path_maps` load + validation.
2. Implement shared `normalize_workdir` / apply path namespace.
3. Wire describe + start (MCP and REST); session snapshot fields.
4. Docs: mcp-guidance, README Streamable HTTP / devcontainer note, skills.
5. Hermetic tests listed above.
6. No mount auto-discovery; no header maps unless a later design reopens Phase 2.

---

## Critique log

### 2026-08-07 — three independent adversarial reviews

**Consensus**

- Dual-identity + input rewrite is the right product direction.
- Free-form Streamable HTTP header maps are a **bad v1 API** (ambient identity,
  project-adjacent IDE config, encoding/plumbing tax, multi-client chaos).
- User-config maps + structured response fields + skill teaching is the MVP.
- Dual identity must be a **session fact**, not only a start-response garnish.
- Top-level / always-on structured fields beat nested guidance essays and
  initialize.instructions.
- Dedicated resolve tool is unnecessary for MVP.
- REST/MCP must share one normalizer.
- Reverse map is caller-side; no transcript mutation.
- Prefer repo-relative paths in review file lists to reduce path confusion.

**Disagreements / refinements adopted**

| Topic | Outcome |
| --- | --- |
| Header maps in leading draft | **Dropped for v1** (all three critics) |
| Identity-wins for existing host paths | **Adopted** (security critic; aligns with “host paths always work”) |
| Always include `path_namespace` vs only when active | **Always on describe** (cognition); start always recommended |
| Free-form `guidance` string in JSON | **Prefer omit**; prose in mcp-guidance/skills |
| Bubblewrap “only workdir visible” | **Corrected** in this doc |
| Phase 2 headers | Only as **select among predeclared maps**, default off |

**Residual risks (accepted for design)**

- Models may still ignore `path_namespace` without skill hard-steps.
- Wrong map to a sibling project under the allowlist remains an operator error
  class (same as passing the wrong host path).
- Exact-prefix maps do not auto-fix WSL vs Windows path spelling differences.

Critiques performed as read-only plan agents on 2026-08-07 against this
document’s first draft and the workdir-trust / MCP code paths.
