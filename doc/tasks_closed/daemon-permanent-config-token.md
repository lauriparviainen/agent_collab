# Daemon permanent token in user config

**Status:** Closed — implemented, verified, and shipped in 0.4.0 (2026-07-12).
The open questions below were resolved as: hard error when generating into a
permissive config, warning when loading one; no minted-token transition
handling (both sides shipped together); `config init` generates the token and
prints a credential notice.

**Created:** 2026-07-11

**Issue:** [#8](https://github.com/lauriparviainen/agent_collab/issues/8)
(milestone 0.4.0)

**Related:** [runtime-layout.md](../runtime-layout.md) (current token layout) and
[daemon-architecture.md](../daemon-architecture.md) (auth model).

## Context

The daemon currently mints a fresh bearer token for every daemon lifetime and
stores it at `~/.agent-collab/data/daemon/token`. Local clients (CLI, stdio
MCP adapter, supervisor readiness probe) read that file automatically, so
rotation is invisible on the daemon's own machine.

Rotation breaks any client that cannot read the file: a directly-registered
HTTP MCP client must be reconfigured after every daemon restart, and the
planned David AI daemon linking needs a credential that stays valid across
restarts on a different machine. An env-var token only helps clients in the
same environment as the daemon.

## Goal

Replace the per-lifetime minted token with one permanent token stored in the
user config:

```toml
# ~/.agent-collab/config.toml
[daemon]
token = "<generated once>"
```

- The daemon reads `[daemon].token` from the user config at startup and
  requires it as the bearer token on every route except `GET /health`.
- If the user config has no token (or no config file exists), the daemon
  generates one (`secrets.token_urlsafe(32)`) at startup and persists it into
  the user config before accepting requests. Generation happens once; every
  later start reuses the stored value.
- The rotating token and the `data/daemon/token` file are removed. Local
  clients read the token from the user config instead.

## Plan

1. **Config schema.** Add a `[daemon]` section with `token` (string) to the
   user-config schema. Accept it **only** from `$AGENT_COLLAB_HOME/config.toml`,
   like `[backends.*]` policy: a project-config copy is stripped with a
   migration warning so a shared repo can never inject or read daemon
   credentials. Bump `schema_version` only if the migration machinery requires
   it; an additive optional section should not.
2. **Startup path.** On daemon/server start: load user config; if
   `[daemon].token` is missing, generate and persist it (create the config
   file with `render_user_config()` content plus the token when absent; append
   a `[daemon]` section when the file exists without one — never rewrite or
   reformat existing user content). Write with owner-only permissions (0600)
   and never log the value.
3. **Auth check.** `AgentCollabHttpServer` keeps the single-token
   `hmac.compare_digest` check; the token value now comes from config instead
   of `mint_auth_token`. `GET /health` stays open; `/mcp` origin validation is
   unchanged.
4. **Client resolution.** CLI, stdio MCP adapter, and the daemon supervisor
   readiness probe resolve the token as: `AGENT_COLLAB_TOKEN` env override
   first, then `[daemon].token` from the user config. Remove the
   `data/daemon/token` file read and the minting path.
5. **Cleanup.** Stop writing `data/daemon/token`; delete a stale file on
   startup if present. Remove `mint_auth_token`.
6. **Docs.** Update [runtime-layout.md](../runtime-layout.md) (layout tree,
   token semantics, manual rotation), [daemon-architecture.md](../daemon-architecture.md)
   (auth model, remote-client guidance), [mcp-guidance.md](../../agent_collab/mcp-guidance.md)
   and the README security bullet if they reference the rotating token.

## Decisions

- **Permanent over rotating.** A rotating token cannot serve clients that
  cannot read the local file; the primary consumer (David AI daemon linking,
  eventually multiple linked daemons) needs a stable credential. Accepted
  trade-off: a leaked token stays valid until manually rotated.
- **User config over a separate secret file.** One file to manage and back
  up; `config init` and auto-generation keep it zero-setup. Accepted
  trade-off: the user config becomes a secret-bearing file, so it must be
  owner-only and must never be committed or shared — this is why a project
  config `[daemon]` section is rejected outright.
- **Manual rotation.** Rotating is: edit or delete the `token` line, restart
  the daemon (a deleted token is regenerated). No automatic rotation.
- **Env override stays.** `AGENT_COLLAB_TOKEN` continues to win for clients,
  which keeps remote/CI callers configurable without touching config files.

## Verification

- Hermetic tests: token generated and persisted on first start (config file
  created 0600 / section appended without reformatting); reused unchanged on
  restart; auth accepts the config token and rejects wrong/missing tokens;
  `GET /health` stays open; project-config `[daemon]` section is stripped
  with a warning; `AGENT_COLLAB_TOKEN` overrides for clients; stale
  `data/daemon/token` is removed; existing user-config content and comments
  survive token insertion.
- Existing auth, supervisor-readiness, and MCP tests updated from minted to
  config tokens; tests keep isolating `AGENT_COLLAB_HOME`.
- Manual: daemon restart keeps a directly-registered HTTP MCP client working
  without reconfiguration.

## Open Questions

- Should a group/world-readable user config holding a token be a hard startup
  error or a logged warning? (Leaning: warning plus refusing to *generate*
  into an insecure file; existing-file behavior decided at implementation.)
- Does the supervisor readiness probe need any transition handling for a
  daemon started with the old minted token while a new client already reads
  config? (Likely no: both sides ship in the same release.)
- Should `agent-collab config init` print a reminder that the file now holds
  a credential?
