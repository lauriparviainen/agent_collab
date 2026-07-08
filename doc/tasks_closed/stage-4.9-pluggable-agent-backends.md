# Stage 4.9: Pluggable agent backends (CLI subprocess vs SDK)

## Purpose

Today an agent's `type` (`claude`, `codex`, `mock`) decides both *which provider*
runs the turn and *how* it is executed: `configured_runner` hard-wires
`claude`/`codex` to `SubprocessRunner` plus a hand-written stream parser in
`events.py`. This stage separates those two concerns by introducing a
**backend** dimension:

- **provider** (agent `type`): `claude`, `codex`, `antigravity`, `mock`, ...
- **backend** (execution mechanism): `cli` subprocess today, `sdk` next,
  possibly `api` or others later.

The stage also adds **Antigravity as a new provider**, available on *both*
backends. That single provider proves the matrix from both sides at once:

- a **new backend** (`sdk`) for a provider, via the official
  `google-antigravity` Python SDK (typed, in-process, high-fidelity events);
- a **new provider on the existing `cli` backend**, via the `agy`
  command-line binary (plain-text, low-fidelity events).

The subprocess (`cli`) backend remains the default and keeps the project
runnable with only the standard library. The `sdk` backend is an optional,
extras-gated dependency.

### Why Antigravity, not Gemini CLI

The previous draft of this stage centered on **Gemini CLI** as the new
provider. That is now stale and must not be carried forward:

- Google announced (I/O 2026) that **Gemini CLI and Gemini Code Assist IDE
  extensions stop serving requests on 2026-06-18** for Google AI Pro, Ultra,
  and free-tier users. **Antigravity CLI is the designated successor.**
  (Standard/Enterprise Code Assist licensees keep access, but the consumer
  path this project targets is gone.)
- Building a fresh provider on a product that is being retired is wrong.
  Gemini CLI is therefore **removed from this plan entirely** — it is not a
  target, not a fallback, and not a compatibility shim. If a user still has a
  working Gemini CLI they can already configure it as a generic subprocess
  agent through existing config; this stage adds no first-class support for
  it.

### Why the SDK path matters (verified)

The old draft justified an SDK backend with "typed content blocks remove
parsing bugs" and "resumable threads." For Antigravity specifically:

- The `agy` **CLI print mode emits plain text**, not a structured stream (see
  "Verified provider facts"). So the `cli` backend for Antigravity is
  inherently *low fidelity* — it cannot reconstruct tool/command/file-change
  structure. The **SDK** is the only path *expected* to yield typed, per-turn
  tool and text events for Antigravity — **expected, not proven**: the SDK's
  event surface is a hypothesis until the implementation spike confirms it
  (see "Implementation spike"). If the spike shows the SDK only exposes text
  (or opaque events), the `sdk` backend **degrades to message-only**, the same
  honest fidelity as the `cli` path, and that fact is documented rather than
  papered over.
- The SDK runs **in-process against the working directory** and bundles its
  own runtime binary, so it needs no separate `agy` install once the extra is
  present.

The subprocess backend stays the default; SDK backends are optional extras.

## Verified provider facts

These facts were checked against the installed binary and official
Google/Antigravity documentation on **2026-07-08**. Anything not confirmed
here is called out as an **open question** or gated behind the
**implementation spike** below — the plan must not assume unverified binary
names, package names, stream formats, or event shapes.

### Antigravity CLI (`agy`) — confirmed

Source: `agy --help` / `agy changelog` from the installed binary (v1.1.0) and
the official Antigravity CLI docs.

- **Binary name:** `agy`. Installed here as `~/.local/bin/agy`, version
  `1.1.0` (Go implementation).
- **Non-interactive:** `--print` / `-p` (alias `--prompt`) — "Run a single
  prompt non-interactively and print the response." Bounded by
  `--print-timeout` (default `5m0s`).
- **Output format:** **plain text only.** There is **no** `--output-format`
  flag and **no** stream-json / NDJSON mode in v1.1.0 (confirmed in `--help`;
  the official docs describe `agy -p` as returning plain text). Any
  `--output-format json` seen in third-party write-ups is aspirational, not
  implemented. **This is the single biggest correction versus the old draft,
  which assumed `gemini -p --output-format stream-json`.**
- **Permissions posture (matters for automation):** default mode is
  `request-review`, which pauses before file writes for interactive diff
  review. `--mode` accepts `default | accept-edits | plan`;
  `--dangerously-skip-permissions` auto-approves all tool permission
  requests. A non-interactive `agy -p` run therefore needs an explicit
  non-blocking posture (e.g. `--mode accept-edits` or
  `--dangerously-skip-permissions`) or it can stall waiting on approval.
- **Resume (exists, not used this stage):** `--continue` / `-c` (continue the
  most recent conversation) and `--conversation <ID>` (resume by ID).
- **Other flags:** `--model`, `--sandbox`, `--add-dir` (repeatable),
  `--project` / `--new-project`, `--log-file`.
- **Auth / config:** interactive Google **OAuth sign-in** (`agy models`
  refuses with "Please sign in…" until signed in). Credentials/config live
  under `~/.gemini/`: OAuth token at
  `~/.gemini/antigravity-cli/antigravity-oauth-token`, accounts in
  `~/.gemini/google_accounts.json` (`active`/`old`), CLI config in
  `~/.gemini/config/`, MCP servers in `~/.gemini/config/mcp_config.json`,
  conversation history under `~/.gemini/antigravity-cli/`. A first-class
  env-var API key is **not** the confirmed primary path — treat auth as
  "user signs in once; agent-collab passes the environment through and never
  manages or logs credentials."

### Antigravity SDK (`google-antigravity`) — package confirmed, API is a spike

Source: PyPI project page and the SDK repository README.

- **Package:** `google-antigravity` on PyPI (v0.1.5, published by Google LLC,
  requires Python >= 3.10). Install: `pip install google-antigravity`.
- **Self-contained:** the wheel **bundles a compiled runtime binary** per
  platform, so the SDK does **not** require the `agy` CLI to be installed
  separately. It runs **locally, against the current working directory**.
- **Hypothesized API (NOT yet verified against a running import — pre-1.0):**
  `from google.antigravity import Agent, LocalAgentConfig`; async context
  manager `async with Agent(config) as agent:`; `response = await
  agent.chat(prompt)` returning a `ChatResponse`; `await response.text()` for
  the full text; `async for token in response` for text deltas;
  `response.thoughts` for reasoning deltas; `response.tool_calls` for typed
  `ToolCall` events. Other names seen: `Conversation`, `ToolRunner`,
  `CapabilitiesConfig`, `Step`, `LocalConnectionStrategy`.
- **Unconfirmed:** exact `ChatResponse` / `ToolCall` / `Step` field shapes,
  whether a **stable resumable conversation id** is exposed, structured-output
  support, and the exact hook/policy surface. The SDK is v0.1.x and its API
  may shift. **The event mapping and any provider-id capture are gated behind
  the implementation spike below.**

### Not this: the remote "Antigravity Agent" (`google-genai`)

There is a *separate* Google product — the **managed Antigravity Agent** via
the Gemini API `google-genai` `client.interactions.create(agent=…)`, which
runs in a **Google-hosted remote Linux sandbox** and returns an `Interaction`
object. It is **explicitly out of scope**: it does not execute against the
user's local repository/workdir, which is the whole point of agent-collab's
subprocess model. This plan's `sdk` backend means the **local
`google-antigravity` SDK**, never the remote Interactions API.

## Target shape

```text
config (TOML):
  [agents.antigravity]
  type = "antigravity"
  backend = "sdk"          # per-agent default; "cli" when omitted

CLI (the override applies to a workflow whose every selected agent
supports that backend — see "Start request and CLI"):
  agent-collab start --workflow antigravity-solo --backend sdk "Task"

MCP / HTTP start payload:
  {"task": "...", "workflow": "antigravity-solo", "backend": "sdk", ...}

Resolution order (most specific wins):
  start request backend > agents.<id>.backend > built-in default "cli"
```

`--backend sdk` against the default Claude/Codex workflow **fails** in this
stage, because neither `claude` nor `codex` has `sdk` registered (see the
matrix below). The override is only valid when *every* selected agent's type
supports the requested backend; the example uses a user-defined
Antigravity-only workflow (`antigravity-solo = ["antigravity"]`).

Selecting a backend never changes the transcript contract: every backend
yields the same `Event` stream (`message`, `tool_call`, `command`,
`file_change`, `status`, `error`) into the existing logger, watch, daemon,
and MCP paths. (For `agy`/`cli` the stream is coarser — see fidelity note —
but the event *types* are unchanged.)

## Design

### Backend abstraction and registry

Add a `backends/` package. A backend is a named factory that builds an
`AgentRunner` for a given agent config; the `AgentRunner.run(prompt, workdir)
-> AsyncIterator[Event]` interface is unchanged and stays the seam that the
referee, mock, and dry-run logic already depend on.

```text
agent_collab/backends/__init__.py      registry + resolution
agent_collab/backends/base.py          AgentBackend protocol, capabilities,
                                        BackendUnavailable, health types
agent_collab/backends/cli.py           current SubprocessRunner + line parsers
                                        (claude, codex) + agy plain-text runner
agent_collab/backends/antigravity_sdk.py  google-antigravity runner (lazy)
```

```python
class AgentBackend(Protocol):
    id: str                      # "cli", "sdk"
    agent_type: str              # "claude", "codex", "antigravity"
    capabilities: BackendCapabilities
    def probe(self) -> BackendHealth: ...       # see "Backend health"
    def create_runner(self, agent: AgentConfig, verbose: bool,
                      options: Dict[str, Any]) -> AgentRunner: ...
```

The registry is keyed by `(agent_type, backend_id)`. In this stage the
registered pairs are:

| provider (`type`) | `cli`            | `sdk`                       |
| ----------------- | ---------------- | --------------------------- |
| `claude`          | ✅ (existing)     | — (deferred, see out of scope) |
| `codex`           | ✅ (existing)     | — (deferred)                |
| `antigravity`     | ✅ (`agy`, new)   | ✅ (`google-antigravity`, new) |

`configured_runner` becomes a thin lookup: resolve the effective backend id
for the agent, fetch the factory, and delegate. `mock` agents and the
`--mock`/`--dry-run` start flags keep their current runner-level handling and
ignore backend selection.

Registration is a module-level dict populated by the built-in backends;
adding a new backend means adding one module and one registry entry. No
entry-point/plugin machinery in this stage.

### Execution wiring (the seam that actually runs turns)

`StartSessionRequest.backend` is not enough on its own: the resolved backend
must reach the runner. The live path is `SessionManager.start_session` →
`_run_session` → `RefereeConfig` → `Referee._runners()` →
`configured_runner(agent, verbose, options)`. Two real gaps in that path must
be closed or the override will show up in the start *settings* but not in
*execution*:

1. **Resolve once, carry the result.** `start_session` already
   `load_config`s and validates; `_run_session` currently calls
   `load_config(workdir)` a *second* time when it builds `RefereeConfig`.
   Backend resolution (`request > agents.<id>.backend > default "cli"`) must
   be computed once during start validation and carried forward — e.g. a
   resolved `{agent_id: backend_id}` map stored on the request/settings and
   passed into `RefereeConfig` — so the runner uses exactly the selection the
   start response advertised, not a possibly-divergent re-resolution.
2. **Thread it through `RefereeConfig` and `configured_runner`.**
   `RefereeConfig` (today: `codex_options`, `claude_options`, `collab_config`,
   ...) gains the resolved per-agent backend map (and `antigravity_options`,
   below). `_runners()` passes the resolved backend id into
   `configured_runner`, which changes from "dispatch on `agent.type`" to
   "look up `(agent.type, resolved_backend_id)` in the registry." Referee,
   daemon, and MCP call sites keep their current shapes; only the argument
   list of `configured_runner` grows.

Provider-options and mock attribution on this path also need Antigravity
awareness:

- `RefereeConfig._options_for()` currently returns only `codex_options` /
  `claude_options`; add an `antigravity` branch. `antigravity_options` must be
  added to `StartSessionRequest`, `RefereeConfig`, and the HTTP/MCP/CLI
  payload allowlists alongside `backend`.
- `MockRunner` currently hard-codes source `codex` for any non-`claude` name,
  so an `antigravity` mock/dry-run would mis-attribute events. Give
  `MockRunner` (and the mock/dry-run selection in `configured_runner`) an
  explicit event source / agent type so an `antigravity` mock emits
  `antigravity`-sourced events.
- `runners._event_source()` (used for subprocess stderr status) also only
  maps `claude`/`codex` to themselves; extend it so `antigravity` stderr
  status attributes to `antigravity`, not `tool`.

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

**Every backend in this stage reports all three `false`.** This is
deliberate and honest:

- `antigravity`/`cli`: `agy --continue`/`--conversation` exist, but this
  stage does not drive them → `resume = false`.
- `antigravity`/`sdk`: the SDK exposes `Conversation` state and
  allow/deny/ask policies, but this stage wires none of them, and a stable
  resumable id is not even confirmed to exist → all `false`.
- `claude`/`codex` on `cli`: unchanged, all `false` (their CLIs have resume
  flags this project still does not use).

No capability is ever inferred from the provider brand. Capabilities are
surfaced in `describe_options` and recorded in session settings metadata, but
nothing consumes `resume`/`tool_gate`/`interrupt` yet — that is future-stage
work this stage only makes structurally possible.

### Honest capability reporting

Sessions must report what is actually possible for *this* session, not what a
provider theoretically supports. Two rules:

1. **A capability is `true` only when the running code path exists.** Since
   every backend reports `false` this stage, every session's `resumable` /
   `interruptible` summary is `false`. The plumbing to compute a `true`
   summary is built, but it has nothing to turn `true` yet — that keeps the
   later "continue a session" stage a pure addition rather than a correction.
2. **Session-level capabilities are derived from runtime facts, not static
   claims.** At start, per-agent effective capabilities (backend flags of the
   agents in the selected workflow) are recorded in session settings
   metadata. The session-level summary is computed by AND-ing across agents
   (`resumable` is true only if every workflow agent has `resume = true`
   *and* a provider session id was actually captured). This computation is
   built and tested now against the all-`false` reality; a future stage flips
   inputs to `true` without touching the reducer.

Reporting surfaces:

- `SessionState` gains a `capabilities` summary (e.g.
  `{"resumable": false, "interruptible": false}`) alongside the existing
  per-agent settings metadata; it is persisted in the session index and
  returned by `status`, `list`, HTTP, and MCP responses.
- `describe_options` reports backend capabilities per agent type so the
  limitation is discoverable *before* starting, not only after.

### Provider session/thread ids (spike-gated)

`SessionState` *may* gain `agent_sessions: {agent_id: provider_session_id}`,
populated only if the implementation spike confirms a backend exposes a
**stable** id worth persisting:

- `antigravity`/`cli`: `agy` owns conversation ids internally
  (`--conversation <ID>`), but print mode does not print one to stdout, so
  there is nothing reliable to capture. **No entry** unless the spike finds a
  documented way to read it.
- `antigravity`/`sdk`: capture the `Conversation` id **only if** the spike
  confirms the SDK surfaces a stable, resume-capable id. Otherwise the field
  is omitted for that agent and no resumability is implied.

If neither backend yields a dependable id, `agent_sessions` is dropped from
this stage entirely rather than shipped as a field that is always empty. The
capabilities reducer already treats a missing id as `resumable = false`, so
either outcome is consistent.

### Configuration

Extend `AgentConfig` with an optional `backend` field:

```toml
[agents.antigravity]
type = "antigravity"
command = "agy"
args = ["-p", "--mode", "accept-edits"]
backend = "cli"        # explicit default; "sdk" to use the SDK runner
enabled = false        # opt-in: requires agy installed + signed in
```

Validation rules in `validate_agent`:

- `backend` must name a backend **registered for the agent's `type`**;
  unknown values list the registered ids for that type in the error.
- `command` stays required for the `cli` backend only; the `sdk` backend must
  not require `command`/`args` (relax the current
  `type in SUBPROCESS_AGENT_TYPES` check so it keys off the effective
  backend, not the type).
- `mock` agents reject a `backend` field.

Built-in default remains `cli` so existing configs are untouched (the field
is additive; no schema migration needed — but if any normalization is
required it belongs in `config_migrations.py`, per the stage 4.8 rule).

### Start request and CLI

Extend `StartSessionRequest` with `backend: Optional[str] = None` (and
`antigravity_options`, per "Execution wiring") and thread them through
`agent-collab start --backend NAME` / `--antigravity-options`, the HTTP
payload, and the `agent_collab_start` MCP tool schema.

Semantics of the session-level override:

- It applies **uniformly** to every agent in the selected workflow — it is not
  a partial or best-effort override.
- Therefore, if **any** selected agent's type lacks the requested backend, the
  whole start is rejected with an `invalid_start_options`-style error naming
  the agent and its available backends (same feedback shape as stage 4.75
  option errors). A backend is only usable session-wide when every selected
  agent's type registers it.
- Per-agent start-time overrides (e.g. `{"agent_backends": {"antigravity":
  "sdk"}}`) are a possible future shape; do not implement until needed,
  mirroring the stage 4.75 decision on per-agent options.
- The effective per-agent backend map resolved here (and validated below) is
  the same map carried into execution via `RefereeConfig` — see "Execution
  wiring"; it must not be re-resolved independently at run time.

Validation happens in `validate_start_options` (or a sibling validator called
from the same place) **before** `SessionManager.start_session` creates state —
including a fresh health probe (see "Backend health") — so a missing SDK
package fails the start request with an actionable message
("backend 'sdk' for agent 'antigravity' requires the antigravity-sdk extra:
pip install agent-collab[antigravity-sdk]") instead of erroring mid-session.

### Discovery and session metadata

- `describe_options` gains a `backends` section: per agent type, the
  registered backend ids, which is the default, whether each is currently
  available (probe result), capability flags, and health/reason.
- `build_session_settings` records the effective backend per agent so start
  responses and the session index show what actually ran. Command previews
  only apply to the `cli` backend; the `sdk` backend contributes an
  equivalent summary (SDK package name + version, mapped options) instead of
  a `command_preview`.

### Antigravity `cli` backend (`agy`, plain text)

Because print mode is plain text, this runner is intentionally simple and its
event fidelity is limited:

- Reuse `SubprocessRunner`, but with a **plain-text parser**
  (`parse_antigravity_line` in `events.py`), not a JSON parser. Each
  non-empty stdout line becomes an `antigravity` `message` event; there is no
  tool/command/file-change classification available from plain text. The
  referee still emits the `command` start event and the `status` exit event
  it emits for every subprocess runner today.
- New agent type `antigravity` in `AGENT_TYPES` / `SUBPROCESS_AGENT_TYPES`,
  and `antigravity` added to `VALID_SOURCES` in `events.py` so message events
  attribute correctly (tool/command/file_change events keep source `tool`,
  matching the other providers).
- Built-in config entry (disabled by default, as above). No built-in workflow
  references it, so default behavior is unchanged.
- Non-interactive posture: the default `args` must include a non-blocking
  mode (`--mode accept-edits`, or `--dangerously-skip-permissions` when the
  user opts into it) so `-p` does not stall on the `request-review` approval
  prompt. Document the trade-off; do not silently pick the most permissive
  option.
- Auth is the CLI's own concern (Google OAuth sign-in cached under
  `~/.gemini/`); agent-collab only passes the environment through. Never log
  credential file contents or tokens in settings metadata or transcripts.

Capability flags: all `false`. Fidelity is explicitly documented as
"message-only" so users understand why `agy` transcripts look coarser than
`claude`/`codex` ones and are steered toward the `sdk` backend for structured
events.

### Antigravity `sdk` backend (`google-antigravity`, typed)

Lazy-imported, extras-gated runner that translates typed SDK events into
`Event`s. The `cli` line parsers stay untouched and exclusive to the `cli`
backend.

Design (all mapping details are **spike outputs**, see below — this is the
intended shape, to be confirmed against captured samples before coding):

- One `async with Agent(config) as agent` + `await agent.chat(prompt)` per
  turn. Map the SDK's streams onto `Event`s:
  - text (`response.text()` / text-token iteration) → `antigravity`
    `message`;
  - `response.tool_calls` typed `ToolCall` events → `tool_call` / `command` /
    `file_change`, reusing the block-classification helpers in `events.py`
    (`_classify_claude_tool_block`-style name heuristics) where the `ToolCall`
    exposes a tool name;
  - `response.thoughts` reasoning deltas → hidden unless `verbose`, then a
    `status` event (reasoning text only; never any opaque signature);
  - lifecycle/system info → verbose `status`.
- Map typed `antigravity_options` onto `LocalAgentConfig` / `CapabilitiesConfig`
  explicitly — same "no blind pass-through" rule as stage 4.75. `cli` maps the
  same options to `agy` flags; `sdk` maps them to SDK config.
- Capture a provider conversation id per agent **only** if the spike confirms
  a stable one exists (see "Provider session/thread ids").

### Implementation spike (do this before writing any Antigravity parser/mapper)

Neither the `agy` plain-text output shape nor the `google-antigravity` typed
event shapes are confirmed from official docs at a level sufficient to write a
parser against. Per project practice (parsers are written against captured
samples from a pinned version), the first Antigravity commit is a spike, not
production code:

1. **CLI samples:** run `agy -p --mode accept-edits "<fixture prompt>"` in a
   throwaway repo (signed-in, low-cost prompt) and capture stdout/stderr to
   `tests/fixtures/antigravity/agy-print-*.txt`. Confirm: is it purely
   free-form prose, or are there any stable line markers? Record the pinned
   `agy --version`.
2. **SDK samples:** in a scratch venv with `pip install google-antigravity`,
   run a minimal `Agent(...).chat(...)` against a fixture that triggers at
   least one tool call and one file edit; capture the concrete
   `ChatResponse` / `ToolCall` / `Step` objects (repr + attribute dump) to
   `tests/fixtures/antigravity/sdk-*.json`/`.txt`. Confirm the real attribute
   names, whether tool calls carry a name/args, whether a conversation id is
   exposed, and the exact async-iteration surface. Record the pinned
   `google-antigravity` version.
3. Only then implement `parse_antigravity_line` (cli) and the SDK
   event-mapping function, each backed by a fixture-driven test.

The spike's captured fixtures become the parser/mapper test corpus. If the
spike shows the SDK API differs from the hypothesis in "Verified provider
facts," update this plan's mapping section before implementing.

#### Spike outcome (2026-07-08)

- **CLI (`agy`) — confirmed, matches the plan.** `agy --version` = `1.1.0`.
  `agy -p --mode accept-edits "<prompt>"` (signed in) prints **free-form plain
  text / Markdown prose**: multiple lines, blank lines, `###` headers, `*`
  bullets, fenced code blocks — **no JSON, no NDJSON, no stable per-line event
  marker**, empty stderr. So `parse_antigravity_line` emits one `antigravity`
  `message` event per non-empty line (message-only), exactly as designed.
  Fixtures: `tests/fixtures/antigravity/agy-version.txt`,
  `agy-print-sample.stdout.txt`, `agy-print-sample.stderr.txt`.
- **SDK (`google-antigravity`) — BLOCKED on live capture.** System Python here
  is 3.9.25 (< the SDK's required 3.10) and `pip install
  "google-antigravity>=0.1,<1"` finds no installable distribution, so the SDK
  cannot be imported and its real object shapes cannot be captured. Per the
  spike rule we do not guess shapes into production code. The `sdk` backend is
  therefore implemented against the plan's hypothesis with a fake
  `google.antigravity` module injected in tests (driven by
  `tests/fixtures/antigravity/sdk-hypothesis.json`), **degrades to message-only**
  if typed tool events are absent, ships **no** `agent_sessions` conversation id
  (unconfirmed), rejects `antigravity_options.mode` on `sdk`, and has one real,
  fully-tested path: a missing module fails the start with the `antigravity-sdk`
  extra install hint. Re-run the SDK half on a Python>=3.10 host with the SDK
  installed, replace the hypothesis fixture with a real capture, and reconcile
  `backends/antigravity_sdk.py` and this section. This resolves open questions
  1–3 as "blocked, conservative default taken" and 4 as "`mode` is cli-only".

### Backend health

Backend availability must be a **live** property of the daemon, not a fact
frozen at daemon startup: installing `agy`, signing in, or `pip install`-ing
the SDK extra should make a backend usable, and removing one should be
diagnosable before a session burns a turn.

Health model — each `(agent_type, backend_id)` pair has a probed status:

```json
{
  "status": "ok" | "unavailable" | "unknown",
  "reason": "agy: command not found on PATH",
  "credentials": "ok" | "missing" | "unknown",
  "version": "1.1.0",
  "checked_at": "2026-07-08T12:00:00Z"
}
```

Probes, in increasing cost, all standard-library and side-effect free:

1. **Presence** (reliable): `shutil.which("agy")` for the `cli` backend;
   `importlib.util.find_spec("google.antigravity")` for the `sdk` backend
   (import check, no execution). Definite `ok`/`unavailable`.
2. **Version** (reliable when present): `agy --version` with a short timeout
   for `cli`; the installed package version for `sdk`. Recorded for
   `describe_options` and to catch broken installs.
3. **Credentials** (best-effort, side-effect free): for Antigravity, check
   for the presence of the cached OAuth token
   (`~/.gemini/antigravity-cli/antigravity-oauth-token`) or an `active`
   account in `~/.gemini/google_accounts.json`. **Never** probe by making a
   real model call (`agy models` and any SDK call cost/require live auth).
   When no cheap check is reliable, report `"unknown"`.

Freshness:

- Probe results are cached with a short TTL (~60s) so `describe_options`
  shows near-current health without hammering the filesystem.
- **Start requests always re-probe fresh** (bypass cache) for the backends the
  workflow needs, so gating never acts on stale state — this is what makes
  "install the CLI / sign in, then start" work with no daemon restart.
- *Optional polish (may land in a follow-up commit, not required for the
  stage):* a periodic background refresh task that logs lifecycle transitions
  ("backend antigravity/cli became available"). The authoritative gate is the
  fresh start-time probe; the background task is only for display latency.

Gating policy — block only on certainty:

- `status: unavailable` → reject the start with the existing
  `invalid_start_options`-style error, including the probe `reason` and the
  fix hint (install command / extras / sign-in). No session state is created.
- `credentials: missing` (definite, e.g. no token file and no active
  account) → reject with a hint to run `agy` and sign in.
- `credentials: unknown` → allow the start but include a warning in the start
  response and session settings metadata. False negatives must never block a
  working setup; the first turn's real error remains the authority.

Reporting: health is surfaced **only through `describe_options`** (which
takes a `workdir` and loads config, so it has the context to enumerate the
registry and probe it). The existing `agent-collab daemon status` command is
supervisor/PID state with no workdir/config context, so this stage does **not**
add backend health there — avoiding an underspecified, untested promise. This
is diagnostic surface, not new API semantics — the existing error shapes carry
the gating detail on start.

### Typed `antigravity_options`

Follow the stage 4.75 pattern (explicit mapping, unknown keys rejected).
Initial fields, with allowed values configurable via
`[agents.antigravity.options]`:

- `model` → `cli`: `--model`; `sdk`: config model field.
- `mode` (allowed `default | accept-edits | plan`) → `cli`: `--mode`. On
  `sdk` it is **`cli`-only until the spike confirms a faithful
  `LocalAgentConfig`/`CapabilitiesConfig` equivalent**: until then, a start
  that resolves an Antigravity agent to the `sdk` backend and passes `mode`
  is **rejected** with an actionable `invalid_start_options`-style error
  ("`antigravity_options.mode` is not supported on the `sdk` backend"), rather
  than silently dropping it. If the spike finds a faithful mapping, wire it and
  relax the rejection.

Threaded through `StartSessionRequest`, `--antigravity-options` on the CLI,
the HTTP payload, the MCP start schema, `validate_start_options` (including
the mode-aware rule: reject non-empty `antigravity_options` when the workflow
has no Antigravity agent), `describe_options`, and both backends'
option-application paths. Add `OPTION_FIELDS["antigravity"]`,
`SETTINGS_DISPLAY_FIELDS["antigravity"]`, an `_apply_antigravity_options`
helper, and `_infer_default` cases mirroring the existing providers.

### Dependencies and packaging

- `pyproject.toml` gains one optional extra:
  `antigravity-sdk = ["google-antigravity>=0.1,<1"]` (pin the tested version
  range once the spike confirms it).
- All SDK imports are lazy (inside the probe's `find_spec` check and
  `create_runner`), so the base install and the default `cli` backend stay
  standard-library only.
- Tests must not require the SDK: SDK runner tests use a fake
  `google.antigravity` module injected via `sys.modules` (or a thin injectable
  client factory on the runner), driven by the fixtures captured in the spike;
  registry/start tests assert the unavailable-backend / missing-extra error
  path.

## Out of scope

- **`claude`/`codex` SDK backends** (`claude-agent-sdk`, `openai-codex-sdk`).
  The registry is built to accept them as a drop-in `(claude, sdk)` /
  `(codex, sdk)` entry later, but implementing them is **firmly deferred** to a
  follow-up stage. This stage does not add them, and no acceptance criterion or
  test here depends on them.
- **Gemini CLI** as a first-class provider — removed (product retired
  2026-06-18).
- The **remote managed Antigravity Agent** via `google-genai` Interactions
  API (runs in Google's cloud sandbox, not the local workdir).
- Resuming provider sessions / continuing completed agent-collab sessions
  (this stage at most *captures* an id, if the spike finds one; nothing
  resumes it).
- Referee tool-gating via SDK policies/permission callbacks.
- Per-agent backend overrides at start time.
- Streaming partial deltas within a turn to clients; turns remain
  whole-message.
- A third-party plugin mechanism for out-of-tree backends.

## Implementation steps

Ordered for small, independently-safe commits. Steps 1–6 land the registry,
selection, and live health with `cli` as the only real backend and are useful
on their own; the Antigravity provider (7–10) builds on top.

1. Create `backends/` package: `base.py` (protocol, `BackendCapabilities`,
   health types, `BackendUnavailable`), registry keyed by
   `(agent_type, backend_id)` with resolution
   (request > agent config > default `cli`).
2. Move subprocess construction from `runners.configured_runner` into
   `backends/cli.py`; keep `configured_runner` as the registry-backed entry
   point. Its argument list grows to accept a resolved backend id (call-site
   *shape* is preserved; the referee passes the resolved id in). Existing
   suite stays green with the default `cli` resolution.
3. Add `backend` to `AgentConfig`, `_merge_agent`, and `validate_agent`
   (registered-for-type check; `command` required only for `cli`; `mock`
   rejects `backend`); update config docs.
4. Add `backend` to `StartSessionRequest`, CLI `--backend`, HTTP payload, and
   the `agent_collab_start` MCP schema; validate (registered + available)
   before any session state is created; resolve the effective per-agent
   backend map once and thread it into `RefereeConfig` →
   `Referee._runners()` → `configured_runner` (see "Execution wiring"), with a
   test that the resolved backend reaches the runner, not just the settings.
5. Extend `describe_options` and `build_session_settings` with backend info
   (ids, default, availability, capabilities); add the `capabilities` summary
   to `SessionState` and the session index (all `false` this stage).
6. Add backend health probes (presence, version, best-effort credentials),
   the TTL cache, fresh re-probe + gating on start, and health in
   `describe_options` (not `daemon status` — see "Backend health").
   *(Optional follow-up: periodic refresh task with transition logging.)*
7. **Spike:** capture `agy -p` and `google-antigravity` samples into
   `tests/fixtures/antigravity/`; confirm output/event shapes and whether a
   resumable id exists; update the mapping section of this doc if reality
   differs.
8. Add the `antigravity` provider on `cli`: agent type + `VALID_SOURCES`
   entry, `_event_source`/`MockRunner` source handling for `antigravity`,
   disabled built-in config entry, `parse_antigravity_line` (plain-text,
   message-only) from captured samples, typed `antigravity_options` end to end
   (validate via `_options_for`, `--antigravity-options`, HTTP, MCP schema,
   describe/settings, `cli` flag mapping).
9. Add `pyproject` extra `antigravity-sdk`; implement
   `backends/antigravity_sdk.py` with lazy import, typed-event → `Event`
   mapping (from spike fixtures), explicit option mapping, and (if confirmed)
   conversation-id capture.
10. Update README, `doc/agent-configuration.md`, and
    `doc/daemon-architecture.md` (backend dimension, Antigravity enablement +
    sign-in notes, `cli` fidelity caveat, `sdk` extra).

Step 8 is independent of step 9 and can land in either order after 7; the
`cli` path is the lower-risk one to land first.

## Tests

- **Registry:** resolution precedence (request > agent config > default);
  unknown backend id rejected with the registered ids for that type listed;
  `(antigravity, cli)`, `(antigravity, sdk)`, `(claude, cli)`, `(codex, cli)`
  resolve, `(claude, sdk)` does not (deferred).
- **Config:** `backend = "sdk"` on an `antigravity` agent parses and
  validates; `backend = "sdk"` on a `claude` agent fails (not registered);
  unknown backend value fails with registered ids; `command` optional for the
  `sdk` backend, still required for `cli`; `mock` rejects `backend`.
- **Start validation:** `--backend sdk` with a workflow agent lacking that
  backend fails before session creation; unavailable SDK package produces the
  install-hint error naming the `antigravity-sdk` extra; valid request records
  effective backends in session settings.
- **Override reaches execution (not just settings):** a start that resolves an
  agent to a non-default backend actually constructs that backend's runner via
  `RefereeConfig` → `configured_runner` (assert with a fake registry/runner),
  guarding against the settings/runtime divergence — the resolved backend map
  is used, not a re-resolution.
- **`describe_options`:** includes a `backends` section with per-type ids,
  default, availability, capabilities, and health/reason.
- **Health probes (fake PATH/clock/filesystem, no real CLIs or SDK):** missing
  `agy` → `unavailable` with reason; `agy` appearing on PATH flips the cached
  status to `ok` on the next fresh probe without daemon restart; SDK
  `find_spec` present/absent flips `sdk` availability; credential check reports
  `ok` when a token file/active account is present, `missing` when absent,
  `unknown` when indeterminate; start re-probes fresh and rejects
  `unavailable`/`missing` with fix hints while `unknown` starts with a warning
  recorded in settings metadata; probes never launch a model call.
- **Capability honesty:** every session this stage reports `resumable: false`
  and `interruptible: false`; the summary is computed by the reducer (not
  hard-coded) so a later stage can flip inputs; `capabilities` survives daemon
  restart via the session index.
- **Antigravity `cli` (subprocess, fixture-driven):** `parse_antigravity_line`
  turns plain-text lines into `antigravity` `message` events with correct
  source attribution and no crash on empty/odd output; the runner emits the
  referee `command` start and `status` exit events; config entry validates and
  is disabled by default; enabling it and referencing it from a workflow
  passes validation; `antigravity_options` validation incl. rejection when the
  workflow has no Antigravity agent; mock and dry-run runners work for an
  `antigravity` agent **and attribute events to source `antigravity`** (guards
  the `MockRunner`/`_event_source` fix, not the old codex fallback).
- **Antigravity `sdk` (fake `google.antigravity` module + spike fixtures):**
  text events become `message`; **if** the spike confirmed typed tool events,
  `ToolCall`s classify as `tool_call`/`command`/`file_change`, otherwise the
  runner degrades to message-only and the test asserts that honest fallback;
  reasoning/`thoughts` never leak opaque signatures and are hidden unless
  verbose; missing-extra import path raises the actionable install error;
  conversation-id capture asserted only if the spike confirmed an id
  (otherwise assert it is absent, not empty-string).
- **Existing suite stays green** with no config changes (default `cli`,
  Antigravity disabled, no new required dependency).

## Acceptance criteria

- `agent-collab start "Task"` behaves exactly as today with no config
  changes; the base install has no new required dependencies and stays
  standard-library only.
- Setting `agents.antigravity.backend = "sdk"` (or passing `--backend sdk`)
  for a workflow whose every selected agent supports `sdk` (i.e. an
  Antigravity-only workflow this stage) runs its turns through the SDK runner
  and emits the standard `Event` types. The *granularity* of those events
  (typed tool/file events vs. message-only) is whatever the spike confirms the
  SDK actually exposes — the criterion is "correct, honest events," not "typed
  tool events" as an unconditional promise.
- Enabling `agents.antigravity` on the default `cli` backend and adding it to
  a workflow runs `agy -p` turns with correct `antigravity` transcript
  attribution and typed `antigravity_options`, with no changes outside config
  for the user — and the `cli` fidelity limit (message-only) is documented,
  not hidden.
- Backend selection is discoverable via `describe_options` and reflected in
  session settings metadata and the session index.
- Every session honestly reports its capabilities (`resumable`,
  `interruptible`) in status/list/HTTP/MCP responses, derived from the actual
  backends in use — never from provider brand; every session this stage
  reports `false` because no backend implements resume/interrupt yet.
- Selecting an uninstalled `sdk` backend fails the start request with a
  machine-readable error and an install hint (`antigravity-sdk` extra); no
  session state is created.
- Backend availability is live: installing `agy` (or signing in, or
  `pip install`-ing the extra) makes the backend startable on the next start
  with no daemon restart; the state is visible in `describe_options`.
- A start request against a truly unusable backend (missing binary/SDK or
  definitely missing credentials) is rejected before any session state exists,
  with the probe reason and a fix hint; uncertain credential state only warns.
- Adding a hypothetical new backend requires only a new module implementing
  `AgentBackend` plus a registry entry, and a new provider only an agent type,
  parser/mapper, and config entry — no changes to referee, daemon, or event
  plumbing.

## Open questions (for review / to resolve during implementation)

1. **SDK API shape** — the `Agent`/`ChatResponse`/`ToolCall` names are a
   PyPI/README hypothesis for a v0.1.x package; the spike must confirm them
   before the mapper is written, and this doc updated if they differ.
2. **SDK event granularity** — the spike must establish whether the SDK
   yields typed tool/file events or text-only; the `sdk` runner degrades to
   message-only if the former is not available (see "Why the SDK path
   matters").
3. **Resumable id** — does the `google-antigravity` SDK expose a stable
   conversation id? Decides whether `agent_sessions` ships this stage at all.
4. **`antigravity_options.mode` on `sdk`** — the spike confirms whether
   `LocalAgentConfig`/`CapabilitiesConfig` has a faithful equivalent to
   `--mode`. Until it does, `mode` is `cli`-only and rejected on `sdk` (see
   "Typed `antigravity_options`"); this is a spike input, not an undecided
   design question.

(Backend-resolution consistency — resolve once at start, carry the map into
`RefereeConfig` — is settled as a hard requirement in "Execution wiring", not
an open question.)
