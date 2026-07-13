# Development Notes

## Commands

Run the complete local gate (Ruff lint, Ruff format verification, then the
hermetic unit suite):

```bash
./agent_collab.sh test
```

Run only the hermetic unit suite:

```bash
python3 -m unittest discover -s tests -t .
```

Validate repository configuration and generate the daemon REST API artifacts:

```bash
./agent_collab.sh setup
./agent_collab.sh setup --check
```

`setup --check` does not write and fails if `doc/daemon_api_doc/` is stale.

Source checkout helper:

```bash
./agent_collab.sh help
./agent_collab.sh install [--editable]
./agent_collab.sh test
./agent_collab.sh integration-test [claude_cli|claude_sdk|codex_cli|codex_sdk|antigravity_cli|antigravity_sdk|xai_cli|xai_sdk] [--strict]
./agent_collab.sh smoke
```

The source helper prefers a persistent virtual environment at
`~/.agent-collab/venv` when one exists. Override its interpreter with `AGENT_COLLAB_PYTHON` or
its environment directory with `AGENT_COLLAB_VENV`. Startup rejects any selected
interpreter older than Python 3.10. Without a configured environment it tries
`python3.12`, `python3.11`, `python3.10`, then `python3` in that order.

`install` creates or updates the persistent environment, installs a normal
non-editable copy of the checkout with the `all` extra (every provider SDK, so
the `sdk` backends work out of the box), and atomically exposes its
`agent-collab` entry point under `~/.local/bin`. The base package itself is
SDK-free; per-provider extras (`claude`, `codex`, `antigravity`, `xai`, `all`)
enable the `sdk` backends, and a missing SDK degrades to an unavailable backend
with an install hint. It does not edit shell startup files or enable
daemon autostart. `--editable` is available for active development;
`AGENT_COLLAB_BIN_DIR` or `--bin-dir` selects a different user command
directory for isolated tests or custom layouts.

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
