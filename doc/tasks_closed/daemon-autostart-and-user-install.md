# User installation and daemon autostart

**Status:** Closed on 2026-07-12 after installed-venv regression remediation.

**Created:** 2026-07-12

**Issue:** [#9](https://github.com/lauriparviainen/agent_collab/issues/9)

## Context

The source-checkout helper makes development commands convenient, but
`./agent_collab.sh tui` is tied to the checkout and current directory. The
package already declares an `agent-collab` console script, yet the README's
editable-venv installation exposes it only while that venv is active.

The global daemon also has to be started manually after login. A systemd user
service can solve that on Linux, but it must not wrap `agent-collab daemon
start`: that command already detaches a child process, while systemd needs to
own one foreground process. Installation and autostart belong in one plan
because the generated service must point at a durable installed interpreter,
not a checkout-only `PYTHONPATH` inherited from the shell helper.

`./agent_collab_dev.sh build` is not the right installation hook. It currently
validates repository configuration and writes or checks generated daemon API
documentation. It is a development operation, can be run repeatedly in CI,
and must not mutate a user's PATH or service-manager state.

## Goals

- From any directory, the user can run `agent-collab tui` and every other
  normal CLI command without activating a venv.
- Installation is explicit, repeatable, and does not edit shell startup files
  or overwrite an unrelated command silently.
- A separate explicit command can register the daemon to start with the Linux
  user's login session.
- Manual and systemd-managed daemon lifecycles cannot compete over the same
  process, state files, or port.
- Tokens, provider credentials, and other secret environment values never
  enter generated service definitions.

## Proposed user experience

From a source checkout:

```bash
./agent_collab.sh install
```

After installation, from any directory:

```bash
agent-collab tui
agent-collab daemon autostart enable
agent-collab daemon autostart status
agent-collab daemon autostart disable
```

`install` only installs the command. It does not enable or start the daemon.
Autostart remains a separate opt-in action with an independently reversible
lifecycle.

## User installation design

1. Add an explicit `install` action to the source helper. By default it creates
   or reuses the durable venv selected by `AGENT_COLLAB_VENV` (defaulting to
   `~/.agent-collab/venv`) and installs the current checkout into it.
2. Expose the installed `agent-collab` console script through
   `~/.local/bin/agent-collab`. Use an atomic symlink replacement when updating
   a link already managed by this installation. Refuse to replace an unrelated
   file or link unless an explicit force option is provided.
3. Do not edit `.bashrc`, `.zshrc`, or other shell startup files. If
   `~/.local/bin` is absent from PATH, finish the installation and print a
   precise, shell-neutral diagnostic explaining that it must be added before
   the command is discoverable in new shells.
4. Make repeated installs safe and use them as the upgrade path for a
   source-based installation. The durable venv path keeps the console-script
   and service `ExecStart` paths stable across upgrades.
5. Keep editable developer installation available as an explicit option, but
   use a normal non-editable install by default so moving or deleting the
   checkout does not break the universal command.
6. Print the installed command path and a smoke command such as
   `agent-collab --help`. Installation does not authenticate providers or copy
   their credentials.

Using `pipx install .` remains a valid documented alternative because the
existing package metadata already defines the console entry points. The source
helper bootstrap is the project-owned path and does not require pipx to be
installed first.

## Systemd autostart design

### Process ownership

- Add an internal foreground daemon entry point, for example
  `agent-collab daemon run`. It runs the HTTP server without forking and writes
  compatible runtime metadata identifying `systemd` as the process manager.
- The runtime state file remains useful discovery metadata, but it is not the
  authority for signalling a systemd-owned process. Systemd and the
  authenticated health endpoint determine service and application health.
- The existing start lock protects startup/state transitions only; it is not
  held for the daemon's full lifetime.
- Use `Type=simple` initially. After `systemctl --user start`, the CLI reuses
  the existing authenticated readiness probe before reporting success.
  `Type=notify` is unnecessary for the first implementation.

### Generated unit

- Install an atomically written per-user unit in the standard systemd user
  configuration directory and manage it only through `systemctl --user`.
- `ExecStart` uses the absolute interpreter/module path of the durable
  installation. `autostart enable` rejects a checkout-only invocation that
  would depend on a transient `PYTHONPATH`, with an instruction to run the
  install bootstrap first.
- Use `Restart=on-failure` with a modest restart delay and start-rate limit.
  A normal `systemctl stop` must remain stopped.
- Preserve the existing daemon log-file behavior so `agent-collab daemon logs`
  has the same contract under both ownership modes. Do not require journald for
  normal log access.
- Capture only the current PATH needed to find provider CLIs, with correct
  systemd escaping. Never copy the complete process environment. Re-running
  `autostart enable` refreshes PATH and the absolute interpreter path.
- Do not put the daemon bearer token, provider keys, or credentials in the
  unit. The process reads the existing owner-only configuration and provider
  credential stores as the current user.

### Lifecycle routing

- While the managed unit is installed, existing `daemon start`, `stop`, and
  `restart` commands delegate to `systemctl --user`; they never signal the
  service PID directly or start a detached competitor.
- `daemon status` combines service-manager state with application health.
  `daemon autostart status` distinguishes unit installed, enabled, active,
  healthy, and stale-definition states and diagnoses a missing interpreter or
  provider executable.
- `autostart enable` first renders and validates the unit, reloads systemd,
  then gracefully transitions any verified manually started daemon before
  starting the service. If service startup fails after that transition, it
  reports actionable recovery and should restore the prior manual daemon when
  it can do so safely.
- Re-running `enable` is idempotent. A changed unit is atomically replaced,
  followed by `daemon-reload` and a controlled restart; an unchanged healthy
  service is left alone.
- `autostart disable` uses `disable --now`, removes only the generated unit,
  reloads systemd, and preserves config, tokens, transcripts, logs, and the
  installed universal command.

### Login versus boot

A systemd user unit starts with the user's login session. Do not silently run
`loginctl enable-linger`: boot-before-login is a separate machine policy with
different credential and resource implications. Document the distinction and
manual linger command for users who explicitly need it.

Systems without a usable systemd user manager receive an actionable unsupported
platform/runtime error. macOS LaunchAgent support is a follow-up. Avoid a broad
cross-platform abstraction until a second implementation exists, while keeping
the Linux-specific code isolated enough to add one later.

## Verification plan

- Hermetic shell-helper tests cover venv selection, install/update behavior,
  atomic link creation, unrelated-link refusal, PATH diagnostics, and no shell
  startup-file edits.
- A subprocess smoke test invokes the installed `agent-collab` entry point from
  a directory outside the checkout and opens CLI help without an activated
  venv.
- Unit rendering uses temporary XDG paths and golden/structural assertions;
  generated units contain no secret environment values.
- Service-manager tests mock subprocess calls and cover enable, unchanged
  re-enable, changed-definition reload/restart, disable, and systemctl error
  propagation without touching the developer's real user manager.
- Lifecycle tests cover manual-to-systemd migration, failed systemd startup and
  recovery, systemd-owned start/stop/restart routing, stale unit/interpreter
  diagnostics, port conflicts, and restart-loop avoidance.
- Existing daemon supervisor, CLI, TUI, config, and shell-wrapper tests remain
  green.
- README, development notes, daemon architecture, and runtime layout describe
  installation, upgrade, uninstall/disable boundaries, login semantics, PATH
  behavior, manual fallback, and `pipx` as an alternative.

## Decisions

- Universal command installation happens at explicit install/bootstrap time,
  not during `setup`, TUI startup, or daemon startup.
- Installing the command and enabling autostart are separate user choices.
- The first autostart backend is Linux systemd user services.
- The service manager owns the process; agent-collab retains application
  readiness, configuration, and session ownership.
- `Type=simple` plus the existing readiness probe is the initial readiness
  contract.
- The generated unit omits `WorkingDirectory`; the managed foreground entry
  point explicitly defaults the server workdir to the agent-collab home. This
  avoids systemd path-directive quoting differences and passes native unit
  verification.
- A force option exists for explicit replacement of a conflicting user-bin
  entry. Without it, installation refuses the conflict.
- Uninstalling the command remains a documented manual boundary; autostart
  disable deliberately removes only service registration.

## Implementation

- `agent_collab/user_install.py` creates or updates the durable venv, installs
  the checkout normally or editable, atomically exposes the console script,
  guards conflicting commands, and reports PATH remediation without editing
  shell files. `agent_collab.sh install` is the source bootstrap.
- `agent_collab/daemon_autostart.py` renders and owns the marked systemd user
  unit, validates durable imports, captures only PATH plus an explicit
  `AGENT_COLLAB_HOME`, manages enable/status/disable, performs authenticated
  readiness checks, and rolls back failed manual-to-service transitions.
- `agent_collab daemon run` is the non-forking service entry point. Managed
  runtime state identifies systemd ownership, SIGTERM unwinds through cleanup,
  and output continues to use the existing private daemon log files.
- Ordinary daemon lifecycle commands delegate to systemd when the marked unit
  exists or live runtime state proves systemd ownership. The raw supervisor
  also refuses to signal a live systemd-owned process.
- README, development, daemon architecture, runtime-layout, helper help, and
  changelog documentation describe the final behavior and boundaries.

## Verification

Post-completion regression: a valid durable installation was rejected because
autostart resolved the venv's `bin/python` symlink to the system interpreter
before probing imports. The system interpreter correctly lacked the installed
package. The fix preserves an absolute venv interpreter path without resolving
the final symlink, with a regression test using the same symlink layout. The
complete 598-test gate, API artifact check, durability probe, and native systemd
unit verification pass after the fix.

- `./agent_collab_dev.sh test`: Ruff and format checks pass; all 598 hermetic tests
  pass.
- `./agent_collab_dev.sh build --check`: effective config and generated REST API
  artifacts verify cleanly.
- A clean Python 3.12 venv installed the local wheel without dependencies and
  ran both `agent-collab --help` and `agent-collab daemon autostart --help`
  from outside the checkout. The host's Python 3.9 attempt was correctly
  rejected by the existing Python >= 3.10 package constraint.
- A rendered unit passes `systemd-analyze verify`. This check caught and led to
  removal of an invalid quoted `WorkingDirectory` directive.
- Independent read-only Gemini 3.5 Flash High and Gemini 3.1 Pro High reviews
  found lifecycle ownership, stale-state routing, dollar escaping,
  rollback/error preservation, and systemd path-format issues. All findings
  were addressed; Pro's final assessment after the path fix was ship-ready
  with no remaining blockers.
