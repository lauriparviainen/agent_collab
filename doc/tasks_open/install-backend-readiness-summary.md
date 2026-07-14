# Install-time configured backend readiness summary

**Status:** Implemented and verified 2026-07-14. A cross-model review
(Gemini 3.1 Pro via agent-collab) confirmed one gap — the no-user-config
collector path was untested — closed by
`test_defaults_to_loading_user_config_without_creating_one`.

**Created:** 2026-07-14.

**Issue:** [#23](https://github.com/lauriparviainen/agent_collab/issues/23)

## Context

`./agent_collab.sh install` is also the upgrade command. It currently:

1. creates or reuses the durable virtual environment;
2. installs the checkout and every provider SDK extra;
3. exposes the `agent-collab` command;
4. migrates and validates the global user config; and
5. restarts the daemon only when it was running before installation.

The config step proves that the file is current and structurally valid, but it
does not tell the user whether the effective backends selected by globally
enabled agents can run in the installed environment. A successful install can
therefore end with, for example, `claude` or `codex` absent from `PATH`, an SDK
module unavailable to the durable interpreter, or a selected backend disabled
by user policy. The user discovers that state later through `agent-collab
options` or a session start.

The backend discovery implementation already owns side-effect-free dependency,
version, and best-effort credential probes. Its design deliberately avoided an
eager daemon-startup summary because startup does not know the eventual session
workdir and optional probes must not delay or prevent daemon startup. An
install-time summary has the same workdir limitation, but it has a useful,
narrower scope: show the **global execution configuration** that installation
has just migrated, and whether the effective backend for each globally enabled
agent is present in the environment where it was checked.

This is advisory evidence. A successful dependency probe is not proof that a
provider turn, account entitlement, selected model, network, or service will
succeed. Session start remains authoritative and the first provider turn
remains the final runtime test.

**Remediation-hint wording, fixed 2026-07-14.** The name `agent-collab` on
PyPI belongs to an unrelated third-party package, so remediation text must
never suggest `pip install agent-collab` or its extras. The backend
`INSTALL_HINT` strings and the README's extras block were corrected to name
the actual SDK distributions (for example `pip install claude-agent-sdk`) or
checkout-relative installs (`pip install '.[claude]'`), with re-running
`./agent_collab.sh install` as the durable-install remediation. The summary
implemented from this design must reuse those backend-owned hints rather than
composing new package names.

## Findings

- Installation already has a natural point for this check: after package
  installation and config migration, and after the optional daemon restart.
- The installer process may be running under a bootstrap/system Python. SDK
  readiness must therefore be checked from the newly installed durable virtual
  environment, not by importing backends in the original installer process.
- When a daemon was restarted, its environment is the best evidence for future
  daemon-owned turns. When no daemon is running, the installer can check from
  the durable interpreter with the installer's environment, but must label that
  scope and must not start the daemon merely to improve the check.
- Project config may compose or rename agents and workflows, but it cannot
  change execution-relevant agent settings. The install summary can safely
  describe global effective agent/backend selection; it must not claim that an
  arbitrary project's workflows are start-eligible.
- Backend policy enablement and agent enablement are different facts. The
  concise summary should center on enabled agents and their selected effective
  backend. Registered or policy-enabled alternatives that no agent selects are
  catalog detail, not install blockers.
- Missing provider commands, SDKs, or credential evidence do not mean package
  installation failed. They should produce actionable warnings while install
  still exits successfully. Invalid or unmigratable config remains fatal.
- The result must reuse the backend-owned health and assessment facts. The
  installer must not grow a separate collection of `which`, import, credential,
  or version rules that can disagree with `/options` and session start.

## Goal

After every install or upgrade, print a concise and honest snapshot answering:

- which global config was evaluated;
- which agents are enabled or disabled;
- which effective backend each enabled agent selects and why;
- whether the selected CLI command or SDK dependency was found;
- what version was observed, when available;
- whether credentials were found, missing, unknown, or not checked;
- which selected backends need attention and how to remediate them; and
- which environment produced the evidence.

The summary should make the common result immediately understandable without
requiring the user to start a paid provider turn.

## Non-goals

- Do not make a model or other paid provider call.
- Do not prove account entitlement, model availability, network access, or a
  successful end-to-end turn.
- Do not evaluate or claim readiness for every possible project workdir.
- Do not start a daemon that was stopped before installation.
- Do not probe every registered alternative backend merely because it exists.
- Do not print tokens, environment values, credential-file contents, prompts,
  transcript data, or raw backend configuration.
- Do not change backend gating or session-start semantics.
- Do not make missing optional providers fatal to installation.

## Proposed behavior

### Scope

Load built-in defaults plus the global user config through `load_user_config`.
If no user config exists yet, report that built-in defaults were evaluated and
preserve the current behavior in which the daemon creates the user config on
first start. Do not create a config solely for the summary.

For each configured agent:

- show disabled agents as disabled without probing them;
- for enabled real agents, resolve the same effective backend that session
  start would select from global config;
- report when that canonical backend is disabled by user policy; and
- otherwise run or reuse one fresh, side-effect-free health probe for the
  selected canonical backend.

Deduplicate probes when multiple agents select the same canonical backend.
Unselected backends are omitted from the default install summary.

The summary may state that project config was not evaluated and point users at
`agent-collab options --workdir PROJECT --fresh` for exact project workflow
eligibility. It must not render global workflow readiness as though it applied
to every project.

### Probe environment and ordering

The install sequence should become:

1. snapshot whether and how the daemon is running;
2. install into the durable virtual environment;
3. migrate and validate global user config;
4. restart the daemon if and only if it was previously running;
5. collect and render configured backend readiness; and
6. print the final install result.

When the daemon was restarted successfully, prefer discovery facts produced by
that daemon and identify the probe source as the restarted daemon. Project-only
composition from the daemon's workdir must not leak into the global summary.

When the daemon is stopped, execute the local readiness helper under the
durable virtual environment's Python and identify the source as the installer
environment. This path must leave the daemon stopped.

The exact internal boundary needs implementation review. A small structured
helper invoked under the durable interpreter is preferable to parsing
human-oriented output. If daemon and local paths need separate adapters, both
must feed one common summary model and renderer.

### Output

Follow the repository CLI-output convention: one progress line begins the
step, one success or warning line ends it, and the details form a stable aligned
status block. For example:

```text
▶ Checking configured backend readiness
! Warning: 1 of 2 enabled agents needs attention
  scope         global user config
  config        built-in defaults + user config
  probe source  installed environment
  agent        backend          dependency      credentials  version
  -----------  ---------------  --------------  -----------  -------
  claude       claude_cli       claude found    not checked  2.x
  codex        codex_cli        codex missing   not checked  —
  antigravity  antigravity_cli  agent disabled  —            —
  xai          xai_cli          agent disabled  —            —
  agent  remediation
  -----  --------------------------------------------------------
  codex  Install codex and ensure it is available on the daemon PATH.
```

A fully healthy dependency check should end with `✓`; unavailable selected
backends should end with `! Warning:` because installation itself succeeded.
Use `✗` only for status detail that follows the marker convention; fatal
installation failures retain the grep-friendly `Error:` prefix.

Command presence, the command name, and version are sufficient for the default
contract. If the resolved executable path is added, keep it on the local human
surface, abbreviate the home directory, and do not automatically add
machine-local paths to REST or MCP responses.

Credential reporting must retain the existing distinctions:

- `ok` means local evidence was found, not that a turn was made;
- `missing` means the probe can establish absence;
- `unknown` means the probe cannot decide; and
- `not checked` means the backend deliberately has no credential probe.

### Failure semantics

- Config migration or validation errors remain fatal, as today.
- A selected backend that is disabled, unavailable, or missing credentials is
  a non-fatal install warning with remediation.
- A bounded probe timeout or indeterminate result is `unknown`, not success or
  failure.
- An unexpected summary/projection failure should warn that readiness could not
  be checked and provide a repeat command; it should not undo an otherwise
  successful package installation or daemon restart.
- The final install line should distinguish a clean result from a result with
  setup warnings without returning non-zero solely for those warnings.

## Implementation notes

- Keep all behavior in Python. `agent_collab.sh` remains a thin dispatcher.
- Refactor the existing backend fact construction only as needed so install,
  `/options`, and session start share backend-owned resolution and probes.
- Do not invoke the current `agent-collab options` human output and scrape it.
  It is daemon-backed, workdir-scoped, and not a stable machine interface.
- A selected-only projection is preferable to `describe_options(...,
  health_refresh="fresh")` if the latter probes every registered backend. The
  install path should pay only for checks it displays.
- Preserve short probe timeouts and deduplicate canonical backend checks.
- The renderer should consume structured facts and remain independently
  testable. A future `agent-collab doctor` command can reuse it, but adding that
  public command is not required to land the install summary.
- Do not persist the snapshot as durable truth. Command installation, PATH,
  credentials, and provider state can change immediately afterward.

## Implementation record

Implemented 2026-07-14 with these decisions:

- Use a small standard-library table formatter in `cli_output.py`; do not add
  `rich`, `tabulate`, `prettytable`, or another runtime dependency for a fixed
  installer table.
- Invoke `agent_collab.install_readiness` as a structured JSON helper through
  the newly installed durable interpreter. The parent installer renders the
  human output and never scrapes another human-oriented command.
- Probe only effective backends of globally enabled agents. Disabled agents
  remain visible in the table but are not probed; identical effective probes
  are deduplicated and distinct probes run with bounded concurrency.
- Add backend-owned `probe_for_agent` methods to CLI backends so configured
  command overrides are checked rather than assuming the canonical executable
  name. SDK backends retain their existing package-owned probes.
- Reuse the existing shared backend assessment function for readiness and
  remediation. Backend-owned remediation remains the only source of provider
  installation wording.
- Label the result `installed environment`. The implementation does not start
  a stopped daemon, does not add a daemon/API route solely for install, and does
  not claim that local evidence is the daemon's PATH when systemd may differ.
- Keep disabled agents as individual table rows, omit resolved executable paths,
  omit workflows, and defer a public `doctor` command.
- Treat missing, disabled, or indeterminate selected backends as non-fatal setup
  warnings. Invalid config remains fatal and a failed readiness helper produces
  a generic non-fatal recheck hint without exposing captured stderr.

## Verification

Add hermetic coverage for at least:

- current user config with both built-in agents enabled and both CLI commands
  found;
- one selected CLI command missing from `PATH`;
- an enabled agent explicitly selecting an SDK backend;
- a selected backend disabled by global backend policy;
- disabled agents being shown without probing their backends;
- two enabled agents sharing a canonical backend and causing one probe;
- no user config, using built-in defaults without creating a config;
- config migration occurring before the summary;
- checks executing under the durable interpreter rather than the bootstrap
  interpreter;
- a previously running daemon being restarted before its facts are queried;
- a stopped daemon remaining stopped while local checks run;
- probe timeout, indeterminate health, and unexpected projection failure;
- warnings remaining non-fatal while invalid config remains fatal;
- output markers and aligned summary formatting; and
- absence of tokens, environment values, raw credential material, and unsafe
  configuration values in output.

Run:

```bash
python3 -m unittest tests.test_user_install tests.test_options tests.test_cli_help
./agent_collab_dev.sh test
./agent_collab_dev.sh build --check
```

An isolated manual smoke should cover one environment with both default CLIs
present and one with a deliberately restricted `PATH`. It must not make a model
call.

## Verification record

Verified 2026-07-14:

- focused readiness, installer, option, and CLI-backend suite: 90 tests passed;
- complete local gate: Ruff lint, Ruff formatting, and 839 hermetic tests
  passed;
- `./agent_collab_dev.sh build --check` verified the generated REST artifacts;
  and
- an isolated no-user-config smoke used side-effect-free probes, made no model
  call, found the configured `claude` and `codex` commands, displayed their
  versions in the table, and left disabled agents unprobed.

The public `doctor` command, executable-path display, daemon-versus-installer
environment comparison, and global workflow projection remain intentionally
deferred rather than open requirements for issue #23.
