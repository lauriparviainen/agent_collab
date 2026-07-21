# Dynamic backend model discovery at installation and startup

**Status:** Open — architecture approved with final dual-review clarifications incorporated. Refined 2026-07-22 with CLI-first/SDK-later phasing and lifecycle clarifications (fingerprint version, cache precedence, cache directory, refresh cost gate). Default handling revised same day from catalog-based suppression to **warn-only** (catalog membership is not proof: option values are aliases/display names, catalogs list canonical IDs); discovery is cache-only and never writes config.

**Created:** 2026-07-22.

**Issue:** [#45](https://github.com/lauriparviainen/agent_collab/issues/45)

## Purpose

Enable `agent-collab` backends (Antigravity, Codex, Claude, xAI) to dynamically discover available model identifiers during installation, daemon startup, and explicit options refreshes, preventing static backend manifests (`options.toml`) from becoming outdated when providers release new models (for example, Gemini 3.6 Flash, Claude 4.7, or GPT-5 updates).

## Problem statement

Currently, backend options schemas in `agent_collab/backends/<provider>_<backend>/options.toml` statically declare accepted model lists under `[options.model] allowed = [...]` (e.g., `antigravity_cli`, `antigravity_sdk`, `claude_cli`, `claude_sdk`), or static `suggested` lists (e.g., `xai_cli`, `xai_sdk`).

When providers release new models:
1. Users cannot select newly released models if `allowed` is strictly enforced by `backend_contract.py`.
2. MCP callers (via `agent_collab_describe_options`) and CLI/TUI users cannot see available new models without a new release of `agent-collab` with updated `options.toml` manifests.
3. Hardcoded model arrays in repository files require continuous manual maintenance across 8 backend packages.

## Approved architecture

Following technical review, peer adjudication, and dual review (Issue #45), the architecture decouples side-effect-free health probes from dynamic model discovery, shifts `options.model` to advisory `suggested` validation, and establishes a structured catalog observation and caching model.

### 1. Option schema policy shift: `suggested` & non-whitespace validation

- Convert `options.model.allowed` constraints in backend `options.toml` to static `suggested` model arrays. Keep strict `allowed` validation for discrete enums (e.g., permission modes, sandboxes, reasoning effort levels).
- Introduce non-whitespace string validation (`value.strip() != ""` non-blank check) for model options in `backend_contract.py` so empty strings and whitespace-only values (`""`, `"   "`) are rejected while any valid model identifier string is accepted.
- Retain shipped default model selections in `defaults.toml` and static fallback suggestions in `options.toml` so offline or unauthenticated environments operate without network access.

#### Configured default handling (warn-only)

The shipped model default in `defaults.toml` (e.g. `model = "opus"`) is applied
and validated **only** as a non-blank string; it is never checked for membership
in a discovered catalog, so offline, unauthenticated, and discovery-unsupported
environments keep working exactly as today. The `usage_windows.targets.*` models
carried in `defaults.toml` ride the same non-blank path and the same policy below.

**The catalog never changes which model is used.** When an authoritative
catalog observation — `status == "ok"` **and** `complete == True` — does not
contain the configured default:

- **Installation** (`agent-collab install`) emits a non-fatal warning naming the
  backend and the missing default (for example, "configured default model for
  `claude_cli` is not present in the live model catalog").
- `describe_options` and start responses carry a non-fatal
  `configured_default_not_in_catalog` reason code / warning so the TUI and MCP
  callers can prompt for an explicit model.
- The default is **still passed through unchanged**. If it is genuinely dead,
  the provider's first-turn error is the authority — consistent with health
  gating, which warns on uncertainty and lets the real error speak.

Rationale for warn-only rather than dropping the default:

- **Catalog membership is not proof.** Configured defaults are aliases or
  display strings (`claude_cli` `"opus"`; `antigravity_cli`
  `"Gemini 3.5 Flash (High)"`), while a provider's catalog listing plausibly
  returns canonical model IDs (`claude-opus-4-8`, `gemini-3.5-flash`). An
  `ok`+`complete` observation proves the *listing* succeeded, not that it speaks
  the option-value namespace — so "missing from catalog" can be a naming
  mismatch, not a retired model.
- **A false drop is the worst failure mode.** Silently un-setting the default
  hands model selection to the provider, changing which model spends money with
  no confirmation — worse than the loud, attributed provider error a dead
  default produces on the first turn.
- Observations that are not `ok`+`complete` (`unsupported`, `unavailable`,
  `timeout`, `error`, incomplete, stale, fingerprint-mismatched) do not even
  warn: absence of evidence is never absence of the model.

If a future backend's spike proves its catalog uses option-value naming
exactly, escalating the warning to automatic suppression can be reconsidered
per-backend — that would need a parser-declared namespace field on the
observation and is explicitly out of scope here.

#### Discovery never writes configuration

Config is user intent; the cache is observation. Discovery results live only
under `$AGENT_COLLAB_HOME/cache/` — they are never written into `config.toml`,
`defaults.toml`, or `options.toml`. The installer and daemon must not mutate
user-owned config from discovery output (diff churn, switchless-install
migration hazards, and fingerprint circularity). Persisting a model *choice* is
a deliberate user action (a future explicit pin command or TUI action), never
automatic. The static `suggested` arrays in `options.toml` remain a small
offline seed maintained by releases; being advisory, their staleness is
harmless.

#### Catalog changes while the daemon is running

- **Running sessions are never affected.** Options are normalized and
  snapshotted into session settings at start (`build_session_settings`); a
  background catalog refresh must not reach into live sessions. This invariant
  is explicit, not incidental.
- **New starts read the cached observation at start time.** Any
  `configured_default_not_in_catalog` warning is echoed in the start response's
  effective settings, so starts that saw different catalog states are auditable
  after the fact.
- **Catalog transitions are logged.** When a background refresh changes a
  backend's catalog (models added/removed, warning state flips), the daemon
  emits a log/event line. Because the policy is warn-only, a flapping catalog
  flaps a warning — never behavior.

### 2. Decoupled model discovery module

Keep model catalog discovery strictly separate from side-effect-free health probes (`health.py`). Create `agent_collab/backends/common/model_discovery.py`:

- Return a structured observation model:
  ```python
  @dataclass(frozen=True)
  class ModelCatalogObservation:
      schema_version: int = 1
      backend_id: str  # canonical backend ID (e.g., "antigravity_cli")
      status: str  # "ok", "unsupported", "unavailable", "timeout", "error"
      models: Tuple[str, ...]
      source: str  # "cli", "sdk", "static"
      complete: bool
      checked_at: str  # ISO-8601 UTC string (e.g. "2026-07-22T00:51:21Z")
      last_success_at: Optional[str]  # ISO-8601 UTC string or None
      last_attempt_at: str  # ISO-8601 UTC string
      source_fingerprint: str  # SHA-256 of non-secret effective config + provider version
      reason_code: Optional[str] = None
  ```
- **Not side-effect-free.** Unlike `health.py` probes (standard-library only, never a model call), catalog discovery runs the provider's `models` listing, which — per the `health.py` contract note — can **require live auth and incur cost**. This is exactly why discovery is a separate module gated behind explicit refresh modes, install, and background startup, and is never invoked under `model_refresh` `"none"`/`"cached"`.
- **Effective Configuration & Security Fingerprinting**: `discover_models(agent_config)` accepts the effective backend configuration (executable paths, API endpoints, project, region) **and the resolved provider CLI/SDK version**. It strips secret tokens/headers before computing a SHA-256 digest for `source_fingerprint`. The version is included so a provider upgrade (which can change the catalog with identical config) invalidates a cached catalog.
- **Capability Matrix**: Probe backends using verified local CLI/SDK mechanisms (candidates to confirm in the spike: `grok models`, `agy models`, or SDK catalog endpoints). Backends without an explicit catalog discovery mechanism return `status="unsupported"` and fall back to static suggestions.
- **Per-backend parser**: Each source owns a tolerant parser for its listing output (format unknown until the spike — JSON vs. table vs. prose, and version-fragile). Any parse or nonzero-exit failure yields `status="error"` with `complete=False` and falls back to static suggestions; it never raises into the caller.
- **Concrete Execution Boundaries**:
  - Execute CLI probes via `asyncio.create_subprocess_exec` wrapped in `asyncio.wait_for` for the per-probe deadline (e.g., 2s), with an overall collection deadline. Dependencies (runner, clock, credential evidence) are injectable exactly as in `health.py` so tests drive discovery with fake runners and never touch real CLIs, SDKs, or the network.
  - Run probes concurrently for enabled backends.
  - Implement in-flight refresh deduplication to prevent redundant concurrent network/CLI calls.
  - Daemon startup triggers background refresh only after the server is ready.
  - Installer (`agent-collab install`) awaits discovery with non-fatal degradation: if discovery times out or errors, install logs the warning and completes using static fallbacks.

### 3. Caching and lifecycle hooks

- **Runtime Cache Location**: Store discovered model catalogs under `$AGENT_COLLAB_HOME/cache/models_<backend>.json`. Add a `cache_dir` to `GlobalDataPaths` (created `0700` by `ensure_dirs`) and write each file with the existing `atomic_write_private_text` helper (`0600`). `paths.py` currently has `data`/`daemon`/`tmp` only, so the `cache` directory is a small addition.
- **Cache Record Metadata**: On-disk JSON maps directly to `ModelCatalogObservation` including `schema_version=1`, `backend_id`, ISO-8601 UTC timestamps, `source_fingerprint`, and model tuple. On an unknown or mismatched `schema_version`, discard the entry and re-probe rather than parsing it.
- **Cache Semantics & Fingerprint Validation**:
  - `model_refresh = "none|cached|fresh"`. Default is `"cached"` in `api_schema.py` (`OptionsRequestModel`) and MCP `agent_collab_describe_options`. Modes `"none"` and `"cached"` never initiate network/CLI calls.
  - On reading cache under `"cached"` or `"none"` mode: if `source_fingerprint` mismatches current effective configuration fingerprint, the cache entry is invalidated and falls back to static suggestions (or initiates background fresh probe if under `"cached"` mode).
  - 24-hour TTL (calculated using UTC `datetime` objects) for successful discoveries. TTL governs whether `"cached"` mode schedules a background refresh; **it does not delete the entry**.
  - **Serve precedence** (highest first): a fresh successful catalog → the last known good catalog (flagged `stale=true` when past TTL or when a refresh attempt just failed) → static suggestions. Static applies only when there is no last known good catalog. An entry is never dropped to static on age alone.
  - Short retry interval for transient failures/unauthenticated states (do not cache errors for 24h, and do not let a failed probe overwrite the last known good catalog).
  - The `configured_default_not_in_catalog` warning (see [configured default handling](#configured-default-handling-warn-only)) requires an `ok`+`complete` catalog; a `stale`-flagged or `error` observation never warns.

### 4. Integration into MCP and API contracts

- `agent_collab_describe_options` accepts `model_refresh="none|cached|fresh"` (default `"cached"`) and exposes per-backend fields under `backends.<canonical_backend>`:
  - `static.option_schema`: Shipped contract and static fallback suggestions.
  - `model_catalog`: Observation status, timestamps, and metadata, including the non-fatal `configured_default_not_in_catalog` reason code when an authoritative catalog omits the configured default.
  - `effective.option_schema`: Deterministically merged suggestions (`[configured_default] + [discovered_catalog] + [static_fallback]` with order-preserving deduplication). The configured default always leads this merge and remains the effective default — the warning never removes it (see [configured default handling](#configured-default-handling-warn-only)).
- **`model_refresh` asymmetry with `health_refresh`.** The existing `describe_options` `health_refresh` accepts only `{cached, fresh}`; `model_refresh` deliberately adds `none` because catalog probes are more expensive (auth/cost/latency) than side-effect-free health probes, so callers must be able to demand a purely local answer.
- **`fresh` cost gate.** `"fresh"` performs live, possibly billable calls. It follows the same confirm-the-paid-action norm the MCP guidance already applies to review workflows, and is bounded by in-flight dedup plus a minimum re-probe interval per backend so repeated `fresh` requests cannot fan out unbounded provider calls.

## Phased implementation plan

The work splits into two parts. **Part 1 (CLI backends)** ships the full feature
for the four default-enabled `*_cli` backends, whose discovery is a local
subprocess that reuses the `health.py` execution/timeout/injectable-runner shape
and is hermetically testable. **Part 2 (SDK backends)** adds the four opt-in
`*_sdk` backends behind the *same* `ModelCatalogObservation` interface later;
their discovery is a network call requiring API-key auth, is harder to test
hermetically, and has a smaller blast radius (all SDK backends ship
`enabled = false`). No contract or schema change is needed to add Part 2 — the
SDK source slots into the Phase 2 interface.

### Part 1 — CLI backends (this milestone)

#### Phase 1: Option contract & schema shift (Immediate PR)
1. Convert `options.model.allowed` in `options.toml` to static `suggested` arrays across all backends (both `*_cli` and `*_sdk`, so the SDK backends are ready for Part 2 without a second schema pass).
2. Add non-whitespace string validation (`value.strip() != ""`) for `options.model` in `backend_contract.py`.
3. Add unit tests verifying:
   - Former allowlisted backends accept custom non-whitespace model strings.
   - Blank and whitespace-only strings are rejected.
   - Non-model enums (`permission_mode`, `sandbox`, `reasoning_effort`) strictly retain `allowed` validation.
   - Shipped `defaults.toml` models (including `usage_windows.targets.*`) still validate as non-blank strings once `allowed` is gone.
   - `describe_options` outputs expected schema without regressions.

#### Phase 2: Capability spike & discovery module (CLI source)
1. Conduct a capability spike for the four `*_cli` backends: confirm the `models` listing command exists, capture its exact output format, and record whether it requires auth.
2. Implement `agent_collab/backends/common/model_discovery.py` — the `ModelCatalogObservation` contract, the fingerprint (config + provider version, secrets stripped), the `source="cli"` probes with per-backend tolerant parsers, and local cache storage. Add the `cache_dir` to `GlobalDataPaths`.

#### Phase 3: Daemon, MCP & installer integration (CLI)
1. Add `model_refresh` option to `agent_collab_describe_options` and `api_schema.py`, plus the `effective`/`model_catalog` per-backend fields.
2. Connect daemon startup non-blocking refresh task and installer discovery awaiter with non-fatal degradation.
3. Wire the warn-only default check: install warning, `configured_default_not_in_catalog` reason code in `describe_options`/start responses, and the catalog-transition daemon log/event.

### Part 2 — SDK backends (follow-up)

#### Phase 4: SDK source
1. Spike each SDK's model-list endpoint (`claude_sdk`, `codex_sdk`, `antigravity_sdk`, `xai_sdk`) and its auth requirements.
2. Implement `source="sdk"` behind the existing `ModelCatalogObservation` interface, with separate credentialed integration tests. No changes to the option contract, cache format, or MCP/API surface.

## Verification plan

### Hermetic tests
- `tests/backends/test_contract.py`: Verify non-whitespace model option validation, custom model string acceptance, strict `allowed` validation for non-model enums, and that shipped `defaults.toml` models (including `usage_windows.targets.*`) validate.
- `tests/backends/test_model_discovery.py`: Verify discovery observation parsing, per-backend parser tolerance (parse/nonzero-exit failure → `status="error"`, never raises), SHA-256 fingerprinting (including provider-version sensitivity), cache atomic writes and `0700`/`0600` permissions, UTC TTL expiration, fingerprint mismatch cache invalidation, unknown `schema_version` discard, serve precedence (fresh → last-known-good `stale=true` → static), and per-probe deadline handling with a fake async runner.
- Warn-only default cases: the `configured_default_not_in_catalog` warning fires **only** on an `ok`+`complete` catalog that omits the configured default; it does **not** fire on `unsupported`/`unavailable`/`timeout`/`error`/incomplete/`stale`/fingerprint-mismatch observations; the warning clears when the model reappears or the fingerprint changes; the configured default is passed through unchanged in **every** case, warning or not; discovery output is never written to any config file; a catalog change after session start does not alter the running session's snapshotted options.

### Integration tests
- Credentialed CLI backend model discovery probes verifying real `models` listing against live provider environments (Part 1). SDK-endpoint probes added with Part 2.
