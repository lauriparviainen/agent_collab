# Opt-in backend live verification and preference policy

**Status:** First-pass proposal; not implementation-ready. Revisit after the
SDK continuity work in #47.

**Created:** 2026-07-23

**Issue:** #48

## Design maturity

This document is an initial requirements and risk sketch, not a completed
design or an instruction to implement the shapes below as written. The
implementing agent must first re-check the post-#47 runtime, existing install
and discovery surfaces, provider behavior, and current CLI/SDK capabilities.

Before implementation begins, conduct and record a focused design pass that:

- validates whether live verification belongs on the install command at all,
  and, if it does, settles the exact switch, target-selection, confirmation,
  failure, and exit-status behavior;
- designs the standalone verification command and determines which logic can
  safely be shared with installation and credentialed integration tests;
- proposes and reviews the exact user-config shape for provider-wide and
  model-specific backend preferences, including migration, precedence, project
  scope, and interaction with canonical workflow members and start overrides;
- decides whether preference is advisory, explicitly selection-changing, or
  supports both modes, without introducing silent fallback;
- verifies version/update data sources separately for every CLI and SDK rather
  than assuming a uniform provider API or package manager;
- estimates model-call cost, latency, credential exposure, transcript
  retention, and cleanup behavior before choosing verification defaults; and
- updates this task document and issue #48 with the settled design and
  acceptance criteria before changing production behavior.

Any configuration or command syntax shown in this first pass is illustrative.
Do not preserve it merely for consistency with this draft if investigation
supports a clearer or safer design.

## Context

Installation already performs a backend-first readiness pass through the newly
installed environment. It reports dependency presence, credential evidence,
and installed provider versions without making a model call. Installation also
attempts provider model-catalog discovery where a real listing API exists.
Those features were delivered by #23, #24, and #45.

This evidence is intentionally advisory. A side-effect-free probe or successful
model listing cannot prove that the configured account may run the configured
model, that a complete turn succeeds, or that provider runtime behavior still
matches the backend adapter. The credentialed `integration-test` developer
command can provide that proof, but it is not an installer option or a normal
user-facing backend diagnostic.

The current readiness table reports installed CLI and SDK versions but does not
compare them with backend-owned compatibility policy or upstream releases. The
current recommendation protocol also keeps the configured canonical backend
and deliberately does not infer that `sdk` is preferable to `cli`.

## Goal

Add an explicit, paid-action verification path that can be requested during
installation and rerun later. It should prove a real minimal turn for selected
backends, explain version compatibility and available updates where that can
be checked safely, and support explicit user-owned backend preferences for a
provider/model without treating SDK as inherently superior.

The normal install remains cheap and non-credentialed apart from the existing
model-catalog listing calls. Live verification is never enabled implicitly.

## Scope

### 1. Reusable live-verification operation

- Add a user-facing backend verification command and expose the same operation
  through an optional installation switch.
- Let the user target one or more canonical backends; define a clear default
  target set only after the cost and latency are visible in help.
- Execute a minimal real turn with the effective backend configuration and
  configured model in a disposable empty workdir.
- Reuse the production backend runner and outcome contract. Do not create a
  second provider-specific verification implementation in the installer.
- Use bounded deadlines and deterministic cleanup. Verification must not start
  or permanently enable the daemon, change provider configuration, update
  models, or mutate the user's project.
- Report one structured result per backend: dependency, credential evidence,
  selected model, live-turn outcome, latency, provider/backend version, and
  sanitized remediation.
- Return a meaningful nonzero status when explicitly requested targets fail,
  while making clear that the package installation itself was not rolled back.
- Never print credential values, raw provider payloads, or machine-specific
  credential locations. State clearly that verification may consume paid
  provider usage.

### 2. Version compatibility and update evidence

- Extend backend-owned metadata so a report can distinguish:
  - installed and within the adapter's verified compatibility range;
  - a newer compatible release is available;
  - installed outside the supported range;
  - a newer upstream release exists but is not yet verified by agent-collab;
  - update status cannot be determined.
- Check CLI and SDK versions independently for every configured backend. Show
  the installed version and, when authoritative data is available, the newest
  agent-collab-compatible version and newest upstream version.
- Warn the user when an installed CLI or SDK is outside the supported range or
  when a newer compatible release is available. Distinguish that actionable
  warning from an informational "newer upstream but not yet verified" notice
  and from `unknown`; never label a backend outdated without authoritative
  evidence.
- SDK status must respect the dependency ranges shipped by agent-collab. The
  durable installer already installs all SDK extras, so "latest upstream" must
  not be presented as safer than the newest compatible version.
- CLI update checks are backend-specific and may report `unknown` when no
  stable, read-only source exists. Every actionable warning must include a
  backend-specific update command or documentation hint, but installation must
  never update provider tooling automatically.
- Network update checks are explicit, bounded, cacheable, and non-fatal to a
  normal install.

### 3. Explicit backend preference by provider/model

- Add user-owned policy capable of expressing an ordered backend preference
  for a provider and, where useful, a provider/model pair (for example, prefer
  `codex_sdk` over `codex_cli` for one model).
- Never infer a general `sdk > cli` ranking. Preference is configuration, not a
  conclusion drawn from mechanism names.
- A candidate must be registered, enabled, compatible with the requested
  option shape, and advertise or accept the requested model. Live-verification
  success is stronger evidence than a health probe, but it must carry a
  timestamp and cannot become permanent truth.
- Keep facts, recommendation, and selection distinct in discovery output.
  Decide during design whether preference resolution is advisory by default
  with a separate explicit opt-in for automatic selection; no silent failover
  or backend oscillation is allowed.
- Preserve canonical backend identity in session settings and transcripts so a
  resolved preference is auditable.

## Relationship to existing work

- #23 and #24 remain the authority for the automatic, non-model-call install
  readiness summary.
- #45 remains the authority for model catalog discovery and its cache.
- #47 completes the current SDK continuity stages first; this work should use
  the resulting stable SDK runner lifecycle rather than designing around an
  intermediate state.
- The completed `backend-discovery-and-recommendation` design remains valid:
  configured selection is kept conservatively, SDK is not inherently
  preferred, and automatic failover requires explicit policy.
- `integration_tests/` remains the credentialed provider regression suite.
  Production live verification may share fixtures or low-level helpers, but
  user behavior must not depend on importing the test package.

## Verification

- Hermetic tests cover target selection, paid-action confirmation/help,
  deadlines, partial failure, exit status, redaction, disposable-workdir
  cleanup, and install behavior with and without the switch.
- Backend-owned fake probes cover every version status without network access.
- Preference tests cover provider-wide and model-specific ordering, disabled
  or incompatible candidates, stale live evidence, deterministic resolution,
  explicit versus advisory behavior, and canonical selection echoes.
- Credentialed integration coverage proves one successful and one
  authentication/model failure shape for each backend that can be exercised in
  CI or a maintainer environment.
- The full hermetic suite and generated API/config documentation remain green.

## Open design questions

- Exact CLI syntax for selecting all enabled backends versus a repeated
  explicit backend list.
- Whether live verification should be part of installation or only a
  post-install command that the installer offers to run.
- Whether the installer should continue after requested live-verification
  failures and return nonzero, or require a separate strictness flag.
- Whether successful live evidence belongs only in command output or in a
  short-lived cache usable by discovery recommendations.
- Whether explicit preference resolves a logical provider agent at start or
  remains a recommendation that callers materialize through member/backend
  selection.
