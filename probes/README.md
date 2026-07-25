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
- [`bubblewrap_antigravity/`](bubblewrap_antigravity/) — Linux Bubblewrap
  probe that runs Antigravity CLI with approvals bypassed and its terminal
  sandbox disabled, while comparing writable and read-only complete
  `~/.gemini` state.
- [`bubblewrap_grok/`](bubblewrap_grok/) — Linux Bubblewrap probe that runs the
  configured Grok Build CLI with approval bypass and native sandbox profile
  `off`, while comparing writable and read-only complete `$GROK_HOME` state.
- [`bubblewrap_antigravity_sdk/`](bubblewrap_antigravity_sdk/) — Linux
  execution-ownership probe showing that a complete Antigravity SDK worker and
  its `localharness` child can be contained, while the current in-daemon
  backend remains unsandboxed.
- [`bubblewrap_xai_sdk/`](bubblewrap_xai_sdk/) — Linux structural probe showing
  that the current xAI SDK backend constructs a model-only request with no
  local tool-execution surface or writable provider-state root.
