# Usage-window alignment

**Status:** Closed in 0.10.0 (2026-07-16). The full hermetic gate passed;
credentialed usage-window verification was not run because it makes paid calls.

**Created:** 2026-07-15

**Issue:** [#41](https://github.com/lauriparviainen/agent_collab/issues/41)

## Context

Some provider plans account for usage in provider-defined rolling or fixed
windows. A user may want an optional daemon-owned scheduler to make one minimal
request near the start of their working time and at a configured interval
thereafter, so any window activated by that request is aligned with the hours
when they expect to work.

`agent-collab` cannot reset, inspect, or guarantee a provider quota window. It
can only make a normal backend request at a scheduled time. Provider accounting
rules can change, can group several models together, can differ between CLI and
SDK credentials, and may not be affected by a minimal request at all. The
feature and documentation must consistently describe the behavior as
**usage-window alignment**, not quota reset.

The daemon is the right owner because it already has global lifetime, backend
configuration, provider credentials inherited from its environment, and a
background-maintenance pattern. This is daemon-global owner policy: project
configuration and agents connected over MCP must not be able to enable or
alter scheduled paid calls.

Scheduled calls run through the same session path as any other collaboration
session and are therefore visible in the TUI, CLI, and API like regular
review workflows. This is deliberate, for auditing: every scheduled paid
request leaves the same trail as a user-initiated session — a session-index
entry, a transcript, and an event stream — so nothing the daemon does with
the user's credentials is invisible.

Scheduled requests may consume quota or incur cost. The packaged config should
make the feature easy to opt into without making any request by default. It
should declare an economical model target for every shipped real backend, with
each target independently disabled, so enabling one target is a one-line user
override.

## Goals

- Schedule minimal backend requests only inside a user-defined work-time
  window.
- Anchor requests at the work-time start and repeat at a configured interval
  without accumulated drift.
- Support every shipped real CLI and SDK backend.
- Support several independently scheduled models on the same backend.
- Provide global schedule defaults with per-target overrides, including
  different work times for different models.
- Ship economical target definitions in `default_config.toml`, all disabled.
- Let a user enable one shipped target without restating its backend, model, or
  the rest of the shipped matrix.
- Keep daemon restart, wall-clock changes, failures, and retries from causing
  duplicate-call storms.
- Make every scheduled call a normal, visible session in the TUI and API, and
  make the last result and next scheduled request observable in daemon status
  without duplicating provider response content there.
- Keep the implementation hermetic under the normal test gate; real provider
  calls belong only in `integration_tests/`.

## Non-goals and policy boundaries

- Do not claim that a request resets or extends provider quota.
- Do not infer provider quota groups from provider, model, backend mechanism,
  account, or credential identity.
- Do not randomize timing to conceal automation or imitate human behavior.
  Bounded jitter exists only for schedule dispersion and collision avoidance.
- Do not bypass provider rate limits, retry instructions, terms, or account
  controls. A provider rejection is a terminal attempt result; the target
  waits for the next eligible anchor and honors any provider retry guidance.
- Do not expose an arbitrary configurable prompt. The request is a fixed,
  transparent, minimal probe owned by the application.
- Do not give the probe project context, conversation history, or access to
  any repository. It runs in an isolated, empty, daemon-owned directory with
  the strictest non-writing posture the backend's options offer.
- Do not add an MCP tool or project-config switch for enabling, disabling, or
  immediately triggering a paid request.
- Do not dynamically discover a provider's cheapest model. Shipped model names
  remain explicit, reviewable config and follow the normal release process
  when they change.
- Do not live-reload this daemon policy in the first implementation. User
  changes take effect after daemon restart, like session retention settings.

## Configuration design

Add global-user-only `[system]` and `[usage_windows]` sections and bump the
config schema from 8 to 9. `[system]` is the designated home for
installation-wide settings that more than one feature may need; its first
field is `timezone`, and usage-window alignment is its first consumer. Later
features (for example CLI/TUI date-time presentation) reuse `[system]`
instead of growing their own copies. The section earns fields only when a
feature needs them — do not add locale, format, or other settings
speculatively in this task. Persisted event, session, and daemon timestamps
remain UTC; this task does not rewrite existing timestamp storage or broadly
change CLI/TUI timestamp presentation.

Defaults live in `agent_collab/default_config.toml` because the target matrix
is intentionally inspectable and user-overridable. This differs from
session-retention defaults, which are typed values without a backend matrix.

Use named target subtables rather than an array of tables. Named tables merge
field by field across built-in and user config, so the user can enable one
packaged target with one setting:

```toml
[usage_windows.targets.codex_cli_luna]
enabled = true
```

An array would force replacement or identity-by-index semantics and would make
that simple override ambiguous.

Target `options` tables follow the same convention as backend options, whose
merge keeps built-in values as defaults precisely so a user table never
silently drops shipped defaults: packaged target options act as defaults, and
a user options table overlays them key by key.

### Proposed packaged defaults

```toml
[system]
timezone = "local"

[usage_windows]
# The feature is effectively inactive while every target is disabled. There is
# no second master enable switch: per-target enablement is the opt-in and avoids
# requiring two settings to activate one model.
days = ["mon", "tue", "wed", "thu", "fri"]
work_time = { start = "09:00", end = "17:00" }
interval = "5h"
jitter = "5m"

[usage_windows.targets.claude_cli_sonnet]
enabled = false
backend = "claude_cli"
model = "sonnet"

[usage_windows.targets.claude_cli_sonnet.options]
thinking_level = "low"
permission_mode = "plan"

[usage_windows.targets.claude_sdk_sonnet]
enabled = false
backend = "claude_sdk"
model = "sonnet"

[usage_windows.targets.claude_sdk_sonnet.options]
thinking_level = "low"
permission_mode = "plan"

[usage_windows.targets.codex_cli_luna]
enabled = false
backend = "codex_cli"
model = "gpt-5.6-luna"

[usage_windows.targets.codex_cli_luna.options]
thinking_level = "low"
sandbox = "read-only"

[usage_windows.targets.codex_sdk_luna]
enabled = false
backend = "codex_sdk"
model = "gpt-5.6-luna"

[usage_windows.targets.codex_sdk_luna.options]
thinking_level = "low"
sandbox = "read-only"

[usage_windows.targets.antigravity_cli_flash_low]
enabled = false
backend = "antigravity_cli"
model = "Gemini 3.5 Flash (Low)"

[usage_windows.targets.antigravity_cli_flash_low.options]
mode = "plan"
sandbox = true

[usage_windows.targets.antigravity_sdk_flash_low]
enabled = false
backend = "antigravity_sdk"
model = "Gemini 3.5 Flash (Low)"

[usage_windows.targets.xai_cli_grok_4_5]
enabled = false
backend = "xai_cli"
model = "grok-4.5"

[usage_windows.targets.xai_cli_grok_4_5.options]
thinking_level = "low"
sandbox = "read-only"
provider_max_turns = 1

[usage_windows.targets.xai_sdk_grok_4_5]
enabled = false
backend = "xai_sdk"
model = "grok-4.5"

[usage_windows.targets.xai_sdk_grok_4_5.options]
thinking_level = "low"
```

The model names match the economical live-integration defaults in
`integration_tests/harness.py` (`DEFAULT_LIVE_OPTIONS`); the installed Grok
CLI's `grok models` catalog reports `grok-4.5` as its default. The option
postures are chosen for this feature, not taken from that harness, which sets
only `model` and `thinking_level`. Two postures are deliberately stricter than
the shipped session defaults in `default_config.toml`: the Claude targets use
`permission_mode = "plan"` (the strictest read-only mode) instead of the
shipped `"default"`, and `antigravity_cli` adds `sandbox = true`. The
`xai_cli` target intentionally sets no `permission_mode`: it inherits the
shipped `bypassPermissions` posture, because `plan` and `auto` are documented
to stall headless runs on that CLI. It does set `provider_max_turns = 1` to
cap Grok's internal model/tool loop, so one anchor cannot fan out into
several provider calls. Revalidate all names and backend option
contracts during implementation and before each release that changes the
matrix. In particular, a model being economical does not establish that it
activates the same usage window as another model the user intends to run.

All eight real canonical backends are represented. `mock` is deliberately not
packaged as a scheduled target because it has no provider usage window. Unit
tests use an injected fake invocation instead.

CLI and SDK targets for one provider are independent definitions. Users should
not enable both merely because both exist: they may share provider accounting,
and `agent-collab` intentionally does not guess.

### Enabling one packaged target

The minimal user config is:

```toml
[usage_windows.targets.codex_cli_luna]
enabled = true
```

The target inherits the packaged backend, model, economical options, days,
work time, interval, and jitter, and all schedules use
`[system].timezone`. Every other packaged target remains disabled.

### Per-model work-time override

```toml
[usage_windows.targets.claude_cli_sonnet]
enabled = true
work_time = { start = "16:00", end = "22:00" }

[usage_windows.targets.codex_cli_luna]
enabled = true
work_time = { start = "08:00", end = "14:00" }
```

Targets may override `days`, `work_time`, `interval`, and `jitter`. An omitted
field inherits the global usage-window value. Timezone is deliberately not a
target field: it is one system-wide setting. A future requirement for
cross-timezone schedules should introduce an explicit schedule-zone concept
rather than overloading the system timezone accidentally.

### Adding another model on the same backend

```toml
[usage_windows.targets.codex_cli_second_window]
enabled = true
backend = "codex_cli"
model = "another-model"
work_time = { start = "13:00", end = "19:00" }

[usage_windows.targets.codex_cli_second_window.options]
thinking_level = "minimal"
sandbox = "read-only"
```

Config-level entity names (workflows, backends, personas) currently have no
enforced identifier rule, so this feature defines one for target names
explicitly instead of pointing at nonexistent precedent; the pattern is in the
validation list below. A new target must provide `backend` and `model`; an
override of a built-in target inherits them. Multiple targets may reference
the same backend. Exact duplicate enabled `(backend, model)` pairs are
rejected because they almost certainly create accidental duplicate calls;
different models on the same backend are valid.

### Validation

- `system.timezone` is `"local"` or a valid IANA timezone accepted by
  `zoneinfo.ZoneInfo`.
- `days` is a non-empty array containing unique lowercase values from `mon`
  through `sun`.
- `work_time.start` and `.end` use zero-padded local `HH:MM` and must differ.
  An end earlier than the start denotes an overnight window.
- `interval` is a positive whole-number duration supporting `m` and `h`, and
  at least `15m`: denser schedules serve no usage-window purpose and only
  multiply paid calls.
- `jitter` is a non-negative whole-number duration supporting `m`; `0m`
  disables it. It must be smaller than the interval.
- Target names match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, the pattern
  `outcomes.py` already enforces for runtime agent and turn identifiers.
- Target `enabled` is a boolean.
- Target `backend` is a canonical registered backend, not a persona or
  workflow member.
- Target `model` is a non-empty string and is normalized as the backend's
  declared `model` option.
- Target `options` uses the existing backend-owned `options.toml` contract.
  `model` may not also appear in `options`; the backend contract treats
  `model` as an ordinary option and would silently accept it there, so
  usage-window validation enforces the exclusion itself.
- Unknown global or target fields are rejected.

An enabled target whose backend is globally disabled or unavailable remains
visible but ineligible, reusing the existing reason codes `backend_disabled`
and `backend_unavailable` (both already emitted verbatim by `options.py`).
Because scheduled calls use the normal session-start path, no other
per-backend capability gate is needed. An ineligible target does not prevent
the daemon from serving sessions. No request is attempted until it becomes
eligible after config correction and daemon restart.

The current retention duration parser is retention-specific and accepts only
positive `h`, `d`, and `w` values. Do not silently broaden the retention CLI
grammar while implementing this feature: `retention.parse_duration` is also
used by the REST prune path. Add a shared low-level helper parameterized by
allowed units and zero policy, re-express the retention parser over it with
unchanged behavior, and give usage windows its own `m`/`h` grammar on the same
helper.

### Scope and migration

Add `system` and `usage_windows` to the known top-level schema. Two
separate lists must gain the new sections: `KNOWN_TOP_LEVEL_KEYS` in
`config.py` and the project-scope strip list in `config_migrations.py`. The
v8-to-v9 migration is an additive no-op stamp for existing files, matching the
v2-to-v7 pattern (v7-to-v8 was the one structural exception). Installation
rewrites user config through the existing comment-preserving path
(`migrate_user_config_file`); because the stamp changes no shape, its
tomlkit-primary/regex-fallback stamping remains sufficient and the module's
"first shape-changing migration must require tomlkit" reservation is not
triggered.

Project configuration cannot influence this policy. During project-scope
migration, strip `[system]` and `[usage_windows]` with sanitized warnings
alongside `[backends]`, `[daemon]`, `[sessions]`, and `[workdir]`. The warnings
should say that system settings and scheduled provider calls are allowed
only in global user config.

`agent-collab config show --workdir PROJECT` should display effective global
settings and target eligibility without provider credentials or daemon-token
content.

## Scheduling semantics

For each target and each configured local day:

1. Resolve the system timezone plus the target's applicable days, work time,
   interval, and jitter.
2. Construct the work interval beginning on that local day. If `end <= start`,
   the interval ends on the following local day.
3. Produce base anchors at `start + N * interval`, beginning with `N = 0`,
   while the anchor is strictly before `end`.
4. Choose one jittered execution time for each anchor from the intersection of
   the anchor's configured jitter range and the half-open work interval
   `[start, end)`.
5. Persist that chosen time before sleeping so daemon restart does not reroll
   it into an immediate duplicate.

For example, `09:00`-`17:00` with `interval = "5h"` produces two base
anchors: 09:00 and 14:00. `16:00`-`22:00` produces 16:00 and 21:00. An anchor
at exactly the end is outside the interval and is not scheduled. The work time
therefore determines the number of calls; there is no `windows_per_day` field.

Jitter is calculated independently from each base anchor and never from the
previous actual execution time, so it cannot accumulate schedule drift. At a
work-window boundary the random range is clipped to remain inside the window.
The codebase has no clock or randomness abstraction to reuse; the established
pattern is `retention.py`'s pure functions taking `now` as an argument. Follow
it: the planner takes `now` and a random source as parameters, with
`secrets.SystemRandom` in production (cryptographically strong rather than the
predictable process-global PRNG), but do not present this as concealment: logs
and the fixed probe identify the request as scheduled automation.

The scheduler uses timezone-aware wall-clock calculations and monotonic sleeps.
It recalculates after waking so clock jumps do not cause a burst. Define and
test daylight-saving behavior:

- an ambiguous local anchor runs once, using a deterministic fold;
- a nonexistent local anchor advances to the first valid instant still inside
  the work window;
- the overnight window belongs to the configured day on which it starts.

A missed anchor — the daemon was not running, or the machine was off or
suspended at the planned time — is accounted for explicitly rather than
silently skipped. Whenever the scheduler starts or wakes it recalculates and
applies one rule:

- An anchor counts as missed only when persisted state for the target
  carries the current schedule fingerprint and shows a planned (jittered)
  execution time that passed with no recorded attempt. A restart between the
  anchor and its planned time resumes the persisted plan without rerolling
  jitter.
- If now is inside the target's work window and the latest missed anchor has
  no recorded attempt, perform a single catch-up attempt now, after a fresh
  jitter delay clipped to the window and to the next anchor. The user
  declared this whole period as work time, so one late call inside the
  window is the expected behavior, not a surprise.
- A target with no state entry (just enabled for the first time) or whose
  state carries a different schedule fingerprint never catches up: the
  scheduler writes fresh state and plans from the next future anchor. This
  closes the loophole where "no plan persisted" would otherwise read as
  "missed" and turn a config edit into an immediate paid call.
- Anything older than that latest anchor is permanently skipped and logged;
  catch-up never replays a backlog.
- Outside the work window nothing is caught up; the scheduler plans the next
  future anchor.
- Catch-up requires trustworthy state. If the state file is missing, corrupt,
  or from a future schema version, the daemon cannot prove the anchor was not
  already served — fail closed: no catch-up, plan the next future anchor
  only.
- A failed attempt counts as an attempt: catch-up applies to anchors that
  were never tried, so it can never become a retry loop for the no-retry
  rule.

Persisted per-anchor attempt/success state is the duplicate guard, and the
deterministic session ID doubles as a collision guard, so restart loops and
repeated wakes cannot turn one missed anchor into several calls. The same
rule covers suspend/resume: monotonic sleeps do not advance during suspend,
so the scheduler treats every wake as a recalculation point and logs whether
each anchor ran on time, was caught up, or was skipped.

## Invocation boundary

A scheduled call is a normal collaboration session started internally by the
daemon — deliberately visible in the TUI, CLI, and API, exactly like regular
review workflows, so every scheduled paid request leaves the same audit trail
as a user-initiated session. The scheduler constructs a `StartSessionRequest`
directly (not via `from_wire`) and awaits `SessionManager.start_session`, the
same internal entrypoint the REST route and MCP `agent_collab_start` converge
on. The bearer-token check lives only at the HTTP boundary, so the in-process
caller crosses no auth layer and needs no special identity. The request uses:

- a packaged single-member `[workflows.usage-window]` workflow (shipped in
  `default_config.toml`), so listings and the TUI workflow column distinguish
  scheduled sessions from user sessions without any `SessionState` schema
  change. The packaged member is only a placeholder: the scheduler always
  overrides the member's backend, model, and options per target through the
  same start-override surface MCP `start` offers (`backend`, `members`,
  `backend_options`), so the placeholder's enablement state must not gate
  scheduling;
- `max_turns = 1` and `interactive = false`: exactly one non-interactive
  provider turn, no input queue, no provider continuation;
- a fixed application-owned prompt that requires no tool use, such as
  `agent-collab usage-window alignment request. Do not use any tools; reply
  with exactly: OK`. The prompt is what makes tool disabling unnecessary; the
  targets' read-only options (`permission_mode = "plan"`,
  `sandbox = "read-only"`, `mode = "plan"`) remain as defense in depth —
  `codex_sdk` has `sandbox` as its only posture control — and
  `antigravity_sdk`, which exposes no posture option, has only the isolated
  workdir scoping its default behavior;
- an isolated daemon-owned empty directory under `data/tmp/usage-windows/`,
  created owner-only, never a repository or session workdir. An empty workdir
  also guarantees `load_config` finds no project configuration, so the
  session resolves against global config only;
- a deterministic session ID derived from target and anchor (for example
  `uw-codex_cli_luna-20260715-0900`), which makes scheduled sessions easy to
  find and makes an accidental duplicate for the same anchor collide instead
  of silently running twice.

Because the session is normal, transcripts, events, the session index,
`list_sessions`, `read_events`/`wait_events`, the TUI picker, and session
retention all apply with no new observation code. Runner cleanup
(`terminate_process` / `close_async_stream`) applies unchanged.

State the isolation limits honestly: the empty workdir scopes the agent's
default behavior but is not filesystem sandboxing — read-only postures still
allow reading user-readable absolute paths, and a model that ignores the
no-tools instruction spends extra provider calls inside its one turn. Bound
that loop where a control exists: the packaged `xai_cli` target sets
`provider_max_turns = 1`, which caps Grok's internal model/tool loop (it is
separate from the workflow's `max_turns` and otherwise keeps a
version-specific provider default). The residual risk — a disobedient model
reading a user-readable path or making a few extra in-turn calls — is
accepted and visible in the session transcript.

One carve-out is required: `_prepare_session_start` enforces
`validate_workdir_allowed`, so a workdir under `~/.agent-collab/data/` is
rejected whenever the user sets `restrict_workdir_roots`. Do not widen
`validate_workdir_allowed` itself — it sees only config and a path, so a
global exemption would let any REST/MCP/CLI caller start sessions under
`data/tmp/usage-windows/` against the user's explicit lockdown. Instead, add
an internal-only exemption flag on `StartSessionRequest` that `from_wire`
never sets (the DTO already carries internal computed fields the wire cannot
reach), set solely by the scheduler for its own daemon-owned workdir.
External starts remain subject to the unmodified allowlist.

Manual starts of the `usage-window` workflow via CLI or MCP are permitted and
harmless: they are ordinary user-initiated paid sessions under ordinary
workdir validation (so under `restrict_workdir_roots` they need an allowed
directory). The policy boundary is unchanged — only global user config can
enable or alter the *schedule*.

The session-start path already performs option normalization
(`normalize_start_options` into the backend-owned `normalize_options`), so
the scheduler needs no normalization machinery of its own. Backend health is
weaker: start-time gating deliberately skips probes for backends with
`block_on_unavailable = false` (`claude_cli`, `codex_cli`), so a missing
executable would not block the start — it would fail a session at every
anchor instead. The scheduler therefore runs the existing side-effect-free
backend probe itself before spending an anchor and marks the target
`backend_unavailable` on failure, regardless of the backend's session-start
gating policy. `server_http.py` contains no backend-specific option branching
today; keep it and the scheduler that way — postures stay in target options,
mapped by backend packages.

`start_session` returns as soon as the session task is launched. The
scheduler then awaits the session's terminal status through the same manager
state observers use, bounded by the session `timeout`, and records the
sanitized outcome plus session ID in scheduler state.

Naming: `probe` already means the backend *health* probe
(`AgentBackend.probe`, `HEALTH_UNAVAILABLE`) in this codebase. In code, logs,
and status output, call this operation the usage-window probe or minimal
request, never a bare "probe".

Only a completed provider turn counts as success. Provider rejection,
authentication failure, timeout, unavailable executable/dependency, malformed
stream, cancellation, and cleanup failure are recorded as sanitized attempt
outcomes. Never retry against an explicit provider rate-limit response before
the provider's retry guidance permits it.

## Daemon ownership and persistence

Introduce typed configuration such as:

```python
@dataclass
class WorkTimeConfig:
    start: time
    end: time

@dataclass
class SystemConfig:
    timezone: str

@dataclass
class UsageWindowTargetConfig:
    id: str
    enabled: bool
    backend: str
    model: str
    options: Dict[str, Any]
    days: Optional[List[str]] = None
    work_time: Optional[WorkTimeConfig] = None
    interval: Optional[timedelta] = None
    jitter: Optional[timedelta] = None

@dataclass
class UsageWindowsConfig:
    days: List[str]
    work_time: WorkTimeConfig
    interval: timedelta
    jitter: timedelta
    targets: Dict[str, UsageWindowTargetConfig]
```

`CollaborationConfig` owns `system: SystemConfig` separately from
`usage_windows: UsageWindowsConfig`. The exact representation may differ, but
runtime code consumes fully validated typed values rather than raw TOML
mappings.

Load built-in plus global user configuration once at daemon startup. The
current `_load_sessions_config` helper should evolve into a daemon-policy load
that supplies both retention and usage-window settings without independently
parsing the user file several times — today `run_server` parses it at least
twice (`ensure_daemon_token`, then `_load_sessions_config`). On any config or
I/O error, fail closed for scheduled calls exactly as the helper already does
for retention (it returns `retention_days=0`): start the daemon with
usage-window scheduling disabled and log the sanitized reason. Do not fall
back to enabled paid defaults. Existing session-start validation continues to
surface the underlying config error.

The server owns one scheduler task, analogous to the retention task
(`start_retention_task` / `_retention_loop` in `server_http.py`), only when at
least one target is enabled; eligibility is evaluated inside the loop, since
backend availability is only knowable at runtime. The task must be cancelled
and awaited during server unwinding, mirroring the retention task's
cancel-and-suppress pattern in `serve()`. A failure in one target must not
terminate the loop or block other targets.

Persist scheduler state atomically at:

```text
~/.agent-collab/data/daemon/usage-window-state.json
```

Keep the file and containing daemon directory owner-only: declare the path in
`GlobalDataPaths` (`paths.py`) and write it with the existing
`atomic_write_private_text` helper (atomic replace, mode `0600`); the daemon
directory is already created `0700` by `ensure_dirs`. Note that `data/tmp/`
exists but is currently unused and is *not* chmod'd by `ensure_dirs`, so it
is created with the process umask before the scheduler ever runs. Patch
`ensure_dirs` to chmod `data/tmp/` itself `0700` (it has no other consumers)
and create `data/tmp/usage-windows/` owner-only as well, so the probe
directory is not enumerable through a permissive parent. State is keyed by
target ID and includes only scheduling metadata:

```json
{
  "schema_version": 1,
  "targets": {
    "codex_cli_luna": {
      "schedule_fingerprint": "...",
      "anchor": "2026-07-15T09:00:00+03:00",
      "planned_at": "2026-07-15T09:03:12+03:00",
      "last_attempt_at": "...",
      "last_success_at": "...",
      "last_outcome": "completed",
      "last_session_id": "uw-codex_cli_luna-20260715-0900",
      "consecutive_failures": 0,
      "retry_not_before": null
    }
  }
}
```

The schedule fingerprint covers target ID, backend, model, normalized options,
and effective schedule fields, but no credentials or environment values.
Provider response content, prompts, and exception detail must not be
persisted in scheduler state — the session transcript is already the full
record, so state stores only the session ID for cross-reference. A changed
fingerprint invalidates future planned state and recalculates it; it does not
trigger the missed-anchor catch-up, because state under the old schedule
proves nothing about anchors of the new one.

The first implementation performs no same-anchor retry: a failed attempt
records its sanitized outcome and the target waits for the next anchor. One
scheduled anchor produces at most one attempt and at most one successful
call. The `retry_not_before` field is honored whenever it is set — no
attempt, including the next anchor's, starts before it — but the typed
`TurnOutcome` carries no retry-after today (outcome, canonical code/message,
stop reason, exit code only), so there is no structured source for provider
retry guidance yet. Step 4 adds an optional typed retry-after to the
turn-outcome mapping where a backend can supply one; until a backend does,
`retry_not_before` stays null and the protection against calling inside a
provider's retry window is the interval spacing plus the no-retry rule — a
documented residual gap, not silent behavior. If a later version adds a
same-anchor retry, it must use bounded
exponential backoff with jitter, never cross the work-window end, and keep a
deliberately small, tested budget.

## Observability

Scheduled sessions are themselves the primary audit record: they appear in
`list_sessions`, the TUI picker, transcripts, and the event APIs like any
review workflow, labeled with the `usage-window` workflow id and a
deterministic `uw-...` session ID.

Add a compact usage-window section to `agent-collab daemon status`
(`_print_daemon_state` in `cli.py`). The command has no `--json` flag today
and this task does not add one; structured output can follow the `--json`
precedent of other commands later. The section derives enablement and the
effective schedule from config, and planned/attempt/outcome data from the
persisted state file — atomic replacement makes direct point-in-time reads
safe, so no new daemon route is needed. Config and the running daemon can
disagree until restart (policy does not live-reload), so the CLI compares
each target's fingerprint computed from current config against the
fingerprint in state and labels mismatches `pending restart` instead of
presenting edited config as the active schedule. The daemon records
ineligibility reasons into the state file when it evaluates targets, so the
CLI never probes backend health just to render status. Show enabled targets in full and
collapse disabled packaged targets into a single count line so the shipped
all-disabled matrix does not drown the output. For every enabled target,
show:

- enabled and eligible state;
- canonical backend and model;
- effective work time, timezone, days, interval, and jitter;
- next planned request, if any;
- last attempt and last success;
- sanitized last outcome or ineligibility reason;
- last session ID, so the transcript can be opened directly.

Daemon logs record target ID, backend/model identity, planned time, attempt
start, sanitized terminal outcome, and — for every anchor — whether it ran on
time, was caught up, or was skipped, so missed starts are visible from the
log files alone. Do not log provider response text, credentials, full
environment, or raw exception payloads that may contain request details.

No manual `run-now` API, REST route, or MCP tool is part of this task. If a
future owner-operated diagnostic command is added, it must make cost and
external side effects explicit and require direct user confirmation.

## Implementation plan

1. **Schema and configuration**
   - add the v8-to-v9 stamp migration and add both `system` and
     `usage_windows` to `KNOWN_TOP_LEVEL_KEYS` and to the project-scope
     strip list — two sections, two entries in each list;
   - add typed system and usage-window config plus field-by-field target
     merging;
   - validate schedules, targets, backend references, models, and options;
   - add all-disabled target defaults for the eight real backends;
   - include effective targets in `config show`.

2. **Pure schedule planner**
   - implement timezone-aware daily and overnight interval calculation;
   - derive anchors, bounded jitter, next-due selection, and schedule
     fingerprints as pure functions taking `now` and the random source as
     arguments, retention-style;
   - define DST and restart/missed-anchor behavior in tests.

3. **State persistence**
   - add an owner-only atomic state store (path declared in
     `GlobalDataPaths`, written via `atomic_write_private_text`);
   - restore planned anchors and prior outcomes defensively;
   - tolerate missing, stale, future-version, or malformed state without
     making an immediate call.

4. **Scheduled session start**
   - ship the packaged single-member `usage-window` workflow;
   - build the internal `StartSessionRequest` (fixed no-tools prompt,
     `max_turns=1`, `interactive=false`, per-target backend/member/options
     overrides, deterministic session ID);
   - create the owner-only isolated workdir and add the scheduler-internal
     workdir exemption (a non-wire `StartSessionRequest` flag), leaving
     `validate_workdir_allowed` unchanged for external callers;
   - await terminal session status bounded by the session timeout and map it
     to a sanitized attempt outcome;
   - add an optional typed retry-after to the turn-outcome mapping where a
     backend can supply one, feeding `retry_not_before`.

5. **Daemon scheduler**
   - load global daemon policy once;
   - start/cancel the task with server lifetime;
   - run the side-effect-free backend probe before each attempt so
     unavailable backends never spend an anchor;
   - serialize each target's attempt, allow independent targets to progress,
     persist before/after attempts, and honor `retry_not_before` across
     anchors;
   - apply the bounded missed-anchor catch-up rule without duplicate calls
     across restarts, wakes, or clock jumps.

6. **Status and documentation**
   - expose effective schedule and sanitized outcomes through daemon status;
   - document opt-in, cost/quota caveats, one-target enablement, custom
     targets, per-model overrides, restart requirement, and disabling;
   - update runtime layout (including the `data/tmp/` wording, currently
     "reserved for future temp review workdirs"), agent configuration, daemon
     architecture, development notes, README, and changelog as appropriate.

7. **Integration verification**
   - add credentialed opt-in tests under `integration_tests/` for scheduled
     usage-window session starts on each backend;
   - revalidate the economical model matrix;
   - never run live calls as part of the hermetic unit suite.

## Verification

Implementation verification on 2026-07-16: `./agent_collab_dev.sh build
--check` and the full hermetic `./agent_collab_dev.sh test` gate pass (937
tests, one pre-existing/conditional skip). Credentialed usage-window calls
were not run; set `AGENT_COLLAB_IT_USAGE_WINDOWS=1` with an explicitly selected
backend to run the opt-in live session test. The packaged model matrix was
rechecked against `integration_tests/harness.py` and the xAI CLI override.

Add focused hermetic coverage for at least:

- packaged config contains exactly one disabled target for each real backend;
- all targets disabled means no scheduler task and no provider call;
- a one-line user override enables exactly one inherited target;
- a user-defined second model on the same backend is accepted;
- duplicate enabled backend/model pairs are rejected;
- per-target schedule fields override global usage-window fields independently;
- project `[system]` and `[usage_windows]` are ignored with sanitized
  warnings;
- invalid timezone, day, clock time, duration, sub-`15m` interval, jitter,
  target name, backend, model, and backend option paths produce precise
  config errors;
- disabled/unavailable backends are visible but never invoked;
- ordinary and overnight work windows derive the expected anchors and exclude
  the end boundary;
- jitter never escapes the work window and never accumulates drift;
- DST folds/gaps execute at most once and stay within the work window;
- daemon startup or wake mid-window catches up the latest unattempted anchor
  exactly once, skips older ones, and never catches up outside the window;
- a restart between an anchor and its persisted planned time resumes the
  plan without rerolling jitter or catching up early;
- a failed anchor is never caught up (an attempt was recorded);
- a schedule-fingerprint change or a newly enabled target replans from the
  next future anchor and never triggers catch-up;
- persisted planned/success state prevents duplicate calls after restart;
- corrupt, missing, or future-version state fails closed: no immediate
  request and no catch-up;
- clock jumps and task cancellation do not burst or leak subprocesses;
- one target failure does not terminate scheduling for other targets;
- provider rejection respects retry guidance and never creates a tight loop;
- the scheduled session uses the isolated cwd, the fixed no-tools prompt,
  `max_turns=1`, `interactive=false`, and the target's posture options;
- scheduled sessions appear in `list_sessions` and the TUI labeled with the
  `usage-window` workflow and deterministic session ID, and normal retention
  prunes them;
- the scheduler's internal exemption admits the probe workdir when
  `restrict_workdir_roots` is set, while external REST/MCP starts under
  `data/tmp/usage-windows/` remain rejected;
- an enabled target whose executable is missing is marked
  `backend_unavailable` by the scheduler's own probe and never spends an
  anchor, including for backends whose session-start gating skips probes;
- the probe workdir under `data/tmp/usage-windows/` and its `data/tmp/`
  parent are owner-only;
- daemon status details enabled targets, collapses disabled packaged targets
  to a count, reads only config plus persisted state, and labels targets
  whose config fingerprint differs from state as pending restart;
- daemon status and logs contain scheduling metadata but no provider content or
  credentials; and
- existing retention scheduling and normal sessions remain unaffected.

Run the normal local gate:

```bash
./agent_collab_dev.sh test
```

Run credentialed backend probes only through the integration-test harness and
only when explicitly selected. Record exact backend/model results in this task
document before closing it; do not paste credentials, account quota details, or
provider response content.

## Decisions

- **Feature name:** Usage-window alignment; config section `[usage_windows]`.
- **Scope:** global built-in plus user config only; project config is ignored.
- **Timezone ownership:** `[system].timezone`; usage-window targets do not
  define their own timezone. `[system]` (renamed from the earlier
  `[localization]` draft) is the designated home for installation-wide
  settings and gains fields only when a feature needs them.
- **Opt-in:** no master switch; every target has `enabled`, and all packaged
  targets default to false.
- **Default coverage:** one economical, disabled target for each of the eight
  shipped real backends; no mock target.
- **Target identity:** named tables keyed by stable target ID, enabling
  field-by-field override and several models on one backend.
- **Schedule bound:** `work_time` determines how many interval anchors fit;
  there is no `windows_per_day` field.
- **Schedule anchor:** start of work time, recalculated per local day; actual
  jitter never becomes the next anchor.
- **Missed calls:** one bounded catch-up for the latest anchor whose
  persisted planned time passed unattempted, when the scheduler wakes inside
  the work window with trustworthy state carrying the current schedule
  fingerprint; a restart before the planned time resumes the plan, new or
  fingerprint-changed targets replan from the next future anchor, and older
  anchors and out-of-window misses are skipped and logged.
- **Jitter purpose:** bounded dispersion only, never automation concealment.
- **Invocation:** a normal, visible session on the standard `start_session`
  path — fixed transparent no-tools prompt, packaged `usage-window` workflow,
  `max_turns=1`, isolated daemon-owned workdir. Visibility in the TUI and API
  is deliberate, for auditing.
- **Session identity:** deterministic `uw-<target>-<anchor>` session IDs; a
  duplicate start for the same anchor collides instead of double-running.
- **Persistence:** sanitized owner-only daemon state with no response
  content; the session transcript is the full record, and state keeps the
  session ID for cross-reference.
- **Provider grouping:** explicit user configuration only; never inferred.
- **Tool posture:** no tool disabling required — the fixed prompt requests no
  tool use, and the targets' read-only options remain as defense in depth;
  `antigravity_sdk`, which has no posture option, has only workdir scoping.
  The workdir scopes default behavior but is not filesystem sandboxing; the
  residual risk is documented and bounded where a knob exists
  (`provider_max_turns = 1` on `xai_cli`).
- **Workdir exemption:** scheduler-internal only, via a non-wire
  `StartSessionRequest` flag; `validate_workdir_allowed` is unchanged for
  external callers.
- **Eligibility probing:** the scheduler runs the side-effect-free backend
  probe itself before each attempt; it does not rely on session-start
  gating, which skips probes for `claude_cli` and `codex_cli`.
- **Same-anchor retry:** none in the first implementation; failed anchors
  wait for the next anchor, and provider retry guidance is honored across
  anchors.
- **`timezone = "local"`:** discover an IANA name from standard platform
  settings (`TZ`, `/etc/localtime`, or `/etc/timezone`) and use fold-aware
  local-time conversion. On platforms exposing only a fixed UTC offset,
  anchors follow that offset and DST cases never arise.
- **Status display:** enabled targets in detail, disabled packaged targets
  collapsed to a count; no `daemon status --json` in this task (none exists
  today).
- **Option normalization:** handled by the normal session-start path
  (`normalize_start_options` into backend `normalize_options`); the scheduler
  performs no normalization of its own.

## Resolved questions

The five original open questions were resolved by verifying the design
against the codebase (2026-07-15). A same-day revision then replaced the
hidden invocation path with visible normal sessions for auditability and
added the bounded missed-anchor catch-up; items 1 and 5 reflect that. The
answers are folded into the sections and decisions above:

1. **Tool disabling:** made moot by the visible-session revision — the fixed
   prompt requests no tool use, so no backend needs a tools-off control, and
   read-only target options remain as defense in depth. (The original
   analysis stands: only `xai_sdk` could disable tools.)
2. **Retry:** no same-anchor retry in the first implementation; the
   missed-anchor catch-up applies only to anchors that were never attempted.
3. **`timezone = "local"`:** discover the platform's IANA zone when available
   and use fold-aware conversion; fixed-offset platforms schedule on that offset.
4. **Status verbosity:** enabled targets in detail, disabled packaged targets
   collapsed to a count.
5. **Normalization API:** moot on the visible-session path — the normal
   session start performs normalization and backend health checks itself.
