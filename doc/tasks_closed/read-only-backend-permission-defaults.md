# Verify and enforce read-only default permissions for backend agents

**Status:** Closed — read-only defaults shipped via the built-in config's
option-defaults layer; see Decisions. Verified by the hermetic suite, a live
`mode=plan` dual Gemini review, and an install round-trip.

**Created:** 2026-07-14.

**Issue:** [#29](https://github.com/lauriparviainen/agent_collab/issues/29)

## Purpose

Double-check the assumption that backend agents run read-only by default, and
turn the answer into an explicit, tested policy. The audit below shows the
assumption does not hold today: the enabled-by-default backends inherit the
underlying provider CLI's own default instead of receiving an explicit
permission or sandbox flag, and one disabled-by-default backend ships a
write-enabled default.

## Audit (2026-07-14)

### How permissions flow

Permission and sandbox settings are ordinary typed backend options. Precedence
is set in `normalize_declared_options`
(`agent_collab/backend_contract.py:142-170`): schema `default` → values
inferred from the configured `args` → `[backends.*.options]` config defaults →
per-session `backend_options`. CLI backends only append a flag when the key is
present in the resolved options, so an option with no default anywhere is
simply never passed and the provider CLI's own default applies.

### Per-backend defaults when nothing is specified

| Backend | Permission option(s) | Shipped default | Effective posture |
| --- | --- | --- | --- |
| `claude_cli` | `permission_mode` → `--permission-mode` (`backends/claude_cli/backend.py:72-82`) | none — no schema default, shipped `args` (`default_config.toml:9`) set no flag | Inherits Claude Code's headless default; not pinned by agent-collab |
| `codex_cli` | `sandbox` → `--sandbox`, `approval_policy` → `--approval-policy` (`backends/codex_cli/backend.py:80-98`) | none — no schema default, shipped `args` (`default_config.toml:14`) set no flag | Inherits `codex exec`'s default sandbox; not pinned by agent-collab |
| `antigravity_cli` | `mode` → `--mode` (`backends/antigravity_cli/backend.py:71-80`) | `accept-edits` (schema default and shipped `args`, `default_config.toml:22`) | **Write-enabled** by default; backend disabled by default |
| `xai_cli` | `permission_mode`, `sandbox` (`backends/xai_cli/backend.py:96-119`) | `permission_mode = bypassPermissions`, `sandbox = read-only` | Writes blocked by sandbox; approval-free inspection; disabled by default |
| `claude_sdk` | `permission_mode` (`backends/claude_sdk/backend.py:355-385`) | none | SDK default |
| `codex_sdk` | `sandbox` (`backends/codex_sdk/backend.py:396-475`) | none | SDK default |
| `antigravity_sdk` | none (mode is CLI-only) | — | Provider default, no override surface |
| `xai_sdk` | none; no tools enabled | — | Remote chat only |

Notes:

- The `codex_cli` test asserting `sandbox=read-only`
  (`tests/backends/codex_cli/test_backend.py:79-90`) only holds because that
  test's agent `args` inject `--sandbox read-only`; it is inferred from args,
  not a shipped default.
- The `antigravity_cli` `accept-edits` default exists so headless `-p` runs do
  not stall on the interactive request-review approval prompt
  (`doc/agent-configuration.md:413-414`).
- `xai_cli` also appends a hard-coded read-only prompt rule
  (`backends/xai_cli/backend.py:31-35`).

### Workflows and review skills

The shipped workflows (`solo-*`, `cross-review`, `dual-review`;
`agent_collab/default_config.toml:45-72`) carry no permission policy. Review
read-only behavior is prompt-level only: the MCP review recipe
(`agent_collab/mcp-guidance.md:208-230`) injects "do not edit files" text and
itself warns that prompt-level read-only instructions are behavioral, not a
security boundary. The sandboxed `readonly` persona plus `readonly-review`
workflow appears only as a documentation example
(`doc/agent-configuration.md:69-93`); the shipped config does not include it.

### Existing guardrails that are adjacent but not sufficient

- `[backends.*]` sections are accepted only from the home config, so project
  config cannot loosen daemon-user policy
  (`doc/agent-configuration.md:270-276`).
- The workdir is a cwd, not an OS sandbox (`doc/runtime-layout.md:131`,
  `doc/daemon-architecture.md:287-307`).
- Open task `sdk-session-control` proposes first-class tool approval, which
  would add an interactive gate but is not implemented.

## Scope

1. Decide the intended default write posture per backend and record the
   decision here (read-only where the backend supports it; documented
   exception otherwise).
2. Ship explicit defaults matching the decision — likely
   `sandbox = read-only` schema defaults for `codex_cli`/`codex_sdk`, and a
   decision on whether to pin `claude_cli`/`claude_sdk` `permission_mode`
   (its mode set — `default`/`acceptEdits`/`bypassPermissions` — has no true
   read-only member, so the decision may be "pin `default` and document what
   it permits").
3. Decide whether review workflows and/or a shipped `readonly` persona should
   enforce sandbox-level read-only instead of prompt text.
4. Add regression tests asserting the shipped default flags per backend so a
   loosening change fails CI.
5. Consolidate the per-backend effective default posture into one place in
   `doc/agent-configuration.md`.

## Decisions (2026-07-15)

Prompted by a real incident: an `antigravity_cli` turn deleted files during a
nominally read-only review, because the shipped default was
`--mode accept-edits` (auto-approve edits).

1. **Read-only is the shipped default wherever the provider has a control.**
   `claude_cli`/`claude_sdk` `permission_mode = "default"` (headless runs deny
   write/exec tools instead of prompting; `"plan"` added to the accepted
   values as the strictest read-only mode), `codex_cli`/`codex_sdk`
   `sandbox = "read-only"`, `antigravity_cli` `mode = "plan"`, `xai_cli`
   unchanged (`bypassPermissions` + read-only sandbox). Agents still call
   tools and run inspection commands; writes need an explicit opt-in per
   backend, persona, or session. `antigravity_sdk` (no control) and `xai_sdk`
   (no tools) are documented exceptions.
2. **Shipped option defaults are configuration, not manifest data.** All
   `default =` values moved out of the backend `options.toml` manifests into
   `[backends.<canonical>.options]` tables in
   `agent_collab/default_config.toml`, per the direction that CLI options
   should live in config defaults rather than hard-coded schemas. They merge
   into a new `BackendPolicyConfig.default_options` field (built-in scope
   only) and apply through the new `configured_defaults` layer of
   `normalize_declared_options` — below argv inference and user options, so
   existing `args` flags and user overrides keep winning, and a user
   `options` table can never silently drop the shipped posture.
3. **`antigravity_cli` gains a boolean `sandbox` option** mapping to the
   `agy --sandbox` terminal-restriction flag (opt-in defense in depth; `plan`
   mode governs the edit tool, the sandbox restricts terminal commands).
4. **Discovery keeps showing defaults**: `_backend_option_schemas` overlays
   the built-in `default_options` onto the serialized option schemas.
5. The `readonly` persona/workflow doc example became a write-enabled
   `writer` persona example, since read-only no longer needs opting into.

## Verification

- `tests/test_default_posture.py` asserts the shipped posture per backend,
  the args-beats-built-in-default precedence, and that a user options table
  does not drop the built-in posture; suite green (868 tests) plus Ruff.
- `agent_collab_describe_options`/settings previews resolve the new defaults
  (checked via `normalize_options` on built-in-derived agents for all eight
  backends).
