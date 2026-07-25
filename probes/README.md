# Manual compatibility probes

This directory contains tracked, manually invoked interoperability harnesses
for behavior owned by external clients or protocols.

Probes are not product code and are not discovered by
`./agent_collab_dev.sh test` or `./agent_collab_dev.sh integration-test`.
Their READMEs state any credential, process, port, and cleanup requirements.
Generated logs and client-local configuration remain untracked; durable
findings belong in the matching task document.

- [`mcp_tasks/`](mcp_tasks/) — Streamable HTTP probes for MCP task-augmented
  requests and the newer Tasks extension.
- [`bubblewrap_codex/`](bubblewrap_codex/) — Linux Bubblewrap probe that runs
  Codex CLI with its own sandbox bypassed, an OS-enforced read-only workspace,
  and read-only-real-state versus writable-ephemeral-state comparisons.
- [`bubblewrap_claude/`](bubblewrap_claude/) — Linux Bubblewrap probe that runs
  Claude CLI without approval prompts or its native sandbox, keeps the
  workspace read-only, and mounts the complete Claude state root writable.
