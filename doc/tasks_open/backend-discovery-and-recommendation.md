# Backend discovery and recommendation protocol

**Status:** Design recommendation; analysis only.

## Decision

Keep `agent_collab_describe_options` as the single pre-start discovery
operation. Do not add `agent_collab_list_backends` or
`agent_collab_discover_backends` now.

The baseline architecture is sound, but the current response is not yet a
complete discovery protocol. Extend the shared `describe_options` builder so a
single workdir-scoped response contains:

- instructions and freshness semantics;
- the registered canonical backend catalog;
- user-level backend enablement policy;
- effective configured-agent and workflow selections;
- raw, two-axis probe evidence plus native-runtime evidence;
- a policy-derived, non-authoritative readiness assessment;
- structured remediation;
- conservative per-agent and per-workflow recommendations; and
- an exact statement of what start will freshly validate.

MCP initialization and guidance should direct callers to this operation. REST,
stdio MCP, Streamable HTTP MCP, and a human CLI projection should all consume
the same response builder. `agent_collab_start` remains authoritative: it
reloads the same workdir configuration, resolves the exact selection, performs
fresh checks before session state or execution, and applies backend policy.
Even then, the first real turn remains the authority for authentication,
entitlement, provider service state, model support, and runtime behavior that a
side-effect-free probe cannot prove.

A dedicated discovery tool would mostly duplicate `/options`, its MCP adapter,
workdir handling, health probing, and versioned response contract. It would be
materially better only if option schemas and backend discovery acquired
different authorization, latency, pagination, or lifecycle requirements. None
does today.

## Current implementation findings

The recommendation is grounded in the current implementation, including these
important details:

- `agent_collab/backends/__init__.py` explicitly registers six packages in one
  flat list. The public identity is `<provider>_<backend>`, and resolution is
  `explicit start override > agents.<id>.backend > cli`.
- Registry membership is a build fact. It says that code exists for a pair; it
  says nothing about the current daemon host, credentials, workdir config, or
  whether a turn will succeed.
- `BackendHealth` has separate `status` (`ok`, `unknown`, `unavailable`) and
  `credentials` (`ok`, `unknown`, `missing`) axes. Its `available` property is
  only `status == ok`.
- CLI probes check `PATH`, optionally run a bounded `--version`, and optionally
  inspect credential files. SDK probes currently use `find_spec`, package
  metadata, and best-effort credential evidence. They do not import or execute
  the SDK runtime and never make a model call. Production SDK imports remain
  lazy.
- `HealthCache` has a 60-second TTL. `describe_options` probes all registered
  backends through that cache.
- Start health gating requests fresh results, but `_gate_backend_health`
  currently skips a backend entirely when both `block_on_unavailable` and
  `checks_credentials` are false. Consequently Claude CLI and Codex CLI are
  selected and option-validated at start, but they are not freshly health
  probed. A blanket `start_rechecks_fresh: true` would therefore overstate the
  current behavior.
- Definite failure blocks only under backend policy. `unknown` warns where the
  policy applies. Claude and Codex CLI deliberately retain their legacy
  first-turn-error behavior; the SDK backends and both Antigravity backends opt
  into stronger checks.
- Workdir configuration is loaded as built-ins, then user config, then project
  config. It determines configured agents, workflows, backend selection,
  backend-owned static settings, and configured session-option defaults. Start
  also uses the workdir as execution cwd.
- There is currently no backend-level `enabled` policy. Agents can be disabled,
  but a registered canonical backend cannot be disabled independently of every
  agent that might select it.
- The current `describe_options` response lists raw `agent.backend`, which may
  be null even though the effective backend is `cli`. Workflows list agent IDs
  and provider types but not effective canonical backends. Callers must perform
  error-prone joins and reimplement fallback rules.
- The current backend view is provider-grouped. It exposes health,
  capabilities, and schemas, but not canonical entries, cache age/source,
  policy, native compatibility, structured remediation, readiness, or a
  recommendation.
- The top-level backend option schemas are canonical-name keyed, while health
  is provider/backend nested. This forces callers to translate between two
  shapes.
- Session status is intentionally post-start and reports the selected effective
  settings. It is not the place to discover unselected registered or configured
  alternatives.

These are response-contract gaps, not reasons to replace the backend-package
architecture. Adding a built-in must remain one backend folder and one
registration entry, with backend-owned metadata and probes rather than a new
central provider/backend matrix.

## The four catalogs and two judgments

Discovery should name its scopes instead of using “available” for all of them.

| Concept | Meaning | Source |
| --- | --- | --- |
| Registered backend | This build contains an implementation for the canonical pair. | Static registry fact |
| Enabled backend | The user's daemon-wide policy permits this registered backend to be selected. | Home config policy fact |
| Configured agent/backend | This workdir's merged config defines the agent and its explicit or fallback backend. | Workdir config fact |
| Probed backend | A side-effect-free observation was made on this daemon host. | Dynamic evidence |
| Start-allowed backend | Applying backend policy to the current evidence would not block start. | Derived, advisory judgment |
| Recommended backend | The system's conservative suggestion for this agent or workflow. | Derived, advisory judgment |
| Exercised backend | A real prior turn succeeded or failed. | Historical evidence, if retained separately |

The registry default, configured selection, and recommendation are not
synonyms:

- **Registry default:** `cli`, the final fallback in selection resolution. It
  is stable program policy, not a quality claim.
- **Configured backend:** `agents.<id>.backend`, when present. This is explicit
  user/project intent.
- **Enabled backend:** a registered backend permitted by the user-level home
  config. Disabling is policy, not evidence that a dependency is unavailable.
- **Effective configured backend:** the configured value or registry fallback,
  before a start override.
- **Explicit start override:** a uniform backend ID applied to every selected
  non-mock agent. It wins for that request; it is not a new configured default.
- **Available backend:** retain only as a compatibility alias for
  `health.status == ok`, and label that narrow meaning in the response. New
  callers should not make decisions from this boolean.
- **Start-allowed backend:** the result of applying blocking policy to one
  snapshot. Cached discovery can only predict the fresh start decision.
- **Preferred-if-available backend:** do not invent this yet. If introduced, it
  must be explicit backend or project policy, not inferred from `sdk`, provider
  brand, or in-process execution.
- **Recommended backend:** an advisory result for a concrete configured agent
  or workflow, including reasons, uncertainty, and an actionable way to use it.

## Required caller protocol

The protocol should be explicit enough that an MCP caller does not have to
infer a workflow from prose.

1. Resolve the intended project directory to an absolute path.
2. Call `agent_collab_describe_options` with that exact `workdir`. Use cached
   health by default; request fresh health only when the caller needs a newer
   advisory snapshot before making a selection.
3. Select one of the returned workflows. Read its effective agent/backend view;
   do not reconstruct backend resolution from registry defaults. Confirm that
   every selected canonical backend is enabled by user policy.
4. Normally keep the configured effective backends. Consider a returned
   alternative only when its recommendation is actionable for the whole
   workflow and its reasons match the caller's needs.
5. Pass options only under returned canonical `backend_options` keys that apply
   to the selected workflow. Use the same absolute `workdir` for start.
6. Treat `agent_collab_start` as a new decision. It reloads config, re-resolves
   the workflow and exact backends, validates normalized options, obtains fresh
   selected-backend evidence, and applies policy before creating session state
   or launching anything.
7. If start returns field-path errors, fix those fields. If it returns a backend
   failure, follow its structured remediation or explicitly choose a returned
   alternative. Do not guess backend or option names.
8. If start succeeds but the real turn fails, prefer the real error. Re-run
   discovery with fresh health, preserve the backend identity and timestamps,
   and either remediate or deliberately choose a fallback. Do not blindly retry
   or automatically oscillate between backends.

Why the workdir is mandatory should be part of both MCP initialization and the
response: it selects project config and therefore the configured agents,
workflows, effective backends, static backend config, and configured session
defaults. Start also uses it as the execution directory. Daemon cwd, MCP stdio
adapter cwd, and caller shell cwd are not substitutes.

## Recommended response model

The following is illustrative rather than a frozen wire schema. The important
decision is ownership and separation of facts.

```json
{
  "discovery": {
    "protocol_version": 1,
    "method": "agent_collab_describe_options",
    "scope": "workdir",
    "workdir": "/absolute/project/path",
    "generated_at": "2026-07-10T12:00:00Z",
    "health_request": "cached",
    "health_source": "side_effect_free_probe",
    "cache_ttl_seconds": 60,
    "probe_is_not_a_model_call": true,
    "probe_proves_turn_success": false,
    "start": {
      "reloads_workdir_config": true,
      "revalidates_selection_and_options": true,
      "rejects_disabled_backends": true,
      "fresh_probes_enabled_selected_backends": true,
      "applies_backend_policy": true,
      "happens_before_session_creation": true
    },
    "first_turn_error_remains_authoritative": true
  },
  "canonical_backends": {
    "antigravity_sdk": {
      "identity": {
        "provider_type": "antigravity",
        "backend_id": "sdk",
        "registered": true,
        "registry_default": false
      },
      "static": {
        "capabilities": {
          "resume": false,
          "interrupt": false,
          "tool_gate": false
        },
        "event_fidelity": "typed",
        "provider_session_id_kind": "conversation",
        "option_schema": {},
        "configuration_schema": {}
      },
      "probe": {
        "health": {
          "status": "unavailable",
          "credentials": "ok",
          "reason": "bundled native runtime requires GLIBC_ABI_DT_RELR"
        },
        "checks": {
          "dependency": {"status": "present", "version": "0.1.6"},
          "native_runtime": {
            "status": "incompatible",
            "required": "glibc >= 2.36",
            "observed": "glibc 2.34"
          },
          "credentials": {"status": "ok", "method": "adc_presence"}
        },
        "checked_at": "2026-07-10T11:59:50Z",
        "age_seconds": 10,
        "cache_hit": true,
        "stale": false
      },
      "policy": {
        "enabled": true,
        "enabled_source": "user_config",
        "block_on_unavailable": true,
        "checks_credentials": true
      },
      "assessment": {
        "state": "unavailable",
        "discovery_gate": "block_if_unchanged_at_start",
        "reason_codes": ["native_runtime_incompatible"],
        "uncertainties": ["no_model_call_was_made"],
        "remediation": [
          {
            "code": "use_compatible_native_runtime",
            "message": "Use a glibc 2.36+ host/container or an EL9-compatible provider binary. Do not replace the host system glibc manually."
          }
        ]
      }
    }
  },
  "provider_groups": {
    "antigravity": {
      "registry_default": "cli",
      "canonical_backends": ["antigravity_cli", "antigravity_sdk"]
    }
  },
  "agents": [
    {
      "id": "antigravity_sdk",
      "provider_type": "antigravity",
      "enabled": true,
      "configured_backend": "sdk",
      "effective_backend": "sdk",
      "canonical_backend": "antigravity_sdk",
      "selection_source": "agent_config",
      "configured_session_defaults": {},
      "static_configuration": {
        "validation": "valid",
        "fields": {
          "vertex": "configured",
          "project": "configured",
          "location": "configured"
        }
      }
    }
  ],
  "workflows": [
    {
      "id": "solo-antigravity-sdk",
      "sequence": ["antigravity_sdk"],
      "effective_agents": [
        {
          "agent_id": "antigravity_sdk",
          "canonical_backend": "antigravity_sdk"
        }
      ],
      "selected_canonical_backends": ["antigravity_sdk"],
      "start_eligible": false,
      "ineligible_reasons": ["native_runtime_incompatible"],
      "uniform_backend_overrides": []
    }
  ],
  "recommendations": {
    "agents": {},
    "workflows": {}
  },
  "backend_options": {}
}
```

`canonical_backends` should be the normalized source in the response. Keep the
current `backends.<provider>.default`, `.backends`, and `.entries` shape as a
compatibility projection generated from the same objects until a future API
major can replace it with the lighter `provider_groups` index. Do not maintain
two builders or probe twice.

The current top-level `available` field may remain during that transition, but
document it inline as `health_status_is_ok`; it must not be the input to
recommendation. Canonical keys make the join to `backend_options` direct.

### Field ownership

| Class | Fields | Notes |
| --- | --- | --- |
| Static registry facts | canonical name, provider type, backend ID, registration, registry default, capabilities, option/config schemas, event fidelity, provider session-ID kind, probe policy | Declared by each backend package and registered explicitly. |
| User policy facts | backend enabled flag and source | Loaded only from the agent-collab home config; project config cannot broaden it. |
| Workdir configuration facts | configured agents, agent enabled state, explicit/effective backend and selection source, workflow applicability, static-config validation/safe summary, configured option defaults | Built from the exact config that start will reload. |
| Dynamic probe results | health, credentials, dependency/native checks, version, reason, checked time, cache hit, age, staleness | Never hard-coded as live availability and never based on a model call. |
| Derived judgments | readiness state, predicted discovery gate, recommendation, reasons, actionable alternatives | Recomputed from the other fields; explicitly advisory. |
| Historical evidence | last successful real exercise, last real failure | Exclude from the initial version. If later retained, keep it timestamped and separate from health. |

Static configuration should be summarized only through a backend-owned safe
formatter. The default is presence/validation state, not raw values. A manifest
may explicitly mark a field safe to display. Never return environment contents,
tokens, credential file contents, raw provider objects, raw SDK data, prompts,
or transcript content. Paths and project identifiers should be redacted unless
the backend contract deliberately marks them public diagnostics.

Do not expose a numeric “confidence” score. It creates false precision. The
check kind, observed state, source (`registry`, `config`, `probe`, or
`execution`), timestamp, and uncertainty list tell callers what is actually
known.

## Backend enablement in home config

Add a user-level `enabled` flag keyed by canonical backend name in
`$AGENT_COLLAB_HOME/config.toml` (normally `~/.agent-collab/config.toml`):

```toml
[backends.claude_cli]
enabled = true

[backends.claude_sdk]
enabled = true

[backends.codex_cli]
enabled = true

[backends.codex_sdk]
enabled = true

[backends.antigravity_cli]
enabled = true

[backends.antigravity_sdk]
enabled = false
```

This is daemon/user policy, distinct from `agents.<id>.enabled`:

- `agents.<id>.enabled` controls whether one configured agent may appear in a
  workflow;
- `backends.<canonical>.enabled` controls whether any agent or explicit start
  override may select that execution backend.

Missing backend entries should mean `enabled = true` for backward
compatibility and so registering a new backend still requires only its package
and one registry entry. A generated home config should materialize one section
for every currently registered canonical backend, while migration and runtime
still treat an absent entry as enabled. Generate that list from the registry;
do not require a central hard-coded support matrix in the default config.

The home-level disable must be authoritative over project configuration. A
project may select an enabled backend for an agent, but it must not re-enable a
backend disabled in home config. The cleanest contract is to accept
`[backends.*]` only from the home config and reject it in project config. This
avoids the normal project-over-user merge precedence silently undoing a user's
daemon-wide restriction. Config compatibility handling still belongs in
`config_migrations.py`.

Discovery should continue to list disabled backends in the registered catalog,
including static schemas and capabilities, with:

- `policy.enabled: false` and `enabled_source: "user_config"`;
- `selection_eligible: false`;
- a workflow/agent ineligibility reason when configured to select it; and
- remediation that points to the home config, not project config.

Disabled must not be collapsed into `health.status = unavailable`. Registration
and runtime health are independent of policy. By default discovery may skip a
disabled backend's dynamic probe and report `probe.status: "not_run"` with
`reason: "disabled_by_user_config"`; a fresh diagnostic mode may probe it
explicitly. Start rejects a disabled selected backend with
`code: "backend_disabled"` before probing, creating session state, or launching
anything. Recommendation never selects a disabled candidate and never silently
falls back from it.

If an agent has no explicit backend, registry resolution still yields `cli`.
Disabling that canonical CLI backend must produce an actionable selection error,
not an automatic switch to SDK. Registry default remains a fallback rule, not a
permission to bypass user policy.

## Availability and readiness semantics

One boolean is insufficient. Preserve `BackendHealth` as the raw compatibility
surface and add structured evidence plus a derived assessment.

Recommended assessment states:

- `usable`: no known blocker in the current probe, while explicitly not
  claiming that authentication, entitlement, model support, or a turn was
  proven. `credentials: unknown` can coexist with this state; it is uncertainty,
  not evidence of failure.
- `degraded`: a known, non-blocking limitation affects the requested workflow
  or requirement. For example, a workflow that explicitly requires structured
  tool events may be degraded on a message-only backend. Do not use `degraded`
  merely because a credential cannot be verified.
- `unknown`: dependency or native readiness could not be determined, the probe
  failed indeterminately, or evidence is outside the advertised freshness
  bound. Unknown must not automatically be ranked below every `ok` backend.
- `unavailable`: a definite condition would prevent a real backend turn, such
  as a missing dependency, incompatible native runtime, or credentials known to
  be required and missing. This assessment is independent of whether legacy
  backend policy chooses to reject start or defer the failure to the turn.

Keep the policy result separate:

- `disabled_by_user_config`;
- `allow_if_unchanged_at_start`;
- `warn_if_unchanged_at_start`; or
- `block_if_unchanged_at_start`.

This wording is intentionally conditional because discovery is cached and
start probes again. `block_on_unavailable` and `checks_credentials` remain
backend policy facts. The user `enabled` gate is evaluated before these health
policies. Health `unknown` and credentials `unknown` warn where applicable but
never block merely because certainty is unavailable. Definite credentials
`missing` blocks only under policy. The first turn still decides facts no safe
probe can establish.

### Native runtime compatibility

Native-runtime compatibility should become an explicit probe dimension. An SDK
being importable is dependency evidence, not execution evidence. The
Antigravity SDK case should report:

- dependency: present, including package version;
- static configuration: valid;
- credentials: `ok` or `unknown` independently;
- native runtime: incompatible, with observed host and required ABI evidence;
- overall health: `unavailable`, not `degraded`, because the bundled executable
  cannot begin a turn;
- remediation: use a compatible host/container or provider binary, and do not
  replace the system glibc manually; and
- no claim that a model call or authentication was attempted.

The native check belongs in the Antigravity SDK package, not a central provider
matrix. It should inspect package/native-binary metadata and host runtime facts
without launching a model operation. For this case it can compare injectable
host libc facts with requirements derived from the installed binary/package.
Inputs such as binary location, ELF requirements, host libc version, filesystem,
and clock must be injectable so hermetic tests cover compatible, incompatible,
missing, and indeterminate cases. A bounded documented `--version`/self-check
may be used only if it is known to be side-effect-free; static inspection is
preferable.

If a real first-turn failure reveals another deterministic precondition that
can be checked safely, improve the backend-owned probe. Do not turn provider
API calls, token validation, entitlement checks, or paid live tests into
discovery probes.

## Recommendation policy

Recommendation should be conservative and should never silently change a
start. Facts and recommendation must remain separable.

### Granularity

- The primary recommendation is **per configured agent**, because static
  configuration and option defaults belong to an agent.
- A second recommendation is **per workflow**, because the public start
  override is uniform across every selected non-mock agent and only a
  workflow-level result can say whether an alternative is actionable.
- Provider-level entries may summarize candidates, but should not claim one
  universal recommendation independent of config and workflow.
- A canonical backend entry reports facts and may state eligibility; it does
  not know whether it should replace a configured agent.

### Ranking and reasons

Use this order:

1. Respect an explicit start override for the request being previewed; otherwise
   respect the configured agent backend. When it has no definite blocker,
   recommend keeping it.
2. Apply explicit workflow requirements if the configuration model later gains
   them. Current workflows declare only a sequence, so there is no basis for
   assuming that they require typed events, provider identity, or a particular
   execution mechanism.
3. Never recommend a user-disabled or definitely unavailable candidate. An
   unknown enabled candidate remains eligible with uncertainty; unknown
   credentials alone are neutral.
4. Prefer an alternative only when the configured selection has a definite
   blocker or fails an explicit requirement, and the alternative passes the
   same option/static-config validation for the concrete agent.
5. Break otherwise equal ties by explicit project/user preference, then current
   configured selection, then a deterministic canonical-name order. Registry
   default is a selection fallback, not a quality tie-break.

Do not infer preference from provider brand, `cli` versus `sdk`, in-process
execution, or all-false capabilities. Event fidelity is a valid static fact,
but it affects ranking only when the caller/workflow explicitly values it.
Antigravity CLI is message-only while its SDK has typed mapping when runnable;
that does not make the incompatible SDK a general recommendation on this host.

Each recommendation should include:

- `selected` and `recommended` canonical backend;
- action: `keep`, `remediate`, `use_uniform_override`, `select_workflow`, or
  `edit_config`;
- reason codes and plain-language reasons;
- evidence timestamps and uncertainties;
- option/static-config incompatibilities; and
- whether the action can be expressed by the current start API.

The workflow recommendation may offer `backend: "sdk"` or `backend: "cli"`
only if every selected non-mock agent registers that ID, validation succeeds
for the exact workflow, and the change does not replace a healthy configured
backend merely to fix an unrelated agent. A mixed-provider workflow often has
no safe uniform override. In that case the honest recommendation is to
remediate, select an existing alternative workflow, or edit backend-specific
agent/workflow config—not to present an unusable per-agent override.

No automatic failover should occur. Silent switching would change option
support, authentication route, event fidelity, command/environment behavior,
and possibly cost or permissions.

## Freshness policy

Retain lazy on-demand probing and the short TTL. Add an optional discovery input
such as `health_refresh: "cached" | "fresh"`; default to `cached`. This keeps one
operation while letting a caller request a newer advisory snapshot. Report
cache hit, checked time, age, TTL, and staleness for every probed canonical
backend, or the reason the probe was not run.

Probe costs differ:

| Check | Typical cost and risk |
| --- | --- |
| `PATH`/`which` | Very cheap process-environment lookup. |
| `find_spec` | Cheap import-system/filesystem lookup, but still incomplete runtime evidence. |
| Package metadata | Cheap filesystem lookup. |
| Environment inspection | Very cheap; report only state, never values. |
| Credential-file inspection | Cheap to moderate filesystem I/O; parsing can fail and must become `unknown`. |
| CLI `--version` | A subprocess and the most expensive current common check; bounded by a five-second timeout and may still be inconclusive. |
| Native-runtime inspection | Cheap to moderate filesystem/host inspection; backend-specific and cached. |

The builder may run independent probes with bounded concurrency, but it should
retain one cache and one result per canonical backend. Mixed per-check TTLs are
not justified yet; they add complexity without changing the start authority.

Acceptable staleness differs by use:

- **Informational discovery:** the current 60-second TTL is acceptable when age
  and source are visible.
- **Recommendation:** use the same internally consistent snapshot. A caller
  about to choose a fallback may request fresh health. Never combine backend
  observations from different hidden refresh policies.
- **Startup diagnostics:** any snapshot is informational only and must print
  its timestamp; it may become stale immediately.
- **Start-time gating:** no TTL. Reject disabled selections, then probe the
  exact distinct enabled selected backends fresh after reloading config and
  before session state or execution.

The final bullet is a deliberate tightening of current behavior. Start should
reject disabled selections and obtain a fresh snapshot for every enabled
selected backend, then let backend policy decide block, warn, or defer to the
first turn. This makes `fresh_probes_enabled_selected_backends: true` accurate
without changing the rule that definite unavailability blocks only according
to policy. If universal fresh probing is not adopted, discovery must instead expose
`start_probe_policy: "fresh" | "not_probed"` per backend and must not use a
blanket recheck claim.

Do not add periodic background sweeps or transition notifications now. They
consume resources when no caller needs the data, add synchronization and
shutdown complexity, and can make a transient probe look authoritative.
Transition push is also a poor fit for the current transports: stdio MCP has no
health subscription, Streamable HTTP MCP has no SSE path, REST has no global
event channel, and the existing long-poll stream is intentionally
session-scoped. A future health watch should be an explicit subscription with
the same snapshot schema, not health events mixed into session transcripts.

## Surface comparison

| Surface | Scope and freshness | Implementation cost | Transport, noise, and duplication | Recommendation |
| --- | --- | --- | --- | --- |
| MCP initialization/guidance prose | Daemon-global instructions; document freshness | Low | Works on both MCP transports and is highly discoverable, but cannot contain live workdir facts and drifts if treated as data | Use only to require the protocol call and explain principles. |
| Extended `agent_collab_describe_options` | Workdir, configured agent/workflow, and daemon runtime; cached or requested-fresh | Medium | Existing direct MCP and stdio-via-REST paths already converge here; the response can be noisy, so normalize and version it rather than duplicate it | Make this the primary operation. |
| Dedicated list/discover tool | Would still need workdir and the same probes | Medium to high | Requires parallel MCP, REST, client, CLI, cache, and schema plumbing; creates a second caller choice and can contradict options | Do not add now. Reconsider only for pagination/auth/latency separation. |
| CLI projection | Workdir; whatever refresh mode it requests | Low if it projects `/options` | Good for humans and `--json` scripts; becomes a second truth if it reads config or probes independently | Add an options/backends/doctor view backed by `/options`. |
| Daemon startup diagnostics | Daemon runtime only unless given an arbitrary default workdir | Low to medium | Visible only to some humans, noisy in logs, absent from MCP/REST responses, stale, and potentially slow | Do not make primary or eager by default. Optional projection only. |
| Session status | Session-specific effective selection after start | Medium if expanded | Consistent on current REST/MCP/CLI session surfaces, but too late and noisy for unselected catalog data | Keep as execution confirmation, not discovery catalog. |
| Start errors/warnings | Exact workflow and fresh request-time facts | Low to medium | Already consistent through shared start errors and highly actionable, but purely reactive | Keep authoritative, with structured codes/remediation. |
| Daemon push/transitions | Daemon-global dynamic state | High | No current common subscription across stdio MCP, Streamable HTTP MCP, REST, and CLI; noisy and easy to diverge from snapshots | Defer. |

The recommended hybrid is therefore small: initialization/guidance tells the
caller what to do; `describe_options` returns all structured pre-start facts;
start freshly validates; status confirms the created session; CLI and optional
startup text are projections of the same builders.

Daemon startup should not eagerly probe every registered backend by default.
Optional backends must never delay or prevent daemon startup, and startup does
not know the eventual workdir. A human `doctor` command is more useful. If a
startup summary is retained, it must use the same discovery/probe objects,
label itself daemon-global and non-authoritative, omit workdir recommendations,
sanitize credential evidence, avoid model calls, and never change daemon exit
status. Bounded concurrency is advisable because several bounded version
checks could otherwise make startup visibly slow.

## Start errors and first-turn failures

Start errors and warnings should preserve the existing `path` and `message`
fields and add machine-readable detail:

```json
{
  "path": "backend",
  "code": "native_runtime_incompatible",
  "agent_id": "antigravity_sdk",
  "canonical_backend": "antigravity_sdk",
  "checked_at": "2026-07-10T12:00:30Z",
  "message": "...",
  "remediation": [{"code": "use_compatible_native_runtime", "message": "..."}]
}
```

The response should say that config, resolution, options, and fresh probes were
completed before session creation. A start-time `ok` still means only that no
checked precondition failed.

When discovery or start says `ok` but the first real turn fails, the caller
should:

1. treat the turn error as authoritative and preserve its canonical backend;
2. avoid assuming that another backend will fix an entitlement, model, task, or
   provider outage;
3. request a fresh discovery snapshot to detect changed dependencies or a
   newly recognized native limitation;
4. follow structured remediation or choose an explicit workflow-level
   alternative; and
5. report the mismatch so a safe deterministic check can be added to the
   backend-owned probe when possible.

Do not fold raw turn failures into `BackendHealth`. A model rejection or task
failure is not necessarily backend unavailability. If historical execution
evidence is added later, retain only sanitized outcome category, canonical
backend, timestamp, and perhaps package/runtime version. Keep it in a separate
`execution_evidence` object, never expose raw provider responses, and expire it.

## Knowledge included and excluded

Include in the primary response:

- canonical identity, provider/backend components, registration, and registry
  default;
- home-config enablement, its source, and selection eligibility without
  conflating disabled with runtime health;
- configured agents, explicit/effective backend, selection source, workflow
  applicability, and effective backend per workflow occurrence;
- raw health and credentials states, structured dependency/native checks,
  version, reason, check source, timestamp, age, TTL, and cache status;
- backend policy and conditional discovery gate;
- install/sign-in/native-runtime remediation without secret material;
- capabilities, event-fidelity classification, and provider session-ID kind as
  backend-declared static facts;
- backend-qualified MCP option schema and per-agent configured effective
  defaults;
- static configuration schema plus backend-owned safe presence/validation
  summary;
- per-agent and actionable per-workflow recommendation with reasons and
  uncertainty; and
- exact start revalidation behavior and first-turn-authority warning.

Exclude initially:

- token or credential contents, environment values, raw credential/provider
  paths, raw provider responses, and raw SDK objects;
- a single “usable” boolean as the new contract;
- numeric confidence;
- inferred preference from provider brand or execution mechanism;
- live integration tests as discovery; and
- last success/failure until there is a deliberate sanitized historical store.

## Answer to the ten caller questions

1. **Operation:** call `agent_collab_describe_options`.
2. **Workdir:** it selects merged project/user/built-in config, agents,
   workflows, backends, static settings, defaults, and later the execution cwd.
3. **Registered backends:** read canonical entries, with provider grouping as an
   index/compatibility projection; then check the separate home-config enabled
   policy.
4. **Configured selection:** read each agent's explicit/effective canonical
   backend and each workflow's effective agent/backend list.
5. **Usability evidence:** read raw health and credentials plus dependency and
   native-runtime checks; then read the separately derived assessment.
6. **Freshness:** read checked time, age, cache hit, TTL, and staleness per
   backend; request fresh discovery when useful.
7. **Remediation:** follow structured backend-owned remediation and use only
   actionable workflow alternatives.
8. **Start recheck:** start reloads and revalidates everything and should fresh
   reject disabled selections, then probe every enabled selected backend before
   creating state; policy controls the result. Until that tightening lands, the
   response must disclose selective probing instead.
9. **Which backend distinction matters:** registry default selects only in the
   absence of config/override; configured backend expresses intent; probed and
   start-allowed describe evidence/policy; recommendation is advisory.
10. **`ok` followed by failure:** the real turn wins. Refresh discovery,
    remediate or deliberately fall back, and improve a probe only when the
    failed precondition can be checked safely without a model call.

## Acceptance criteria for a later implementation

- MCP initialization requires discovery with the intended absolute workdir.
- One versioned builder serves direct Streamable HTTP MCP, stdio MCP through
  REST, REST clients, and JSON/human CLI projections.
- The registered catalog is canonical-name keyed and remains package-driven.
- Home config supports `backends.<canonical>.enabled`; disabled backends remain
  registered/discoverable but cannot be selected, recommended, or started, and
  project config cannot re-enable them.
- Each agent and workflow reports effective backend selection and source.
- Raw health, credentials, native readiness, policy, readiness, and
  recommendation remain separate fields.
- Every probe reports freshness and never makes a model call or exposes secret
  material.
- Antigravity SDK reports the Oracle Linux 9/glibc incompatibility as definite
  native-runtime unavailability with safe remediation.
- Unknown credentials do not block or lose recommendation rank by themselves.
- `available == (health.status == ok)` is documented as compatibility-only.
- Start rejects disabled selections, then freshly checks every distinct enabled
  selected backend before session creation, or the response honestly reports
  which policies skip probing.
- Start errors contain field paths, codes, canonical backend identity,
  timestamps, and remediation.
- No daemon startup or optional backend failure prevents the daemon from
  serving discovery for other backends.
- No automatic backend switch occurs.
