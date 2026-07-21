# Dynamic backend model discovery at installation and startup

**Status:** Open — architecture approved with final dual-review clarifications incorporated.

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
      source_fingerprint: str  # SHA-256 digest of non-secret effective configuration
      reason_code: Optional[str] = None
  ```
- **Effective Configuration & Security Fingerprinting**: `discover_models(agent_config)` accepts the effective backend configuration (executable paths, API endpoints, project, region). It strips secret tokens/headers before computing a SHA-256 digest for `source_fingerprint`.
- **Capability Matrix**: Probe backends using verified local CLI/SDK mechanisms (e.g., `grok models`, `agy models`, or SDK catalog endpoints). Backends without an explicit catalog discovery mechanism return `status="unsupported"` and fall back to static suggestions.
- **Concrete Execution Boundaries**:
  - Run probes concurrently for enabled backends with per-probe deadlines (e.g., 2s) and an overall collection deadline.
  - Implement in-flight refresh deduplication to prevent redundant concurrent network calls.
  - Daemon startup triggers background refresh only after the server is ready.
  - Installer (`agent-collab install`) awaits discovery with non-fatal degradation: if discovery times out or errors, install logs the warning and completes using static fallbacks.

### 3. Caching and lifecycle hooks

- **Runtime Cache Location**: Store discovered model catalogs under `$AGENT_COLLAB_HOME/cache/models_<backend>.json` using atomic file writes with `0700` directory and `0600` file permissions (via `agent_collab.paths`).
- **Cache Record Metadata**: On-disk JSON maps directly to `ModelCatalogObservation` including `schema_version=1`, `backend_id`, ISO-8601 UTC timestamps, `source_fingerprint`, and model tuple.
- **Cache Semantics & Fingerprint Validation**:
  - `model_refresh = "none|cached|fresh"`. Default is `"cached"` in `api_schema.py` (`OptionsRequestModel`) and MCP `agent_collab_describe_options`. Modes `"none"` and `"cached"` never initiate network/CLI calls.
  - On reading cache under `"cached"` or `"none"` mode: if `source_fingerprint` mismatches current effective configuration fingerprint, the cache entry is invalidated and falls back to static suggestions (or initiates background fresh probe if under `"cached"` mode).
  - 24-hour TTL (calculated using UTC `datetime` objects) for successful discoveries under `"cached"` mode.
  - Short retry interval for transient failures/unauthenticated states (do not cache errors for 24h).
  - Retain last known good catalog when a fresh refresh attempt fails.

### 4. Integration into MCP and API contracts

- `agent_collab_describe_options` accepts `model_refresh="none|cached|fresh"` (default `"cached"`) and exposes per-backend fields under `backends.<canonical_backend>`:
  - `static.option_schema`: Shipped contract and static fallback suggestions.
  - `model_catalog`: Observation status, timestamps, and metadata.
  - `effective.option_schema`: Deterministically merged suggestions (`[configured_default] + [discovered_catalog] + [static_fallback]` with order-preserving deduplication).

## Phased implementation plan

### Phase 1: Option contract & schema shift (Immediate PR)
1. Convert `options.model.allowed` in `options.toml` to static `suggested` arrays across all backends.
2. Add non-whitespace string validation (`value.strip() != ""`) for `options.model` in `backend_contract.py`.
3. Add unit tests verifying:
   - Former allowlisted backends accept custom non-whitespace model strings.
   - Blank and whitespace-only strings are rejected.
   - Non-model enums (`permission_mode`, `sandbox`, `reasoning_effort`) strictly retain `allowed` validation.
   - `describe_options` outputs expected schema without regressions.

### Phase 2: Capability spike & discovery module
1. Conduct backend capability spike for CLI and SDK catalog endpoints across all 8 backends.
2. Implement `agent_collab/backends/common/model_discovery.py` and local cache storage under `$AGENT_COLLAB_HOME/cache/`.

### Phase 3: Daemon & MCP integration
1. Add `model_refresh` option to `agent_collab_describe_options` and `api_schema.py`.
2. Connect daemon startup non-blocking refresh task and installer discovery awaiter with non-fatal degradation.

## Verification plan

### Hermetic tests
- `tests/backends/test_contract.py`: Verify non-whitespace model option validation, custom model string acceptance, and strict `allowed` validation for non-model enums.
- `tests/backends/test_model_discovery.py`: Verify discovery observation parsing, SHA-256 fingerprinting, cache atomic writes, UTC TTL expiration, fingerprint mismatch cache invalidation, and fallback behaviors.

### Integration tests
- Credentialed backend model discovery probes verifying real CLI/SDK model listing against live provider environments.
