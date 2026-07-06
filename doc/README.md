# agent-collab design docs

This folder tracks the planned move from the current one-shot CLI/MCP prototype to a session-oriented collaboration service that humans and agents can both join.

Start here:

- [Agent handoff notes](../AGENTS.md)
- [Daemon architecture](daemon-architecture.md)
- [Agent configuration](agent-configuration.md)
- [Runtime layout](runtime-layout.md)
- [Development notes](development.md)

Implementation stages:

- [Stage 4.5: TUI watch](tasks_open/stage-4.5-tui-watch.md)
- [Stage 4.8: Global runtime and config migrations](tasks_open/stage-4.8-global-runtime-and-config-migrations.md)
- [Stage 5: Hardening and operations](tasks_open/stage-5-hardening.md)

Completed stages:

- [Stage 1: Watch and attach to logs](tasks_closed/stage-1-watch.md)
- [Stage 1.5: Agent configuration](tasks_closed/stage-1.5-agent-config.md)
- [Stage 2: Local daemon and session manager](tasks_closed/stage-2-daemon-api.md)
- [Stage 3: CLI client commands](tasks_closed/stage-3-cli-client.md)
- [Stage 4: MCP daemon adapter](tasks_closed/stage-4-mcp-daemon-adapter.md)
- [Stage 4.25: Foreground Streamable HTTP server](tasks_closed/stage-4.25-foreground-streamable-http-server.md)
- [Stage 4.75: Daemonize server and typed session options](tasks_closed/stage-4.75-daemonize-and-session-options.md)

Task folders:

- [Open tasks](tasks_open/)
- [Closed tasks](tasks_closed/)

The current implementation already has the core event model, runners, referee loop, log writing, foreground server, project-local daemon lifecycle, CLI client commands, stdio MCP adapter, Streamable HTTP MCP endpoint, and typed validated start options for agents. Open architecture work includes the additive TUI watch mode and moving daemon runtime state to a global session registry with per-session workdirs.
