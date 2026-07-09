# Antigravity CLI backend

Registered as `antigravity_cli` (`type="antigravity"`, `backend="cli"`). It runs `agy` print mode. Since output is plain text, fidelity is message-only.

## Selection and requirements

Select with `backend="cli"`; `agy` must be on PATH. The health probe checks the binary, version, and local Antigravity OAuth/account files. Agent-collab never manages those credentials. This opt-in backend blocks start when definitely unavailable.

## Options

[`options.toml`](options.toml) is authoritative. `model` and `mode` map to flags before print mode and may be inferred from argv. The resolved workdir is supplied with `--add-dir` unless already configured.

## Events and identity

Every non-empty stdout line becomes `antigravity/message`. Tool structure and provider conversation identity cannot be recovered from print mode.

## Capabilities and security

`resume`, `interrupt`, and `tool_gate` are false. Execution is confined to the resolved cwd/add-dir configuration.

## Testing

Hermetic: `./agent_collab.sh test -k antigravity_cli`. Live: `./agent_collab.sh integration-test antigravity cli`.
