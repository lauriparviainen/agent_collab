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
  permissions. The session `workdir` selects project config and is the default
  process cwd; it is not an operating-system sandbox or filesystem boundary.
- Execution-relevant agent settings are accepted only from built-in and global
  user config. Project config may rename known agents and compose workflows
  from agents already enabled globally, but cannot change commands,
  environment, cwd, backend, options, type, enablement, or timeouts.
- Session state and transcripts live under `~/.agent-collab/`.

Anything that breaks those boundaries is a finding: reaching the daemon
without the token, leaking the token, project config influencing agent
execution fields, or the daemon deleting or writing files outside its managed
state. Agents accessing paths outside `workdir` according to their configured
provider permissions is expected behavior, not a workdir escape. Prompts and
repository content being sent to configured model providers is also documented
behavior.
