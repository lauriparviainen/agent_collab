# agent-collab design docs

Design and reference documentation for agent-collab. [README.md](../README.md)
is the user-facing overview; these documents hold the detail.

- [Agent entrypoint](../AGENTS.md)
- [Implementation notes](implementation-notes.md) — current runtime, backend,
  and handoff notes
- [Daemon architecture](daemon-architecture.md) — server, sessions, CLI, and
  MCP design
- [Agent configuration](agent-configuration.md) — agents, workflows, backends,
  and typed start options
- [Runtime layout](runtime-layout.md) — global runtime files and config
  precedence
- [MCP guidance](mcp-guidance.md) — usage guidance served to MCP agents
- [Generated daemon REST API](daemon_api_doc/http-api.md)
- [Development notes](development.md) — commands, tests, and coding constraints

Task folders record planning and implementation history. They are not
maintained documentation and may describe superseded designs:

- [Open tasks](tasks_open/)
- [Closed tasks](tasks_closed/)

The current implementation state is summarized in
[implementation-notes.md](implementation-notes.md); remaining architecture work
is tracked in the open task folder.
