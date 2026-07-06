# Development Notes

## Commands

Run tests:

```bash
python3 -m unittest discover -s tests
```

Source checkout helper:

```bash
./agent_collab.sh help
./agent_collab.sh test
./agent_collab.sh smoke
```

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

When launching real Claude/Codex subprocesses, the server may need to run outside a restricted sandbox so the child CLIs can see normal user credentials.

This repository currently includes project config at:

```text
.agent-collab/config.toml
```

It currently configures Claude with:

```bash
claude -p --output-format stream-json --verbose --model opus --effort high
```

It currently configures Codex with:

```bash
codex exec --json -c model_reasoning_effort="high"
```

`SubprocessRunner` closes child stdin with `DEVNULL`; keep this. It prevents `codex exec --json` from waiting on the server terminal for additional stdin.

## Coding Constraints

- Keep dependencies minimal.
- Prefer Python standard library.
- Keep `agent-collab serve` foreground-only; daemon lifecycle commands are separate.
- Keep localhost as the default security boundary.
- Preserve cursor-based event reads and long-polling.
- Do not let agents recursively spawn Claude, Codex, `agent-collab`, or other agent processes.
- Keep `watch` plain and pipe-friendly.
- Add focused tests with each behavior change.
- For server and MCP changes, cover the affected route or tool behavior plus one shared-session path when relevant.

Before handing back implementation work, run:

```bash
python3 -m unittest discover -s tests
```

If live smoke is needed, use mock mode first. Real Claude/Codex smoke can be expensive and may need unsandboxed credentials.
