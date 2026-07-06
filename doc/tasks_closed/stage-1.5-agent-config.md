# Stage 1.5: Agent configuration

## Purpose

Move agent and mode definitions out of hardcoded referee logic.

This stage does not introduce the daemon. It only adds runtime config support for the existing supervised loop.

## Config lookup

Effective precedence:

```text
WORKDIR/.agent-collab/config.toml
~/.agent-collab/config.toml
built-in defaults
```

Implementation loads built-ins first, then user config, then project config so project values override user values.

## Built-in defaults

```toml
[agents.claude]
type = "claude"
command = "claude"
args = ["-p", "--output-format", "stream-json", "--verbose"]
enabled = true

[agents.codex]
type = "codex"
command = "codex"
args = ["exec", "--json"]
enabled = true

[modes.claude-leads]
sequence = ["claude", "codex", "claude"]

[modes.codex-leads]
sequence = ["codex", "claude", "codex"]

[modes.debate]
sequence = ["claude", "codex", "claude", "codex"]
```

## Implemented scope

- Add `agent_collab/config.py` for built-ins, config loading, merging, and validation.
- Support project and user `config.toml` files without adding dependencies.
- Use `tomllib` when available and a small TOML subset parser otherwise for Python 3.9 compatibility.
- Let configured modes drive the referee turn sequence.
- Let configured agents drive subprocess command prefixes.
- Preserve `--mock` and `--dry-run`.
- Validate that mode sequences reference known enabled agents.

## Out of scope

- No daemon or session manager changes.
- No config init/list/doctor CLI commands.
- No permission approval model in config.

## Acceptance criteria

- Existing one-shot CLI still works:

```bash
python3 -m agent_collab.cli --mock --workdir . "task"
```

- Missing config falls back to built-in Claude/Codex agents and built-in modes.
- Project config overrides user config.
- Custom modes can reference configured enabled agents.
- Invalid mode sequences fail before launching an agent.
- Existing tests and focused config/referee tests pass.
