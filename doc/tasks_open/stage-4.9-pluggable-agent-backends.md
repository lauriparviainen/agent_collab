# Stage 4.9: Pluggable agent backends (CLI subprocess vs SDK)

## Purpose

Today an agent's `type` (`claude`, `codex`, `mock`) decides both *which provider*
runs the turn and *how* it is executed: `configured_runner` hard-wires
`claude`/`codex` to `SubprocessRunner` plus a hand-written stream parser in
`events.py`. This stage separates those two concerns by introducing a
**backend** dimension:

- **provider** (agent `type`): claude, codex, gemini, mock, ...
- **backend** (execution mechanism): `cli` subprocess today, `sdk` next,
  possibly `api` or others later.

The stage also adds **Gemini CLI as a new provider** on the `cli` backend,
which proves the matrix from both sides: a new backend for existing
providers (SDK), and a new provider on the existing backend (Gemini).

Motivation:

- The official SDKs (`claude-agent-sdk` on PyPI; `openai-codex-sdk` /
  `codex` app-server for Codex) deliver typed content blocks instead of raw
  stream-json lines, removing the whole class of parsing bugs the line
  parsers guard against (thinking signatures, usage metadata, etc.).
- SDK sessions/threads are resumable by ID, which is the foundation for a
  later "continue a completed session" stage.
- The Claude SDK exposes permission callbacks (`PreToolUse` hooks) that a
  future referee can use to actually enforce guardrails instead of only
  observing output.

The subprocess backend remains the default and keeps the project runnable
with only the standard library. SDK backends are optional extras.

## Target shape

```text
config (TOML):
  [agents.claude]
  backend = "sdk"          # per-agent default; "cli" when omitted

CLI:
  agent-collab start --backend sdk "Task"      # session-level override

MCP / HTTP start payload:
  {"task": "...", "backend": "sdk", ...}

Resolution order (most specific wins):
  start request backend > agents.<id>.backend > built-in default "cli"
```

Selecting a backend never changes the transcript contract: every backend
yields the same `Event` stream (`message`, `tool_call`, `command`,
`file_change`, `status`, `error`) into the existing logger, watch, daemon,
and MCP paths.

## Design

### Backend abstraction and registry

Add a `backends/` package. A backend is a named factory that builds an
`AgentRunner` for a given agent config; the `AgentRunner.run(prompt, workdir)
-> AsyncIterator[Event]` interface is unchanged and stays the seam that the
referee, mock, and dry-run logic already depend on.

```text
agent_collab/backends/__init__.py    registry + resolution
agent_collab/backends/base.py        AgentBackend protocol, BackendUnavailable
agent_collab/backends/cli.py         current SubprocessRunner + line parsers
agent_collab/backends/claude_sdk.py  claude-agent-sdk runner (lazy import)
agent_collab/backends/codex_sdk.py   codex sdk runner (lazy import)
```

```python
class AgentBackend(Protocol):
    id: str                      # "cli", "sdk"
    agent_type: str              # "claude", "codex"
    def probe(self) -> BackendHealth: ...       # see "Backend health" below
    def create_runner(self, agent: AgentConfig, verbose: bool,
                      options: Dict[str, Any]) -> AgentRunner: ...
```

The registry is keyed by `(agent_type, backend_id)`. `configured_runner`
becomes a thin lookup: resolve the effective backend id for the agent, fetch
the factory, and delegate. `mock` agents and the `--mock`/`--dry-run` start
flags keep their current runner-level handling and ignore backend selection.

Registration is a module-level dict populated by the built-in backends;
adding a new backend means adding one module and one registry entry. No
entry-point/plugin machinery in this stage.

### Capability flags

Each backend declares capabilities so later stages can build on them without
type-sniffing:

```python
@dataclass(frozen=True)
class BackendCapabilities:
    resume: bool = False        # provider-side session/thread continuation
    interrupt: bool = False     # mid-turn stop
    tool_gate: bool = False     # programmatic tool approve/deny
```

`cli` reports all false (resume via `--resume`/`exec resume` is deliberately
deferred). SDK backends report what the installed SDK actually supports.
Capabilities are surfaced in `describe_options` and recorded in session
settings metadata, but nothing consumes `resume`/`tool_gate` yet — that is
future-stage work this stage only makes possible.

### Honest capability reporting

Sessions must report what is actually possible for *this* session, not what
a provider theoretically supports. Two rules:

1. **A capability is `true` only when the running code path exists.** The
   `cli` backend reports `resume = false` for Claude and Codex even though
   their CLIs have resume flags, because this stage does not implement
   resuming through them. Gemini CLI reports `resume = false`
   unconditionally — it has no provider-side continuation to offer. No
   capability is ever inferred from the provider brand.
2. **Session-level capabilities are derived from runtime facts, not static
   claims.** At start, per-agent effective capabilities (backend flags of
   the agents in the selected workflow) are recorded in session settings
   metadata. The session-level summary is computed, e.g. `resumable` is
   true only if every agent in the workflow has `resume = true` *and* a
   provider session/thread ID was actually captured into
   `agent_sessions`. If an SDK backend fails to capture an ID mid-session,
   `resumable` degrades to false rather than staying at its optimistic
   start value.

Reporting surfaces:

- `SessionState` gains a `capabilities` summary (e.g.
  `{"resumable": false, "interruptible": false}`) alongside the existing
  per-agent settings metadata; it is persisted in the session index and
  returned by `status`, `list`, HTTP, and MCP responses.
- A client asking "can this completed session be continued?" reads the
  session's own `capabilities.resumable` — for any workflow containing a
  Gemini agent the honest answer is `false`, and for mixed workflows
  (e.g. Claude on `sdk`, Gemini on `cli`) the session-level answer is also
  `false` even though the Claude agent's thread individually is resumable;
  the per-agent detail remains visible in settings metadata.
- `describe_options` reports backend capabilities per agent type so the
  limitation is discoverable *before* starting, not only after.

### Configuration

Extend `AgentConfig` with an optional `backend` field:

```toml
[agents.claude]
backend = "sdk"

[agents.codex]
backend = "cli"    # explicit default, same as omitting
```

Validation rules in `validate_agent`:

- `backend` must name a registered backend for the agent's `type`;
  unknown values list the registered ids in the error.
- `command` stays required for the `cli` backend only; SDK backends must
  not require `command`/`args` (relax the current check accordingly).
- `mock` agents reject a `backend` field.

Built-in default remains `cli` so existing configs are untouched
(no schema migration needed; the field is additive).

### Start request and CLI

Extend `StartSessionRequest` with `backend: Optional[str] = None` and thread
it through `agent-collab start --backend NAME`, the HTTP payload, and the
`agent_collab_start` MCP tool schema.

Semantics of the session-level override:

- Applies to every agent in the selected workflow whose type has that
  backend registered.
- If any workflow agent's type lacks the requested backend, the start is
  rejected with an `invalid_start_options`-style error naming the agent and
  its available backends (same feedback shape as stage 4.75 option errors).
- Per-agent start-time overrides (e.g. `{"agent_backends": {"claude": "sdk"}}`)
  are a possible future shape; do not implement until needed, mirroring the
  stage 4.75 decision on per-agent options.

Validation happens in `validate_start_options` / before
`SessionManager.start_session` creates state — including a fresh health
probe (see "Backend health and self-healing"), so a missing SDK package
fails the start request with an actionable message ("backend 'sdk' for agent 'claude'
requires the claude-sdk extra: pip install agent-collab[claude-sdk]")
instead of erroring mid-session.

### Discovery and session metadata

- `describe_options` gains a `backends` section: per agent type, the
  registered backend ids, which is the default, whether each is currently
  available (import check), and capability flags.
- `build_session_settings` records the effective backend per agent so start
  responses and the session index show what actually ran. Command previews
  only apply to the `cli` backend; SDK backends contribute an equivalent
  summary (SDK name/version, mapped options) instead.

### SDK backends

Both SDK runners translate typed SDK messages into `Event`s; the line
parsers in `events.py` remain untouched and exclusive to the `cli` backend.

`claude_sdk` (package `claude-agent-sdk`):

- One `query()`/client run per turn; map `TextBlock` → `message`,
  `ToolUseBlock` → `tool_call`/`command`/`file_change` (reuse the block
  classification helpers from `events.py`), `ThinkingBlock` → hidden unless
  verbose (thinking text only; never the signature), result/system messages
  → verbose `status`.
- Map the existing typed `claude_options` (`model`, `permission_mode`,
  `thinking_level`, `thinking_budget_tokens`) onto `ClaudeAgentOptions`
  explicitly — same "no blind pass-through" rule as stage 4.75.
- Capture the SDK session id from the result message and store it in the
  session state per agent (see below).

`codex_sdk` (package `openai-codex-sdk`, drives the local `codex`
app-server; still requires the `codex` binary):

- One thread run per turn; map thread items/events onto `Event`s the same
  way `parse_codex_line` classifies them today.
- Map typed `codex_options` onto the SDK/thread options explicitly.
- Capture the thread id per agent.

Provider session/thread IDs are stored in `SessionState` as
`agent_sessions: {agent_id: provider_session_id}` and persisted through the
session index. Nothing resumes them in this stage; they exist so a future
"continue session" stage has the IDs it needs.

### Backend health and self-healing

Backend availability must be a **live** property of the daemon, not a fact
frozen at daemon startup. Installing the Gemini CLI, `pip install`-ing an
SDK extra, or exporting an API key should make the backend usable without a
daemon restart; removing one should make failures diagnosable before a
session burns a turn on them.

Health model — each `(agent_type, backend_id)` pair has a probed status:

```json
{
  "status": "ok" | "unavailable" | "unknown",
  "reason": "gemini: command not found on PATH",
  "credentials": "ok" | "missing" | "unknown",
  "checked_at": "2026-07-08T12:00:00Z"
}
```

Probes, in increasing cost, all standard-library and side-effect free:

1. **Presence** (reliable): `shutil.which(command)` for `cli` backends;
   import check for SDK backends. Definite `ok`/`unavailable`.
2. **Version** (reliable when present): run `<command> --version` with a
   short timeout; records the version for `describe_options` and catches
   broken installs.
3. **Credentials** (best-effort, per provider): environment/config checks
   only — e.g. `GEMINI_API_KEY` set or a Gemini OAuth credentials file
   present; `codex login status` where the CLI offers a cheap auth query.
   Providers with no cheap, side-effect-free check report `"unknown"`.
   Never probe by making a real model call — health checks must not cost
   tokens or mutate provider state.

Freshness and self-healing:

- Probe results are cached with a short TTL (~60s). The daemon runs a
  periodic refresh task (same ~60s cadence) so `describe_options` and
  `daemon status` always show near-current health, and logs lifecycle
  transitions ("backend gemini/cli became available") for troubleshooting.
- Start requests always re-probe the backends the workflow needs, bypassing
  the cache, so gating decisions never act on stale state — this is what
  makes "install the CLI, then start a session" work with no restart.

Gating policy — block only on certainty:

- `status: unavailable` → reject the start with the existing
  `invalid_start_options`-style error, including the probe `reason` and the
  fix hint (install command / extras). No session state is created.
- `credentials: missing` (definite, e.g. required env var absent and no
  credentials file) → reject with a hint naming the expected variable or
  login command.
- `credentials: unknown` → allow the start but include a warning in the
  start response and session settings metadata. False negatives must never
  block a working setup; the first turn's real error remains the authority.

Reporting: health is included per backend in `describe_options` (so MCP
agents see what is truly startable and why not, before trying) and in
`daemon status`. This is diagnostic surface, not new API semantics — the
existing error shapes carry it.

### Gemini CLI provider

Gemini CLI has a documented non-interactive mode that emits
newline-delimited JSON, so it fits the `cli` backend directly:

```text
gemini -p "<prompt>" --output-format stream-json
```

Additions:

- New agent type `gemini` in `AGENT_TYPES`/`SUBPROCESS_AGENT_TYPES`, and
  `gemini` added to `VALID_SOURCES` in `events.py` so transcript events
  attribute correctly.
- Built-in config entry:

  ```toml
  [agents.gemini]
  type = "gemini"
  command = "gemini"
  args = ["-p", "--output-format", "stream-json"]
  enabled = false   # opt-in: not everyone has the CLI installed/authed
  ```

  Users enable it and reference it from workflows in project/user config
  (e.g. a `tri-review` sequence). No built-in workflow uses it, so default
  behaviour is unchanged.
- `parse_gemini_line` in `events.py`, written against captured
  `--output-format stream-json` samples from the pinned CLI version, using
  the same provider-aware extraction policy as the Claude parser: visible
  text only from real content fields; tool/command/file-change
  classification from typed event fields; provider metadata (ids, usage,
  auth/status noise) never rendered as transcript text, verbose-only status
  for the rest. Registered in `backends/cli.py` next to the existing
  parsers.
- Typed `gemini_options` following the stage 4.75 pattern (explicit
  mapping, unknown keys rejected): initially `model` (`-m`) and
  `approval_mode` (`--approval-mode`), with allowed values configurable via
  `[agents.gemini.options]`. Threaded through `StartSessionRequest`,
  `--gemini-options` on the CLI, the HTTP payload, the MCP start schema,
  `validate_start_options` (including the mode-aware rule: reject non-empty
  `gemini_options` when the workflow has no Gemini agent), and
  `describe_options`.
- Auth is the CLI's own concern (OAuth login, `GEMINI_API_KEY`, or Vertex
  credentials); agent-collab only passes the environment through, and
  `agents.gemini.env` can inject `GEMINI_API_KEY` per project. Never log
  key values in settings metadata or transcripts.

Caveats, reflected in capabilities:

- Gemini CLI is scriptable automation, not an app-server/SDK-style API —
  there is no `gemini`+`sdk` registry entry in this stage. If Google ships
  an equivalent agent SDK later, it lands as `backends/gemini_sdk.py`
  without touching the provider.
- Capabilities report all false: no provider-side resume is captured
  (`agent_sessions` simply has no entry for Gemini agents), no interrupt,
  no tool gate.
- Treat the stream-json schema as unstable: pin a tested minimum CLI
  version in docs, and keep the parser tolerant of unknown event types
  (verbose-only status, never raw dumps).

### Dependencies and packaging

- `pyproject.toml` gains optional extras:
  `claude-sdk = ["claude-agent-sdk>=..."]`,
  `codex-sdk = ["openai-codex-sdk>=..."]`, and a convenience
  `sdk = [both]`.
- All SDK imports are lazy (inside `available()` / `create_runner`), so the
  base install and the default `cli` backend stay standard-library only.
- Tests must not require the SDKs: SDK runner tests use fake SDK modules
  injected via `sys.modules` (or a thin injectable client factory on the
  runner), and registry tests assert the unavailable-backend error path.

## Out of scope

- Resuming provider sessions / continuing completed agent-collab sessions
  (this stage only captures and persists the IDs).
- Referee tool-gating via SDK permission callbacks.
- Per-agent backend overrides at start time.
- Streaming partial deltas within a turn; turns remain whole-message.
- A third-party plugin mechanism for out-of-tree backends.

## Implementation steps

1. Create `backends/` package: `base.py` (protocol, capabilities,
   `BackendUnavailable`), registry with resolution
   (request > agent config > default).
2. Move subprocess construction from `runners.configured_runner` into
   `backends/cli.py`; keep `configured_runner` as the registry-backed
   entry point so referee/daemon call sites do not change.
3. Add `backend` to `AgentConfig`, merge/validate logic, and config docs.
4. Add `backend` to `StartSessionRequest`, CLI `--backend`, HTTP payload,
   and the `agent_collab_start` MCP schema; validate (registered +
   available) before any session state is created.
5. Extend `describe_options` and `build_session_settings` with backend
   info; add `agent_sessions` to `SessionState` and the session index.
6. Add backend health probes (presence, version, best-effort credentials),
   the TTL cache plus daemon refresh task with transition logging, fresh
   re-probe and gating on start, and health in `describe_options` /
   `daemon status`.
7. Add the `gemini` provider: agent type + `VALID_SOURCES` entry, built-in
   disabled config entry, `parse_gemini_line` from captured stream-json
   samples, typed `gemini_options` end to end (validate, CLI flag, HTTP,
   MCP schema, describe/settings).
8. Implement `backends/claude_sdk.py` with typed-message → `Event` mapping
   and explicit option mapping.
9. Implement `backends/codex_sdk.py` the same way.
10. Add pyproject extras; update README, `doc/agent-configuration.md`, and
    `doc/daemon-architecture.md` (including Gemini enablement + auth notes).

Steps 1–6 are useful on their own (registry, selection, and live health
with `cli` as the only real backend) and should land before the SDK
runners. Step 7 is independent of 8–9 and can land in either order after 6.

## Tests

- Registry: resolution precedence (request > agent config > default);
  unknown backend id rejected with registered ids listed.
- Config: `backend = "sdk"` parses and validates; unknown value fails;
  `command` optional for SDK backends, still required for `cli`; `mock`
  rejects `backend`.
- Start validation: `--backend sdk` with a workflow agent lacking that
  backend fails before session creation; unavailable SDK package produces
  the install-hint error; valid request records effective backends in
  session settings.
- `describe_options` includes backends with availability and capabilities.
- Health probes: missing command → `unavailable` with reason; command
  appearing on PATH flips the cached status to `ok` on the next refresh
  without daemon restart (fake PATH/clock, no real CLIs); credential
  checks report `ok`/`missing`/`unknown` per provider rules; start
  requests re-probe fresh and reject `unavailable`/`missing` with fix
  hints while `unknown` starts with a warning recorded in settings
  metadata; probes never launch a model call.
- Capability honesty: session with all-`cli` agents reports
  `resumable: false`; SDK-backed session reports `resumable: true` only
  after IDs are captured, and degrades to false when capture fails; a
  workflow mixing an SDK agent with a Gemini `cli` agent reports
  session-level `resumable: false` while per-agent metadata still shows
  the SDK agent's captured thread; `capabilities` survives daemon restart
  via the session index.
- Claude SDK runner (fake SDK): text blocks become `message` events;
  thinking signature never appears in any event text; tool-use blocks
  classify as before; SDK session id lands in `agent_sessions`.
- Codex SDK runner (fake SDK): equivalent coverage incl. thread id capture.
- Gemini: config entry validates and is disabled by default; enabling it
  and referencing it from a workflow passes validation;
  `parse_gemini_line` covers text display, tool/command classification,
  metadata/unknown events hidden unless verbose (no raw dumps);
  `gemini_options` validation incl. rejection when the workflow has no
  Gemini agent; mock and dry-run runners work for a `gemini` agent.
- Existing suite stays green with no config changes (default `cli`,
  Gemini disabled).

## Acceptance criteria

- `agent-collab start "Task"` behaves exactly as today with no config
  changes; the base install has no new required dependencies.
- Setting `agents.claude.backend = "sdk"` in config, or passing
  `--backend sdk` (CLI/MCP/HTTP) at start, runs turns through the SDK
  runner, and the transcript/watch/log experience is unchanged.
- Backend selection is discoverable via `describe_options` and reflected in
  session settings metadata and the session index.
- Every session honestly reports its capabilities (`resumable`,
  `interruptible`) in status/list/HTTP/MCP responses, derived from the
  actual backends in use and captured session IDs — never from provider
  brand; Gemini-CLI sessions always report `resumable: false`.
- Selecting an uninstalled SDK backend fails the start request with a
  machine-readable error and an install hint; no session state is created.
- Backend availability is live: installing the Gemini CLI (or an SDK
  extra, or exporting a required key) makes the backend startable within
  one refresh interval, with no daemon restart; the transition is visible
  in `describe_options`, `daemon status`, and the daemon log.
- A start request against a truly unusable backend (missing binary/SDK or
  definitely missing credentials) is rejected before any session state
  exists, with the probe reason and a fix hint; uncertain credential state
  never blocks a start, only warns.
- Enabling `agents.gemini` and adding it to a workflow runs Gemini turns
  through the standard `cli` backend with correct transcript attribution
  and typed `gemini_options`, with no changes outside config for the user.
- Adding a hypothetical new backend requires only a new module implementing
  `AgentBackend` plus a registry entry, and a new provider only an agent
  type, parser, and config entry — no changes to referee, daemon, or event
  plumbing.
