# Development Notes

## Commands

Run the complete local gate (Ruff lint, Ruff format verification, then the
hermetic unit suite):

```bash
./agent_collab_dev.sh test
```

A green local gate is necessary but not sufficient: CI also runs the suite on
Python 3.10 and 3.11 and on an SDK-free base install, so version- and
dependency-specific failures pass locally. After every push, confirm the CI
run is green (`gh run list --branch main`, or `gh run watch` on the new run)
instead of assuming the local result carries over. Never tag a release
without a green run on the release commit.

Run only the hermetic unit suite:

```bash
python3 -m unittest discover -s tests -t .
```

Validate repository configuration and generate the daemon REST API artifacts:

```bash
./agent_collab_dev.sh build
./agent_collab_dev.sh build --check
```

`build --check` does not write and fails if `doc/daemon_api_doc/` is stale.

Entrypoint scripts:

```bash
./agent_collab.sh help                 # user commands: install, uninstall, daemon, sessions
./agent_collab.sh install
./agent_collab.sh uninstall
./agent_collab_dev.sh help             # developer commands
./agent_collab_dev.sh build [--check]
./agent_collab_dev.sh test
./agent_collab_dev.sh integration-test [claude_cli|claude_sdk|codex_cli|codex_sdk|antigravity_cli|antigravity_sdk|xai_cli|xai_sdk] [--strict]
./agent_collab_dev.sh smoke
```

Both scripts source `scripts/agent_collab_lib.sh` for interpreter selection;
shell stays a thin dispatch layer and all real logic lives in Python (see
`.claude/skills/cli-scripting/SKILL.md`). The library prefers a persistent
virtual environment at `~/.agent-collab/venv` when one exists. Override its
interpreter with `AGENT_COLLAB_PYTHON` or its environment directory with
`AGENT_COLLAB_VENV`. Startup rejects any selected interpreter older than
Python 3.10. Without a configured environment it tries `python3.12`,
`python3.11`, `python3.10`, then `python3` in that order.

`install` is switchless and is also the upgrade command: re-run it after
every `git pull`. It creates or updates the persistent environment, installs
a normal non-editable copy of the checkout with the `all` extra (every
provider SDK, so the `sdk` backends work out of the box), atomically exposes
the `agent-collab` entry point under `~/.local/bin`, migrates the user config
to the current schema (with a `config.toml.bak` backup, preserving comments),
and restarts the daemon if it was running before install — which interrupts
active sessions. It does not edit shell startup files, never starts a stopped
daemon, and never enables autostart. Verbose pip output is captured to
`~/.agent-collab/install.log` and shown only on failure. For active
development set `AGENT_COLLAB_INSTALL_EDITABLE=1`; `AGENT_COLLAB_BIN_DIR`
selects a different user command directory for isolated tests or custom
layouts.

`uninstall` reverses install: it stops the daemon, disables autostart,
removes the venv and the command link (only when the link points into the
venv). Config and session data under `~/.agent-collab` are always kept.

`test` runs Ruff lint and format checks before the hermetic suite, which
discovers only `tests/`; install the `dev` extra to provide the pinned Ruff
version. `integration-test` discovers only `integration_tests/`, may make paid
model calls, and uses native provider credentials. Missing
dependencies/credentials skip by default; `--strict` returns exit `2` when an
explicitly selected provider cannot run. Behavioral failures return `1`.

Live tests use cheap, fast defaults because they verify backend transport and
event fidelity rather than model quality: Claude `sonnet`/low, Codex
`gpt-5.6-luna`/low, Antigravity `Gemini 3.5 Flash (Low)`, xAI CLI
`grok-build`, and xAI SDK `grok-4.5`/low. See
`integration_tests/README.md` for environment overrides.

Run a one-shot mock session:

```bash
python3 -m agent_collab.cli --mock --workdir . "Smoke test"
```

Run the foreground server:

```bash
python3 -m agent_collab.cli serve
```

Start and watch a mock server-owned session:

```bash
python3 -m agent_collab.cli start --mock --watch --workdir . "Smoke test"
```

Start and inspect the daemon:

```bash
./agent_collab.sh daemon start
./agent_collab.sh daemon status
./agent_collab.sh daemon logs --tail 100
```

## Local Runtime Notes

The server binds to `127.0.0.1:8765` by default.

When launching real provider subprocesses, the server may need to run outside a
restricted sandbox so the child CLIs can see normal user credentials.

Execution-relevant agent configuration is global-user-only. To opt into the
disabled-by-default `antigravity` or `xai` agents for local development, enable
them and add their workflows in `$AGENT_COLLAB_HOME/config.toml`; project config
cannot enable agents. Grok Build runs as
`grok --no-auto-update --output-format streaming-json -p`; xAI SDK starts
require `backend="sdk"`, `XAI_API_KEY`, and an explicit
`backend_options.xai_sdk.model`.

`SubprocessRunner` closes child stdin with `DEVNULL`; keep this. It prevents `codex exec --json` from waiting on the server terminal for additional stdin.

## Coding Constraints

- Prefer the Python standard library and keep dependencies minimal.
- Keep `agent-collab serve` foreground-only; daemon lifecycle commands are separate.
- Keep localhost as the default security boundary.
- Preserve cursor-based event reads and long-polling.
- Do not let agents recursively spawn Claude, Codex, `agent-collab`, or other agent processes.
- Keep `watch` plain and pipe-friendly.
- Keep daemon logs from dumping full transcript events by default.
- MCP agents should call `agent_collab_guidance` for usage guidance and
  `agent_collab_describe_options` with the required workdir before passing
  non-default model, reasoning, sandbox, permission, backend, or provider
  settings.
- Invalid `agent_collab_start` options should be fixed from returned field-path
  details, not retried by guessing.
- Tests must isolate `AGENT_COLLAB_HOME` by pointing it at a temp dir, so
  nothing writes to the real `~/.agent-collab`.
- All config shape compatibility handling belongs in
  `agent_collab/config_migrations.py`; runtime code should consume the latest
  schema.
- Add focused tests with each behavior change.
- For server and MCP changes, cover the affected route or tool behavior plus one shared-session path when relevant.

Before handing back implementation work, run:

```bash
python3 -m unittest discover -s tests
```

If live smoke is needed, use mock mode first. Real Claude/Codex smoke can be expensive and may need unsandboxed credentials.
