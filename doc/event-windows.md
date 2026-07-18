# Event Windows (beta)

Event Windows are optional, daemon-owned schedules that make minimal provider
calls during chosen working hours. The intent is to align the timing of a
provider's rolling usage window with when you work, so more of the allowance
available under your plan may be ready during your coding day.

This is an experimental convenience, not a quota-management API. Agent-collab
cannot inspect a provider's usage accounting, determine when its quota resets,
increase or reset a limit, or guarantee that a scheduled call changes a usage
window. Provider rules can change. Every scheduled call is a real model call
and may consume usage or incur cost.

The public feature name is **Event Windows**. Its existing configuration keys
are `[usage_windows]`, and its visible sessions use the `usage-window` workflow.

## Quick start

Event Windows are disabled by default. Configuration belongs only in the
global user file at `~/.agent-collab/config.toml`; project configuration and MCP
clients cannot enable or trigger schedules.

To enable all four packaged CLI targets while inheriting every other default:

```toml
[usage_windows.targets.claude_cli_sonnet]
enabled = true

[usage_windows.targets.codex_cli_luna]
enabled = true

[usage_windows.targets.antigravity_cli_flash_low]
enabled = true

[usage_windows.targets.xai_cli_grok_4_5]
enabled = true
```

Enable only providers you want called. Preview the effective configuration and
then restart the daemon, because it loads scheduling policy once at startup:

```bash
agent-collab config show
agent-collab daemon restart
agent-collab daemon status
```

Running `./agent_collab.sh install` after editing also validates the config and
restarts the daemon if it was already running. Its readiness output shows the
shared Event Window configuration followed by the participating backend/model
table.

## Default schedule

The packaged schedule is deliberately simple and economical:

| Setting | Default | Meaning |
| --- | --- | --- |
| Timezone | `local` | Discover the platform's local IANA zone when possible |
| Days | Monday–Friday | Only intervals owned by these local days participate |
| Work time | `09:00-17:00` | Calls stay inside this half-open local-time interval |
| Interval | `5h` | Drift-free anchors begin at work time and repeat every five hours |
| Jitter | `5m` | Each execution is randomized by up to five minutes and clipped to work time |

With those defaults, a normal weekday has anchors at 09:00 and 14:00. Jitter
is selected once and persisted before the daemon sleeps, so polling and daemon
wake latency do not continually reroll the time.

`timezone = "local"` checks an authoritative `TZ` environment value first,
then standard host settings such as `/etc/localtime` and `/etc/timezone`. An
explicit IANA name is clearest when daylight-saving behavior matters:

```toml
[system]
timezone = "Europe/Helsinki"
```

Named zones resolve daylight-saving folds and gaps. If a platform exposes only
a fixed offset, scheduling follows that offset. Work intervals may cross
midnight: an end time equal to or earlier than the start time belongs to the
following local day.

## Packaged targets

Each backend's colocated `defaults.toml` contributes one disabled economical
target to the assembled built-in config. The central `default_config.toml`
owns only the shared Event Window schedule. The initial target matrix is:

| Target | Backend | Model |
| --- | --- | --- |
| `claude_cli_sonnet` | `claude_cli` | `sonnet` |
| `claude_sdk_sonnet` | `claude_sdk` | `sonnet` |
| `codex_cli_luna` | `codex_cli` | `gpt-5.6-luna` |
| `codex_sdk_luna` | `codex_sdk` | `gpt-5.6-luna` |
| `antigravity_cli_flash_low` | `antigravity_cli` | `Gemini 3.5 Flash (Low)` |
| `antigravity_sdk_flash_low` | `antigravity_sdk` | `Gemini 3.5 Flash (Low)` |
| `xai_cli_grok_4_5` | `xai_cli` | `grok-4.5` |
| `xai_sdk_grok_4_5` | `xai_sdk` | `grok-4.5` |

The packaged target options favor low reasoning and read-only or plan posture
where each backend supports it. Enabling a target does not enable its backend:
the matching `[backends.<name>]` policy must also be enabled and its dependency
and credentials must be available.

Because config merges with the packaged defaults, an inherited target needs
only `enabled = true`. Do not copy its model or options into your user config
unless you intend to take ownership of those overrides.

## Configure the shared schedule

Override the shared defaults under `[usage_windows]`:

```toml
[system]
timezone = "Europe/Helsinki"

[usage_windows]
days = ["mon", "tue", "wed", "thu", "fri"]
work_time = { start = "09:00", end = "17:00" }
interval = "5h"
jitter = "5m"
```

The fields are:

- `system.timezone`: `local` or an IANA timezone such as `Europe/Helsinki`.
- `days`: any non-empty selection of `mon`, `tue`, `wed`, `thu`, `fri`, `sat`,
  and `sun`.
- `work_time`: local `HH:MM` start and end times. End is excluded.
- `interval`: a whole-number duration in minutes or hours, at least `15m`.
- `jitter`: a non-negative whole-number duration in minutes or hours, smaller
  than the effective interval. Use `0m` for exact anchors.

## Override one target

Any target may override `days`, `work_time`, `interval`, or `jitter` without
changing the shared schedule for other targets:

```toml
[usage_windows.targets.codex_cli_luna]
enabled = true
work_time = { start = "08:00", end = "14:00" }
interval = "3h"
jitter = "2m"
```

The installer reports target-specific schedule changes in an `overrides`
column. `agent-collab config show` prints every target's effective schedule.

## Add another model target

You can add a target instead of modifying a packaged one. A target requires a
canonical backend and a non-empty model:

```toml
[usage_windows.targets.my_xai_target]
enabled = true
backend = "xai_cli"
model = "grok-4.5"

[usage_windows.targets.my_xai_target.options]
thinking_level = "low"
sandbox = "read-only"
provider_max_turns = 1
```

`options` is validated by that backend's colocated `options.toml` contract.
Put the model in the target's `model` field, not inside `options`. Two enabled
targets cannot use the same normalized backend/model pair, but one backend may
have targets for different models.

## What happens at an anchor

1. The daemon calculates drift-free local-time anchors and persists a bounded
   jitter choice before sleeping.
2. Before making a call, it runs the backend's side-effect-free health probe.
   A disabled or unavailable backend is recorded as skipped rather than invoked.
3. It atomically marks that anchor attempted, then starts a normal visible
   `usage-window` session through the same session manager used by CLI and MCP.
4. The selected backend receives a fixed request to avoid tools and reply with
   `OK`. The workflow is limited to one supervised turn.
5. The session, events, outcome, and transcript follow ordinary retention and
   remain visible in the CLI, TUI, HTTP API, and MCP tools.

Each session has a deterministic `uw-...` id and uses an owner-only empty
working directory under the global runtime data. Different targets run in
independent tasks, so one slow provider does not block the other schedules.

## Restarts, missed anchors, and retries

Private scheduler state is stored atomically with mode `0600` under
`~/.agent-collab/data/daemon/usage-window-state.json`. It records planned and
attempted anchors without storing provider credentials or response text.

- A normal restart resumes a trustworthy persisted plan without rerolling it.
- Missing, malformed, incompatible, or schedule-mismatched state fails closed:
  the daemon plans only a future anchor whose jitter window has not opened.
- If trustworthy state proves that anchors were missed, at most the latest
  anchor may receive one freshly jittered catch-up while its work interval is
  still open. Older and expired anchors are skipped.
- A failed or ineligible attempt still marks that anchor handled. There is no
  same-anchor retry; later anchors may run normally.
- Provider retry guidance is honored across later anchors when exposed by the
  backend outcome.

These rules reduce duplicate paid calls after restarts or state damage. They
cannot prove what happened outside agent-collab or in a provider's own quota
system.

## Observe or disable Event Windows

Use these commands after a restart:

```bash
agent-collab daemon status
agent-collab config show
agent-collab list
agent-collab tui
```

Daemon status shows enabled targets, effective schedules, next persisted plans,
eligibility, and sanitized recent outcomes. Scheduled sessions also appear in
normal session history and transcripts.

To stop one target, set its inherited override back to false and restart:

```toml
[usage_windows.targets.xai_cli_grok_4_5]
enabled = false
```

```bash
agent-collab daemon restart
```

Stopping the daemon stops all future schedules. Editing config without a
restart does not change the already loaded daemon policy.

## Cost and safety boundaries

- Every invocation is a real provider request. Enable only targets whose cost
  and quota use you accept.
- Agent-collab does not know whether a provider meters requests, tokens, tool
  loops, time, or another unit, and it does not claim a scheduled call will
  start or move a provider window.
- The fixed prompt asks the model not to use tools. Packaged CLI targets also
  use low-reasoning and read-only/plan controls where supported.
- The owner-only empty workdir and read-only posture reduce exposure but are
  not an operating-system filesystem sandbox. A provider process may still
  read an absolute path available to the current user, and a model that ignores
  the prompt may perform additional provider-side work during its turn.
- Event Windows never originate from project config or an MCP request. Only the
  global user config and daemon lifecycle control them.

## Troubleshooting

If a target is enabled but does not run:

1. Run `agent-collab config show` and confirm the target is enabled with the
   expected backend, model, and schedule.
2. Confirm `[backends.<name>] enabled = true` for the selected backend.
3. Re-run `./agent_collab.sh install` and inspect backend dependency and
   credential readiness plus the participating Event Window table.
4. Restart the daemon and inspect `agent-collab daemon status` for
   `backend_disabled`, unavailable credentials/dependencies, a future plan, or
   a recent outcome.
5. Remember that calls occur only on configured local days and inside work
   time. A plan outside that interval is deliberately skipped rather than run
   late.

Invalid global scheduling config disables automatic scheduled calls rather
than silently falling back to paid defaults. Fix the reported config error and
restart the daemon.
