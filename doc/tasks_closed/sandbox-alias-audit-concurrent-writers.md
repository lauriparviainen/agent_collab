# Sandbox alias audit under concurrent workspace writers

**Status:** Implemented and verified on 2026-07-31. The two-phase audit,
revalidation discipline, accounting-only peer roots, same-device protected
search, and operational diagnostics are shipped in the working tree. A
post-implementation review sequence found and closed a peer-path aliasing
fail-open, an all-or-nothing peer-walk availability regression, a mount-
multipath peer alias, missing Claude SDK peer accounting, and the dual
writable-file-bind peer alias. A final independent review also closed duplicate
mount views within and across accounting peer roots, then the same class within
writable remainders.

**Created:** 2026-07-30

**Issue:** [#55](https://github.com/lauriparviainen/agent_collab/issues/55)

## Context

The outer sandbox refuses to start a session whose workspace is being written
by unrelated local processes. A repository that keeps a gitignored data
directory bind-mounted into service containers — databases, search indices, an
observability stack — never holds still, and every launch fails with
`outer_sandbox_alias_audit_failed`.

This is not a start-time-only gate. `audit_aliases` runs at plan resolution and
again in `SandboxSupervisor._launch` before every preflight and provider launch
(`agent_collab/sandbox/supervisor.py:334-344`), so a session that survives its
first turn can still fail on a later one, and every turn pays a full
workspace-sized walk.

### Root cause

The hard-link half of the audit inventories the entire protected tree in order
to compare it against writable remainders: `_collect_protected_inodes`
(`agent_collab/sandbox/paths.py:976-1003`) drives `_walk_no_symlinks`
(`paths.py:1046-1109`). The walk `listdir`s a directory and then reopens each
child with `O_NOFOLLOW`; **any** `OSError` from that reopen becomes
`outer_sandbox_alias_audit_failed` (`paths.py:1084-1095`). A file unlinked
between the `listdir` and the `os.open` is therefore reported identically to
genuine tampering.

Reproduced directly against `_walk_no_symlinks`: one thread creating and
unlinking a single file in one subdirectory of an 8k-entry tree failed 1 of 5
walks. Production workloads write thousands of files continuously across the
tree, so failure is the expected outcome rather than the tail case.

On a large data directory the same walk is also a plausible route to
`outer_sandbox_alias_audit_exceeded` against the 10 s / 1e6-entry defaults.

### Interim workaround

Point the session at a tree nothing else writes: clone the tracked content into
a scratch directory and copy the modified working-tree files on top. Untracked
data directories do not come along and the resulting diff is identical. Manual,
and it moves the paths agents report.

## Goal

A session starts and takes turns in a workspace with continuously written
gitignored subtrees, on every shipped backend, without weakening the hard-link
containment guarantee and without new per-session operator work.

## Decisions

### D1 — Both walks stay strict on every errno

The obvious fix, treating `ENOENT`/`ESTALE` as "entry absent", is **rejected**.

Given a pre-existing hard link `workspace/secret` <-> `state/alias`, an attacker
renames `workspace/secret` away during the protected walk so the child reopen
returns `ENOENT`, lets the walk skip the entry, then renames it back. The
writable walk finds no match, the audit passes, and bwrap mounts both trees —
the sandboxed process now has a writable name for read-only protected content.
Today that race fails closed.

The adversary is not new: planting `state/alias` requires a host process running
as the daemon uid, because writable state roots are private (0700) and
re-checked for daemon ownership at audit time (`paths.py:857-867`), and the
sandboxed agent cannot create the link itself — `link(2)` across the read-only
workspace bind and the writable bind returns `EXDEV`. That same process can
plant the link in the post-audit / pre-mount window, which no audit closes. So
softening is window-widening rather than a new principal — but the widened
window is the full duration of the workspace walk, on exactly the large,
churning workspaces this task is about. Not worth it when D2 removes the walk.

Corollary: during a targeted candidate search, an `ENOENT` on an "unrelated"
file may **not** be ignored either. A failed open yields no `(st_dev, st_ino)`,
so a hidden-and-restored candidate name is indistinguishable from a benign
transient file. Completeness comes from emptying the unaccounted set *before*
the walk, never from assuming an error could not have been a candidate.

### D2 — Account hard links instead of inventorying the workspace

A hard link between writable state and protected content requires the
writable-side file to have `st_nlink > 1`, and `link(2)` cannot cross devices.
So:

1. Walk the writable remainders first, keeping the existing protected-below
   prune (`paths.py:1015-1027`). The prune is load-bearing for the accounting —
   see the warning below.
2. Group non-directory entries by `(st_dev, st_ino, file type)`, counting
   distinct names found across the **union** of all writable remainders (not per
   root — a file linked only between two state roots must not stay unaccounted
   forever).
3. An inode with exact `names_found == st_nlink` has every link accounted for
   inside writable state; no protected alias is possible for it — **but only if
   the counted names and the `st_nlink` sample coexisted**, see D2a. Either
   fewer or more names is an incomplete/unstable observation.
4. Every inode without exact equality remains an unaccounted candidate. Search
   protected coverage for those candidates alone, only on their devices,
   raising `outer_sandbox_hardlink_alias` at the first match.
5. With no unaccounted candidates the protected tree is not walked at all.

Also add the same-`st_dev` filter that the design document already describes
(`doc/tasks_closed/antigravity-read-only-bubblewrap-sandbox.md:2104-2105`) but
`_collect_protected_inodes` does not implement.

### D2a — Counted names must coexist with the `st_nlink` sample

A count accumulated over the duration of a walk is **not** a completeness proof.
Two concrete fail-open sequences, both verified on ext4:

- **Rename inflation.** Writable `state/d1/a` is hard-linked to protected
  `workspace/secret`, so `st_nlink == 2` and the walk counts one name. A
  concurrent `rename` to `state/d2/b` — which never changes `st_nlink` — lets the
  same walk count the same inode a second time under its new path.
  `names_found == 2 == st_nlink`, the inode is declared accounted, the
  protected walk is skipped, and `workspace/secret` still exists.
- **Link-count deflation.** With the same starting state, unlinking
  `workspace/secret` mid-walk drops `st_nlink` to 1, so one counted writable name
  satisfies the gate; relinking it afterwards restores the alias.

`max(st_nlink)` does not fix either: rename does not touch the link count, and
deflation is a genuine observation at the time it is taken.

The rule that must be implemented: **after the writable and peer walks complete,
re-`stat` every counted path, drop any that is missing or no longer resolves to
the same `(st_dev, st_ino, file type)`, re-read `st_nlink` from a surviving name,
and only then compare.** An inode whose count does not survive revalidation stays
an unaccounted candidate and gets the protected search. This is the same
discipline as D1: completeness comes from a consistent observation, never from a
union of observations taken at different times.

The deflation case remains bounded by the residual TOCTOU the design already
accepts — an adversary who can unlink and relink protected names concurrently
with the audit can equally plant a link in the post-audit / pre-mount window.
Revalidation closes the cheap version of it.

> **Warning — the protected-below prune is security-critical under D2.**
> Protected anchors that sit component-wise below a writable root (Git metadata
> under a state root) are path-reachable from the writable walk. Without the
> prune, a protected name and a writable-remainder name of the same inode would
> both count toward `names_found`, which can reach `st_nlink` while a real
> protected alias exists — fail open. Hard links purely among protected anchors
> staying allowed is a *consequence* of the prune, not its reason.

### D3 — Backend-declared accounting-only peer roots

Without this, D2 does not reach `claude_cli` at all.

Measured on a live host: the `claude_cli` writable state root is `~/.claude`
(`agent_collab/backends/claude_cli/sandbox.py:41-43`, writable at line 74), and
it holds 45 files with `st_nlink == 2` whose second name is
`/tmp/claude-<uid>/<project>/<session>/tasks/<id>.output`. Claude Code
hard-links its tool-result files between the state directory and its host temp
directory; they accumulate with normal use and agent-collab never cleans them
up. Those names sit outside every writable remainder, so the unaccounted set is
permanently non-empty, the protected walk never skips, and the default backend
stays exactly as broken as before.

Fix: a backend may declare **accounting-only peer roots** — trees walked solely
to count hard-link names. They are never mounted, never trusted, and are not a
new trust boundary. An inode whose names are fully accounted across writable
remainders plus peer roots needs no protected walk.

Peer roots are an availability aid only, so their walk is best-effort: any error
while walking one is non-fatal and simply leaves names uncounted, which is the
conservative direction (the candidate stays unaccounted and the protected search
runs). This matters because the Claude temp tree is itself written continuously
by other host sessions — a strict walk there would reintroduce the original
race.

If a provider later invents a new external link location, the set goes
unaccounted again and the walk returns: fail-closed, not a silent hole.

The Claude SDK worker uses the same persistent Claude state contract as the CLI
adapter, so both adapters declare the same host temp peer. The SDK worker's
session-private `CLAUDE_CODE_TMPDIR` applies only after the pre-launch audit and
does not account for persistent state links created by earlier Claude sessions.

These peer names are not themselves a threat. Host `/tmp` is over-mounted by a
session-private scratch directory (`agent_collab/sandbox/bubblewrap.py:250`,
`286-288`), so it is not visible inside the sandbox at all. They matter only for
counting.

### D4 — Diagnostics are part of the fix, not a nicety

The stable outcome code stays non-sensitive, but the daemon log must name the
path and errno that aborted an audit, and must report separately when
unaccounted candidates forced a protected walk (count and devices). Without it
an operator cannot tell which subtree to exclude, which makes the remaining
escape hatches unusable — diagnosing the original failure required reading the
sandbox source.

### D5 — Amend the documented `st_nlink` invariant

`doc/tasks_closed/antigravity-read-only-bubblewrap-sandbox.md:2108` states the
audit "never uses `st_nlink` as a security filter". D2 uses `st_nlink` to prove
a negative — all names accounted inside writable state — rather than as a
positive alias detector, which is consistent with the intent but contradicts the
sentence as written. Replace it with wording along these lines:

> The audit never treats `st_nlink` as a positive hard-link detector and never
> substitutes link count for an inode intersection check. On allowlisted local
> filesystems, after the writable remainder has been walked without following
> symlinks, the audit may use `st_nlink` only as a negative completeness check:
> for each non-directory `(st_dev, st_ino, file type)`, only an exact equality
> between the revalidated name count across the union of all writable
> remainders and declared accounting-only peer roots and `st_nlink` proves that
> every hard-link name is accounted for. Any mismatch keeps the inode
> unaccounted: the audit walks protected coverage on that device, fails closed
> on the first match, and never treats mid-walk `ENOENT`/`ESTALE` as proof of
> absence.

## Follow-ups, deliberately out of scope here

- **Operator-excluded subtrees.** Declare high-churn workspace subtrees, cover
  each with a non-writable empty mount emitted after the workspace bind, and
  prune them from the audit walk (`_walk_no_symlinks` already takes a prune
  list). Note that the mount proof rejects any writable submount below protected
  coverage (`supervisor.py:1125-1138`), so the cover must be non-writable and
  represented in the frozen plan. Shrinks the protected walk when one is still
  required, and doubles as a privacy and cost win.
- **Cross-device writable state.** Session-scoped state on a dedicated
  filesystem makes hard links to the workspace physically impossible, which the
  D2 device filter turns into an unconditional skip. Good hardening where a
  durable second device exists; not viable as the required fix, since persistent
  provider homes normally share a device with the workspace.

## Rejected alternatives

| Alternative | Why rejected |
| --- | --- |
| Treat `ENOENT`/`ESTALE` as absence on either walk | Fail-closed to fail-open via rename hide-and-restore (D1). |
| Ignore `ENOENT` for "unrelated" files during a targeted candidate search | A failed open yields no inode, so it reinstates the same bypass (D1 corollary). |
| Retry the whole audit | Only lengthens the odds against continuous writers. Acceptable only as a bounded clean re-walk complementing D2, never as the fix. |
| Per-candidate read-only masking (`--ro-bind <file> <file>`) of unaccounted files | Makes the provider's own state files read-only to the provider, and rests on locked-mount semantics for a security property. The obvious bypasses were tested against bwrap 0.6.3 and blocked (direct write, nested user+mount namespace rebind of the writable parent, remount-rw, unlink, rename), so it is not known-broken — it is simply the wrong tool now that D3 reaches the same availability outcome without touching the mount namespace. |
| Require cross-device state for `claude_cli` | Persistent provider homes normally share a device with the workspace; gating the fix on it fails availability harder than the bug. |

## Implementation notes

Primary file: `agent_collab/sandbox/paths.py`.

- Replace `_collect_protected_inodes` / `_audit_writable_hardlinks` with a
  two-phase shape: collect writable-side candidates first, then search protected
  coverage only if the unaccounted set is non-empty.
- Retain each counted path per inode, not just a running count, because D2a's
  revalidation pass has to re-`stat` them. `max(st_nlink)` is **not** a
  substitute for revalidation.
- Peer roots must be component-boundary disjoint from every writable remainder,
  or the same name could be counted twice; validate that at plan time rather
  than deduplicating during the walk.
- Keep the mount-alias half (`paths.py:877-937`) mandatory and unchanged — it
  catches bind aliases, which do not raise `st_nlink`.
- Keep the existing protected-below prune on the writable walk. Under D2 it is
  a security dependency, not an optimization (see the warning in D2a).
- The shared `visited` budget now counts a different mix of trees; keep counting
  only what is actually walked, and keep `outer_sandbox_alias_audit_exceeded`
  fail-closed.

Wiring, which ripples further than it looks:

- `BackendSandboxSpec` grows an accounting-only peer-root field and both Claude
  adapters declare the shared Claude Code temp tree; `ResolvedSandboxPlan`
  (`agent_collab/sandbox/plan.py`) is a frozen dataclass that must carry the
  resolved peer roots so the pre-launch re-audit in
  `SandboxSupervisor._launch` uses the same set as plan resolution.
- `audit_aliases` grows a peer-roots parameter defaulting to `()`, and both call
  sites (`plan.py:298` and `supervisor.py:339`) pass it. The default keeps the
  existing hermetic tests calling the old signature.
- Peer roots are **not** state roots. Do not resolve them through
  `resolve_state_root`: its writable-path ownership and `0o022` checks
  (`paths.py:204-217`) would reject a valid peer tree under a sticky
  world-writable `/tmp` parent. Resolve them leniently — a missing tree is fine
  and simply accounts nothing — and never mount them or treat them as a trust
  boundary.

## Verification

Tests to add under `tests/sandbox/`:

- Churn under a protected tree while no unaccounted candidates exist: the audit
  completes, and the protected walk is proven not to run (instrument the walk
  and fail if entered).
- Hard links wholly inside writable state are accounted and do not trigger a
  protected walk.
- A file linked between writable state and a declared peer root is accounted; the
  same file additionally linked into the workspace is unaccounted and rejected.
- Peer-root walk failure is non-fatal and only widens the unaccounted set.
- Writable state on a different device skips the protected walk.
- Hide-and-restore during a walk still fails closed.
- **D2a rename inflation:** a writable name of an inode that also has a
  protected name is renamed within the writable tree mid-walk so it is counted
  twice; revalidation must leave the inode unaccounted and the alias must still
  be rejected.
- **D2a link-count deflation:** the protected name is unlinked mid-walk and
  relinked afterwards; the inode must not be accounted on the strength of the
  deflated count.
- **The prune is load-bearing:** a protected anchor below a writable root with a
  hard link into the writable remainder is rejected, and a test that disables the
  prune demonstrates the fail-open it prevents.
- A peer root overlapping a writable remainder is rejected at plan resolution.
- Diagnostics (D4): an aborted audit logs path and errno, and a protected walk
  forced by unaccounted candidates is logged distinctly from an alias match.

Must keep failing closed:

- `test_hardlink_from_protected_tree_into_writable_remainder_is_rejected`
  (`tests/sandbox/test_paths.py:278-300`) — `outer_sandbox_hardlink_alias`.
- `test_hardlink_between_two_pruned_protected_anchors_is_allowed`
  (`test_paths.py:302-324`) — must still pass.
- `test_nested_writable_bind_below_workspace_is_rejected` (`test_paths.py:325`)
  — `outer_sandbox_mount_alias`.
- `test_relative_directory_reopen_wraps_filesystem_races`
  (`test_paths.py:238-256`) — unchanged, because D1 keeps every errno strict.
- Unsupported filesystem, budget exhaustion, and the launch-time re-audit
  fixtures.

Live verification must include a `claude_cli` session on a host that already has
tool-result hard links in `~/.claude` and a concurrently written gitignored
subtree in the workspace. Remember that the daemon runs the installed package:
`./agent_collab.sh install` and restart before verifying.

## Resolved questions

- Claude Code 2.1.219's shipped binary was inspected before implementation. It
  uses non-empty `CLAUDE_CODE_TMPDIR`, otherwise Node's OS temp directory
  (`TMPDIR`/`TMP`/`TEMP` on Linux), and appends `claude-<uid>`.
  `CLAUDE_CONFIG_DIR` does not participate, and a literal `~` in a temp
  environment value remains an unexpanded relative path under the provider
  cwd, matching Node rather than shell expansion. Existing tool-result hard
  links on the verification host matched the derived tree.
- The session-private scratch anchor does not join the writable union. It is
  fresh and empty when the pre-launch audit runs, so it contributes no names.
- No comparable external hard-link factory was found in the shipped Grok,
  Gemini, or Codex state trees on the verification host.

## Implementation record

- Writable remainders are walked as one union with protected anchors pruned.
  Counted names are reopened without following symlinks, checked against their
  original inode identity, and deduplicated by parent directory identity plus
  basename before an exact `names_found == st_nlink` completeness decision.
  Mount backing sides are canonicalized within and across writable roots so
  bind views cannot inflate that count; ordinary protected descendants still
  use the dedicated load-bearing lexical prune.
- Peer roots are lexically normalized, rejected when they overlap writable
  state, pruned against every declared writable/protected operation by
  mount-table backing identity, canonicalized against earlier peer mount sides,
  and walked best-effort. An individual traversal error leaves only that entry
  or subtree uncounted; successfully observed peer names remain subject to the
  full revalidation pass. Claude CLI and SDK share the shipped runtime's temp
  derivation.
- Residual candidates search only protected coverage on matching devices.
  Forced searches and hard-link matches have distinct daemon diagnostics;
  traversal aborts log the failing path and errno without changing the stable
  client-facing outcome.
- Verification passed Ruff, 1,448 hermetic unit tests, and 10 Linux Bubblewrap
  acceptance tests. The installed daemon and provider readiness checks passed.

## Review record

| Round | Reviewers | Outcome |
| --- | --- | --- |
| 1 | Grok 4.5 (high), Gemini 3.1 Pro (high) | Split. Gemini endorsed `ENOENT`-as-absence and could construct no attack; Grok constructed the rename hide-and-restore bypass and proposed the `st_nlink` accounting gate instead. |
| 2 | Same | Converged on dropping the softening and on the accounting gate. Disagreed on severity: Gemini called it categorical, Grok called it window-widening. Adjudicated to window-widening — the adversary must already be able to write the daemon-private state root — with the conclusion unchanged. Gemini's proposal to ignore `ENOENT` on "unrelated" files during a targeted search was rejected as unsound by both the judge and Grok. |
| 3 | Same | Both accepted the shape after the `~/.claude` measurement showed the accounting gate alone never fires for `claude_cli`, and both landed on accounting-only peer roots as the companion. Grok's stated reason for rejecting per-file masking — that the sandbox RW-binds host `/tmp` — is factually wrong (`bubblewrap.py:286-288` binds a session-private scratch directory over `/tmp`); masking is rejected on the grounds recorded above instead. |
| 4 | Same, reviewing this document | Both found the D2 monotone-safety claim false. Grok supplied the sharper counterexample (rename inflation, which leaves `st_nlink` untouched) and identified that the document gave the wrong reason for the protected-below prune, hiding a fail-open if an implementer dropped it. Both counterexamples were reproduced on ext4 before the document was corrected; D2a and the prune warning are the result. Grok additionally caught that peer roots must not be resolved as state roots. Its turn was recorded as `provider_terminal_failure` after emitting a complete review — the outcome is a stop-reason artifact, not lost work. |
| 5 | Same, reviewing the implementation | Split. Grok reproduced a fail-open in which lexical `..` components let a peer root reopen protected or writable storage under a second path and inflate the completeness count. Gemini's nested-writable-mount and tilde-expansion findings were rejected: the unchanged mount-alias gate rejects the former before hard-link search, and the shipped CLI treats literal `~` as a relative path. Peer roots were normalized, and revalidation was strengthened to count unique parent-dirent identities. |
| 6 | Same, after the fix | Converged with no high- or medium-severity findings. Grok again emitted a complete review before its CLI labeled `end_turn` as `provider_terminal_failure`; the full attributed answer was retained and adjudicated. |
| 7 | Same, reviewing the closed-task diff | Inconclusive pair, with one actionable finding. Gemini returned an invalid one-line payload and was not counted. Grok found that one unrelated peer traversal race discarded the entire peer root's successful counts, re-forcing the protected walk under the exact multi-session workload D3 targets. Peer traversal was changed to skip errors per entry/subtree while retaining successfully opened names for strict revalidation. |
| 8 | Same, final-scope review | Inconclusive. Gemini reported no qualifying finding; Grok exceeded the 900-second local turn deadline without an answer. |
| 9 | Same, repeated final-scope review | Split. Gemini's high-severity writable-workspace premise was rejected because resolved workspace coverage is always read-only and any overlapping writable declaration fails before audit. Grok reproduced a mount-multipath peer fail-open and identified that Claude SDK shared the persistent state contract without declaring the corresponding host temp peer. Both medium findings were confirmed and fixed; Grok's complete answer was retained despite the known `end_turn` wrapper artifact. |
| 10 | Same, after the mount/SDK fixes | Split. Gemini repeated the rejected tilde-expansion premise; literal `~` remains relative in the shipped Node runtime, so Python `expanduser()` would audit the wrong tree. Grok found the dual mount-multipath case: a writable file bind below a peer was counted as a second hard-link name. Peer pruning was generalized to every declared writable/protected backing identity and exact link-count equality was made explicit in the docs. |
| 11 | Same, after the generalized multipath fix | Converged with no high- or medium-severity findings. Both reviewers accepted exact link-count equality, declared-operation mount pruning (including nested file binds), Claude CLI/SDK peer wiring, and the audited literal-tilde behavior. Grok again emitted a complete clean review before the CLI labeled `end_turn` as `provider_terminal_failure`; the full answer was retained and adjudicated. |
| 12 | Independent subagent after convergence | Found one medium fail-open missed by both provider reviewers: a file bind of a legitimate peer hard-link below the same or another peer root could still inflate the revalidated name count without increasing `st_nlink`. Peer mount sides are now canonicalized in deterministic order, duplicate backing views are pruned, and the reproduced self-peer file-bind case is covered hermetically. |
| 13 | Same subagent, reviewing the peer canonicalization fix | Found the dual medium fail-open in writable collection: a nested file bind of a writable hard-link name could inflate the count without increasing `st_nlink`. The same deterministic mount-side canonicalization now applies within/across writable roots, non-lexical protected aliases are pruned, and the ordinary protected-below prune remains the explicit lexical security mechanism. |
| 14 | Same subagent, after writable/peer canonicalization | Converged with no high- or medium-severity findings. The reviewer confirmed duplicate mount views are excluded within/across writable and peer roots, genuine hard-link names remain countable, and the dedicated lexical protected-below prune remains intact and load-bearing. |
| 15 | Grok 4.5 (high), Gemini 3.1 Pro (high), final frozen-diff review | Converged with no high- or medium-severity findings. Both reviewers independently accepted writable-first discovery, exact descriptor-based revalidation, peer best-effort handling, mount-view canonicalization, Claude CLI/SDK peer wiring, and the regression coverage. Grok's complete clean answer was retained despite the known `end_turn` wrapper artifact. |
