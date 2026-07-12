# Security policy

`agent-collab` is a single-maintainer prototype. Reports are read and taken
seriously, but there is no security team, no response-time guarantee, and no
long-term support for old releases. Only the latest release receives fixes.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability
reporting:

<https://github.com/lauriparviainen/agent_collab/security/advisories/new>

Please do not open a public issue for a security problem. Include the exact
command or request that demonstrates the issue and what you observed.

## What counts as a finding

The trust model is local and small:

- The daemon binds to loopback by default and authenticates requests with a
  bearer token generated into the user's `~/.agent-collab/config.toml`.
- The agents a session launches run with the invoking user's local
  permissions, in the working directory the session was given.
- Session state and transcripts live under `~/.agent-collab/`.

Anything that breaks those boundaries is a finding: reaching the daemon
without the token, leaking the token, escaping the session working directory,
or the daemon deleting or writing files outside its managed state. Prompts
and repository content being sent to the configured model providers is
documented behavior, not a vulnerability.
