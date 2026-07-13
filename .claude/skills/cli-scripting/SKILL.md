---
name: cli-scripting
description: Use when writing or changing shell scripts, CLI commands, or user-facing terminal output — covers the bash-is-only-an-entrypoint rule, scripts/agent_collab_lib.sh, and the CLI output marker convention.
---

# CLI Scripting and Terminal Output

Use this skill when touching `agent_collab.sh`, `agent_collab_dev.sh`,
anything under `scripts/`, or Python code that prints user-facing CLI output
(install, uninstall, daemon lifecycle, session commands).

## Bash Is Only an Entrypoint

Shell scripts orchestrate; they do not implement. A wrapper script may select
a Python interpreter, resolve the venv, set environment, and dispatch to
`python -m agent_collab.<module>` — nothing more. All real logic (installing,
migrating config, managing the daemon, formatting output) lives in Python
modules such as `agent_collab/user_install.py`.

**Why:** Python is the portable layer. Keeping bash to a thin dispatch shim
means porting to another OS later (Windows, or a different shell) only
requires replacing the entrypoint, not rewriting behavior. It also keeps the
logic testable by the hermetic unittest suite, which shell code is not.

Before adding a function to a shell script, ask whether it is dispatch or
logic. Branching on user input, parsing files, computing paths beyond venv
resolution, and producing multi-step output are logic — move them to Python.

## Script Layout

- `agent_collab.sh` — user-facing entrypoint: `install`, `uninstall`, `help`,
  and runtime pass-through commands (`daemon`, `serve`, `start`, `watch`,
  `list`, `status`, `stop`).
- `agent_collab_dev.sh` — developer entrypoint: `build`, `test`,
  `integration-test`, `smoke`.
- `scripts/agent_collab_lib.sh` — shared bash sourced by both entrypoints:
  Python interpreter selection, venv resolution, the version check. Never
  duplicate this preamble in an entrypoint; if both scripts need it, it
  belongs in the library.

Both entrypoints must work from a fresh clone before anything is installed,
so the shared library may rely only on POSIX tools and a system Python.

## CLI Output Markers

Use these markers consistently in all user-facing terminal output:

- `▶` for progress or an action starting.
- `ⓘ Info:` for neutral information.
- `✓` for success or completion.
- `! Warning:` for non-fatal warnings.
- `✗` for failed health or status checks that are not necessarily fatal.
- `Error:` for fatal errors that exit non-zero.

Rules:

- Do not add success icons to warnings or status headings.
- Do not replace `Error:` with an icon-only marker; grep-friendly fatal
  errors matter in logs and scripts.
- Multi-step commands print one `▶` progress line when a step starts and one
  result line (`✓`/`! Warning:`/`✗`) when it finishes, stating what actually
  happened (`✓ Installed agent-collab 0.6.0 (23 packages)`), so the user is
  never surprised by what a command did.
- Hide verbose subprocess output (pip and similar). Capture it to a log file
  under `~/.agent-collab`; on failure print the log location and the tail of
  the captured output.
- Keep lines stable and grep-friendly; anything machine-consumed should use
  the daemon HTTP API, not scraped CLI text.
- Lifecycle commands (`daemon start`/`stop`/`restart`/`status`) always state
  the agent-collab version in their outcome line.
- Status-style output renders as an aligned key/value block — keys padded to
  a common width, one fact per line — never a bare unaligned `key: value`
  dump:

  ```
  ✓ Daemon running
    version   0.6.0
    pid       12345
    uptime    2h 14m
    sessions  2 active
  ```

Example shape for a multi-step command:

```
▶ Installing agent-collab into ~/.agent-collab/venv
✓ Installed agent-collab 0.6.0 (23 packages)
▶ Migrating user config
! Warning: config had no schema_version; stamped 6 (backup: config.toml.bak)
▶ Restarting daemon (was running, 2 active sessions interrupted)
✓ Daemon restarted on 0.6.0
✓ Install complete — run: agent-collab --help
```

## Help Text

Keep the user entrypoint's help short and free of developer commands. Every
command listed in `help` must be something an end user is expected to run;
developer workflow lives in `agent_collab_dev.sh` and `doc/development.md`.
