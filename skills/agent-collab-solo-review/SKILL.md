---
name: agent-collab-solo-review
description: Run one read-only review of the current diff through agent-collab (one provider turn). Use for a "second opinion", "cross-vendor review", or "have another model review my diff" when one independent reviewer is enough.
---

# Agent Collab Solo Review

Get one independent model to review the current diff, validate its claims against
the code, and report only confirmed high- or medium-severity findings.

## Preflight

1. Resolve the project to an absolute workdir.
2. Verify that the `agent_collab_*` MCP tools are connected and call
   `agent_collab_describe_options` for that workdir. If the tools or daemon are
   unavailable, stop and print this remediation:

   ```text
   agent-collab daemon start
   claude mcp add agent-collab -- agent-collab mcp

   For Codex, add this to ~/.codex/config.toml:
   [mcp_servers.agent_collab]
   command = "agent-collab"
   args = ["mcp"]
   startup_timeout_sec = 10
   tool_timeout_sec = 60
   enabled = true

   For Antigravity, merge this into ~/.gemini/config/mcp_config.json:
   {
     "mcpServers": {
       "agent-collab": {
         "command": "agent-collab",
         "args": ["mcp"]
       }
     }
   }

   For Grok, add this to ~/.grok/config.toml:
   [mcp_servers.agent-collab]
   command = "agent-collab"
   args = ["mcp"]
   ```

3. Fetch `agent_collab_guidance` with `topic: "review-recipe"`. Follow that
   canonical recipe for scope, prompt construction, polling, and triage. If the
   topic is unavailable, tell the user to upgrade agent-collab with
   `./agent_collab.sh install` and restart the daemon.

## Select and confirm

Use only a start-eligible single-member workflow returned by discovery. Compare
the underlying configured model, not merely the agent or backend name, with the
host model. Antigravity can run Claude models.

- Honor a reviewer model or backend named by the user.
- If the reviewer model is unclear, show the eligible workflow's configured
  model plus schema-allowed model overrides and ask the user to choose. Ask for
  a backend only when the chosen model maps to more than one eligible backend.
- If the host model is unknown, ask for it before claiming the review is
  cross-model.
- Never silently choose the cheapest or strongest reviewer.
- If no eligible single-member workflow matches the choice, explain which
  user-config workflow is missing; do not substitute another model.

Before starting the paid review, show one concise confirmation containing the
workflow, agent id, underlying model, canonical backend, and effective options.
Identify which options are configured defaults and which are overrides. Ask
whether to proceed; do not call `agent_collab_start` without explicit
confirmation. Do not require the user to choose each minor default.

## Run and report

Build the exact scope and prompt from the review recipe. Start with
`interactive: false`, watch the session with the bounded cursor loop, and wait
for terminal status even when an event batch is empty.

Treat the reviewer as advisory. Open every cited location and reproduce the
failure path against the real code before surfacing it. Drop unresolvable or
unconfirmed claims and never auto-apply a reviewer's proposed edit.

Report:

1. the reviewed base and changed-file scope,
2. confirmed findings ordered high before medium, each prefixed with
   `[<session_id> <canonical_backend>]`, and
3. `No confirmed high- or medium-severity findings.` when none remain.
