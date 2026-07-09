# Stage 5.1: Backend packages and hermetic/integration test split

## Status

Implemented. This stage also intentionally replaced the undeployed provider-wide
start-option fields with one backend-qualified contract; no compatibility shim
was retained.

## Delivered architecture

Every provider/backend pair is a peer package:

```text
agent_collab/backends/
  common/
  claude_cli/
  claude_sdk/
  codex_cli/
  codex_sdk/
  antigravity_cli/
  antigravity_sdk/
```

Each backend package owns:

- `backend.py` — normalization, execution construction, settings, and preview;
- `options.toml` — accepted fields, types, hard limits, defaults, and whether CLI
  inference is supported;
- `README.md` — requirements, authentication, mappings, event fidelity,
  identity, capabilities, security, limitations, and test commands;
- parser code where the backend consumes a CLI stream.

SDK runner/event mapping remains colocated in its backend module so the whole
implementation can be understood within one package. SDK imports remain lazy.
Backends do not import one another.

The registry has one flat `_BUILTIN_BACKENDS` list and imports each package's
`build()` factory. Adding a backend for an existing provider requires its package,
manifest, README, tests, and one list entry.

## Option ownership and request contract

`backend_contract.load_option_schema()` validates each manifest into
`OptionSpec` objects. Cross-field transformations that cannot be expressed as
field metadata remain pure Python rules. Per-agent config may narrow allowed
values or override defaults; it cannot expand the backend contract.

The public start request contains only a generic backend-qualified map:

```json
{
  "backend_options": {
    "claude_cli": {"model": "opus", "thinking_level": "high"},
    "codex_sdk": {"model": "gpt-5.6-sol", "sandbox": "workspace-write"}
  }
}
```

The old `claude_options`, `codex_options`, and `antigravity_options` fields were
removed from CLI, REST, MCP, daemon, referee, TUI payloads, tests, and current
documentation. This is an intentional clean break because the application was
not deployed.

The selected `(agent.type, backend_id)` maps to the canonical
`<provider>_<backend>` key. Unknown entries and entries not selected by the
workflow are rejected. Exact per-agent normalized options are carried to runner
construction.

## CLI ownership

Provider parsers moved out of `events.py`; that module now contains only the
provider-neutral event model and JSON utility. CLI backends own inference,
argument mapping, workdir flags, command preview, and subprocess construction.
Shared flag and JSONL heuristics are stateless helpers under `backends/common/`.

`SubprocessRunner` receives a backend-supplied command builder instead of
branching on provider type. Dry-run resolves the selected backend: CLI backends
show argv, while SDK backends emit an in-process backend summary.

## Test split

```text
tests/                         # hermetic only
  backends/                    # mirrors backend packages
integration_tests/             # credentialed live calls only
  backends/                    # all six pairs
```

`./agent_collab.sh test` runs `unittest discover -s tests -t .` and cannot
discover `integration_tests/`. The former live smoke module was removed from the
hermetic tree.

`./agent_collab.sh integration-test [PROVIDER] [BACKEND] [--strict]` runs only
the live package. Environment selection is also supported through
`AGENT_COLLAB_IT_PROVIDERS` and `AGENT_COLLAB_IT_BACKENDS`. Model overrides use
`AGENT_COLLAB_IT_<PROVIDER>_MODEL`.

Exit codes:

- `0`: selected tests passed; ordinary missing/unselected skips are allowed;
- `1`: behavioral assertion or runtime error;
- `2`: strict mode and an explicitly selected provider was unavailable because
  a dependency or definite credential check was missing.

Credential status `unknown` is attempted rather than automatically skipped,
because Claude and Codex local sign-in cannot be verified side-effect-free.
Every live turn uses a disposable workspace and isolated `AGENT_COLLAB_HOME`.

## Packaging and documentation

Wheel package data includes every backend `README.md` and `options.toml`.
Development, implementation, configuration, MCP guidance, architecture, root
README, and agent entrypoint documentation describe the new contract and test
split.

## Verification

```bash
python3 -m compileall -q agent_collab integration_tests tests
./agent_collab.sh test
python3 -m integration_tests antigravity sdk --strict  # exit 2 when wheel absent
python3 -c "from agent_collab import backends; print(backends.registered_backend_names())"
```

The hermetic suite passes without credentials or SDK wheels. Live model calls
remain opt-in and must be recorded separately on a credentialed machine.
