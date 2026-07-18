# Backend-owned shipped defaults

**Status:** Closed — implemented, verified, and included in 0.10.1.

**Created:** 2026-07-17.

**Issue:** [#42](https://github.com/lauriparviainen/agent_collab/issues/42)

## Purpose

Move every shipped backend-specific setting out of the central
`agent_collab/default_config.toml` and into the package that implements that
backend. Build the effective built-in `CollaborationConfig` deterministically
from the general defaults plus the registered backend fragments, without
changing public configuration, precedence, generated user config, discovery,
or runtime behavior.

This includes the packaged Event Window target for each backend. The shared
Event Window schedule is general daemon policy and remains central; a complete
packaged target — disabled posture, backend, economical model, options, and any
target-specific schedule overrides — is a backend-specific shipped default and
moves with that backend.

## Data boundary and non-goals

This task changes only defaults shipped in the repository and the code that
loads and validates those built-in defaults. It must not read, rewrite,
migrate, or otherwise modify actual user or project configuration files. It
also must not modify daemon session state or other runtime data.

The relocation is representation-only. After normal config loading, every
valid existing configuration must retain the same schema, accepted syntax,
merge precedence, effective values, generated-config output, discovery, and
runtime behavior. In particular:

- do not bump `schema_version` or add a user-config migration;
- do not rename, remove, or reinterpret any public config field;
- do not persist the composed built-in document or backend fragments into a
  user or project config location;
- do not change any shipped value while moving it, including Event Window
  enablement, models, options, or schedule overrides; and
- do not change the shared Event Window schedule defaults that remain in
  `default_config.toml`.

The two parser/validation hardenings in this plan affect diagnostics for
invalid input only: reporting shipped option failures under `.options` and
making the Python 3.10 fallback parser reject duplicate TOML definitions with
source-qualified errors. Neither hardening changes valid configuration data or
its effective result.

## Current state

`agent_collab/default_config.toml` currently combines four kinds of built-in
configuration:

1. the public config `schema_version`;
2. global system and Event Window schedule defaults;
3. one disabled Event Window target for every real registered backend;
4. every backend definition and the built-in workflows.

Each backend already owns a package under
`agent_collab/backends/<canonical>/`, containing `backend.py`, `options.toml`,
an optional static `config.toml`, and a README. The central defaults file is
therefore an additional backend-maintenance surface for:

- enablement;
- CLI command and arguments;
- backend static configuration;
- shipped session option defaults, including safety posture;
- the backend's packaged Event Window model and target options.

`builtin_config()` currently loads one TOML document, migrates it, and merges
it with `scope="built_in"`. That scope is important: it stores a built-in
`[backends.<canonical>.options]` table in `default_options`, separate from
user-configured `options`. Backend option normalization then applies the
existing precedence:

```text
shipped defaults < configured CLI argv < user backend options < start options
```

Event Window target tables already merge field by field, allowing a user to
enable an inherited packaged target with only `enabled = true`.

## Ownership decision

Use `defaults.toml` as the backend fragment name. `default_options.toml` is too
narrow because the file also owns execution configuration, policy, and Event
Window targets. `defaults.toml` is distinct from:

- `options.toml`, the declarative session-option schema; and
- `config.toml`, the optional schema for static backend configuration.

The central and backend-owned files divide responsibility as follows:

| Setting | Owner after this task |
| --- | --- |
| `schema_version` | central `default_config.toml` |
| `system.timezone` | central `default_config.toml` |
| shared Event Window days, work time, interval, and jitter | central `default_config.toml` |
| complete packaged Event Window target, including `enabled` and any per-target schedule overrides | owning backend `defaults.toml` |
| backend enablement, command, args, and static settings | owning backend `defaults.toml` |
| backend shipped session option defaults | owning backend `defaults.toml` |
| built-in workflows, including `usage-window` | central `default_config.toml` |

The central file must contain neither `[backends]` nor
`[usage_windows.targets]` after the move. This keeps ownership unambiguous and
prevents an old central definition from silently overriding a fragment.

## Fragment contract

Each registered backend package must contain exactly one `defaults.toml`. The
fragment reuses the current public config tables rather than introducing a new
parallel schema. For example:

```toml
[backends.codex_cli]
enabled = true
command = "codex"
args = ["exec", "--json"]

[backends.codex_cli.options]
model = "gpt-5.6-sol"
thinking_level = "high"
sandbox = "read-only"

[usage_windows.targets.codex_cli_luna]
enabled = false
backend = "codex_cli"
model = "gpt-5.6-luna"

[usage_windows.targets.codex_cli_luna.options]
thinking_level = "low"
sandbox = "read-only"
```

A fragment:

- must contain a `backends` mapping with exactly one
  `[backends.<canonical>]` table; a scalar or array in place of either table is
  a source-qualified fragment-shape error;
- must declare the same canonical name as its parent directory;
- must set backend `enabled` explicitly to a boolean. Omission is an error
  rather than an implicit `true`, because accidentally enabling an opt-in SDK
  changes discovery and execution policy;
- may contain zero or more `[usage_windows.targets.<id>]` tables; both
  `usage_windows.targets` and each target value must be mappings when present;
- must set every contributed target's `backend` to the fragment's canonical
  backend;
- must set every contributed target's `enabled` explicitly to `false` and its
  `model` to a non-empty string. Packaged targets remain inert until a user
  deliberately enables one;
- may use exactly the public target fields `enabled`, `backend`, `model`,
  `options`, `days`, `work_time`, `interval`, and `jitter`. The last four are
  optional per-target schedule overrides and remain backend-owned when a
  packaged target needs them;
- may use the ordinary backend-section fields `enabled`, `command`, `args`,
  `name`, `env`, `cwd`, `timeout`, and `options`. Shipped nested `agents` are
  outside this task and rejected in a fragment; users may continue defining
  personae in user config. Other scalar keys are backend static configuration,
  not fragment-shape errors: they must be accepted by that registered backend's
  `normalize_config` contract (and its colocated `config.toml`, when present).
  A backend without a static-config contract rejects every such key, including
  misspellings such as `comand`;
- may not define `schema_version`, workflows, shared Event Window schedule
  fields, system settings, a top-level `[agents]` table, daemon policy, session
  policy, or workdir policy;
- does not carry its own config schema version. The assembled built-in document
  has one schema version from `default_config.toml` and passes through the
  existing migration boundary once.

The initial fragments contain exactly one packaged Event Window target each,
matching current behavior. The contract permits more than one future target
for a backend because a backend may intentionally ship several economical
models; target identifiers must remain globally unique.

## Loading and composition

Refactor `builtin_config()` around a private, testable composition helper.
The helper should accept the main defaults path, backend root, and registered
canonical names so failure tests can use temporary directories without
patching package files.

Composition proceeds as follows:

1. Load `default_config.toml` and reject central `[backends]` or
   `[usage_windows.targets]` definitions.
2. Discover `backends/*/defaults.toml` and sort paths deterministically.
3. Parse all fragments with source-qualified errors.
4. Validate the fragment shape, required explicit fields, allowed target
   fields, and target ownership. Collect both its declared identifiers and
   provenance maps from canonical backend and target id to fragment path.
5. Reject duplicate canonical declarations and duplicate target identifiers
   before composing data.
6. Compare fragment directories and declarations with
   `registered_backend_names()`. Report missing, unregistered/orphaned, or
   canonically mismatched fragments explicitly.
7. Insert the collected backend tables under the main document's `backends`
   table and target tables under the existing `usage_windows.targets`, ordered
   by canonical backend and target id for deterministic diagnostics and output.
   Add only the `targets` child; never replace the central `usage_windows`
   mapping or its shared schedule fields.
8. Run `migrate_config_data(..., scope="built_in")` once on the assembled raw
   document. If migration or merge validation raises a field-qualified
   `ConfigError`, resolve its backend or target identity through the provenance
   maps and prepend the owning fragment path. Recognize both dotted forms
   (`backends.<canonical>...`) and the existing bracketed table forms
   (`[backends.<canonical>]`, `[usage_windows.targets.<id>]`); do not assume all
   merge errors begin with an unbracketed dotted prefix. Errors in general
   sections retain the central defaults path.
9. Run `merge_config_data(..., scope="built_in")` once, then run dedicated
   built-in composition validation followed by ordinary `validate_config`.
   The dedicated validation receives the provenance maps and covers every
   backend section, including disabled backends with no Event Window target,
   rather than relying on the derived agents of enabled backends. It also
   source-qualifies every packaged target validation error.

Do not load and merge each fragment independently. A single assembled
built-in layer preserves the current treatment of option defaults, keeps one
schema/migration boundary, and avoids making fragment filename order a new
precedence rule.

The Python 3.10 fallback TOML parser must attach the source path to parse
errors, matching `tomllib` behavior on newer interpreters. Duplicate TOML keys
or table declarations must not be accepted silently by the fallback path.

Built-in paths remain implementation inputs, not user config paths:
`CollaborationConfig.loaded_paths` continues to list only user and project
configuration. `config show` may continue to identify the central
`default_config.toml` as the built-in entrypoint; documentation explains that
it composes backend fragments.

## Implementation slices

### 1. Add fragment composition and validation

In `agent_collab/config.py`:

- add constants for the backend package root and fragment filename;
- add the private fragment loading/composition helper;
- make all file and contract failures `ConfigError` instances containing the
  relevant path and canonical or target name;
- retain private canonical-backend→fragment and target-id→fragment provenance
  maps through composition validation. Do not add them to public
  `CollaborationConfig` or `loaded_paths`;
- preserve one migration and one built-in merge;
- validate the completed built-in config;
- validate each backend section's static configuration and shipped
  `default_options` through its registered backend contract regardless of
  enablement or target presence. Build a representative `AgentConfig` for
  every section, not only when a derived agent is absent. Populate its
  canonical id, provider type/backend id, command, args, enabled state, name,
  env, cwd, timeout, complete `backend_config`, `options={}`, and the section's
  complete `default_options`; do not accidentally validate the empty
  `section.options` bag in place of shipped defaults;
- update `validate_agent`'s `BackendOptionError` mapping so a field present in
  either `agent.options` or `agent.default_options` is reported beneath
  `backends.<canonical>.options.<field>`. The dedicated built-in validator then
  prefixes that canonical field path with the backend provenance path; do not
  rely on the current mapping, which omits `.options` for `default_options`;
- source-qualify errors raised during raw migration and merge as well as errors
  from post-merge backend/target validation. Invalid primitive field types must
  name their fragment even when composition fails before a
  `CollaborationConfig` exists;
- validate every packaged Event Window target through the existing target and
  backend option contracts, including disabled targets, and prefix failures
  with the owning fragment path while retaining the
  `usage_windows.targets.<id>...` field path;
- validate backend static keys through `normalize_config` for enabled and
  disabled sections. Preserve valid backend-owned fields, but reject unsupported
  keys and typos with the owning fragment path.

Keep backend registration as the authority for required fragments. Do not add
entry-point discovery or a second registry.

### 2. Move the shipped data

Create `defaults.toml` for all eight registered real backends:

- `claude_cli` and `claude_sdk`;
- `codex_cli` and `codex_sdk`;
- `antigravity_cli` and `antigravity_sdk`;
- `xai_cli` and `xai_sdk`.

Move the current backend section and matching packaged Event Window target
verbatim, including comments that explain safety posture and headless CLI
behavior. Leave only general system, shared schedule, and workflow data in
`agent_collab/default_config.toml`.

### 3. Package and installed-wheel verification

Add `backends/*/defaults.toml` to `[tool.setuptools.package-data]` in
`pyproject.toml`.

Strengthen the SDK-free CI install check so it changes to a directory outside
the source checkout and runs an operation that calls `builtin_config()`, such
as `agent-collab config show --workdir <temporary-directory>`. `--help` alone
does not prove that installed package data can be discovered and loaded.

Add a hermetic package-data contract that compares the registered canonical
set with the source fragment set. The installed-wheel CI check remains the
end-to-end proof that setuptools included the files and runtime lookup does
not accidentally depend on the checkout.

### 4. Documentation

Update:

- every backend README to distinguish `options.toml`, optional `config.toml`,
  and shipped `defaults.toml`;
- `doc/agent-configuration.md`;
- `doc/runtime-layout.md`;
- `doc/implementation-notes.md`;
- `doc/event-windows.md`.

The Event Windows documentation should say that packaged targets are assembled
from backend-owned defaults, while the central file owns the shared schedule.
Users still enable an inherited target with a one-field override and do not
need to know which physical package-data file supplied it.

## Verification plan

Add focused tests for the composition helper:

- the central file rejects backend or target ownership;
- every registered backend has one fragment and every fragment belongs to a
  registered backend;
- fragment order does not change the effective config;
- missing files fail with the missing canonical and expected path;
- malformed TOML fails with its source path on Python 3.10 and newer;
- repeated keys and repeated table declarations fail through a test that
  directly exercises the forced fallback parser, preventing Python 3.10
  last-write-wins behavior from diverging from `tomllib`;
- duplicate canonical declarations fail;
- canonical name versus directory mismatches fail;
- unexpected top-level and `usage_windows` fields fail;
- scalar/array values in place of the fragment's backend table,
  `usage_windows.targets` table, or individual target table fail with the
  fragment path, covering both dotted and bracketed error forms;
- a nested `[backends.<canonical>.agents]` table fails, proving fragments cannot
  silently add built-in personae or change discovery/runtime surface;
- valid backend static fields pass, while unsupported static keys and backend
  field misspellings fail through the backend contract with the fragment path;
- invalid primitive types caught during merge include the owning backend or
  target fragment path;
- duplicate Event Window target identifiers fail;
- a target pointing at another backend fails with both target and owner named;
- omission of backend `enabled` fails instead of inheriting `true`;
- a packaged target missing explicit `enabled = false`, `backend`, or `model`
  fails;
- a packaged target with `enabled = true` fails before scheduler startup;
- all public per-target schedule override fields are accepted, while any other
  target field fails;
- invalid static settings or shipped option defaults fail even when the backend
  is disabled and has no packaged target;
- shipped `default_options` failures contain the exact fragment path and
  `backends.<canonical>.options.<field>` path;
- invalid packaged-target models or options fail even when the target is
  disabled, and the error contains both its fragment path and target field
  path;
- a synthetic central schedule whose values differ from the dataclass defaults
  survives target insertion unchanged, proving composition does not replace
  the central `usage_windows` mapping;
- the assembled configuration validates before use.

Extend behavior regression coverage to pin the current effective built-ins:

- all backend enablement values;
- CLI commands and argument arrays;
- all normal-session model and reasoning defaults;
- read-only/plan safety posture;
- all eight packaged Event Window target ids, backends, models, and options;
- the shared Event Window schedule and built-in workflows;
- `describe_options` output;
- generated user config;
- representative `config show` output.

Retain and exercise the precedence cases:

- user configuration overriding one backend option keeps unrelated shipped
  option defaults;
- configured CLI argv still outranks shipped backend defaults where inference
  is supported;
- explicit start options still outrank both;
- a user target table containing only `enabled = true` inherits its shipped
  backend, model, options, and schedule.

Run the complete local gates:

```bash
./agent_collab_dev.sh test
./agent_collab_dev.sh build --check
```

CI must additionally pass on Python 3.10 and 3.12 and load the built-in config
from the SDK-free installed wheel outside the checkout. No credentialed model
call is required for this configuration-only refactor.

## Done when

- every registered backend owns one validated `defaults.toml`;
- all backend definitions and packaged Event Window targets have left the
  central defaults file;
- shared Event Window policy and built-in workflows remain central;
- every backend and packaged target declares its enablement explicitly, and
  every packaged target remains disabled by default;
- built-in composition is deterministic and fails clearly for every broken
  ownership contract;
- effective config, precedence, safety posture, discovery, generated config,
  Event Window behavior, and CLI output remain unchanged;
- source and installed-wheel package-data contracts pass;
- backend, configuration, runtime, and Event Window documentation describe the
  assembled layout.

## Implementation outcome

Implemented as planned. The complete 953-test suite, build checks, generated
documentation checks, installed-wheel loading checks, and independent dual
review passed. No user or project configuration data, public config schema, or
config precedence changed.

## Open questions

None required before implementation. The filename, fragment shape, Event
Window ownership boundary, composition order, and validation behavior are
settled by this plan. If implementation reveals another backend-specific
built-in outside `[backends]` or `[usage_windows.targets]`, move it into the
owning fragment and record the additional allowed fragment section here rather
than leaving split ownership silently.
