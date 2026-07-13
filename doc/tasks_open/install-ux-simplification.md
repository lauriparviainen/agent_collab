# Install UX simplification

**Status:** Designed; not started.

**Created:** 2026-07-13

**Issue:** [#15](https://github.com/lauriparviainen/agent_collab/issues/15)

## Context

agent-collab is distributed as a git checkout plus the `agent_collab.sh`
wrapper. Today `./agent_collab.sh install` takes `--editable`, `--force`, and
`--bin-dir` switches, `setup` looks like a getting-started step but is actually
a repo maintenance tool (config validation plus generated API docs), and there
is no uninstall. Upgrading after `git pull` is undocumented: a non-editable
install silently keeps running old code until install is re-run, and a running
daemon keeps executing the old version even after reinstall.

Config migrations (`agent_collab/config_migrations.py`) are lazy and in-memory
only; nothing ever rewrites an old config file. The checkout-plus-script
distribution makes install the natural post-install hook that pip lacks, so
install is where upgrade-time work (config migration, daemon restart) belongs.

The guiding principle: users do not read switch documentation. The entire user
mental model must be one sentence — *clone the repo, run
`./agent_collab.sh install`, and re-run it after every `git pull`*.

## Goals and decisions

1. **Switchless install.** `./agent_collab.sh install` takes no options.
   - `--bin-dir` is dropped; the `AGENT_COLLAB_BIN_DIR` environment variable
     (already the default source) remains for the rare override.
   - `--editable` is dropped from the user surface; it is a developer concern
     and moves behind an environment variable or the dev script.
   - `--force` is dropped. Install still refuses to clobber a foreign file at
     the command path, but the error message states exactly what to remove and
     re-run; safety by clear error, not by option.

2. **Install is the upgrade command.** Re-running install after `git pull` is
   the documented and only upgrade path. On each run install:
   - reinstalls the checkout into the durable venv (current behavior);
   - migrates the **user** config to the current schema, writing a `.bak`
     backup first; project configs are never rewritten;
   - restarts the daemon **if and only if it was running before install** —
     on every install run, unconditionally, with no version comparison; the
     simple rule beats a clever one. It prints what it did (including how many
     active sessions were interrupted). Install never starts a stopped daemon.
   - Docs state plainly: install interrupts active sessions. (Framed as
     interruption, not "destructive" — session state under
     `~/.agent-collab/data` and all configs are preserved.)

3. **Add uninstall as the exact inverse of install.**
   `./agent_collab.sh uninstall`, also switchless:
   - stops the daemon and disables systemd autostart (otherwise a user unit is
     left pointing at a deleted venv);
   - deletes the durable venv (`~/.agent-collab/venv`);
   - removes the `agent-collab` command link only if it points into that venv
     (same foreign-file safety posture as install);
   - **keeps** user config and session data, printing one line telling the
     user the `~/.agent-collab` path to delete for full removal. No `--purge`
     flag — the printed sentence replaces the switch.

4. **Split developer commands into `agent_collab_dev.sh`.** The user script
   keeps `install`, `uninstall`, `help`, and the runtime pass-through commands
   (`daemon`, `serve`, `start`, `watch`, `list`, `status`, `stop`). The dev
   script gets `build`, `test`, `integration-test`, and `smoke`.

5. **Rename `setup` to `build`.** Same behavior (validate effective config,
   regenerate `doc/daemon_api_doc/openapi.json` and `http-api.md`;
   `build --check` verifies without writing), but it lives in the dev script
   and no longer masquerades as a user onboarding step.

6. **Shared bash code goes to `scripts/agent_collab_lib.sh`.** The Python
   interpreter selection, venv resolution, and version-check preamble are
   sourced from that library by both scripts; nothing is duplicated.

7. **Clear step-by-step CLI output.** Install and uninstall narrate every
   step: what is being done, then whether it succeeded and what it changed, so
   the user is never surprised by what happened. Verbose tool output (pip in
   particular) is hidden; each step renders as one progress line followed by
   one result line. On step failure the captured output is shown so the error
   is diagnosable. Use these output markers consistently:

   - `▶` progress / an action starting;
   - `ⓘ Info:` neutral information;
   - `✓` success or completion;
   - `! Warning:` non-fatal warnings;
   - `✗` failed health or status checks that are not necessarily fatal;
   - `Error:` fatal errors that exit non-zero.

   No success icons on warnings or headings; fatal errors keep the
   grep-friendly `Error:` prefix rather than an icon-only marker. Target
   shape:

   ```
   ▶ Checking Python interpreter
   ✓ Python 3.12.3 (~/.agent-collab/venv)
   ▶ Installing agent-collab into ~/.agent-collab/venv
   ✓ Installed agent-collab 0.6.0 (23 packages)
   ▶ Migrating user config
   ✓ Config already at schema 6; nothing to do
   ▶ Restarting daemon (was running, 2 active sessions interrupted)
   ✓ Daemon restarted on 0.6.0
   ✓ Install complete — run: agent-collab --help
   ```

   Captured tool output goes to a log file under `~/.agent-collab` and the
   failure path prints its location plus the tail of the output.

8. **Professional daemon lifecycle output.** `daemon start`, `stop`,
   `restart`, and `status` adopt the same markers and tone. Today they print
   raw lines like `started agent-collab daemon pid 12345` and bare
   `key: value` dumps. Instead, every lifecycle command states the action and
   outcome and always includes the agent-collab version; `status` renders an
   aligned key/value block (keys padded to a common width, one fact per
   line):

   ```
   ✓ Daemon running
     version   0.6.0
     pid       12345
     uptime    2h 14m
     sessions  2 active
   ```

   Failures use `✗`/`Error:` consistently. Output stays stable and
   grep-friendly enough for scripts that match on it; anything
   machine-consumed should prefer the HTTP API. The full convention lives in
   `.claude/skills/cli-scripting/SKILL.md`.

9. **The in-memory migration layer stays.** Install-time migration is a
   write-back convenience on top of `config_migrations.py`, never a
   replacement: files can still be old (skipped installs, restored backups,
   synced dotfiles), and lazy migration keeps downgrades soft. PyPI users
   bypass the wrapper script entirely, so a first-run-of-new-version migration
   hook in the CLI/daemon will eventually be needed; design the install-time
   migration as a wrapper over that same code path rather than a second
   mechanism.

## Implementation notes

- Write-back migration ships with this task, using `tomlkit` (decided: do it
  now, not deferred). The goal is that the global config on disk always stays
  clean and current, and install is the moment the user is told if something
  is wrong with it. `tomlkit` preserves user comments and formatting; stdlib
  `tomllib` is read-only and a naive re-serialize destroys them. Even while
  all migrations only stamp `schema_version`, install stamps the current
  version (after a `.bak` backup) and surfaces any config
  warnings/errors — e.g. ignored sections or a missing `schema_version` — as
  `! Warning:` / `Error:` lines instead of leaving them buried in daemon logs.
- Daemon restart needs only a reliable "was running" probe; no version
  comparison (decided: restart on every install run whenever the daemon was
  running — keep the rule simple and predictable).
- Update every reference to moved commands in the same change: `AGENTS.md`,
  `doc/development.md`, `doc/runtime-layout.md`, CI workflows, and any
  task-document instructions that say `./agent_collab.sh test` or
  `setup --check`.
- `agent_collab_dev.sh` and the user script must both work from a fresh clone
  before install (bootstrap Python detection lives in the shared library).
- Shell entrypoint rules, the marker convention, and the bash-is-only-an-
  entrypoint principle are codified in `.claude/skills/cli-scripting/SKILL.md`
  — follow it for all script and CLI-output changes in this task.

## Verification

- Fresh clone: `install` with no arguments produces a working `agent-collab`
  command; re-running it is a no-op apart from pip reinstall.
- Upgrade: after `git pull`, `install` puts the new version in the venv,
  migrates and backs up the user config (comments and formatting preserved,
  `schema_version` stamped current), and restarts a previously running daemon
  (and only then); a stopped daemon stays stopped.
- Install surfaces user-config problems as `! Warning:`/`Error:` lines; a
  clean config produces a `✓` with no noise.
- Install and uninstall output follows the marker convention, one progress
  line and one result line per step; pip output is captured to a log file and
  shown only on failure.
- `daemon start`/`stop`/`restart`/`status` output uses the same markers,
  always includes the agent-collab version, and `status` renders an aligned
  key/value block.
- Foreign file at the command path: install fails with an actionable message
  and touches nothing.
- `uninstall` removes venv, command link, and autostart unit; leaves
  `~/.agent-collab` config and data; prints the removal hint; is safe to run
  when nothing is installed.
- `agent_collab_dev.sh build --check`, `test`, `integration-test`, and
  `smoke` work; the user script no longer exposes them.
- No duplicated preamble: both scripts source `scripts/agent_collab_lib.sh`.
- Docs and CI reference the new command layout; `rg` finds no stale
  `agent_collab.sh setup` or user-script `test` references.
