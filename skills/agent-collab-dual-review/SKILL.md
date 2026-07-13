---
name: agent-collab-dual-review
description: Run two independent read-only reviews of the current diff in parallel through agent-collab, then reconcile them (two provider turns plus local adjudication). Use for a "dual review", "two-model review", or "compare reviewers".
---

# Agent Collab Dual Review

Get two independent model reviews over one frozen diff, validate their claims,
and reconcile agreements and disagreements by reading the code.

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
   canonical recipe for scope, prompt construction, parallel attribution,
   polling, reconciliation, and triage. If the topic is unavailable, tell the
   user to upgrade agent-collab with `./agent_collab.sh install` and restart the
   daemon.

## Select and confirm

Use only a start-eligible two-member `parallel` workflow returned by discovery.
Compare underlying configured models, not merely agent or backend names, with
each other and the host model. Antigravity can run Claude models.

- Honor reviewer models or backends named by the user.
- If either reviewer model is unclear, show each eligible workflow member's
  configured model plus schema-allowed model overrides and ask the user to
  choose both. Ask for a backend only when a chosen model maps to more than one
  eligible backend.
- If the host model is unknown, ask for it before claiming the review is
  cross-model.
- Never silently replace a choice or infer diversity from provider names.
- If no eligible parallel workflow matches both choices, explain which
  user-config workflow is missing; do not run two client-managed sessions.

Before starting the paid reviews, show one concise confirmation containing the
workflow, both agent ids, underlying models, canonical backends, and effective
options. Identify configured defaults and overrides. State that this spends two
provider turns. Ask whether to proceed; do not call `agent_collab_start` without
explicit confirmation. Do not require the user to choose each minor default.

## Run and reconcile

Build one exact scope and prompt from the review recipe. Start the parallel
workflow once with `interactive: false`; the daemon freezes one prompt, runs
both members concurrently, and merges their attributed events into one cursor
stream. Watch that one session until it is terminal. Key member output by
`agent_id` and map each member to its canonical backend from discovery/start
settings.

Treat both reviewers as advisory. Open every cited location and reproduce the
failure path against the real code before surfacing it. Drop unresolvable or
unconfirmed claims and never auto-apply proposed edits.

Reconcile only after terminal status:

- `Agreement`: both reviewers cite the same or overlapping location and the
  same concrete failure scenario. Treat this as higher confidence, not proof.
- `Disagreement`: reviewers conflict, or only one raises the scenario. Decide
  by inspecting the code and tests, never by majority vote.

Report the reviewed base and changed-file scope, then confirmed findings ordered
high before medium. Label each agreement or disagreement and prefix every
reviewer-attributed finding with `[<session_id> <canonical_backend>]`. End with
`No confirmed high- or medium-severity findings.` when none remain.
