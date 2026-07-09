# agent-collab design docs

This folder tracks the planned move from the current one-shot CLI/MCP prototype to a session-oriented collaboration service that humans and agents can both join.

Start here:

- [Agent entrypoint](../AGENTS.md)
- [Implementation notes](implementation-notes.md)
- [Daemon architecture](daemon-architecture.md)
- [Agent configuration](agent-configuration.md)
- [Runtime layout](runtime-layout.md)
- [MCP guidance](mcp-guidance.md)
- [Development notes](development.md)

Task folders:

- [Open tasks](tasks_open/)
- [Closed tasks](tasks_closed/)

The current implementation already has the core event model, runners, referee loop, log writing, foreground server, global daemon lifecycle with per-session workdirs, a persistent session index, workflow-based orchestration, centralized config migrations, effective session settings confirmations, CLI client commands, interactive TUI, live referee input, stdio MCP adapter, Streamable HTTP MCP endpoint, typed validated start options, pluggable agent backends (provider `type` separate from execution `backend`, with the Antigravity provider on both a `cli` and an extras-gated `sdk` backend, live backend health, and honest per-session capability flags), and MCP usage guidance. Remaining architecture work is tracked in the open task folder.

Backends are standalone `<provider>_<backend>` packages with colocated option
manifests and READMEs. Hermetic and credentialed integration tests are separate
top-level suites.
