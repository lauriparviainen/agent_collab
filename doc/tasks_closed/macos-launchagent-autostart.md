# macOS LaunchAgent daemon autostart

**Status:** Closed

**Created:** 2026-08-11

**Completed:** 2026-08-12

**Issue:** [#58](https://github.com/lauriparviainen/agent_collab/issues/58)

## Context

`agent-collab daemon autostart enable` currently supports only Linux systemd
user services. The public command rejects every non-Linux platform before
calling a systemd-only implementation, so macOS users can start the detached
daemon manually but cannot opt into the same managed, supervised login-time
lifecycle.

The original systemd task deliberately left macOS LaunchAgent support as the
second service-manager backend. That second backend now justifies extracting a
small service-manager boundary: the user-facing commands, readiness checks,
manual-to-managed transition, durable-install validation, runtime ownership,
and rollback guarantees should be shared, while definition rendering and
native lifecycle commands remain platform-specific.

Apple's launchd contract requires the process it launches to stay in the
foreground rather than daemonize. A per-user LaunchAgent is the appropriate
scope for a process that runs while the user is logged in. The implementation
must therefore use the existing internal foreground daemon path, generalized
to identify launchd ownership, rather than wrapping `daemon start`.

Relevant platform references:

- [Apple: Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- [Apple: Service Management](https://developer.apple.com/documentation/servicemanagement/)
- [Apple: Updating helper executables from earlier versions of macOS](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)
- [Apple: legacy service status through SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice/statusforlegacyplist(at:))

## Goals

- Make the existing `agent-collab daemon autostart enable`, `status`, and
  `disable` commands work on macOS without platform-specific user syntax.
- Register a user-owned LaunchAgent that starts at graphical login and owns one
  foreground daemon process.
- Preserve the systemd behavior and user experience on Linux.
- Route ordinary `daemon start`, `stop`, `restart`, and `status` through the
  active platform service manager whenever a managed definition exists or live
  runtime state proves managed ownership.
- Keep enable idempotent and safely migrate an already-running detached daemon
  into launchd ownership, including rollback if managed startup fails.
- Preserve private daemon logs, configuration, tokens, transcripts, and session
  state across enable, disable, restart, upgrade, and uninstall operations.
- Keep generated definitions free of daemon tokens, provider secrets, and the
  caller's general environment.
- Add macOS-native verification so launchd behavior is not inferred solely from
  Linux-hosted mocks.

## Non-goals

- Boot-before-login or system-wide `/Library/LaunchDaemons` installation.
- Root privileges, multi-user registration, or management of another user's
  LaunchAgent.
- An app bundle, signed helper, `SMAppService`, GUI preferences, or App Store
  distribution. This CLI installation continues to use the documented legacy
  per-user LaunchAgent location.
- A new native or PyObjC bridge solely to query Background Items approval.
  launchctl's persisted disabled override is observable without another
  runtime dependency; separate System Settings authorization is reported from
  native command failures with actionable remediation rather than proactively
  queried in this iteration.
- Socket activation or launchd-managed listening sockets.
- Windows service support.
- Copying shell startup configuration or provider API keys into the generated
  plist.

## Proposed user experience

After the existing durable installation:

```bash
./agent_collab.sh install
agent-collab daemon autostart enable
agent-collab daemon autostart status
```

The same commands continue to select systemd on Linux and select launchd on
macOS. Unsupported systems receive an error naming the supported platforms.
The existing `--host`, `--port`, and `--workdir` options retain their meaning.

Status output becomes service-manager-neutral and identifies the selected
manager and definition path, for example:

```text
✓ Daemon autostart enabled and healthy
  version             0.x.y
  manager             launchd
  definition          ~/Library/LaunchAgents/io.github.lauriparviainen.agent-collab.plist
  installed           true
  enabled             true
  active              true
  healthy             true
  definition_current  true
  detail              healthy
```

The CLI may abbreviate the real OS-account home as `~` for display only; path
resolution never reads caller-controlled `HOME`.

Disabling remains non-destructive:

```bash
agent-collab daemon autostart disable
```

It stops and unregisters only the managed daemon definition. It leaves the
durable command, user config, bearer token, logs, transcripts, and session
index intact.

If the fixed per-user registration belongs to another `AGENT_COLLAB_HOME`,
enable and disable refuse by default. Recovery or deliberate reassignment is an
explicit non-interactive operation:

```bash
agent-collab daemon autostart enable --takeover
agent-collab daemon autostart disable --takeover
```

The command names the displaced home/interpreter and never treats token
readability as consent.

## Design

### Platform-neutral autostart boundary

Refactor `agent_collab/daemon_autostart.py` into the public, platform-neutral
facade while keeping native behavior in isolated modules, for example:

```text
agent_collab/daemon_autostart.py
agent_collab/daemon_autostart_systemd.py
agent_collab/daemon_autostart_launchd.py
```

The facade owns or exposes:

- platform selection (`linux` -> systemd, `darwin` -> launchd);
- the shared `AutostartStatus` contract;
- durable-interpreter validation;
- authenticated readiness probing;
- shared manual-daemon snapshot and restoration helpers;
- generic managed-owner discovery and lifecycle dispatch.

Represent native discovery with one structured managed-service identity that
records manager, attribution source, loaded state, native enabled/disabled
override state, interpreter/arguments, effective `AGENT_COLLAB_HOME`, durable
environment root, host, port, and default workdir when recoverable. Omission of
`AGENT_COLLAB_HOME` is a legacy-definition case and canonicalizes only to the
pwd-account default home; every newly rendered definition always carries the
caller's absolute `AgentCollabHome.resolve()` result, even when no override was
explicitly set. Omission never aliases an explicit alternate home. All
lifecycle, autostart, install, and uninstall paths consume this same identity
instead of re-implementing narrower ownership tests.

The backend modules own definition paths and rendering, native command
execution, loaded/enabled queries, enable/disable transactions, and rollback.
Do not put service-management logic into `agent_collab.sh`; Python remains the
portable and hermetically testable layer.

Replace systemd-specific public names used by the CLI with manager-neutral
operations. `AutostartStatus` should include `manager` and rename `unit_path`
to `definition_path`; CLI status should print `manager` and `definition` rather
than calling every backend artifact a unit. Linux-specific helpers may remain
private to their backend tests.

### Managed runtime ownership

Generalize `run_managed_daemon` to accept a validated native-manager identifier
from the closed set `systemd` or `launchd`. The generated systemd unit invokes
it with `systemd`; the generated LaunchAgent invokes it with `launchd`.
Detached execution remains a separate supervisor path. Runtime state records the actual owner in
`manager`, and cleanup removes PID/state only when both PID and manager still
match.

The foreground process continues to handle `SIGTERM` by unwinding the server
and cleaning owned state. The raw supervisor must refuse to signal any live
service-manager-owned daemon directly, not just a systemd-owned daemon. Its
error should name the recorded manager and direct the caller through the
ordinary daemon lifecycle command.

Manager routing follows three pieces of evidence:

1. A managed definition installed for the current platform reserves lifecycle
   ownership even when the service is stopped.
2. Live daemon state whose manager matches the platform-native backend
   (`systemd` on Linux or `launchd` on Darwin) preserves routing if the
   definition was removed externally.
3. A loaded native service whose in-memory program/argument identity matches
   the internal agent-collab foreground command preserves routing while the
   definition and live runtime state are both absent. A loaded label that
   cannot be attributed is a collision, not ownership evidence.

Classify that evidence at two strengths. `agent-collab-owned` means the fixed
marker, label/unit, and internal command shape prove this project created the
global registration. `current-home-owned` additionally requires the parsed
effective home and durable interpreter/environment identity to equal the
calling installation. Canonicalize the effective home everywhere—rendering,
parsing, ownership, runtime state, and comparison—with the existing
`AgentCollabHome.resolve()` contract (`expanduser().resolve()`, including symlink
resolution) when a value is present. A legacy definition with the variable
omitted canonicalizes to `<pwd-account-home>/.agent-collab`, matching the login
environment rather than the inspecting CLI's `HOME`. Keep non-symlink-resolving
normalization only for the durable venv interpreter path, whose final symlink
must remain inside the environment.
Missing or ambiguous home/interpreter evidence can never be promoted to
current-home ownership.

Only current-home ownership authorizes native-manager mutation by ordinary
`daemon` commands, installer quiesce/restore, or uninstall teardown. A different
home may still start, stop, restart, and inspect its own verified detached daemon
on a non-conflicting endpoint; it never routes those operations into the other
home's native manager. A foreign canonical definition or attributable loaded
job reserves its parsed listen endpoint even while stopped/dormant, so the
server-equivalent IPv4/IPv6/wildcard conflict check must reject a detached start
that would prevent the registered job from starting later. Recovery-only
non-loadable artifacts do not reserve an endpoint. With no current-home
detached process, stop is an idempotent no-op and start may create one after the
normal bind/ownership probe.
This preserves the documented isolated-home workflow while preventing it from
hijacking the global registration. Status reports both scopes, does not probe
the global job with the caller's unrelated bearer token, and cannot claim
authenticated health for it.

Cross-home registration mutation requires an explicit, non-interactive
`--takeover` flag on `autostart enable` or `autostart disable`; the ordinary
commands refuse after naming the owning home and interpreter without exposing
its token. The flag is never inferred from a readable same-user token and the
native smoke never supplies it. Stop only through an attributable native
manager, never by raw PID.

For `autostart enable --takeover`, snapshot the displaced definition,
loaded/disabled state, and recovery artifacts. When its private token is
readable, use it only to authenticate rollback readiness. When the owning
home/token or durable prior definition is missing, takeover remains available
but is explicitly irreversible:
first establish a disabled/non-loadable barrier and retain exact prior bytes in
an owner-marked recovery slot when they exist (otherwise retain only the parsed
in-memory identity for diagnostics); do not promise to restore the broken job
on failure. Remove the displaced recovery only after the new current-home service
is PID-bound healthy. `autostart disable --takeover` similarly provides the
supported escape hatch for a marked cross-home definition whose home/token has
vanished: establish the login-safety barrier, tear down only the attributable
native target, and preserve unrelated home data. Neither token value is
rendered, logged, or copied into a definition.

Stale managed state does not permanently lock out a later detached start, but
a live daemon with an unknown or cross-platform manager is never routed into
the current platform backend or signalled as though it were detached. Report
the recorded manager, explain the mismatch, and provide explicit manual
recovery instructions for stopping the attributable PID and cleaning stale
state. Hermetic tests cover both cross-platform directions.

Serialize every mutating daemon lifecycle operation with a dedicated global
lifecycle-transaction lock. Only top-level public entrypoints acquire it:
`daemon start/stop/restart`, autostart enable/disable, and install/uninstall
hold it across occupancy discovery, native stop/start actions, install-time
durable venv/link mutation, readiness, and rollback. Every nested success-path
or failure-path action—detached stop/start, native bootout/bootstrap/kickstart,
definition removal, uninstall teardown, readiness, and restore—uses an
explicitly lock-held internal helper; public lock-acquiring functions must
never call one another inside a transaction. Detached `start_daemon` acquires
the lifecycle lock only when called as a top-level operation, so it cannot
enter between a managed preflight and readiness check. Keep this separate from
the existing short-lived daemon start/state lock acquired by
`run_managed_daemon`; otherwise a CLI transaction could deadlock the child it
is waiting to become ready.
Define one lock order (lifecycle transaction before state/start lock), and make
the lock-held helper contract visible in names and tests.

Implement the lifecycle transaction as a cross-process file lock with
fail-fast, non-blocking acquisition, matching the repository's existing daemon
start-lock convention. A competing command does not wait through a potentially
long install; it exits without mutation and names the operation already in
progress, then succeeds when explicitly retried after that operation releases
the lock. “Serialized” and “cannot enter” below mean rejected while busy, not
queued.

Because launchd labels/LaunchAgent paths and systemd user-unit paths are shared
by all `AGENT_COLLAB_HOME` values for one OS user, native registration
additionally uses one fixed per-user, manager-identity-scoped lock outside
`AGENT_COLLAB_HOME`. Acquire it after the home-scoped lifecycle lock and before
inspecting or mutating the label/unit, definition, enabled/disabled state, or
recovery slots; hold it through readiness and rollback. The macOS smoke wrapper
does not hold this non-reentrant lock while spawning CLI children. Instead, each
child lifecycle transaction acquires it and repeats authoritative ownership
preflight; the wrapper's initial read-only check is advisory, and its
identity-bound trap preserves any definition changed by a concurrent command.
Define and test this lock order so two homes cannot overwrite or delete one
another's registration.

Derive the lock root from the real OS account home returned by
`pwd.getpwuid(os.getuid()).pw_dir`, never caller-controlled `HOME`, XDG, or
`AGENT_COLLAB_HOME`. Use fixed paths:

```text
Darwin: <account-home>/Library/Application Support/io.github.lauriparviainen.agent-collab/launchd-registration.lock
Linux:  <account-home>/.local/state/io.github.lauriparviainen.agent-collab/systemd-registration.lock
```

These are persistent application-state directories, not reclaimable cache or
temporary storage. Create/validate the app-specific lock directory as an
owner-only (`0700`) real directory and the lock as an owner-only (`0600`)
regular file owned by the current uid; reject symlink, non-directory,
wrong-owner, or permissive-existing app-specific components. This gives every
home one identical location without a world-writable `/tmp` trust boundary or
cache-cleaner inode replacement.

Once created, the registration lock inode is permanent application state:
normal release, disable, cleanup, and uninstall close the descriptor but never
unlink the lock file or its directory. Only the diagnostic owner sidecar is
removed. This avoids an application-created unlink/recreate split between
successive waiters.

After acquiring the lock, atomically write a private (`0600`) owner sidecar
containing the operation name, holder PID, and start time. A failed acquirer
reads that record only for diagnostics and reports the named operation when it
is well formed; a missing, racing, or malformed record falls back to a generic
busy message. The holder removes only its matching record immediately before
unlock. Lock state—not the sidecar—remains authoritative, so a crashed holder's
stale metadata never blocks later work and is replaced by the next acquirer.

### LaunchAgent identity and location

Use a stable label and matching filename:

```text
io.github.lauriparviainen.agent-collab
<account-home>/Library/LaunchAgents/io.github.lauriparviainen.agent-collab.plist
```

Reserve two deterministic non-loadable recovery slots beside it:

```text
<account-home>/Library/LaunchAgents/io.github.lauriparviainen.agent-collab.agent-collab-recovery
<account-home>/Library/LaunchAgents/io.github.lauriparviainen.agent-collab.agent-collab-recovery-fallback
```

Neither name ends in `.plist`; both retain the definition's agent-collab owner
marker and have mode `0600`. They are fixed OS-user-global paths, independent
of `HOME`, XDG variables, and `AGENT_COLLAB_HOME`: derive `<account-home>` from
the same `pwd.getpwuid(os.getuid()).pw_dir` as the registration lock, never
`Path.home()` or `expanduser("~")`. They do not overlap daemon state, token, lock, PID, or
log paths. Quarantine inspects both slots with `lstat`, parses every regular
owner-marked file's home identity, and may replace/remove a slot only when it is
current-home-owned or was snapshotted by the command's explicit `--takeover`.
Prefer the primary authorized/empty slot; use the fallback when the primary is
unowned or contains an unauthorized foreign identity. Before moving the current
definition, remove stale artifacts only from authorized slots, preserving every
unowned or unauthorized foreign collision. Then
atomically rename the owner-marked canonical plist directly into the selected
slot in the same account-home `Library/LaunchAgents` directory and fsync the directory.
The single rename is the login-safety commit: there is no copy-then-unlink
window in which a crash can leave the canonical `RunAtLoad` plist loadable. If
both slots are blocked by unowned/unauthorized entries or the atomic rename
cannot be made,
removing the marked canonical plist remains the last-resort login-safety action
and the loss of recoverable definition bytes is a critical appended error;
never retain a loadable plist merely because recovery collided.

Native/status discovery checks both recovery paths and reports
`installed=false` plus a quarantined/recovery detail when the canonical plist
is absent. If both owner-marked slots somehow survive an interrupted older
implementation, report both without guessing which to restore. Parse every
marked recovery definition into the same effective-home/interpreter identity;
a recovery-only residual reserves cross-home ownership exactly like a canonical
definition. Ordinary enable/disable cannot reclaim another home's slot.
Current-home enable, or explicit `--takeover`, retains applicable recovery
artifacts until the replacement is healthy and intended enabled state is
established, then removes only the snapshotted identities it was authorized to
replace. If the process dies after health but before that cleanup, a
current-home canonical definition takes precedence for later ordinary lifecycle
operations: a foreign recovery artifact is reported and preserved, but does not
block disabling or uninstalling the current-home service and is not silently
deleted. Once the canonical path is absent, that foreign recovery becomes the
sole registration residual and again requires explicit `--takeover` to replace
or remove. Disable and uninstall remove current-home or explicitly authorized
marked recovery artifacts only after daemon teardown; an unowned collision is
always fatal and preserved.

Give systemd the same two-slot, same-directory atomic recovery contract at the
authoritative unit path. Keep the manager-registration lock rooted in the real
pwd account home, but do not use the caller's environment or that lock location
to infer where the systemd user manager reads unit files.

While holding the manager-registration lock, query the user manager with
`systemctl --user show-environment`. For a fresh definition, derive the manager
configuration root from the manager's absolute `XDG_CONFIG_HOME`, otherwise
from its absolute `HOME` plus `/.config`, and otherwise from the real pwd
account home plus `/.config` when both variables are absent. A malformed value
or a failed/ambiguous manager query is not evidence that a unit is absent:
status reports discovery as indeterminate, and every mutating or destructive
operation fails closed before changing a definition, process, venv, or command
link. Caller `HOME`, XDG variables, and `AGENT_COLLAB_HOME` never select this
path. The fresh definition is:

```text
<manager-config-root>/systemd/user/agent-collab.service
```

If systemd already knows `agent-collab.service`, query its `FragmentPath`
through a structured wrapper. A nonempty, owner-marked fragment is the
authoritative definition for status, lifecycle, install, uninstall, and
explicit autostart enable and disable even when it is outside the currently
derived manager root; update or remove it in place and never create or migrate
to a second unit automatically. An unowned fragment is a fatal collision. A
failed, empty, conflicting, or otherwise ambiguous fragment query makes
discovery indeterminate and invokes the same fail-closed rule rather than
inferring absence.

The two recovery files use fixed non-loadable suffixes, for example
`agent-collab.service.agent-collab-recovery` and
`agent-collab.service.agent-collab-recovery-fallback`, beside the authoritative
`FragmentPath` or, for a fresh unit, beside the manager-derived path. Systemd
disable has one fixed order: disable and prove removal of every enable symlink;
stop the attributable unit and prove its captured PIDs gone; atomically rename
the authoritative definition into an authorized same-directory recovery slot;
run `daemon-reload` after the definition leaves its canonical path; then remove
the authorized recovery artifacts. No stale enable symlink may point to a
loadable unit. Status, explicit enable, disable, and uninstall discover and
clean only marked systemd recovery artifacts exactly as above. They never
search caller-derived alternative directories or split a definition from its
recovery slots.

The definition belongs to the current user's graphical domain:

```text
gui/<uid>
gui/<uid>/io.github.lauriparviainen.agent-collab
```

Use modern `launchctl bootstrap`, `bootout`, `enable`, `kickstart`, `print`, and
`print-disabled` operations; do not use the deprecated `load`/`unload`
interface. A missing graphical login domain, a background-item denial, or
unavailable `launchctl` must produce an actionable error rather than falling
back to a detached process.

Inject one fixed, value-free XML ownership comment into the `plistlib` output;
do not add undocumented keys to the launchd dictionary. Ownership validation
requires that marker plus the expected `Label` and the internal daemon module
shape in `ProgramArguments`. Derive the recorded interpreter from
`ProgramArguments[0]`, so arbitrary paths never need to be embedded in an XML
comment. Refuse to replace or delete a plist at the target path unless all
ownership checks pass. Write the plist atomically with owner-only permissions,
and preserve exact prior bytes for rollback.

### Generated plist

Generate XML with the standard library's `plistlib` so argument and environment
values receive property-list escaping rather than shell escaping. The
definition contains:

- `Label` with the stable label;
- `ProgramArguments` containing the absolute durable venv interpreter,
  `-m agent_collab.cli daemon run`, the explicit launchd manager identity, and
  the selected host/port/default-workdir arguments;
- `EnvironmentVariables` containing only a PATH snapshot and an explicit,
  `AgentCollabHome.resolve()`-canonicalized `AGENT_COLLAB_HOME` on every render,
  including when the caller used its current default;
- `RunAtLoad = true`;
- `KeepAlive = { SuccessfulExit = false }`, matching systemd's existing
  restart-on-failure behavior without restarting a successfully exited job;
- a modest `ThrottleInterval` to bound crash-loop churn;
- `ExitTimeOut` aligned with the existing bounded graceful-stop contract.

Omit `ProcessType`, leaving launchd's standard/default classification. The
daemon synchronously serves user-waiting CLI, TUI, HTTP, and MCP requests and
owns in-memory sessions; it is not deferrable background maintenance.

The interpreter path uses the existing `_absolute_path` contract: expand the
user and make the path absolute without resolving the final venv symlink. Use
the same non-resolving normalization when rendering, validating ownership, and
computing `definition_current`. Resolving `venv/bin/python` to its base
framework or Homebrew interpreter can leave the durable environment and make
`agent_collab` unimportable.

Set `ExitTimeOut = 10` seconds, matching the existing systemd
`TimeoutStopSec`. Every post-bootout observation budget is at least that value
plus a small scheduling grace (initially 2 seconds). A process that exits
within the launchd timeout is a successful shutdown; only a PID that remains
attributably live past the full observation budget makes replacement or
restart fail.

Capture the old managed PID before bootout. Shutdown success means that exact
attributable process is no longer live within the observation budget. Then run
normal daemon-status cleanup to reap matching dead PID/state files before
bootstrapping a replacement. A forced launchd kill cannot run the managed
daemon's `finally`, so residual state for a dead old PID is expected cleanup,
not a second fatal shutdown condition. If state changes to a different live
process or manager while waiting, stop the transaction rather than deleting or
competing with it.

`ThrottleInterval` provides launchd-native restart throttling, not systemd's
burst-stop policy. Do not claim parity with systemd's
`StartLimitIntervalSec`/`StartLimitBurst`: a repeatedly failing launchd job may
continue retrying at the throttled interval until it is booted out or fixed.
Status/detail and documentation should make a crash-loop diagnosis actionable.

Do not invoke a shell and do not serialize the full caller environment. The
definition omits the daemon token, provider keys, credential paths, and a
working directory. The daemon's existing default-workdir argument remains the
only session fallback.

Keep the existing private daemon log contract. If launchd continues to rely on
the foreground process's internal log redirection, document that deliberate
choice and preserve mode `0600`; if native `StandardOutPath` and
`StandardErrorPath` are used instead, pre-create the directory and files
privately and prove their modes in a native test. Do not split normal logs
between launchd and agent-collab locations.

### Enable transaction

`autostart enable` on macOS performs these steps:

1. Resolve the current-user LaunchAgent path and GUI-domain service target.
2. Validate that the current interpreter is a durable installation and that
   `launchctl` plus the GUI domain are usable.
3. Ensure `<account-home>/Library/LaunchAgents` exists before the atomic write, creating it
   owner-only when absent and refusing a non-directory. Preserve an existing
   directory and its permissions.
4. Render and structurally validate the complete plist before changing the
   running daemon. On macOS, also validate the candidate with `plutil -lint`
   before installation.
5. Read any existing definition and query the service target independently.
   Refuse an unmarked definition; otherwise retain its exact bytes. Snapshot
   prior loaded state, in-memory arguments, live runtime state, and the prior
   launchctl disabled override regardless of whether the plist exists. Refuse
   a loaded service only when none of the three ownership evidence types proves
   that agent-collab owns it. Classify a different-home owned service as an
   explicit `--takeover` candidate, include recovery-only identity, and make
   ordinary enable plus every unauthorized native-manager caller refuse it
   without mutation. A missing prior token selects the irreversible,
   quarantine-first takeover branch rather than making recovery impossible.
6. Build a daemon-occupancy snapshot that combines the three-source managed-
   service identity with the existing verified detached-daemon probe. Record
   every attributable live PID plus its manager, host, port, and default
   workdir. Probe the requested listen endpoint with the same address-family
   resolution and socket options as `asyncio.start_server`: on POSIX this
   includes `SO_REUSEADDR`, excludes `SO_REUSEPORT`, and applies
   `IPV6_V6ONLY=1` to IPv6 sockets exactly where CPython does. “Exclusive” means
   a concurrent live listener still makes the matching server bind fail, while
   a harmless TIME_WAIT socket does not. `EADDRINUSE` without attributable
   daemon evidence records an unknown listener. Release the probe immediately
   before native start, while retaining the lifecycle lock; an external non-
   agent process can still race, so post-start PID-bound readiness remains
   authoritative.
7. Before changing the definition, apply the fail-closed half of the ownership
   arbitration required by `daemon start` and `restart`: reject an unloaded
   same-manager orphan with PID-specific recovery and refuse to disturb or
   compete with an unattributable listener. These refusal paths must leave the
   prior plist bytes, loaded state, and disabled override unchanged.
8. Atomically install a changed definition, then gracefully stop a verified
   detached daemon before every bootstrap, reload, or kickstart and restore it if the
   transaction fails. An unchanged, loaded, live, healthy job is left running.
   An unchanged, loaded, live-but-unhealthy job enters the one controlled
   recovery branch: boot it out by target, prove the captured PID gone, reap
   matching dead runtime state, then select it for exactly one bootstrap after
   step 9 clears any persisted override and require step 10's final PID-bound
   health. If any other attributable loaded target must be
   loaded/reloaded, boot it out by target even when the prior plist was missing;
   bootstrap must never race an already-loaded label. After bootout, wait for
   every recorded managed PID to exit, then reap matching dead runtime state
   before replacing/bootstrapping; inability to prove bounded shutdown of the
   old process is fatal.
9. Clear a persisted launchctl disabled override because the user explicitly
   ran `autostart enable`, independently of whether native bootstrap is needed.
   If an unchanged attributable target remains loaded and live because it was
   already healthy, do not bootstrap it again; clear the override and re-prove
   stable PID-bound health. If an unchanged target is loaded-dormant, clear the
   override and kickstart it without `-k`; there is no successful dormant branch
   because explicit enable must finish healthy. Bootstrap the plist when the
   target is unloaded, including after step 8's controlled unhealthy recovery.
   A failure after clearing the override enters the login-safety rollback below.
   Treat successful clearing as the explicit enable-intent commit for abrupt
   process/power-loss semantics; an observed command failure still must
   establish a proven disabled or non-loadable residual before returning. This
   does not and cannot clear a distinct denial in macOS System Settings.
10. Wait for an attributable live PID reported for the launchd target and bind
    authenticated application readiness to that exact process. Add a dedicated
    protected `GET /ready` response with serving process PID and validated
    runtime manager, and require its PID to equal launchd's current PID and its
    manager to equal `launchd`; re-sample launchctl around the probe so a
    crash/restart cannot satisfy the comparison with stale data. A token-
    authenticated response from a detached, orphaned, or otherwise pre-existing
    process is not evidence that launchd successfully started the candidate.

Rollback is phase-aware. If the forward path cleared a snapshotted disabled
override, rollback's first registration action is to re-disable the label and
prove the override before restoring any loadable canonical bytes. If that
cannot be proven, atomically move the current marked canonical definition to a
non-loadable recovery slot (or remove it under the defined last-resort rule),
store the exact prior marked bytes only at a non-loadable recovery slot, and
return a critical login-safety residual; re-disable failure is never merely an
appended warning. The command may return only after proving either the prior
disabled override or an absent/non-loadable canonical definition.

Failure to prove that re-disable is terminal for registration restoration: once
the quarantine/removal branch is entered, later rollback still tears down and
reaps any unproven candidate, but it must never restore bytes to the canonical
plist, bootstrap/kickstart a prior generation, or otherwise recreate loadable
registration. Exact prior bytes remain only in an authorized non-loadable
recovery slot with explicit recovery diagnostics. All ordinary restorable-state
rules below are conditional on either no snapshotted disabled override or a
successfully restored and proven disabled override.

After that required login-safety action, tear down every generation that the
forward path attempted to bootstrap or kickstart but did not prove healthy:
boot out the service target, prove all captured candidate PIDs gone, and reap
matching runtime state before restoring definitions/native state or starting a
prior generation. This also applies to the one-shot bootstrap after
live-unhealthy recovery and to a fresh/unloaded bootstrap; no rollback branch
may race restoration or re-bootstrap against a still-loaded failed candidate.
It does not apply when the forward path kept the snapshotted original healthy
PID loaded and changed only its override, because no candidate generation was
created in that branch.

Otherwise, rollback first branches on whether the prior registration is
restorable. Current-home state may restore exact prior bytes/native state or use
the same-home missing-plist detached fallback below. Cross-home `--takeover` is
restorable only when both exact prior definition bytes and readable prior-home
credentials exist. Missing-home/token or cross-home missing-definition takeover
is explicitly irreversible: never restore bytes to the canonical path, never
re-enable or re-bootstrap the displaced job, and never start it as a detached
process. Boot out and reap only the candidate generation, remove its marked
canonical definition, retain any displaced bytes non-loadably (or retain the
snapshotted in-memory identity only for diagnostics when no bytes existed), and
report the exact residual.

Within the restorable branch, any failure after a changed definition is written
must restore the exact prior bytes (or remove the newly created file) and prior
launchctl state, even when takeover has not begun; this branch is unreachable
after the terminal re-disable-failure quarantine above. A failure before the
pre-existing target is booted out must also leave that target running/loaded.
After takeover begins, boot out only the candidate generation. Re-bootstrap
restored prior bytes only if the service had a live process before the
transaction. A previously unloaded definition remains unloaded, and a
previously loaded-but-not-running `RunAtLoad` job also remains unloaded rather
than being started as a rollback side effect; report this deliberate inability
to reproduce launchd's loaded-dormant flag while preserving stopped intent.
When the prior plist was already missing but a managed daemon was live, restore
it as detached only when it is proven current-home-owned (or is a verified
current-home detached daemon stopped by arbitration) and its current-home
credentials remain readable; cross-home takeover never maps its arguments into
the caller's detached supervisor. This is the existing same-home manual-to-
managed recovery guarantee, not a generic native-job fallback.
A prior loaded-but-dormant job with no plist has no durable definition to
restore; report that limitation explicitly without fabricating one. This
includes a previously disabled label when no plist existed. Preserve the
primary error and append ordinary rollback failures, while elevating inability
to prove the login-safety residual as specified above. The transaction must not
leave two daemons competing for the port.

Rollback never temporarily clears a snapshotted disabled override. Distinguish
an override-only repeated enable from a takeover that booted out the old job.
If the forward path left the original live PID loaded and only cleared its
override, re-disable and prove the override while leaving that exact PID loaded;
never boot it out merely because the post-clear health probe failed. If the
forward path kickstarted a prior loaded-dormant target but did not prove the
candidate healthy, first restore and prove any snapshotted disabled override,
then boot out that target, prove every candidate PID gone, and reap matching
state before restoring exact prior bytes and leaving the target unloaded; this
branch never inherits the original-live-PID exception and cannot leave a crash-
loop or live-plus-disabled job. If re-disable was not provable, use the terminal
non-loadable quarantine branch instead. If the forward path had already booted
out a prior live-plus-disabled job, restore and prove the disabled override,
then restore the exact prior bytes without bootstrap, report that rollback
could not safely reproduce the unusual live-plus-disabled in-memory state, and
leave it stopped; re-disable failure again keeps those bytes non-loadable.
Prior unloaded and loaded-but-not-running definitions likewise restore bytes
and disabled state without bootstrap, only after the override is proven as
ordered above. This is a deliberate fail-closed observed-failure rollback; the
override-clear step is the documented commit for an abrupt process or power
loss during an explicit enable. A prior live definition with no disabled
override may still be re-bootstrapped and proven healthy.

Repeated enable behavior mirrors systemd:

- unchanged + loaded + healthy: no restart;
- unchanged + loaded + healthy + persisted-disabled: clear only the disabled
  override, do not bootstrap/restart, and verify the same job remains healthy;
- unchanged + loaded-dormant: clear the override if needed, kickstart without
  `-k`, and wait for PID-bound health; failure first restores/proves any prior
  disabled intent, then boots out/reaps the unproven candidate and restores
  prior stopped intent, while re-disable failure leaves only a non-loadable
  quarantine after candidate cleanup;
- unchanged + unloaded: bootstrap and wait for health;
- unchanged + loaded + live-but-unhealthy: perform one controlled
  bootout/bootstrap recovery and report failure if health still does not
  recover;
- changed + loaded: controlled bootout/bootstrap and readiness check;
- changed + unloaded: replace, bootstrap, and readiness check.

### Start, stop, restart, and disable

Ordinary lifecycle commands delegate to launchd only when the native evidence
is `current-home-owned`. Generic agent-collab attribution for another home is
status/diagnostic evidence, never native mutation authority; commands from that
home manage only their own verified detached daemon as defined above. Within
the current-home native branch, behavior depends on definition state:

- `daemon start`: bootstrap an unloaded installed definition, or start a
  loaded but dormant current-home-attributable job with `launchctl kickstart` (without
  `-k`), after applying the occupancy arbitration below in either case. Then
  require both a current-home-attributable live PID for the launchd target and
  authenticated health at its effective endpoint. A response from a verified
  detached daemon cannot make kickstart/bootstrap succeed. A healthy already-
  running job is a no-op. If the in-memory job can run but its plist is
  missing, it may be kickstarted for the current login session, but the command
  must return a prominent success-with-warning detail that `installed=false`
  and future-login autostart is absent, directing the user to explicit
  `autostart enable`; do not silently recreate registration or claim autostart
  is repaired. If a current-home-attributable managed PID is already live but
  fails authenticated PID-bound
  health, do not silently restart it under `start`: leave the live process
  untouched and select guidance from the snapshotted persisted override before
  emitting the generic health error. A disabled override directs the user to
  `autostart enable` first, because `daemon restart` must refuse that state; an
  enabled label directs the user to `daemon restart` or diagnostics. Never
  issue restart-only guidance that the same snapshot makes ineligible;
- `daemon stop`: boot out the current-home-attributable job by service target and retain an
  existing plist so login-time registration remains enabled. This also works
  when the plist was removed externally. The occupancy snapshot must also
  find and gracefully stop a verified detached daemon even when an owned plist
  caused lifecycle routing to launchd; if both a managed target and a verified
  detached daemon are present, stop and prove exit for both rather than
  silently leaving a competitor. Capture every current-home-attributable live PID, wait
  through the full shutdown budget, and reap matching dead state before
  reporting success. If launchctl says the target is unloaded while a same-
  manager PID remains live, fail with explicit orphan recovery instead of
  treating “service not found” as stopped. Refuse to signal a process whose
  ownership cannot be proved;
- `daemon restart`: require a current-home-owned plist before any mutation. If it is
  missing, fail closed and direct the user to `autostart enable`, leaving the
  currently running/loaded job untouched. With an installed definition,
  gracefully boot out a loaded job, reap dead old runtime state, bootstrap the
  definition again, and wait for health. If the service is already unloaded,
  skip bootout and bootstrap directly; “service not found” from a preflight-
  proven unloaded target is not a restart failure;
- `daemon status`: combine definition ownership, launchctl loaded state,
  attributable live runtime state, and authenticated application health.

Resolve the health endpoint from what will actually run. For an already live
process, prefer attributable runtime state and fall back to the loaded job's
in-memory arguments. For a loaded dormant job that `kickstart` will execute,
use its in-memory arguments before any on-disk definition, which may have been
changed without reload. For an unloaded job that bootstrap will create, use
the installed definition. Parse and validate `--host`/`--port` from the same
argument vector used for evidence #3; do not silently probe default
`127.0.0.1:8765` when the effective job carries custom values. If no
trustworthy endpoint exists, report status as indeterminate and refuse a
health-dependent mutation.

For native managed status and readiness, the authenticated application probe
returns the server process PID and manager. `healthy=true` requires that pair
to match the native manager's current attributable live PID, not merely that a
token-bearing process answers on the expected endpoint. Re-sample native PID
state after the HTTP response and reject a generation change. Detached status
likewise matches the response PID to verified detached runtime state. This is
the final race check after the preflight bind probe and lifecycle lock; it also
protects against unrelated external processes that cannot honor the lock.

The probe values come from immutable in-process server context, not pid/state
files: PID is `os.getpid()`, and the validated runtime manager is passed into
the server at process creation. `run_managed_daemon` passes its closed-set
`systemd`/`launchd` manager directly; the detached `serve` subprocess receives
the literal `detached` through a hidden validated argument. Removing or
rewriting runtime state therefore cannot change what the serving process
reports. State files remain discovery evidence, not the readiness response's
source of truth.

Preserve public/debug compatibility: `run_server(..., manager="detached")`
defaults omission to `detached`, and public `agent-collab serve` invokes that
same unmanaged default without exposing a manager flag. The detached supervisor
still passes `detached` explicitly, while only the hidden managed `daemon run`
path may pass `systemd` or `launchd`. Validate the same closed manager set before
app construction so existing direct `run_server()` callers do not gain a
required argument or accidentally report a native owner.

Keep both published wire contracts unchanged: unauthenticated `GET /health`
continues returning the existing `HealthModel` and authenticated
`GET /sessions` continues returning `SessionListModel`. Add authenticated
`GET /ready` with a new typed `DaemonReadinessModel`, at minimum
`{pid: int, manager: detached|systemd|launchd, version: str}`. The new route
uses the same bearer-token requirement as `/sessions`; native readiness and
status consume it rather than changing public liveness or session schemas.
Register it in the central `ROUTES` table with `client_method=None` and add the
exact `("GET", "/ready")` pair to `SERVER_ONLY_ROUTES`; it is an internal
service-management probe, not a new `AgentCollabClient` method. Preserve the
bidirectional route/client registry invariants.

Update the explicit API-build surfaces in `agent_collab/project_build.py`: add
the readiness handler to `_summary()`'s fixed name map, add
`DaemonReadinessModel` to `_MODELS`, and update any pinned field/enum metadata
used by schema generation. Regenerate and check in the affected
`doc/daemon_api_doc/` outputs, including `openapi.json` and `http-api.md`, so
both `./agent_collab_dev.sh build` and `build --check` pass without missing
schema references or generated-doc drift.

Before any start/restart action that may create a managed process, including
both bootstrap of an unloaded definition and kickstart of a loaded dormant
job, arbitrate other process ownership. Gracefully stop a verified detached
agent-collab daemon and restore it if the managed start fails. If runtime state
claims the same native manager but launchctl proves the target unloaded, treat
it as an orphaned managed process: fail with PID-specific manual recovery
rather than raw-signalling it or starting a competitor. Refuse to disturb an
unattributable listener.

Manual lifecycle commands use one phase-aware start transaction whether the
prior job is unloaded, loaded-dormant, or live. Snapshot loaded/live state,
disabled override, any verified detached daemon stopped by arbitration, and
the definition before mutation. Use the snapshotted `print-disabled` state—not
launchctl stderr—to determine whether the operation can proceed without
changing persisted login intent. A loaded-dormant job with no disabled override
uses kickstart directly. When the persisted override is disabled, refuse every
`start` or `restart` before mutating or stopping anything and direct the user to
explicit `autostart enable`, except that `start` remains a successful no-op if
the existing managed job is already live and healthy. Thus manual lifecycle
cannot create a new live-plus-disabled job or a crash window by temporarily
enabling a persistently disabled label. `stop` remains available.

Every failure before initial PID-bound health preserves the primary error and
performs ordered cleanup:
boot out any candidate generation that was not proven healthy (preventing an
indefinite throttled crash-loop), wait/reap its PID, restore a detached daemon
stopped by arbitration, restore a prior live managed job when restart had
booted it out, then restore the exact prior registration state. A prior unloaded
job returns to unloaded; a prior loaded-dormant job may remain unloaded when
restoring loaded state would trigger `RunAtLoad`, with explicit stopped-intent
diagnostics. Cleanup failures are appended.

No manual lifecycle path clears a prior disabled override, so the former
post-health re-disable/quarantine branch is unnecessary. If launchctl state
changes despite a command that was not meant to alter it, treat that as a fatal
state-integrity failure, establish the safest login-disabled residual before
returning, and report observed state plus exact recovery. `restart` does not
boot out a live disabled job when restart would require clearing the override.
No manual lifecycle exit may silently leave an enabled `RunAtLoad` definition
when prior intent was disabled.

Do not use `launchctl kickstart -k` as the normal restart path if it bypasses
the daemon's graceful `SIGTERM` cleanup. Prefer bootout, a bounded wait for the
attributed old PID to disappear, cleanup of matching dead runtime state, then
bootstrap.

`autostart disable` uses the combined daemon-occupancy snapshot and first makes
the next-login intent fail closed. On launchd, persist and prove the disabled
override, then atomically rename an owned canonical plist to a non-loadable
recovery slot before bootout. On systemd, disable and prove removal of every
enable symlink before stopping the unit. Only after that login-safety barrier
does the command boot out/stop an attributable loaded job, prove every captured
managed PID is gone, and reap matching dead managed state. Launchd then removes
the already-quarantined definition/recovery artifacts. Systemd atomically
renames the authoritative definition into its authorized same-directory
recovery slot, runs `daemon-reload`, and removes the authorized recovery
artifacts. A crash after the barrier may leave the current process running, but
cannot re-enable it at the next login. The operation is idempotent.
A marked launchd recovery artifact is part of managed registration state:
disable removes every current-home or explicitly takeover-authorized marked
primary/fallback artifact after job teardown. A foreign recovery beside a
current-home canonical definition is preserved and reported without blocking
current-home disable; when it is the sole residual, ordinary disable refuses
and `--takeover` is required. Any unowned collision remains fatal.
A separately verified detached daemon is outside registration ownership: leave
it running and report that fact in the result detail. If the plist and live
state were removed externally but the in-memory job remains attributable,
disable must still boot out the service target. “Service not found” is
ignorable only when the job is unloaded and no attributable same-manager PID
remains live; permission, domain, orphaned-process, unknown-ownership, and
malformed-definition failures remain fatal.

### Status semantics and diagnostics

Keep the existing booleans with explicit cross-platform meaning:

- `installed`: an agent-collab-owned primary definition exists at the backend's
  authoritative path: the canonical LaunchAgent plist on Darwin or systemd's
  discovered `FragmentPath` (the manager-derived fresh path only when no unit
  is known) on Linux;
- `enabled`: the owned definition has persistent login-time registration. The
  launchd backend requires the canonical plist and no disabled persisted
  override; the systemd backend requires the persistent enabled state reported
  by `systemctl --user is-enabled`, not merely a loaded or runtime-only unit;
- `active`: the native service manager reports an attributable running process
  (loaded plus live PID/equivalent), without requiring agent-collab runtime
  state to exist;
- `healthy`: active plus an authenticated daemon probe whose reported process
  PID and manager match stable native-manager evidence;
- `definition_current`: the recorded interpreter exists and matches the
  current durable installation.

The launchd backend may isolate parsing of `launchctl print` and
`print-disabled` output behind focused helpers, but exit status and exact
service targeting should be preferred over parsing human-readable fields.
This `enabled` value does not claim to expose the separate authorization state
shown in macOS Background Items; without a native bridge, bootstrap denial is
the reliable signal and must carry System Settings remediation. Inconsistent
states must be visible in `detail`, including:

- plist installed but job not loaded;
- job loaded but dormant, or loaded with an unattributable process identity;
- daemon live but authenticated health failing;
- definition disabled in launchctl's persisted override state;
- bootstrap denied because the user disabled the item in macOS Background
  Items;
- missing recorded interpreter after an install location changed;
- missing GUI domain, commonly from an SSH-only/headless context;
- externally removed or unowned plist.

Diagnostics for a user-denied background item should point to the relevant
System Settings location without claiming launchctl can override that choice.
Only explicit `autostart enable` may clear launchctl's separate persisted
disabled override; approval denied in System Settings remains user-controlled.

### Install, upgrade, and uninstall

Replace the installer's `systemd` boolean snapshot with the combined daemon-
occupancy snapshot used by lifecycle and uninstall: the three-source managed-
service identity plus the existing verified detached-daemon probe. Perform
this preflight before mutating the durable venv. If the global registration is
agent-collab-owned but not current-home-owned, abort with the owning home and
interpreter before quiescing or changing a venv/link; this applies whether the
two homes share an interpreter or use separate durable installations. If any
attributable native job lacks an owned definition—whether it is loaded and
dormant, loaded and live, or represented by a same-manager live PID whose
target is unloaded—abort
before installation with state-specific recovery guidance. Likewise abort on
unattributable occupancy. For a clean live loaded job, compare the in-memory
interpreter/ProgramArguments with the owned on-disk definition using the same
strict parser as ownership evidence #3. Parse both into a typed identity:
non-symlink-resolving absolute interpreter and workdir paths, fixed module and
manager values, validated host, integer port, and absence of duplicate,
unknown, or missing flags. Compare typed fields and argument boundaries, not
raw `launchctl print`/plist rendering. If they differ, abort before mutation
and direct the user to reconcile explicitly with `daemon restart` or
`autostart enable`; install must not silently activate a changed-but-not-
reloaded plist or claim to restore arguments it cannot reconstruct after
bootout.

The typed parser shares the runtime compatibility rule for legacy definitions:
on Linux only, omitted `--manager` on the hidden `daemon run` command
canonicalizes to `systemd`. Omit/omit and omit/explicit-systemd identities may
therefore compare equal during an upgrade. Darwin and other platforms reject
the omission, and omission can never canonicalize to `launchd` or `detached`.
Before venv mutation, quiesce every verified running detached daemon and every
attributable loaded managed job. Boot out a clean managed job even when it is
dormant or sampled between KeepAlive respawns, so it cannot start against a
partially updated environment. Preserve live/health state and native loaded
state independently. Count running sessions first and print the existing
interruption warning before quiescing, explicitly stating that the daemon and
sessions remain unavailable for the complete package/config migration rather
than only the final restart.

On launchd, refuse before mutation when the owned job is live while its
persisted override is disabled. Safely reconstructing that unusual state after
quiescing would require a crash-vulnerable temporary enable; direct the user to
explicitly enable autostart before upgrading, then disable it again afterward
if desired. Systemd does not need this refusal because a disabled unit can be
started without enabling it.

Establish a native auto-load barrier for every owned login-enabled definition,
even when its job is already unloaded/stopped, and retain it for the full
mutation window. On launchd, snapshot the persisted disabled override, then
temporarily `launchctl disable` the label before any required bootout; on
systemd, snapshot enabled state and temporarily disable the unit before any
required stop. Prove the target unloaded and PID-free before mutation. This
barrier is independent of the CLI file lock and prevents a new GUI/user-
manager login from loading the still-installed definition against a partial
venv. If the process dies mid-transaction, leaving autostart temporarily
disabled is the fail-safe state and status/recovery guidance must explain how
to restore it.

Treat quiescing, installation, and restoration as one failure-aware operation.
Hold the lifecycle transaction lock continuously across preflight, quiescing,
the complete `install_user_command` venv and command-link mutation, config
migration that can affect daemon startup, restoration, and failure recovery.
No concurrent daemon lifecycle command may observe the intact definition and
start it against a partially replaced environment.
After a successful install, restore a detached daemon only if it was running,
and restore a managed definition only if it had a live process before
quiescing, using the same prior manager and effective arguments. A definition
that was installed but unloaded remains unloaded. A loaded-but-not-running
LaunchAgent is also left unloaded after quiescing because bootstrapping its
`RunAtLoad` definition would start a daemon as an install side effect; report
that preserved stopped intent and direct the user to `daemon start` if desired.
Require PID-bound health when the prior daemon was live/healthy. If a mutation
or restoration phase fails—including venv creation/package installation,
command-link mutation, config migration, or PID-bound restore health—preserve
that primary error and make a best-effort restoration of those same prior
running states; a prior loaded-but-not-running LaunchAgent remains unloaded for
the same `RunAtLoad` reason. Append any restore failure with actionable
recovery. Once native state and daemon health are successfully restored, do
not tear them down or repeat restoration because later backend-readiness checks
or UI/result rendering warn or fail; those diagnostics preserve the successful
daemon outcome and follow their existing fatal/non-fatal contract. Do not leave
old in-memory code running against a newly replaced environment or recreate an
intentionally removed login item as an install side effect.

Successful detached restoration always launches through the post-install
durable venv interpreter, preserving only the snapshotted host, port, and
default workdir—not the prior `argv[0]`. Managed restoration uses the owned
definition whose typed identity was proven equal to the prior in-memory job.
Restore native enabled/disabled state without ever temporarily enabling a
previously disabled launchd label. The preflight refusal above removes the only
live launchd case that would require that unsafe sequence; non-running launchd
jobs restore their prior override without starting. A prior live systemd unit
can be started while remaining disabled. Apply the same fail-closed rule during
best-effort failure restoration, choosing the durable interpreter only when it
is usable and otherwise reporting the exact manual recovery needed. After a
managed residual is safe, restore any separately verified detached daemon
stopped by the install transaction and prove its PID-bound health. Do not treat
prior PID health as successful restoration, do not proceed to post-install
success diagnostics after a restoration failure, and never leave a
login-enabled definition where the snapshot was disabled.

Apply this quiesce/restore envelope to an existing systemd-owned daemon as well
as launchd so the neutral installer has one safety contract. This intentionally
moves the Linux managed stop from after package mutation to before it, while
preserving the observable rules that a previously running service is healthy
after a successful upgrade and a stopped service stays stopped.

Reinstalling/upgrading a running launchd-owned daemon with a valid definition
routes its pre-install stop and post-install restoration through launchd so the
durable interpreter update takes effect without KeepAlive racing the install
or starting a detached competitor. A stopped managed service remains stopped;
install retains the existing rule that it never starts a stopped daemon or
enables autostart implicitly.

Uninstall uses the combined occupancy snapshot, including current-home-owned
definition, attributable live state, attributable loaded-job evidence, and the
verified detached-daemon probe. An agent-collab-owned registration for another
home as the canonical/active or sole recovery identity aborts uninstall before
native teardown, command-link deletion, or venv removal, since either durable
artifact may still serve that registration; the diagnostic directs the user to
uninstall from the owning home or explicitly take it over first. A foreign
non-loadable recovery beside a current-home canonical identity is preserved and
reported but does not block current-home teardown. For current-home ownership
it disables/stops the native manager and gracefully stops any verified detached
daemon before removing the durable venv or command link, including when plist
and runtime state were removed externally, because an in-memory job that
references a deleted interpreter is broken. Unknown
occupancy and same-manager orphans fail closed before interpreter removal.
Teardown errors remain fatal and leave the venv intact for recovery. User
config and runtime data remain preserved.

The top-level uninstall transaction holds the lifecycle lock through managed
definition removal, detached/native shutdown, command-link deletion, and venv
removal. It calls only lock-held disable/stop/teardown helpers; it never invokes
the public lock-acquiring lifecycle API from inside `_teardown_daemon`.

### Compatibility and migration

- Linux-generated unit content and lifecycle behavior remain compatible.
  Fresh definitions follow the systemd user manager's configuration root, not
  the invoking shell. An existing owner-marked `FragmentPath`, including one
  under a prior manager `HOME`/`XDG_CONFIG_HOME`, remains authoritative and is
  updated in place by explicit enable; no operation automatically migrates it
  or creates a duplicate. Manager-environment or fragment discovery failure is
  indeterminate and fails closed before mutation.
- Existing detached macOS daemons transition through the same verified
  manual-to-managed path used by systemd.
- The workaround LaunchAgents users may have created themselves are unowned;
  the command refuses to overwrite them. Documentation should explain how to
  unload and remove a conflicting custom definition before enabling managed
  autostart.
- The hidden `daemon run` option gains a tightly validated manager argument,
  but omission means `systemd` only on Linux. Existing installed systemd units
  invoke `daemon run` without that argument and must survive a package upgrade
  and restart before the unit is refreshed. On Darwin and every other
  platform, omission is an actionable error; every new generated definition
  passes either `--manager systemd` or `--manager launchd` explicitly. This
  hidden foreground entrypoint is only for native service managers: ordinary
  detached `daemon start` and rollback restoration continue to use the
  detached supervisor/`serve` path, record `manager=detached`, and never invoke
  `daemon run`.
- Unsupported platforms retain detached daemon commands and receive an
  actionable error only for autostart operations.
- macOS support is capability-based: Darwin must provide modern launchctl
  bootstrap/bootout service commands and a current-user GUI domain. CI's
  `macos-latest` image is the continuously tested baseline; older systems that
  lack those capabilities fail with an explicit unsupported-runtime error.

## Implementation plan

1. Introduce the neutral status/manager selection layer and move current
   systemd-native operations behind it. Implement the manager-environment fresh
   path, authoritative owner-marked `FragmentPath`, same-directory recovery,
   fail-closed discovery, and explicit effective-home definition identity
   specified above while preserving all unrelated Linux behavior.
2. Generalize managed foreground runtime state and raw-supervisor refusal from
   the literal `systemd` value to a validated managed-owner set.
3. Add LaunchAgent path resolution, plist rendering/ownership checks, native
   command wrappers, status queries, and enable/rollback/disable transactions.
4. Route CLI lifecycle and status output through the neutral manager API,
   add `--takeover` only to autostart enable/disable, and preserve the
   repository's progress/result markers and version reporting. Reject the flag
   on status and ordinary daemon lifecycle commands.
5. Generalize install-time daemon probing, managed restart, and uninstall
   teardown.
6. Integrate `/ready` with `project_build.py`'s handler summaries/model registry
   and regenerate the daemon API artifacts.
7. Update tests, macOS CI/native validation, README, runtime layout, daemon
   architecture, development notes, and changelog.

## Verification plan

### Hermetic tests on every platform

- Platform selection chooses systemd for Linux, launchd for Darwin, and rejects
  unsupported systems without attempting native commands.
- Plist rendering has stable structure, uses foreground mode, carries the
  launchd manager identity, preserves argument boundaries for spaces and
  special characters, and includes only PATH plus the always-explicit resolved
  effective `AGENT_COLLAB_HOME`.
- Ownership detection refuses unmanaged or malformed plists and never deletes
  them.
- Enable tests cover fresh registration, unchanged healthy re-enable, stopped
  managed service, changed definition, persisted-disabled state, and a running
  detached daemon, plus refusal when the chosen label is occupied by an
  unattributable loaded job. Enable also refuses an unloaded same-manager
  orphan and an unattributable listener before bootstrap, and readiness cannot
  pass on a token-authenticated detached/orphan response unless launchd reports
  the same serving PID in stable before/after samples.
- Repeated-enable tests give an unchanged loaded healthy job a persisted
  disabled override, prove enable clears only that override without bootstrap,
  and verify the same PID remains healthy; injected failure restores the prior
  override. Inject re-disable failure after the forward path cleared it and
  prove rollback leaves the canonical definition absent/non-loadable, preserves
  exact prior marked bytes in recovery, reports a critical residual, and never
  returns with a clear override plus loadable plist.
- Repeated-enable state-machine tests give an unchanged job each distinct
  loaded-dormant, unloaded, live-healthy, and live-unhealthy state. Loaded-
  dormant uses `kickstart` without `-k`, never bootstrap, and succeeds only
  after stable PID-bound readiness. Inject kickstart, crash-loop, and readiness
  failures and prove rollback first restores/proves prior disabled intent, then
  boots out/reaps every unproven candidate, restores exact bytes, and leaves the
  target unloaded with no live-plus-disabled residual. Live-unhealthy performs
  exactly one ordered
  bootout/PID-proof/bootstrap recovery; a second health failure cleans the
  candidate and returns failure rather than looping. Unloaded uses bootstrap,
  while live-healthy preserves its PID. A loaded-dormant case with a verified
  detached daemon proves the detached PID is gracefully stopped before
  kickstart and restored after candidate cleanup when kickstart/readiness fails.
- The repeated-enable matrix crosses live-unhealthy with both enabled and
  persisted-disabled intent. The disabled case proves the exact forward order:
  boot out/reap the old unhealthy PID, clear and prove the override, bootstrap
  once, then perform PID-bound readiness. On injected bootstrap/readiness
  failure, rollback first restores/proves the disabled override, then boots
  out/reaps the failed candidate, restores exact bytes, and leaves the job
  stopped; it never takes the prior-live re-bootstrap branch and never leaves a
  live-plus-disabled residual. Equivalent bootstrap-failure cases for prior
  unloaded and enabled live-unhealthy state prove every failed candidate is
  removed before native/definition restoration or any prior-generation start.
- Candidate-bearing re-disable-failure tests cover loaded-dormant kickstart,
  unloaded bootstrap, and live-unhealthy bootstrap after the forward path
  cleared a snapshotted override. Each proves rollback quarantines/removes the
  canonical plist first, then boots out/reaps every unproven candidate, keeps
  exact prior bytes only in an authorized non-loadable recovery slot, never
  restores canonical bytes or starts any generation, and restores a stopped
  detached daemon only after that safest managed residual is established.
- Occupancy tests use the server-equivalent bind probe to reject an unknown
  endpoint listener before definition mutation, cover IPv4/IPv6 and wildcard
  conflict cases supported by the server (including `SO_REUSEADDR`, TIME_WAIT,
  and `IPV6_V6ONLY` parity), and still rely on PID-bound readiness for an
  external-listener race after the probe is released.
- Cross-home ownership tests vary effective home, durable interpreter, and
  definition/runtime evidence independently. Status reports generic ownership
  without probing with the wrong token. Native-manager mutation, install, and
  uninstall refuse before mutation, while a second home's detached lifecycle
  works on a free endpoint and cannot route to or signal the global job. A
  stopped/dormant foreign canonical definition and an attributable loaded job
  both reserve their parsed endpoints; same-port and wildcard-overlap detached
  starts refuse before spawn, while a genuinely disjoint port succeeds.
  Ordinary enable/disable refuse cross-home state; explicit `--takeover` is
  required non-interactively. With readable prior-home credentials an injected
  failure restores/authenticates the displaced job; with missing home/token it
  boots out only the candidate, preserves the old definition non-loadably,
  never re-bootstraps or starts it detached, and leaves no login crash-loop.
  Missing or ambiguous identity never becomes current-home ownership, and token
  values never appear in output.
- Home-canonicalization tests set `AGENT_COLLAB_HOME` through a symlink and prove
  render, parse, runtime paths, status, lifecycle, install, and uninstall all use
  the same `AgentCollabHome.resolve()` result, while the durable interpreter
  still preserves its non-resolved venv symlink path. Separate cases vary
  `HOME` with `AGENT_COLLAB_HOME` omitted: new definitions embed the caller's
  resolved effective path, the launched daemon uses that exact value, and
  legacy omitted definitions resolve to the pwd-account default rather than the
  inspecting process's altered `HOME`.
- Fresh-registration tests begin without `<account-home>/Library/LaunchAgents`, prove it is
  created before the atomic write, and reject a non-directory parent.
- Rollback tests cover plist write/bootstrap/readiness failures, restoration of
  an older loaded definition, restoration of a detached daemon, restoration of
  a prior disabled override (with and without a prior plist), and combined
  primary/recovery error reporting.
- Disabled-override rollback tests distinguish an override-only repeated enable
  (restore disable while leaving the untouched original PID loaded) from a
  takeover that booted out the old job (restore exact bytes/disable without
  bootstrap and report deliberate fail-closed loss of prior live state). Prior
  unloaded and loaded-but-not-running definitions also restore without
  bootstrap.
- Pre-takeover refusal tests place an orphan or unknown listener beside a
  changed candidate and prove enable leaves the exact prior plist bytes,
  loaded state, and disabled override untouched.
- Rollback state tests restore/re-bootstrap a previously live loaded definition
  but restore bytes without bootstrap when the prior definition was
  intentionally unloaded. A prior loaded-but-not-running `RunAtLoad` job also
  remains unloaded after rollback, with explicit stopped-intent diagnostics.
- Missing-plist enable tests snapshot loaded state independently of file
  presence, boot out an attributable target before bootstrap, never disturb it
  for a pre-takeover failure, and restore a prior live daemon as detached only
  for verified current-home rollback with readable current-home credentials.
  Cross-home `--takeover` with no prior definition is classified irreversible
  even when its token is readable; an injected failure dismantles only the
  candidate, never fabricates/re-bootstraps a definition or starts the foreign
  identity detached, and reports that the displaced live state could not be
  reconstructed.
- Disable tests cover loaded, unloaded, missing, externally removed, and
  unowned definitions while preserving logs/config/session data. Stop and
  disable diverge intentionally when an owned but unloaded plist coexists with
  a verified detached daemon: `daemon stop` stops it, while `autostart disable`
  removes only managed registration and reports that the detached daemon is
  still running. Mixed managed-plus-detached `daemon stop` proves exit for
  both, while unknown occupancy remains untouched and fails closed.
- Lifecycle routing covers launchd-owned start/stop/restart/status, live state
  after external definition removal, stale managed state, and refusal to raw-
  signal any live managed process. Restart/change tests prove the old PID and
  owned state are gone before a replacement bootstrap begins; loaded dormant
  start uses non-destructive `kickstart`. Cross-platform live manager tests
  prove the CLI neither dispatches to the wrong native backend nor raw-signals
  the process and prints deterministic recovery guidance.
- Missing-definition lifecycle tests cover an attributable loaded/running job:
  status diagnoses it; stop and disable boot it out by service target; start
  may use the already-loaded in-memory job for the current session, returns a
  prominent success-with-warning with `installed=false`, and does not recreate
  registration;
  restart and install-time restart fail before mutation and preserve the live
  job; explicit enable may transactionally create and load a new owned plist.
- Evidence-#3 tests remove both plist and runtime state while retaining an
  attributable loaded job; enable adopts it transactionally, and disable plus
  uninstall boot it out before registration or interpreter removal.
- Endpoint tests recover custom host/port from installed or in-memory
  ProgramArguments when runtime state is absent, prefer loaded in-memory
  arguments over a changed-but-not-reloaded plist, use disk arguments for a
  future unloaded bootstrap, and refuse an untrustworthy fallback.
- Readiness response tests remove or corrupt runtime pid/state files and prove
  the live server still reports its own `os.getpid()` and immutable startup
  manager; detached startup can complete its initial probe before the parent
  writes runtime state. Existing direct `run_server()` callers and public
  `agent-collab serve` omission report `detached`; the supervisor passes it
  explicitly, managed run accepts only `systemd|launchd`, and invalid values
  fail before app construction.
- Readiness-route contract tests cover typed `DaemonReadinessModel`
  serialization, bearer-token rejection, the closed manager set, and
  PID/version fields while proving the existing open `HealthModel` `/health`
  and authenticated `SessionListModel` `/sessions` responses are unchanged.
  Route-registry tests pin `GET /ready` in both `ROUTES` and
  `SERVER_ONLY_ROUTES` with no client method and retain bidirectional client
  coverage.
- Project-build tests include the readiness handler summary and
  `DaemonReadinessModel` schema, regenerate `doc/daemon_api_doc/openapi.json`
  plus `http-api.md`, and make both build and `build --check` pass from a clean
  tree without a handler-map `KeyError` or generated drift.
- Installer tests cover launchd-managed restart after upgrade, stopped-service
  preservation, disable-before-venv-removal, externally removed plist with live
  launchd ownership, detached-daemon stop/restore across upgrade and stop
  before uninstall, managed bootout before venv mutation, absence of KeepAlive
  respawn during mutation, active-session warning before the longer quiesced
  install window, and teardown failure safety.
- Installer definition-drift tests give a live job in-memory arguments that
  differ from its owned plist and prove install aborts before quiescing or venv
  mutation with explicit reconciliation guidance. Equivalent launchctl/plist
  renderings parse to the same typed identity, while interpreter/workdir path,
  host, port, manager, argument-boundary, and duplicate/unknown-flag changes do
  not.
- Installer quiesce tests sample a loaded managed job between crash-loop
  respawns (no current PID), prove it is booted out before venv mutation, and
  leave it unloaded afterward so `RunAtLoad` does not start a previously non-
  running daemon. The owned plist and login-time registration remain intact.
- Installer failure tests fail after detached and managed daemons are quiesced,
  inject failures separately in package install, command-link mutation, config
  migration, and PID-bound restoration readiness, preserve each primary error,
  attempt to restore every prior running mode, and append deterministic
  recovery detail if restoration also fails.
- Post-restore diagnostic tests prove backend-readiness warnings and injected
  UI/result-rendering errors do not stop or re-restore a daemon whose native
  state and PID-bound health were already restored.
- Installer lock tests pause during durable venv and command-link mutation and
  during config migration, and prove concurrent `daemon start`, autostart, and
  uninstall mutations fail fast without mutation while the transaction is
  held, then succeed on explicit retry after restoration or rollback releases
  it.
- Installer native-barrier tests recreate the GUI/user-manager domain during
  each mutation phase and prove the temporarily disabled launchd/systemd
  definition cannot auto-load. Success and injected failures restore the exact
  prior enabled/disabled intent without a temporary-enable window. Initial
  states include live, loaded-dormant, enabled-but-unloaded/stopped, and
  already-disabled definitions; live-plus-disabled launchd refuses before any
  mutation, while a disabled systemd unit is restored without enabling it.
- Installer preflight tests prove live-plus-disabled launchd state refuses
  before quiescing or durable-environment mutation with actionable enable/
  upgrade/disable guidance. A separately stopped detached daemon is restored
  after any later managed failure, and backend/UI success diagnostics do not
  run. Bootout failure remains a critical live-PID residual even when
  registration quarantine succeeds.
- Detached upgrade tests start from a different interpreter and prove
  successful restoration uses the post-install durable venv interpreter while
  preserving snapshotted host, port, and workdir.
- Installer evidence-#3 tests remove plist and state while leaving the native
  job active/loaded and prove install aborts before changing the durable venv.
  A separate case leaves the in-memory job loaded-dormant with no PID and
  proves the same pre-mutation abort without bootout or registration creation.
- Installer orphan tests leave an attributable launchd PID live while its
  target is unloaded and prove install aborts before changing the durable venv;
  unattributable occupancy has the same fail-closed ordering.
- Existing systemd tests remain green and gain neutral-dispatch regression
  coverage, including upgrade compatibility for an existing unit whose hidden
  `daemon run` command omits the manager argument. Darwin rejects the same
  omission, and new launchd definitions always supply it. Installer typed-
  identity tests treat Linux omit/omit and omit/explicit-systemd as equal while
  rejecting omitted manager identity on Darwin.
- Durable-interpreter tests render a venv `bin/python` symlink and prove the
  plist plus `definition_current` retain the venv path rather than resolving
  to the base interpreter.
- Shutdown timing tests accept an old PID that exits any time through the full
  `ExitTimeOut` plus scheduling grace, and fail before replacement bootstrap
  only when the attributable PID outlives that budget.
- Restart tests skip bootout for a preflight-proven unloaded service and
  bootstrap its installed definition directly, after stopping/restoring a
  verified detached daemon or refusing a same-manager orphan/unattributable
  listener. Loaded-dormant kickstart applies the same arbitration and cannot
  pass readiness against the detached process it replaced.
- Status tests report `active=true` and permit `healthy=true` from attributable
  native live-process evidence plus a successful probe even when runtime state
  is absent; a merely loaded dormant job remains inactive. PID-mismatch and
  native-PID-generation-change probes remain unhealthy even when authentication
  succeeds.
- Concurrency tests hold the lifecycle transaction at each snapshot/stop/start
  boundary while a second detached or managed CLI mutation attempts to enter;
  the second operation receives the deterministic busy error and cannot
  interleave. Enable migration and install
  quiesce/restore exercise nested success and rollback helpers and prove none
  reacquires the transaction lock.
- Busy-lock tests validate private owner metadata, named-operation diagnostics,
  generic fallback for missing/malformed/racing metadata, matching-owner
  cleanup, and harmless replacement of a crashed holder's stale sidecar.
- Cross-home registration-lock tests run two distinct `AGENT_COLLAB_HOME`
  values against the same label/unit and hold each definition, recovery,
  readiness, and rollback phase. They prove both take the same per-user manager
  lock after their separate lifecycle locks, the loser fails before mutation,
  and no enable/disable/install or recovery write can overwrite the holder's
  identity. Lock-order assertions reject manager-lock-before-lifecycle
  acquisition. Smoke tests prove each child command acquires the lock and
  repeats preflight, while the wrapper never holds it across a child process and
  its trap calls lock-acquiring ordinary disable rather than shell check/remove;
  a takeover between failure and cleanup is preserved.
- Registration-lock path tests vary caller `HOME`, XDG variables, and
  `AGENT_COLLAB_HOME` while holding the OS uid fixed and prove every process uses
  the same documented persistent-state lock path and account-home LaunchAgents
  path. They prove neither lock path uses cache or temporary storage, enforce
  `0700` app-lock-directory/`0600` regular-file ownership, and reject symlink,
  wrong-owner, permissive, and non-directory pre-existing app-specific
  components without mutation. Systemd tests keep the mocked user-manager
  environment fixed while varying the caller environment and prove the fresh
  definition path is stable; then vary the manager's XDG/HOME values and prove
  the path follows the manager, with the pwd account home used only when both
  are absent.
- Legacy-systemd-path tests return an owner-marked noncanonical `FragmentPath`
  from the user manager and prove status, ordinary lifecycle, install,
  uninstall, and explicit autostart enable/disable use that single path
  regardless of the inspecting environment. Enable updates it in place and
  leaves no duplicate; disable removes that authoritative fragment and its
  authorized same-directory recovery slots without touching the manager-derived
  fresh path. Failed, malformed, empty, or conflicting manager-environment or
  fragment queries make status indeterminate and make enable, disable, install,
  uninstall, and ordinary daemon start, stop, and restart fail before any
  definition, process, venv, or command-link mutation.
- Uninstall tests exercise a managed definition plus detached occupancy through
  the top-level transaction and prove lock-held teardown/definition-removal
  helpers neither self-deadlock nor expose the venv-removal window to another
  lifecycle command.
- Start tests make a live healthy managed daemon a no-op, but return explicit
  guidance without mutation for a live PID whose PID-bound health fails:
  enabled state points to restart/diagnostics, while a disabled persisted
  override points directly to explicit `autostart enable` and never first
  recommends the ineligible restart command.
- Disabled-override lifecycle tests cover unloaded, loaded-dormant, and live
  jobs: a healthy live `start` remains a no-op, but every other `start` and every
  `restart` with a disabled override refuses before registration or process
  mutation and points to explicit `autostart enable`; in particular, a dormant
  disabled job is never kickstarted into a live-plus-disabled state. Tests
  inject termination
  at every remaining phase and prove no manual lifecycle path can leave login
  intent enabled. Loaded-dormant start with no override still uses kickstart;
  bootstrap/kickstart/initial-readiness failures boot out the candidate and
  return to prior stopped intent.
- Quarantine tests use both fixed same-directory recovery slots and cover
  owner/mode validation, atomic canonical-to-slot rename, marked replacement,
  removal of a stale authorized alternate slot before commit, preservation of
  an unauthorized foreign marked alternate via fallback/last-resort handling,
  and preservation of an
  unowned primary collision via the fallback slot, preservation of an unowned
  fallback collision, last-resort marked canonical deletion when both slots or
  rename fail, crash injection before/after the atomic commit, discovery by
  later status independent of `AGENT_COLLAB_HOME`, retention until successful
  explicit enable, and marked-only disable/uninstall cleanup. A recovery-only
  artifact from home A blocks ordinary home-B enable/disable; `--takeover`
  preserves it through failed replacement and removes it only after home B is
  healthy. Missing-token takeover and disable tests prove the old canonical
  path remains absent/non-loadable rather than entering a login crash-loop. A
  crash after B health but before A-recovery cleanup leaves mixed B-canonical
  plus A-recovery state: ordinary B status/disable/uninstall remains operable,
  preserves and reports A's recovery, and requires `--takeover` only once that
  foreign recovery is the sole registration residual.
  Candidate bootout failure plus successful quarantine still reports a critical
  live-PID residual; any detached daemon stopped by arbitration is restored only
  after managed cleanup reaches its safest state. Equivalent systemd tests
  cover the non-loadable unit recovery suffix, stale enable symlink, daemon-
  reload, status discovery, and cleanup. Systemd disable tests record the exact
  command/filesystem phase order and inject a crash at every boundary: no unit
  rename occurs before all enable symlinks are proven absent and captured PIDs
  are proven gone; the authoritative fragment is atomically renamed into its
  same-directory recovery slot before `daemon-reload`; recovery is not removed
  until reload succeeds. Every residual is disabled/non-loadable, discoverable,
  and recoverable by the next locked operation without touching an alternate
  manager-derived fresh path.
- Enabled unloaded start/restart failure tests likewise boot out an unhealthy
  candidate and prove no throttled launchd crash-loop remains after the command
  returns.
- Stop/disable tests wait for captured PIDs, reap matching dead state, and
  reject “service not found” as success while a same-manager orphan PID remains
  live.
- Forced-kill shutdown tests leave matching dead PID/state files, reap them
  after proving the old process is gone, and continue without a false fatal;
  changed state that identifies a different live process is preserved and
  aborts replacement.
- Crash-loop tests and documentation distinguish launchd's throttled retries
  from systemd's bounded start burst.
- Plist tests prove `ProcessType` is absent so launchd uses its standard/default
  service classification.

### macOS-native verification

Add a `macos-latest` CI job, at least on the primary supported Python version,
that runs the hermetic suite and `plutil -lint` against a rendered plist. Add a
credential-free native lifecycle smoke test when the hosted runner provides a
GUI launchd domain:

1. install agent-collab into a temporary durable environment;
2. isolate `AGENT_COLLAB_HOME`, select an unused loopback port, and perform a
   read-only preflight of the production label plus canonical and both recovery
   paths; abort without cleanup or mutation if any definition, recovery entry,
   loaded job, disabled override, or attributable process already exists;
3. enable autostart without `--takeover` and verify manager, registration,
   runtime ownership, and authenticated health; if another home wins the race
   after advisory preflight, require refusal and preserve its identity;
4. verify delegated stop/start/restart;
5. disable autostart and, before trap cleanup, prove the plist, live job, and
   both marked recovery slots are gone while config and logs remain;
6. arm an unconditional cleanup trap only after the empty preflight succeeds;
   record the exact definition identity created by this invocation, then invoke
   ordinary `autostart disable` under that recorded smoke home without
   `--takeover`. The command acquires lifecycle then manager locks and performs
   identity check plus removal inside the locked transaction; the shell trap
   must never implement a separate check-then-`rm`. Thus a failed run cannot
   delete a pre-existing or concurrently installed LaunchAgent. Cleanup remains
   marked-and-identity-bound and reports every unowned or changed collision
   rather than removing it.

If GitHub's macOS runner has no usable GUI domain, keep the hermetic/plutil CI
job mandatory and document a repeatable real-Mac acceptance script rather than
turning an environmental limitation into a product test failure.

Before handoff, run the complete local gate on Linux and the native macOS
acceptance path. Credentialed provider calls are unnecessary.

## Documentation updates

- README: replace “macOS LaunchAgent registration is not yet supported” with
  the shared command flow, login-only semantics, Background Items note, and
  unsupported headless-domain diagnostic.
- `doc/runtime-layout.md`: add the LaunchAgent path and preservation/removal
  boundaries.
- `doc/daemon-architecture.md`: describe the neutral ownership model and both
  service-manager backends.
- `doc/development.md`: add macOS native verification commands and cleanup
  cautions.
- `doc/daemon_api_doc/openapi.json` and `doc/daemon_api_doc/http-api.md`:
  regenerate through the project build after adding `/ready` and its schema.
- `CHANGELOG.md`: add a concise enhancement entry linked to the issue.

## Done when

- On a supported macOS login session, `agent-collab daemon autostart enable`
  installs and bootstraps an owned LaunchAgent and returns healthy status.
- The daemon is automatically running after the user's next login without a
  shell startup hook or manual `daemon start`.
- `autostart status`, `daemon status`, and ordinary lifecycle commands report
  and respect launchd ownership.
- Repeated enable, changed-definition replacement, detached-to-managed
  migration, failed startup rollback, disable, upgrade, and uninstall satisfy
  the behavior above.
- Generated plists contain no secrets or unrelated environment values and pass
  native plist validation.
- Linux systemd autostart remains green with the intentional manager-derived
  path, authoritative-fragment, recovery, fail-closed discovery, and explicit
  effective-home identity changes above; unrelated behavior remains unchanged.
- Hermetic tests, macOS-native verification, documentation, and changelog are
  complete.

## Decisions

- Use a per-user LaunchAgent, not a root LaunchDaemon.
- Use the same public autostart and daemon lifecycle commands on Linux and
  macOS; platform selection is internal.
- launchd owns one foreground process and agent-collab owns application
  readiness and session state.
- Use the legacy per-user plist path for the current CLI distribution; defer
  `SMAppService` until agent-collab ships as a macOS app bundle.
- Use modern launchctl domain/service commands, targeting the current GUI user
  domain explicitly.
- Leave `ProcessType` unset; this user-facing session host is not deferrable
  background maintenance.
- Match systemd's restart-on-failure intent while documenting launchd's native
  throttle-only retry behavior rather than claiming burst-stop parity.
- Preserve the existing authenticated readiness check and transactional
  detached-to-managed migration.
- A user-disabled Background Item remains authoritative; agent-collab reports
  remediation but cannot clear it. Explicit `autostart enable` clears only a
  separate launchctl disabled override.
- Keep service definitions owner-marked, atomically written, and free of
  secrets.
- Prefer a quiesced, race-free upgrade over the current shorter restart window:
  warn before interrupting active sessions, then keep the daemon stopped for
  the full venv/link/config mutation so native restart policies cannot execute
  partially installed code.

## Completion

Implemented a platform-neutral daemon lifecycle facade with systemd and launchd
backends, authenticated PID-bound readiness, cross-home ownership and takeover
rules, lifecycle and registration locking, crash-safe recovery slots, and
quiesced install/uninstall handling. Added hermetic backend coverage, a native
macOS smoke script and CI job, regenerated daemon API documentation, and updated
the user and architecture documentation.

Repeated read-only Grok 4.5 and Gemini 3.6 Flash High review rounds converged
with no confirmed high- or medium-severity findings. The final focused daemon,
autostart, lifecycle, CLI, supervisor, and installer suite passed 235 tests with
one platform skip. `./agent_collab_dev.sh build --check` verified the generated
API documentation, and the complete `./agent_collab_dev.sh test` gate passed
1,637 tests with two expected platform skips.
