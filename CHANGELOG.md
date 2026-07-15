# Changelog

All notable changes to agent-collab are documented here.

This project follows Semantic Versioning. The package version is declared in
`pyproject.toml` and `agent_collab/__init__.py`; keep it in sync with the latest
released version in this document.

Changelog entries stay concise. Refer to the design docs under `doc/` (indexed
from `AGENTS.md`) and the task documents in `doc/tasks_open/` and
`doc/tasks_closed/` for implementation details instead of expanding this file
into a detailed work log.

## [Unreleased]

## [0.9.1] - 2026-07-15 - Autostart stop fix and MCP token ergonomics

- Make the daemon bearer token available for MCP client setup without hand
  editing `config.toml` (#35). Install now creates the user config with the
  durable token (and adds one to an existing tokenless config) instead of
  deferring it to first daemon start, and a new `agent-collab daemon token`
  command prints it on plain stdout so it composes into client registration,
  e.g. `--header "Authorization: Bearer $(agent-collab daemon token)"`.
- Add a top-level `agent-collab --version` flag that prints the installed
  package version and exits (#36). The version was previously only visible as a
  side effect of install and `daemon status` output.
- Fix `agent-collab daemon autostart enable` aborting and leaving the daemon
  stopped when a manually-started daemon was already running (#34). The stop
  handoff's post-signal wait loops no longer treat a transient
  `IDENTITY_UNKNOWN` — a pid we already attributed and signaled becoming
  momentarily unreadable while it exits — as fatal; they keep polling for exit
  or pid recycle. The terminal post-SIGKILL check now preserves daemon state
  and reports failure for any still-live pid that is not a confirmed recycle,
  so an unreadable-but-alive daemon is never falsely reported as killed.

## [0.9.0] - 2026-07-15 - Member selection and lean discovery

- Shrink discovery to one authoritative backend catalog (#31, protocol_version
  2): drop the compatibility `backends`-by-provider and `provider_groups`
  projections plus the top-level `backend_options` schema copy (configured
  option defaults now overlay the catalog's own `option_schema`), rename
  `canonical_backends` to `backends` and `selected_canonical_backends` to
  `selected_backends`, and remove the redundant per-entry `available`/
  `available_semantics` flags, `probe.checks` copy, and agents' duplicate
  `backend` key. Backends are isolated peers — `claude_cli` and `claude_sdk`
  are different backends, and grouping them by provider misrepresented that —
  so the flat catalog is the only shape. The `discovery` preamble shrinks to
  its four per-response data fields (`protocol_version`, `workdir`,
  `generated_at`, `health_request`) and the static `examples` block is gone —
  invariant contract semantics live in `agent_collab_guidance`, which now also
  documents the mock/dry_run probe exception and the mock smoke-test start.
  MCP tool results also serialize compactly (no indentation) — the consumer is
  a model, not a human. A typical discovery response drops from ~81k to ~28k
  characters.
- Collapse the built-in `solo-<provider>-<cli|sdk>` workflow family (shipped
  in 0.8.1) into one `solo` workflow (`sequence = ["claude_cli"]`): with
  member selection the backend is a slot choice, not a workflow id. Existing
  references to the removed ids fail at start with the available-workflows
  message; a fixed alias is one user-config line, e.g.
  `[workflows.solo-xai-cli] sequence = ["xai_cli"]`. The TUI session list
  gains a `backends` column showing each session's effective members
  (`claude_cli+xai_cli`, reprised slots deduped), since the workflow id alone
  no longer says which backends ran.
- Select workflow members at session start (#21): the additive `members` start
  field maps a workflow slot (its configured member id; `[a, b, a]` stays one
  lead plus one reviewer) to any globally enabled agent, validated with the
  same rules as configured workflows before any session state exists.
  Discovery advertises per-slot eligible members under
  `workflows[].member_selection`, `agent-collab start` gains `--members`, and
  the TUI `/new` wizard asks shape first, then the backends that fill its
  slots, with the configured members preselected so Enter-through reproduces
  the configured workflow. Task document: workflow-member-selection.

## [0.8.4] - 2026-07-15 - Reliable TUI polling

- Preserve every TUI event exactly once when referee posts restart the poller
  or `/stop` races with a final event batch, and ignore late errors from a
  stopped poller (#28).

## [0.8.3] - 2026-07-15 - Read-only backend defaults

- Ship read-only write posture by default for every backend that has a
  permission or sandbox control (#29): `claude_cli`/`claude_sdk`
  `permission_mode = "default"` (with `plan` newly accepted), `codex_cli`/
  `codex_sdk` `sandbox = "read-only"`, and `antigravity_cli` `mode = "plan"`
  instead of the write-enabled `accept-edits`. Writing now needs an explicit
  option override per backend, persona, or session.
- Move all shipped option defaults out of the backend manifests into the
  built-in config's `[backends.<canonical>.options]` tables. They rank below
  flags configured in `args` and below user-config options, so existing
  overrides keep working and overriding one value never drops the rest;
  discovery still shows the effective defaults.
- Add the boolean `antigravity_cli` `sandbox` option mapping to the `agy
  --sandbox` terminal-restriction flag.

## [0.8.2] - 2026-07-14 - Readiness output polish

- Tidy the install readiness output: the agents column appears only when a
  backend has agents beyond its default (personae or renames), blank lines
  separate the summary block, each table, and the final install result, and
  table cells are never truncated.

## [0.8.1] - 2026-07-14 - Backend-first built-in workflows

- Ship one built-in solo workflow per canonical backend (#26):
  `solo-<provider>-<cli|sdk>` for all eight backends, replacing the
  agents-first `solo-claude`/`solo-codex` names. Disabled backends' solo
  workflows stay listed and become start-eligible the moment the backend is
  enabled, so a typical user config needs no `[workflows]` section. The sdk
  backends are declared (disabled) in the built-in defaults, which also means
  a request-level `--backend sdk` override now requires enabling the backend
  first.
- Show a disabled backend's canonical name in `agent-collab options` workflow
  lines instead of the `(mock)` placeholder.

## [0.8.0] - 2026-07-14 - Backend-first configuration

- Make configuration backend-first (#25), config schema 8: `[backends.<canonical>]`
  owns enablement, command, environment, and default options, and every enabled
  backend defines its default agent under the canonical name (`claude_cli`,
  `codex_sdk`, …). Options-only personae nest as
  `[backends.<canonical>.agents.<name>]`; workflows reference backends or
  personae. Top-level `[agents.*]` execution sections are removed — install
  migrates old configs automatically (with a `config.toml.bak` backup) and
  fails with a clear error when a config cannot be expressed in the new
  shape. Workflows referencing a disabled backend now load but are
  start-ineligible instead of failing the whole config.
- Make the install readiness table backend-first (#24): one row per selected
  backend and command identity with its agents listed alongside, disabled
  agents collapsed into one summary line, and counts that speak about
  selected backends instead of enabled agents.
- Report global configured backend readiness after every install/upgrade (#23):
  probe only effective backends of enabled agents from the durable environment,
  honor configured CLI commands, and render dependency, credential, version,
  and remediation facts in aligned tables without making model calls.

## [0.7.1] - 2026-07-14 - Public repository and install-instruction fix

- Stop referring to the unowned PyPI name: `agent-collab` on PyPI is an
  unrelated third-party package, so the README extras block now installs from
  the checkout with an explicit not-on-PyPI warning, and backend install
  hints name the actual SDK distributions or `./agent_collab.sh install`.
- Make the repository public (#14): final content-audit delta and release-gate
  verification on the public HEAD, then private vulnerability reporting,
  secret scanning with push protection, Dependabot alerts, branch and
  release-tag rulesets, and hardened Actions settings.

## [0.7.0] - 2026-07-14 - Cross-model review skills and parallel dual review

- Fix `./agent_collab.sh skills install|uninstall` without a client crashing
  on Python 3.10: argparse there rejects an empty optional positional that
  declares choices, so client validation moved out of the parser.
- List `agent_collab_guidance` and `agent_collab_describe_options` first in
  the MCP tool catalog, and describe session start over the configured agent
  backends instead of only Claude/Codex. Declare the agent-collab MCP server
  as a dependency in the Codex skill metadata and tell users to restart their
  agent after MCP registration changes.
- Fix an unknown workflow name at session start surfacing as a raw HTTP 500 /
  MCP internal error instead of the structured `invalid_start_options` shape
  (#22): the start path now rejects an unknown workflow with a `workflow` field
  error listing the available ids, and wraps other configuration errors from a
  known workflow as a sanitized 400 rather than letting them escape as a 500.
- Add portable solo and dual cross-model review skills (#18), with daemon-served
  diff-scoping, polling, attribution, and triage guidance; Claude and Codex
  plugin metadata; explicit model/backend confirmation before provider calls;
  and explicit managed install/uninstall commands for Claude Code, Codex,
  Antigravity, and Grok. Persist each completed skill destination before the
  next client operation, and include MCP registration remediation for all four
  clients.
- Remove the built-in `compare` workflow: its second turn saw the first answer
  in the transcript, so it was a weaker `cross-review`, and independent
  side-by-side answers are what `dual-review` now provides. User-config
  workflows named `compare` keep working.
- Rework the TUI around one menu language: band headers with column titles on
  the palette, session picker, and `/new` wizard; accent-on-fill rows with the
  selected bar; bottom-aligned short menus; combined picker header with
  right-aligned key hints, aligned columns, and minute-precision timestamps.
  The transcript gutter attributes parallel members (`codex-tool`), the task
  row reads `prompt`, and the hardware cursor parks (and blinks) in the input
  field.
- `/new` offers workflows as a selectable list (↑↓ + Enter toggles ✓, each row
  shows its members, a `continue` row proceeds; typing still works) with no
  preselection, starts one session per selected workflow, and starts parallel
  workflows non-interactively so `dual-review` works from the TUI. Choosing
  the shape first and then its member backends is the follow-up (#21). The
  TUI-side directed input (`/ask`, `#AGENT`) is removed — CLI backends run
  each turn as a fresh one-shot; the daemon `post_message` target routing for
  API callers is unchanged.
- Add daemon-orchestrated parallel review workflows (#19): the built-in
  `dual-review` runs Claude and Codex concurrently over one frozen prompt,
  merges attributed events into one cursor stream, retains per-member terminal
  outcomes, reports degraded groups through a structured stage summary, and
  fails canonically when no reviewer produces an accepted review. Config schema
  7 adds bounded flat `parallel` groups while project workflows remain
  sequence-only.
- Harden public-release repository hygiene (#14) by ignoring local virtual
  environments, environment files, coverage/tool output, OS metadata, and
  common private-key containers. Remove the obsolete hardening umbrella task
  after reconciling its completed and superseded scope.
- Make xAI CLI supervision non-interactive and read-only by default (#17). Safe
  inspection commands no longer stall on approval, and cancelled or otherwise
  unsuccessful Grok terminal reasons are reported as fatal provider errors
  instead of empty successful turns. Expose Grok's separate internal
  model/tool-loop limit as `provider_max_turns`.
- Add the shared explicit backend turn-outcome contract across all eight CLI
  and SDK backends (#17): awaited event sinks, bounded cleanup, deterministic
  `turn-N` records, fail-fast sequential/directed aggregation, monotonic
  session terminals, sanitized structured failures, and coherent outcome views
  on REST/MCP session and event-polling responses. Legacy session records remain
  readable without fabricated outcomes.
- Make direct authenticated Streamable HTTP the preferred MCP registration
  path and expose the stdio fallback as `agent-collab mcp` (#14). Remove the
  separate `agent-collab-mcp` console script and checkout-local README commands
  so the durable install has one stable public executable.
- Reject unknown bare-word commands instead of running them as one-shot
  collaboration tasks (#16): `agent-collab install` (or a typo like
  `statsu`) used to silently launch a full multi-agent workflow against the
  current directory. A deliberate one-word task still runs when any option
  precedes it, and the contract is documented in the root help. Also stop
  re-reading the user config as project config when the session workdir is
  the home directory, which stripped user-only sections with confusing
  warnings (#16).
- Make `./agent_collab.sh install` switchless and the documented upgrade
  command (#15): it migrates the user config to the current schema in place
  (tomlkit-based, comment-preserving, with a `config.toml.bak` backup),
  captures pip output to `~/.agent-collab/install.log`, and restarts the
  daemon when it was running before install. Add a switchless `uninstall`
  that reverses install while keeping config and session data.
- Split developer commands into `agent_collab_dev.sh` and rename `setup` to
  `build` (#15). Shared shell setup lives in `scripts/agent_collab_lib.sh`;
  `agent_collab/project_setup.py` is now `agent_collab/project_build.py`.
- Give install, uninstall, and the daemon lifecycle commands consistent,
  step-by-step CLI output with the marker convention from
  `.claude/skills/cli-scripting/SKILL.md` (#15). `daemon status` now renders
  an aligned summary including the daemon's version, uptime, and running
  session count, and warns when the running daemon is older than the
  installed version.
- Validate session and discovery workdirs, add optional user-global
  `[workdir].restrict_workdir_roots` confinement with missing-or-empty
  unrestricted semantics plus exact-directory exceptions, and
  clarify that workdir is a config root/default cwd rather than a sandbox (#13).
- Prevent project config from changing any execution-relevant agent field or
  defining project-only agents. Project config remains useful for display names
  and workflows over globally enabled agents; ignored fields and unsafe
  workflows produce sanitized start/discovery warnings (#13).

## [0.6.0] - 2026-07-13 - Optional SDK extras and session retention

- Allow selecting the Fable model on the Claude `cli` and `sdk` backends by
  adding `fable` to the `model` option's allowed values, alongside `sonnet` and
  `opus` (`opus` remains the default).
- Make the vendor SDKs per-provider optional dependencies (#12). The base
  install is now SDK-free (the `cli` backends need no SDK); `sdk` backends are
  enabled with the `claude`, `codex`, `antigravity`, `xai`, or `all` extras.
  `./agent_collab.sh install` installs the `all` extra so the durable user
  environment keeps working out of the box, missing-SDK install hints name the
  matching extra, and CI verifies the SDK-free base install.
- `./agent_collab.sh test` now explains how to install the `dev` extra when
  Ruff is missing from the selected environment, instead of failing with a raw
  `No module named ruff` error (#4).
- Add SECURITY.md (private vulnerability reporting, local trust model),
  CONTRIBUTING.md (Apache 2.0 inbound terms, the required
  `./agent_collab.sh test` and `./agent_collab.sh setup --check` gates), a
  basic bug-report issue template, and README links to both documents (#2).
- Add automatic session retention and manual pruning (#5). Terminal sessions
  are kept 30 days by default; a user-config `[sessions]` section (config
  schema 5) changes or disables it, the daemon prunes on startup and every
  `cleanup_interval_hours`, and `agent-collab sessions prune` previews or
  applies the same selection through the new authenticated
  `POST /sessions/prune` API. Deletion is convergent and bounded to managed
  transcripts: live sessions, custom log directories, symlinks, and special
  files are never touched.

## [0.5.0] - 2026-07-12 - Apache 2.0, user install autostart, and code health

- License the project under Apache License 2.0 and publish matching package
  metadata (#1).
- Add an explicit source-checkout user installer that exposes the existing
  `agent-collab` console command outside an activated venv, plus Linux systemd
  user-service registration with coherent daemon lifecycle routing, health,
  logs, safe manual-daemon migration, and reversible autostart (#9).
- Preserve the selected venv interpreter symlink during autostart registration
  so durability checks and generated units use the installed environment
  instead of incorrectly falling through to the system Python (#9).
- Make root CLI help provider-neutral and advertise every public command,
  including the TUI and daemon/session inspection surfaces (#10).
- Ship the MCP guidance document as package data so installed daemons can
  serve `agent_collab_guidance` instead of failing with an internal error
  (#11).
- Attribute renamed agents' verbose provider stderr to the provider type
  instead of `tool`, and log `Event.create` source/type coercion (with
  per-value deduplication) instead of silently relabeling invalid events
  (#6).
- Code health: deduplicate backend boilerplate into `backends/common/`
  helpers, define loopback trust detection once in `agent_collab/net.py`,
  coalesce per-event watcher notifications in the daemon, and make the
  supervisor readiness timeout configurable via
  `AGENT_COLLAB_DAEMON_READY_TIMEOUT` (#6). Per-provider optional
  dependencies are deferred to #12.

## [0.4.0] - 2026-07-12 - Permanent daemon token

- Replace the per-daemon-lifetime minted bearer token with one permanent
  `[daemon].token` in the user config, auto-generated on first daemon start,
  so MCP and remote clients stay authenticated across daemon restarts.
  The token is accepted only from the user config (a project copy is stripped
  with a warning), generation refuses group/world-readable config files, and
  `data/daemon/token` is no longer written (#8).
- Fix `config show` crashing with `'str' object has no attribute 'items'`
  when any agent has configured options; option values now print as
  `option = value` lines (#3).
- Print the previously omitted agent fields in `config show` — the effective
  `backend`, `name`, `cwd`, `timeout`, `env` (key names only, values never
  echoed), and static backend config such as antigravity_sdk's
  vertex/project/location (#7).

## [0.3.0] - 2026-07-11 - Daemon hardening, CI, and project process

- Adopt GitHub issues for discrete task tracking alongside `doc/tasks_open/`
  design documents, with the conventions captured in a project skill
  (`.claude/skills/github-issues/SKILL.md`) linked from `AGENTS.md`.
- Adopt three-part SemVer with annotated `vX.Y.Z` tags, GitHub Releases fed
  from changelog sections, and milestones for version planning; retro-tag
  v0.1.0 and v0.2.0, normalize the declared package version to `0.2.0`, and
  capture the procedure in `.claude/skills/release/SKILL.md`.
- Revise the user-facing documentation for public-release readiness: split
  human (CLI/TUI) and agent (MCP) access paths in the README architecture
  diagram, describe the in-progress David AI daemon-linking integration,
  state the missing-license status, retire stale planning language in the
  design docs, correct backend README live-test commands to canonical
  `integration-test <provider>_<backend>` names, and fix the described
  contents of the tracked project config.
- Honor configured CLI arguments ahead of manifest defaults and use the final
  repeated flag/config occurrence; validate final effective options and expand
  Claude, Codex, Antigravity, and subprocess stderr/failure contract coverage.
- Add least-privilege, SHA-pinned GitHub Actions CI on Python 3.10 and 3.12,
  enforcing Ruff lint/format checks, the hermetic suite, and generated API
  artifact validation; establish and regression-test the repository-wide Ruff
  baseline, including automatic Ruff checks in `./agent_collab.sh test`.
- Capture proven Claude, Codex, and xAI CLI provider-session identities under
  the shared session schema, with trusted attribution and spoof-resistant
  bookkeeping; leave Antigravity CLI identity unset until its wire format
  exposes one.
- Close every SDK turn stream deterministically on completion, error, and
  cancellation without allowing cleanup failures to mask task cancellation.
- Require backend gating and event/session-fidelity metadata at registration,
  rejecting missing or invalid policy fields instead of silently applying
  permissive defaults.
- Prevent REST and MCP 500 responses from exposing internal exception text;
  preserve intentional client-error contracts and JSON-RPC ids/envelopes while
  logging unexpected failure details only on the server side.
- Bound HTTP request bodies to 16 MiB and request headers to 100 fields/64 KiB;
  return structured client errors for oversized, malformed, ambiguous-framing,
  and incomplete requests before they can trigger unbounded daemon allocation.
- Polish the TUI chrome: move the per-agent cluster to the top row as
  `{type}_{backend}: {model}` with the backend always shown, label the context
  line `workdir: <path> (<branch>)`, extend the referee/human band to wrapped
  lines, give the session-picker title a raised band and aligned indent,
  de-duplicate the read-only indicators to one per region, and give all body
  text the chrome's one-column left margin.
- Keep the daemon responsive during session preflight, backend health probes,
  option discovery, restored-event replay, and transcript reads by moving
  blocking work off the asyncio event loop and snapshotting session events
  before worker-thread projection.
- Prevent daemon lifecycle races by verifying stored process identity before
  shutdown signals, rechecking before forced termination, and serializing the
  complete daemon-start transaction with a private cross-process lock.

## [0.2.0] - 2026-07-10 - First-class backends and daemon hardening

- Promote the Claude Code, Codex, and Antigravity SDK integrations to packaged,
  self-describing backends; add backend-qualified configuration, automatic
  supported-Python selection, and a hermetic/live test split. The base install
  now requires Python >= 3.10 and includes the supported SDK dependencies.
- Add xAI as a first-class provider with CLI and SDK `grok-build` backends.
- Add backend discovery, recommendation, availability, health, and remediation
  reporting, including safer subprocess transport and explicit enablement for
  backends that require it.
- Complete the calm TUI refresh with provider brand colors, a stable source
  gutter, layered Escape behavior, clipped status text, and UTF-8-safe chrome.
- Bump the daemon REST contract to API v2: typed client responses and event and
  transcript reads that summarize tool payloads by default, with
  `tool_output=full` and single-event `limit` retrieval available when needed.
- Require a per-daemon-lifetime bearer token on every HTTP route except the
  minimal `/health` probe; local token, pid, and state files are owner-only and
  daemon readiness now proves authenticated access to a protected route.
- Add `./agent_collab.sh setup` to validate effective config and generate the
  daemon REST API artifacts under `doc/daemon_api_doc/`; `setup --check`
  provides a non-writing drift gate.

## [0.1.0] - 2026-07-09 - Initial release

First tagged version of the agent-collab prototype: a local terminal referee
that runs bounded, turn-based collaboration sessions between Claude Code, Codex,
and other configured agent backends, streaming visible agent/tool events and
writing JSONL + Markdown transcripts.

Current state:

- One global local daemon (`127.0.0.1:8765`) owns sessions across projects, with
  a persistent session index that survives restarts; runtime state lives under
  `~/.agent-collab/data/` (override with `AGENT_COLLAB_HOME`).
- CLI client (`serve`, `daemon`, `start`, `list`, `status`, `events`, `watch`,
  `stop`, `config show`) plus MCP access to the same live sessions: a Streamable
  HTTP endpoint at `/mcp` and a stdio adapter, with cursor-based event reads and
  long-polling.
- Configurable agents and workflows; a per-session `workdir` selects the project
  config and subprocess cwd. Typed `codex_options` / `claude_options` /
  `antigravity_options` with pre-launch validation, discoverable through
  `agent_collab_describe_options`.
- Pluggable agent backends: a provider `type` is separate from its execution
  `backend` (default standard-library-only `cli`; optional extras-gated `sdk`),
  with availability/health probes and honest per-session capability flags.
- Standard-library-only base install (Python >= 3.9), no runtime dependencies.
- Typed HTTP API contract: shared request/response DTOs are the single source of
  truth for the CLI/daemon REST API, carrying an explicit `X-Agent-Collab-API`
  version with a client compatibility check. See
  `doc/tasks_closed/stage-5.3-daemon-api-contract.md`.
