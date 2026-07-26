# Design the common Bubblewrap workspace sandbox

> **Continuation status (2026-07-26):** Complete Round 11 feedback was
> retrieved from agent-collab session `daemon-06219f667ad44f0e`. Its one
> unresolved Medium finding is reconciled below by the normative Git protection
> record, coverage-mount normalization, ordering, and establishment-proof
> contract. Four additional design-review loops converged in session
> `daemon-0173bb7e678046d9`: both independent reviewers reported no confirmed
> High/Medium findings. Stage 1 production implementation review has used
> all six permitted parallel sessions. The latest was
> `daemon-675a2f82d8de4bc7`; its two confirmed findings are fixed and locally
> verified. There are no unresolved confirmed findings or disagreements.
> Formal review convergence is not claimed because the sixth-session diff
> changed after adjudication and the explicit limit forbids a seventh session.

**Status:** Stage 1 implemented and locally verified; six-session
production-review limit reached without formal post-fix convergence.

**Created:** 2026-07-25.

**Issue:** [#43](https://github.com/lauriparviainen/agent_collab/issues/43)

## Final review reconciliation (2026-07-26)

Round 11 session `daemon-06219f667ad44f0e` used the configured parallel
`grok-gemini-review` workflow. Gemini reported no High/Medium findings. Grok
reported one Medium: the document required every logical session-root Git path
to receive a specific post-writable bind while the argv and proof described
only external binds, leaving ordinary worktrees, duplicates, and bare
workspace-equal roots without one deterministic mount plan. That finding was
confirmed and is resolved by the logical-record/coverage-operation contract in
the authoritative path rules.

Before selecting the resolution, independent consultation session
`daemon-12e09317356e4015` used `claude_cli`, model `fable`, high effort, in the
`solo` workflow. It recommended retaining every logical Git role/provenance
while mapping roots onto the smallest effective coverage set, rather than
emitting mounts that workspace-last would shadow. Its requested writable-child
rejection, effective-topmost proof, in-namespace logical-root identity check,
external-above-workspace decision, and recursive-read-only compatibility
fixture were adopted.

All four additional loops used one `interactive=false` parallel session so
both members inspected the same frozen diff independently:

- `xai_cli`: Grok 4.5, high reasoning, `permission_mode=bypassPermissions`,
  provider sandbox `read-only`;
- `antigravity_cli`: Gemini 3.1 Pro High, mode `plan`; and
- workflow: `grok-gemini-review`.

| Loop | Session and frozen diff | Findings and adjudication |
| --- | --- | --- |
| 1 | `daemon-299dd23842564385`; `51e4101aa53c76e8a493da9081a1c49d3cda3e0e489bc886fe5c2eac88bdf504` | Gemini: none. Grok: High, logical descendant Git roots still formed separate alias-containment pairs and could false-reject an allowed anchor below writable state. Confirmed; containment authority changed to workspace/external coverage sides while logical roots retain identity/inode participation. |
| 2 | `daemon-1d1cc98674c54aa6`; `7bf564f746c7ffb4f6447f6f7f08a33327ab20ca435854ca2a12eb88b087476e` | Gemini: none. Grok: High, workspace absorption said “below” without requiring a component boundary, risking sibling `workspace.git` misclassification. Confirmed; every normalization and overlap relation now states component-boundary semantics and lexical-prefix siblings are tested. |
| 3 | `daemon-c3940dd8532a4f4b`; `0b7c1e6282ff1c6f98d674939f184606eb19e088b0c6b4190d61758d2de2b799` | Gemini: none. Grok: three Medium findings: per-anchor pruning false-rejected hardlinks between regions that both become protected; generic workspace overlap still lacked component-boundary wording; explicit mutation/inode guarantees named only workspace. All confirmed; the audit now prunes the protected union, generic overlap is component-boundary based, and guarantees/tests include external Git coverage. |
| 4 | `daemon-0173bb7e678046d9`; `ccabea43763224e007742378727340ffe53fa7c93a5ab67b41ae4f1face5107b` | Both reviewers: no High/Medium findings. All prior closeouts were independently rechecked. Convergence achieved; loop 5 was not run. |

The earlier one-reviewer findings were disagreements in the review sense:
Gemini reported a clean diff while Grok reported them. Local reproduction
confirmed each concrete scenario, so none was overridden. The final loop is
agreement: no confirmed High/Medium finding or unresolved disagreement
remains. Production implementation and its acceptance fixtures remain
intentionally pending in that historical design-review record rather than
review findings; the implementation record below supersedes that delivery
status.

## Stage 1 implementation review reconciliation (2026-07-26)

Implementation loop 1 used one `interactive=false`
`grok-gemini-review` session over the same frozen working-tree diff:

- session: `daemon-1d0409a4dc1b4b8f`;
- frozen diff digest:
  `0f20f0ab52fd2b70a45ea77da08200fdbd505a410ce880c645a697f290b71890`;
- `xai_cli`: Grok 4.5, high reasoning,
  `permission_mode=bypassPermissions`, provider sandbox `read-only`; and
- `antigravity_cli`: Claude Opus 4.6 Thinking, mode `plan`.

The reviewers disagreed; every claim was checked against the cited code and
the authoritative design:

| Reviewer finding | Adjudication |
| --- | --- |
| Grok High: alias auditing occurred at session-plan time but not immediately before each preflight/provider launch, permitting a host hard link planted after planning to bridge writable state into a protected inode | Confirmed. Audit limits are now retained in the immutable plan and the bounded host audit runs through `asyncio.to_thread` before every `_launch`. A real Bubblewrap conditional fixture plants the link after plan resolution and requires rejection before provider execution. |
| Grok Medium: establishment proved logical Git identities but did not prove the retained logical-root-to-coverage-operation mapping | Confirmed. Establishment now validates workspace-last ordering, exact coverage origins/roles/logical destinations, one anchor per logical root, and writable-ancestor narrowing before ACK, then checks every logical identity through the pinned namespace root. |
| Grok Medium: the alias walk used pathname `stat`/`scandir` rather than pinned directory descriptors | Confirmed. The walk now opens the absolute root component-by-component with `O_NOFOLLOW`, retains a pinned root descriptor, reopens queued descendants relative to it, compares pinned identities, and opens each observed child without following a final symlink. |
| Antigravity High: synchronous Bubblewrap discovery could block the daemon event loop for up to the probe timeout on every turn | Confirmed with severity reduced to Medium: launch remained fail-closed, but unrelated sessions could stall. Runtime discovery now runs in `asyncio.to_thread`; daemon start preflight already ran on its preparation worker. |
| Antigravity Medium: changing the shared absolute-path-list parser from canonical to lexical paths regressed symlink-based `[workdir].restrict_workdir_roots` | Confirmed. Workdir roots again use canonical resolution, while new sandbox operator lists use a dedicated lexical parser so launch-time no-symlink validation retains evidence. |
| Antigravity Medium: sandbox launch ignored `SubprocessRunner.env` | Rejected. Both the runner and immutable plan derive their environment from the same `agent.env`; adapter-owned changes are applied by deterministic Bubblewrap `--setenv`/`--unsetenv` operations. |
| Antigravity Medium: `proof_child.close()` could run twice | Rejected. Python socket close is idempotent, the descriptor is not reused through the closed socket object, and cleanup intentionally closes every partially initialized resource. |

Loop 1 therefore produced five confirmed fixes and two rejected claims. A
fresh frozen diff will be reviewed in loop 2 after verification.

Implementation loop 2 used the same reviewer configuration in session
`daemon-0f48651bd6de4ed3` over frozen diff
`73265522ada9557d6b365ad29345a2716075a9cb06a1e3d1acf56fd8ca73e9e5`.
Antigravity completed its turn but emitted only inspection-progress messages,
not a final qualifying finding report. Grok rechecked all five loop 1 fixes
successfully and reported two independent findings:

| Reviewer finding | Adjudication |
| --- | --- |
| Grok High: mount alias comparison inspected nested mounts only below writable roots, not nested writable aliases below protected workspace/Git coverage | Confirmed. Alias comparison now builds the complete enclosing/nested underlying-identity set on both protected and writable sides and compares their cross product. The exact external-Git-below-writable narrowing relation remains allowed; a nested writable bind below workspace is rejected by a hermetic mountinfo fixture. |
| Grok Medium: provider/operator writable roots were validated only during plan resolution; a path replaced or made unsafe before a later launch could still be mounted | Confirmed. Normalized operations now retain their pinned root identity. Every launch audit repeats component no-symlink, identity, daemon ownership, and group/world-writable checks for declared writable roots before mount construction. |

A local normative recheck also found that compatibility-preserving command
events for `none` still carried the full prompt-bearing `argv`, contrary to the
common launcher logging contract. Execution remains byte-for-byte unchanged,
but command and dry-run events now expose only the prompt-free command preview;
regression tests assert that `argv` and the task prompt are absent.

Loop 2 therefore produced two confirmed reviewer fixes plus one local contract
fix. Because one reviewer did not produce a final report and the diff changed,
convergence was not claimed; loop 3 follows after verification.

Implementation loop 3 reviewed frozen diff
`f9e1b5abb484d698bdfb570f6a6d6f91dd1095e87457fa765d4280715e6b414d`.
The configured parallel session was `daemon-b47c0dc7c59546bb`. Grok 4.5
completed its full review and reported no High/Medium findings, including an
explicit recheck of every loop 1 and loop 2 closeout. The requested
Antigravity Claude Opus 4.6 Thinking turn failed before inspection because the
provider reported `Individual quota reached`.

A supplementary Antigravity Gemini 3.1 Pro High read-only solo session,
`daemon-365adc19b4de4015`, inspected the same frozen digest and independently
reported `No High/Medium findings`. That solo retry did not follow the required
normal-parallel fallback procedure, so it is recorded as supplementary
evidence only and is not used to claim convergence. Loop 3 consumed the third
of six permitted parallel sessions. The exact sanitized Opus failure selects
Gemini 3.1 Pro High, mode `plan`, for every subsequent normal
`grok-gemini-review` loop; loop 4 follows after the complete verification gate.

Implementation loop 4 used normal `interactive=false` parallel session
`daemon-70e53b5299ff49f0`, the fourth of six permitted sessions, over the
59-file frozen diff
`ac9f1b386a6af4f83c2d21c1ce3b8f4172061c1ff3691666b95b9b1528131c52`.
The effective members were Grok 4.5 with high reasoning,
`permission_mode=bypassPermissions`, and provider sandbox `read-only`, plus
the required quota fallback Gemini 3.1 Pro High in mode `plan`. Gemini reported
no High/Medium findings. Grok rechecked the previous closeouts and reported one
independent High finding:

| Reviewer finding | Adjudication |
| --- | --- |
| A launch-time `SandboxFailure` returned `TurnOutcome("failed", exc.code)`, but the strict outcome allowlist did not contain any `outer_sandbox_*` codes. Constructing the outcome therefore raised `ValueError`, and the referee reduced the escaped exception to the incorrect `provider_transport_failed` code. | Confirmed directly: `TurnOutcome("failed", "outer_sandbox_hardlink_alias")` raised before the fix, while the launch remained fail-closed. Every current outer-sandbox failure code is now explicitly canonical with a non-sensitive message. A focused runner test injects a launch-time hard-link rejection and proves that the exact stable code reaches the turn outcome without provider execution; an outcome test constructs every registered sandbox code. |

Loop 4 therefore produced one confirmed reporting fix. The focused outcome and
runner suites pass. Because the diff changed, convergence is not claimed; loop
5 follows after proportionate verification.

Implementation loop 5 used normal `interactive=false` parallel session
`daemon-9a99d5a519514dc9`, the fifth of six permitted sessions, over the
62-file frozen diff
`6257fac24a92d6fb8d98c68685a72b571d54ed34202e0cfe324e7a83d5dec0df`.
The effective members and options were unchanged from loop 4. Gemini rechecked
all prior closeouts and reported no High/Medium findings. Grok confirmed the
loop 4 launch-time fix, then reported two independent Medium findings:

| Reviewer finding | Adjudication |
| --- | --- |
| A post-establishment `SandboxFailure` from `SupervisedProcess.wait()` was caught as a generic stream/parser exception and reduced to `provider_output_invalid`; cleanup then waited on the same failed completion task again. | Confirmed. The runner now preserves a post-ACK sandbox code separately from provider evidence, emits the sanitized sandbox failure event, records the reaped exit code when available, gives the boundary failure precedence over partial or terminal provider output, and retrieves the owned completion task during cleanup. A focused fake supervised process proves `outer_sandbox_status_contradiction` survives even after a provider-completed marker. |
| Scratch allocation and absolute executable resolution could raise raw `FileNotFoundError`, which the runner's ordinary unsandboxed missing-command handler mislabeled `provider_transport_failed`. | Confirmed. Private scratch creation, permissions, and subtree setup now clean up and reduce `OSError` to `outer_sandbox_scratch_anchor_invalid`; absolute/located executable resolution reduces filesystem and symlink-resolution failures to `outer_sandbox_inner_command_invalid`. The public launch boundary also converts remaining launch `OSError` failures to a stable sandbox bootstrap code. Focused tests cover a removed scratch anchor and missing absolute provider path. |

Loop 5 therefore produced two confirmed reporting/diagnostic fixes. The
focused sandbox, outcome, and runner suites pass. Because the diff changed,
convergence is not claimed; the sixth and final permitted parallel loop follows
after complete verification.

Implementation loop 6 used normal `interactive=false` parallel session
`daemon-675a2f82d8de4bc7`, the sixth and final permitted session, over the
62-file frozen diff
`320a6a03625bb364a9cecb3965d307c487cf1f6affaf88366c6650d886e99552`.
The effective members and options were unchanged from loops 4 and 5. Gemini
reported no High/Medium findings. Grok confirmed every loop 5 closeout, then
reported two independent Medium findings:

| Reviewer finding | Adjudication |
| --- | --- |
| `asyncio.wait_for` establishment timeout escaped as a non-sandbox exception, becoming `provider_transport_failed` on a turn or an unstructured start failure. | Confirmed. Establishment timeouts are now reduced to `outer_sandbox_bootstrap_failed` during the proof phase, and the public launch boundary defensively performs the same reduction. A focused injected-timeout test pins the stable code and phase. |
| Start preflight reused the session plan with `true` but omitted the normative private nested-bind fixture that version-gates recursive read-only behavior. | Confirmed against the availability-control contract. Preflight now runs a credential-free private Bubblewrap control with a writable parent bind, a separately writable nested bind mount, and a final read-only bind over the parent. Both direct and nested writes must fail; exit/host-marker contradictions use the distinct `outer_sandbox_recursive_read_only_unavailable` code. A hermetic non-recursive failure fixture and the real conditional Bubblewrap preflight test cover the gate. Direct Linux evidence with the production argv shape passed before adoption. |

Loop 6 therefore produced two confirmed fixes. The reviewers disagreed only in
the advisory-report sense: Gemini was clean, while Grok reported the two
findings; direct code-path reproduction and the missing normative fixture
confirmed both, so neither is an unresolved disagreement. The focused suites
and real Bubblewrap control pass. The six-session limit is reached and no
seventh review will run. Because these final fixes necessarily changed the
reviewed diff, formal convergence is not claimed; the handoff records
limit-reached with no unresolved confirmed finding, followed by the complete
final verification gate.

## Stage 1 implementation handoff (2026-07-26)

Stage 1 is implemented on branch `design/bubblewrap-implementation`. It adds
the backend-neutral policy/spec/plan/launcher/supervision seam, Linux
Bubblewrap enforcement, immutable path and Git coverage normalization,
fail-before-provider bootstrap proof, per-launch alias and writable-root
revalidation, typed configuration/API/settings/CLI/TUI reporting, stable
outer-sandbox outcomes, and conditional real-namespace coverage.

`codex_cli` is the only Stage 1 backend that advertises
`direct_process` read-only support. Its adapter declares the complete effective
`CODEX_HOME` as persistent writable state and applies the composite native
approval/sandbox bypass only inside the proven outer boundary. The other seven
real CLI/SDK backends remain explicitly unsupported for outer read-only and
reject that policy before session creation; their later backend stages are not
silently emulated or weakened. Explicit `sandbox = "none"` remains the visible
rollback and does not itself relax provider-native controls.

Final local verification after the sixth review and its fixes:

- `./agent_collab_dev.sh test`: 1,238 tests passed, one expected skip;
- `./agent_collab_dev.sh bubblewrap-test`: four real Bubblewrap tests passed,
  including recursive read-only preflight, workspace/descendant denial,
  writable state/scratch, hard-link re-audit, termination, and cleanup;
- `./agent_collab_dev.sh build --check`: effective config and generated daemon
  API artifacts verified current;
- `git diff --check`: passed; and
- GitHub issue #43 was fetched read-only and remained open.

The paid, credentialed Codex acceptance in
`integration_tests/backends/codex_cli/test_live.py` was not run because this
implementation request did not authorize credentialed provider calls. It
remains the supported-but-pending acceptance: an operator must supply a
dedicated complete `CODEX_HOME` through
`AGENT_COLLAB_IT_CODEX_SANDBOX_STATE` and explicitly authorize
`./agent_collab_dev.sh integration-test codex_cli --strict`. The test requires
a real tool turn, denied workspace write, successful persistent state write,
and guarded marker cleanup.

The Stage 1 guarantee is filesystem integrity for the normalized workspace and
session-root Git coverage inside the contained process tree. The documented
MVP limitations remain: visible host files and credentials are readable;
network, remote tools/services, keyrings, IPC services, and delegated writes
are not isolated; declared provider state and workspace symlink targets into
that state remain writable; nested-repository external Git metadata requires
selecting that repository as the session root; resource/seccomp/DoS controls
are absent; and macOS, Windows, unsupported filesystems, and unimplemented
backend adapters fail closed rather than receive weaker enforcement.

Six of six permitted production review sessions were used. The final two
Medium findings were confirmed, fixed, and covered locally, leaving no
unresolved confirmed finding or disagreement. Formal convergence is not
claimed because no post-fix seventh session was permitted.

## Purpose

Design a common outer sandbox that makes headless agent execution usable while
protecting the supplied repository independently of prompts, model behavior,
approval policy, and provider-native sandbox implementations. Antigravity's
default headless review posture motivated issue #43, but the production
boundary and typed adapter contract cover every configured CLI and SDK backend.

The first enforcement engine is Linux Bubblewrap (`bwrap`). The design leaves a
clean engine seam for other operating systems, but does not claim
cross-platform OS isolation before those engines exist. A separately reported,
versioned `no_local_effects` capability is not an OS-isolation claim.

## Context

Issue #29 made provider-level read-only controls explicit in the shipped
configuration. In particular, `antigravity_cli` now defaults to `mode = "plan"`
and exposes Antigravity's boolean `--sandbox` flag. Those controls are useful
defense in depth, but neither is a proven filesystem boundary:

- `mode = "plan"` is behavioral planning and permission policy. In headless
  print mode it also denies unallowlisted shell commands because no approval UI
  is available, including harmless commands such as `git status --short`.
- The probe recorded in issue #43 found the workdir writable when `agy` ran
  with both `--sandbox` and `--dangerously-skip-permissions`.
- `--dangerously-skip-permissions` makes headless command use possible, but
  makes unintended writes possible too unless a separate boundary blocks them.

The current launch path has no outer sandbox abstraction:

1. `Referee` resolves the session workdir and passes it to the selected runner.
2. `AntigravityCliBackend.create_runner` uses the shared
   `create_cli_runner`.
3. `SubprocessRunner.run_turn` resolves the effective run directory, builds
   the final provider argv, and calls `asyncio.create_subprocess_exec` with
   that directory as `cwd`.
4. The Antigravity command builder also passes the resolved run directory via
   `--add-dir`.

This makes the shared subprocess launch boundary the likely enforcement seam.
Antigravity motivates the issue, but the default policy applies to every CLI
backend that declares the state and capabilities needed to establish it.

## Initial Codex feasibility probe (2026-07-25)

The tracked `probes/bubblewrap_codex` harness now exercises the proposed launch
shape with Bubblewrap 0.6.3 and Codex CLI 0.145.0. It first supports a free
structural control, then a credentialed Codex turn with Codex's own approvals
and sandbox explicitly bypassed.

Observed in both the structural control and the real Codex turn:

- one temporary workspace path containing spaces remained identical as the
  host path, Bubblewrap `--chdir`, Codex `-C` value, filesystem-policy prompt
  path, and shell-reported `pwd`;
- tracked input was readable;
- workspace writes and writes to a separate protected host directory failed;
- writes to the private staged home and scratch directory succeeded; and
- host-side marker checks confirmed the filesystem results independently of
  the model's prose.

For the credentialed turn, the probe copied only the existing Codex
`auth.json` into a mode-0700 temporary home, invoked Codex with
`--ignore-user-config --ephemeral`, and authenticated successfully. The real
Codex home was never mounted writable, and the staged home was deleted when
the probe exited.

The follow-up whole-home read-only experiment produced a negative result on
Codex CLI 0.145.0. The structural control still blocked workspace, protected
host, and home writes while allowing scratch writes. A real Codex invocation,
however, failed before the model turn:

- failure to create PATH aliases was warning-only;
- opening `state_5.sqlite` attempted a write and failed with SQLite read-only
  error code 8; and
- initialization then stopped because the in-process app-server client
  encountered `EROFS`.

This occurred despite `--ignore-user-config --ephemeral` and writable
`TMPDIR`/XDG cache, config, data, and state paths. Codex therefore cannot use a
completely read-only `CODEX_HOME` in this version. The MVP decision below
therefore mounts the effective complete `CODEX_HOME` persistently writable.
A writable ephemeral state root with a read-only bind of the real `auth.json`
remains a possible later hardening experiment, not a prerequisite for the
initial implementation.

This is positive feasibility evidence, not the final Antigravity design. It
does not yet establish Antigravity's required credential/config paths, token
refresh behavior, safe persistent state, environment minimization, or fallback
behavior when Bubblewrap/user namespaces are unavailable.

## Initial Claude feasibility probe (2026-07-25)

The tracked `probes/bubblewrap_claude` harness applies the same outer boundary
to Claude CLI. It preserves the real workspace path, makes the visible root
read-only, mounts the selected Claude state root writable, supplies private
scratch, requests Claude's native sandbox disabled through transient settings,
and uses `--dangerously-skip-permissions` for non-interactive Bash execution.

The credentialed probe passed with Bubblewrap 0.6.3 and Claude Code 2.1.219:

- Claude authenticated from its existing Linux state without copying
  credentials;
- Bash ran without an interactive approval prompt;
- the cwd and prompt used the same real workspace path containing spaces;
- workspace, protected-host, and general-home writes were blocked;
- writes to private scratch and the selected `~/.claude` state root succeeded;
  and
- host-side marker checks independently confirmed the results and cleanup.

The whole-state-read-only comparison authenticated but failed before the
action script. Claude's Bash tool attempted to create
`~/.claude/session-env/<session-id>` and received `EROFS`. The writable
provider-state exception is therefore required for this tested Claude version,
even with session persistence disabled.

The default environment also has legacy `~/.claude.json` state outside the
selected `~/.claude` directory. It remained read-only during the successful
turn, so it was not a required writable exception in this probe. Custom
`CLAUDE_CONFIG_DIR`, managed settings, credential helpers, token refresh, and
future Claude versions still require targeted compatibility coverage.

## CLI feasibility probes (complete 2026-07-25)

Equivalent tracked probes now cover Antigravity CLI (`agy`, backend
`antigravity_cli`) and the configured Grok Build command (backend `xai_cli`).
Both provide writable/read-only credential-free structural controls,
credentialed writable-state turns, whole-state-read-only comparisons, exact
workspace/cwd identity, private scratch, guarded provider-specific real-state
markers, and host-side action evidence.

Together with the existing Claude and Codex probes, the evidence now covers
all four initial subprocess-backed providers. All require their complete
backend-declared state root writable for a usable real turn on the tested
versions. Production implementation remains deliberately separate and must
preserve the common-launcher/backend-adapter boundary settled below.

## Antigravity feasibility probe (2026-07-25)

The tracked `probes/bubblewrap_antigravity` harness tested Bubblewrap 0.6.3
with Antigravity CLI 1.1.7. Antigravity's complete file-state root is
`~/.gemini`: CLI-specific settings, logs, plugins, MCP cache, conversations,
artifacts, and tool helpers live below its `antigravity-cli` subtree, while
shared configuration and legacy sign-in hints also live elsewhere below
`.gemini`.

No provider-specific state-root relocation environment variable was exposed
by the installed CLI or current official documentation. The probe's explicit
state override therefore accepts another `.gemini` path and changes `HOME` to
its parent, matching the CLI's actual tilde-based lookup contract.
Authentication may be found in that tree or in the operating-system keyring;
the latter is accessed through the inherited keyring service rather than a
writable filesystem exception.

The verified permissive native profile was:

- `--dangerously-skip-permissions` for non-interactive approval bypass;
- `--mode accept-edits` to avoid behavioral plan restrictions during the
  enforcement test; and
- documented `--sandbox=false` to disable the provider-native terminal
  sandbox after Bubblewrap was selected as the outer boundary.

Both structural modes passed. The real writable-state turn also passed:
authentication succeeded, the ordinary shell action ran without an approval
prompt, workspace/protected-host/general-home writes were blocked, private
scratch and complete provider-state writes succeeded, and host-side marker
checks plus cleanup passed.

The whole-state-read-only turn authenticated and reached the model but failed
before the action script. Log, crash-report, conversation, MCP-cache, and
artifact writes first received `EROFS`; the decisive command-execution failure
was inability to materialize the CLI's `antigravity-cli/bin/agentapi` helper
below the selected state root. The provider process returned zero after
reporting the tool failure, while the probe correctly returned nonzero because
its host-side action evidence was missing.

Complete writable `~/.gemini` is therefore required for Antigravity 1.1.7.
The successful control needed no writable filesystem state outside that root
and private scratch. Installation-directory update access remained read-only
and was skipped without preventing the turn. Remaining limitations include OS
keyring confidentiality/side effects, custom hooks and MCP servers, managed
settings, alternative keyrings, token refresh, and future CLI state changes.

## Grok feasibility probe (2026-07-25)

The tracked `probes/bubblewrap_grok` harness tested Bubblewrap 0.6.3 with Grok
Build 0.2.111, using the configured `grok` command shape with update checks
disabled and streaming JSON output.

Grok's complete provider-owned state root is `$GROK_HOME`, defaulting to
`~/.grok`. It contains cached authentication, configuration, sessions, search
indexes, skills, plugins, hooks, and custom sandbox profiles. Authentication
may instead come from `XAI_API_KEY`, a model-specific configured environment
key, or an external auth provider; the probe used path/environment presence
checks only and did not inspect credential or configuration contents.

The verified permissive native profile was:

- `--permission-mode bypassPermissions`, matching the `xai_cli` headless
  backend configuration; and
- documented `--sandbox off`, which disables Grok's native Landlock sandbox
  only after Bubblewrap was selected as the outer boundary.

Both structural modes passed. The real writable-state turn also passed:
cached authentication succeeded, Bash ran without an approval prompt,
workspace/protected-host/general-home writes were blocked, private scratch and
complete provider-state writes succeeded, and host-side marker checks plus
cleanup passed.

The whole-state-read-only turn failed before the model or action script.
Startup attempted SQLite WAL/search-index maintenance and then failed to
create the new local session with a read-only-filesystem error and nonzero
exit. Complete writable `$GROK_HOME` is therefore required for Grok Build
0.2.111.

During the successful writable-state control, one warning-only attempt to
create a legacy session folder below the shared system temporary directory was
blocked; the turn and action still completed. No second persistent writable
state root was required. Remaining limitations include managed and project
configuration, custom hooks/MCP/plugins, external authentication providers,
token refresh, managed sandbox-profile pins, the warning-only legacy path, and
future CLI state changes.

### Probe-change verification

Both Python probes compiled successfully. All four structural modes were run
again after formatting and passed. The credentialed writable-state turn passed
for both providers; each whole-state-read-only comparison returned nonzero at
the provider-specific failure stage documented above. No guarded marker or
probe-generated bytecode remained. `git diff --check` passed, and the complete
`./agent_collab_dev.sh test` gate passed 1,196 tests with one skip.

Commands and outcomes:

| Command | Outcome |
| --- | --- |
| `python3 -m py_compile probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py probes/bubblewrap_grok/probe_bubblewrap_grok.py` | Passed |
| `python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py --preflight-only --state-mode writable` | Passed all host assertions |
| `python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py --preflight-only --state-mode read-only` | Passed all host assertions, including blocked state write |
| `python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py --preflight-only --state-mode writable` | Passed all host assertions |
| `python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py --preflight-only --state-mode read-only` | Passed all host assertions, including blocked state write |
| `python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py --state-mode writable --model gemini-3.6-flash-low` | Passed the credentialed turn and all host assertions |
| `python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py --state-mode read-only --model gemini-3.6-flash-low` | Expected negative: authenticated and reached the model, then failed before the action because the provider could not create its command helper in read-only state |
| `python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py --state-mode writable --model grok-4.5` | Passed the credentialed turn and all host assertions |
| `python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py --state-mode read-only --model grok-4.5` | Expected negative: failed before the model/action while creating mutable local session state |
| `git diff --check` | Passed |
| `./agent_collab_dev.sh test` | Passed 1,196 tests with one skip |

## SDK feasibility research (2026-07-25)

The tracked `probes/bubblewrap_antigravity_sdk`,
`probes/bubblewrap_xai_sdk`, `probes/bubblewrap_claude_sdk`, and
`probes/bubblewrap_codex_sdk` harnesses extend the execution-ownership
research to all SDK backends. They distinguish a complete SDK worker placed
inside Bubblewrap from the current production architecture, where SDK runners
live in the daemon.

The explicit support decisions are:

| Backend | Decision | Reason |
| --- | --- | --- |
| `antigravity_sdk` | **feasible with out-of-process worker** | A complete standalone SDK worker, bundled runtime, local action, and action child were contained successfully. The current production runner and runtime instead share the unsandboxed host namespace. |
| `xai_sdk` | **no local tool execution** | The current backend constructs a model-only request with no built-in or custom tools and performs no local provider-state writes or child launches. |
| `claude_sdk` | **feasible with out-of-process worker** | A complete standalone SDK worker, Claude Code runtime, local action, and action child were contained successfully. The current production runner and runtime instead share the unsandboxed host namespace. |
| `codex_sdk` | **feasible with out-of-process worker** | A complete standalone SDK worker, Codex app-server, local action, and action child were contained successfully. The current production runner and app-server instead share the unsandboxed host namespace. |

None of these results implements production sandboxing. In particular,
`antigravity_sdk`, `claude_sdk`, and `codex_sdk` must not advertise outer
`read-only` support until the whole runner is moved across a supervised worker
boundary. The xAI decision must be revisited if that backend later enables
provider built-ins or client-executed function tools.

## Antigravity SDK feasibility probe (2026-07-25)

### Tested versions and production path

The source trace and probe used Bubblewrap 0.6.3 and
`google-antigravity` 0.1.8 in an isolated Python environment with protobuf
7.35.1. The shared all-provider environment was deliberately not modified:
`xai-sdk` 1.17.0 requires protobuf below 7, while the generated Antigravity
0.1.8 code requires protobuf 7.35 or newer despite looser published base
metadata.

The current call path is:

```text
Referee
  -> AntigravitySdkBackend.create_runner
  -> AntigravitySdkRunner / persistent conversation (daemon Python process)
  -> google.antigravity.Agent(LocalAgentConfig)
  -> bundled localharness child (inherits daemon cwd, environment, namespace)
  -> remote Vertex model service
```

The backend passes only the resolved workspace, model, Vertex configuration,
strict continuation fields, and a runner-owned temporary trajectory
`save_dir`. It does not configure custom tools, hooks, triggers, MCP servers,
skills, plugins, or custom subagents. The installed SDK's default
`CapabilitiesConfig` enables its built-in tools and subagents. Its default
policy permits file tools but denies `run_command` in the non-interactive
current backend.

### Tool and side-effect ownership

| Capability | Current production ownership |
| --- | --- |
| Agent construction, SDK policies, and callback dispatch | In the unsandboxed daemon. No custom callbacks are configured today. |
| Model transport | Initiated by the daemon SDK through `localharness`, then executed remotely by Vertex. |
| Built-in file read/write | Executed locally by `localharness` against the configured workspace. |
| Built-in shell/terminal | Owned by `localharness`, but denied by the backend's effective default SDK policy. |
| Shell descendants | Would inherit the harness cwd, environment, and namespace; this production path was not fabricated by weakening the backend policy. |
| Built-in subagent | Enabled in the harness. Any local built-in tools it selects retain harness ownership. |
| Search-web/read-URL/generate-image | Routed through the harness to provider services. Remote effects are outside the local Bubblewrap filesystem claim. |
| Custom Python tools | The public SDK tool runner invokes them in the SDK caller process, using a thread for synchronous callables. The backend exposes none. |
| Hooks, policies, and triggers | The public SDK invokes them in the SDK caller process. The backend supplies no custom hooks or triggers. |
| MCP stdio | Public SDK configuration is serialized to `localharness`, which would start the local MCP process. The backend supplies no MCP configuration, so no unsupported server was invented for the probe. |
| MCP HTTP | The harness would contact the configured remote endpoint. The backend supplies none; a remote endpoint's filesystem is not local. |
| Skills and custom subagents | Public SDK configuration exists, but the backend exposes neither. |

This distinction matters for future changes. Wrapping only the SDK-managed
`localharness` child cannot contain Python tool callbacks or hooks running in
the daemon. Outer support requires the complete SDK runner and all of its
extension points to live in the worker namespace.

### Native controls, state, and authentication

The public SDK exposes capability selection and Python policies rather than a
provider-native OS sandbox. `policy.allow_all()` was used only in the wrapped
standalone feasibility worker, after Bubblewrap was the outer boundary. It is
approval behavior, not containment.

The production backend creates one temporary trajectory `save_dir`, keeps it
across resets for strict continuation, and removes it on final close. The SDK
also supports an `app_data_dir` and otherwise resolves provider app data under
the user's Gemini/Antigravity state. Vertex authentication reads Application
Default Credentials. The probe mounted the ADC file read-only and never read
or printed its contents.

For the standalone control, both trajectory and app data lived below one
private provider-state root. That root alone was mounted writable; general
home remained read-only. This is feasibility evidence for the tested flow,
not a claim that alternative authentication, token refresh, callbacks, MCP,
or future SDK versions require no additional state.

### Wrapped worker and current architecture results

Both credential-free structural modes passed. Their direct action and child
confirmed:

- the exact temporary workspace path containing spaces remained the cwd;
- tracked input was readable;
- workspace, protected-host, and general-home writes were blocked;
- private scratch was writable;
- private provider-state writes matched the selected writable/read-only mode;
- the child inherited the same mount namespace and filesystem result; and
- host-side evidence showed the action ran exactly once.

The real writable-state standalone worker also passed. A permissive public-SDK
policy allowed one `run_command` action. The SDK caller, `localharness`, action,
and action child all shared the Bubblewrap mount namespace and differed from
the host namespace. No writable path outside private provider state and
scratch was needed.

The deliberate current-architecture negative control used the exact
production backend factory. A real built-in `create_file` tool completed, its
marker reached the temporary host workspace, and the harness shared the
caller's host mount namespace. The current in-daemon architecture is therefore
not contained.

The real `--state-mode read-only` standalone comparison failed during
conversation initialization, before the model request or tool action. The
harness could not create its trajectory database below the read-only
`save_dir`. This establishes that mutable private trajectory state is required
for this tested worker shape.

### Cancellation and remaining work

Production cancellation closes the active response before the agent context
exits. SDK disconnect closes its communication tasks and streams, then waits
for, terminates, or kills `localharness`. The referee bounds reset/close and
can retain a slow runner for background reaping. A forced one-second probe
timeout killed the standalone worker process group; the recorded
`localharness` process was confirmed gone. An allowed production shell
descendant was not exercised because the current policy denies shell.

The production design needs a backend-neutral supervised SDK-worker launch
path. An Antigravity adapter must declare:

- outer modes supported by that worker;
- the required private/persistent state-root contract;
- the capability/policy mapping used only after the outer boundary exists;
- sanitized reporting of those controls; and
- compatibility checks for the SDK, bundled runtime, credentials, and
  namespace setup.

Common code must launch and supervise the complete worker without inspecting
the backend id. Antigravity code must not construct generic Bubblewrap mounts.
The worker protocol must preserve event streaming, strict identity, bounded
cancellation, cleanup, and fail-closed startup.

Remaining limitations include readable host files and credentials, inherited
network access, remote provider effects, resource limits, unsupported SDK
extension points, alternative auth/keyrings, token refresh, future runtime
changes, and non-Linux platforms.

Primary provider references:

- <https://github.com/google-antigravity/antigravity-sdk-python>
- <https://antigravity.google/docs/mcp>

## xAI SDK feasibility probe (2026-07-25)

### Tested version and production path

The structural probe used Bubblewrap 0.6.3 and `xai-sdk` 1.17.0. The current
call path is:

```text
Referee
  -> XaiSdkBackend.create_runner
  -> XaiSdkRunner / persistent conversation (daemon Python process)
  -> xai_sdk.AsyncClient gRPC request
  -> remote xAI model service
```

Every turn creates a fresh chat containing one new user prompt,
`store_messages=True`, and the latest remote `previous_response_id` after the
first turn. The backend maps only `model` and `reasoning_effort`. It passes no
tools, discards the workdir for SDK request purposes, maps only assistant
message content, and fails conservatively if an unexpected tool call appears.

### Tool and side-effect ownership

| Capability | Current production ownership |
| --- | --- |
| SDK request and gRPC client | In the unsandboxed daemon process. |
| Model execution and stored continuation | Remote xAI service. |
| File read/write, shell, and child processes | Not exposed. |
| Client custom-function callbacks/tool runner | Not exposed. No caller-side executor runs. |
| Provider built-in web/code tools | Not enabled. If enabled later, they would run in xAI's remote environment and remain outside the local filesystem claim. |
| MCP, hooks, plugins, and subagents | Not exposed. |
| Local state | No provider state root. The API key comes from backend environment; the gRPC runtime uses threads but launches no provider process. |

Official xAI function-calling documentation distinguishes provider-hosted
built-ins from custom functions that the caller must execute. The absence of
both kinds in the current request is therefore an important backend limitation,
not a general claim about the SDK.

### Structural evidence, blocker, and cancellation

The structural probe ran the complete Python worker inside Bubblewrap with
read-only root, workspace, and general home plus private writable scratch. It
constructed the production-mapped SDK request against a non-network fixture
endpoint and passed all assertions:

- worker mount namespace differed from the host;
- cwd matched the exact temporary workspace containing spaces;
- tracked input was readable;
- production mapping contained only `model` and `reasoning_effort`;
- the request contained zero tools;
- Python audit instrumentation observed no subprocess launch; and
- workspace, protected-host, and general-home markers remained absent.

No writable provider-state exception was required. A credentialed model-only
comparison was blocked because `XAI_API_KEY` was unavailable. The probe reports
that exact sanitized blocker without inspecting configuration contents.
Source tracing, exact request construction, and the hermetic backend suite
still establish that there is no configured local tool path to exercise.

On cancellation, the conversation shields an in-flight SDK sample until it
settles so response identity is not lost, then reset/close deletes stored
remote completions best-effort and closes the gRPC client. The referee can
bound and later reap a slow runner. There is no local SDK or tool subprocess
tree to terminate. Bubblewrap would constrain the Python client but neither
contains nor needs to contain xAI's remote model or future provider-hosted
tool filesystem.

The current decision is **no local tool execution**. If the backend later
enables client custom functions, those callbacks require a contained executor
before outer `read-only` can be advertised. Provider built-ins must be
reported as remote, and any new local caches, credential helpers, processes,
hooks, or MCP integrations require a fresh state and ownership audit.

Primary provider references:

- <https://github.com/xai-org/xai-sdk-python>
- <https://docs.x.ai/developers/tools/function-calling>

## Claude SDK feasibility probe (2026-07-25)

### Tested versions and production path

The source trace and probe used Bubblewrap 0.6.3,
`claude-agent-sdk` 0.2.126, and its bundled Claude Code 2.1.218. The current
call path is:

```text
Referee
  -> ClaudeSdkBackend.create_runner
  -> ClaudeSdkRunner / persistent ClaudeSdkConversation (daemon Python)
  -> ClaudeSDKClient
  -> bundled Claude Code subprocess (inherits configured cwd/environment)
  -> local tool processes and remote Anthropic model service
```

The SDK transport launches Claude Code locally with the resolved workdir as
cwd. The backend maps model, `permission_mode`, and optional
effort/token settings. It supplies the Claude Code system/tool presets and
`setting_sources=[]`; it does not configure hooks, SDK MCP handlers, plugins,
or custom agents. The generated Claude Code command is not strict-MCP, so
ambient MCP discovery remains relevant even though the backend supplies no
explicit MCP configuration.

The official Claude Agent SDK documentation distinguishes this local Agent
SDK execution from provider-managed agents: Agent SDK built-in tools operate
from the caller's environment and filesystem. Provider/server tools execute
remotely and are outside the local filesystem claim.

### Tool and side-effect ownership

| Capability | Current production ownership |
| --- | --- |
| SDK client, event translation, policies, and optional callbacks | In the unsandboxed daemon. No custom callbacks or hooks are supplied today. |
| Model transport | Local Claude Code runtime to the remote Anthropic service. |
| Built-in file and shell tools | Executed locally by the Claude Code child against the configured workspace. |
| Shell descendants | Children of Claude Code; they inherit its cwd, environment, and mount namespace. |
| Built-in Agent/Skill selection | Owned by Claude Code. Local built-in tool effects retain runtime ownership. The backend supplies no custom agents or plugins. |
| SDK `can_use_tool`, hook callbacks, and in-process SDK MCP handlers | Public extension points execute in the Python SDK caller. The backend supplies none. |
| External stdio MCP | Would be started by Claude Code. The backend supplies no explicit server, so no unsupported path was fabricated for the probe. |
| Server tools such as provider web tools | Provider-hosted. Their remote filesystem and side effects are outside the local Bubblewrap claim. |

Wrapping Claude Code alone would not contain a future in-process SDK callback,
hook, or SDK MCP handler. The complete runner must move into the supervised
worker namespace.

### Native controls, state, and authentication

`permission_mode` controls approval behavior. Claude's native sandbox defaults
to disabled in this backend. The standalone feasibility worker used
`bypassPermissions` only after Bubblewrap established the outer boundary.
Neither setting is treated as proof of OS containment.

The tested runtime required the complete `CLAUDE_CONFIG_DIR` to be writable.
It contains authentication plus mutable session/environment data. Filesystem
setting sources are disabled by the production options, which prevents loading
the ordinary filesystem settings surface through that SDK option, but this is
not a minimized state contract and does not make the entire provider root
immutable.

The live probe inherited credentials from the selected state root or supported
environment without reading or printing their contents. General `HOME`
remained read-only; only the complete selected provider root and private
scratch were writable.

### Wrapped worker and current architecture results

Both credential-free structural modes passed. They exercised the exact
production option construction and confirmed:

- the workdir was mapped exactly and the Claude Code system/tool preset was
  retained;
- filesystem setting sources were empty;
- no SDK MCP server, hook, plugin, or custom agent was configured;
- the current CLI command did not enable strict MCP;
- the exact temporary workspace path containing spaces remained the cwd;
- tracked input was readable;
- workspace, protected-host, and general-home writes were blocked;
- provider-state writes matched the writable/read-only mode;
- private scratch was writable; and
- the action child inherited the worker mount namespace and filesystem result.

The credentialed writable-state standalone worker passed a real Bash turn.
The action ran exactly once; the SDK caller, Claude Code runtime, action, and
action child shared the Bubblewrap namespace and differed from the host.

The deliberate current-architecture control used the exact production backend
factory. The same action and child reached every controlled host marker,
including workspace, protected-host, general-home, and provider-state markers,
while Claude Code shared the caller's host mount namespace. The current
in-daemon architecture is therefore not contained.

The credentialed read-only-state comparison reached a Bash command event but
failed before the action script ran. Sanitized runtime errors included
read-only-filesystem/`EROFS` and session-environment categories. Workspace,
protected-host, general-home, provider-state, and action markers all remained
absent. This establishes that the tested Claude flow requires mutable private
runtime state even though model/tool planning can begin before that failure is
reported.

### Cancellation and remaining work

Production reset/close asks the SDK to close the active stream and disconnect.
Its transport closes communication tasks and attempts graceful Claude Code
termination followed by escalation. Raw cancellation and slow teardown still
have edge cases; the referee bounds close and may retain a runner for
background reaping.

A forced one-second probe timeout killed the standalone worker process group
and confirmed that the recorded Claude Code descendant was gone. Production
support needs the common worker supervisor to own this forceful tree cleanup,
because cancelling only the in-daemon conversation does not establish a
fail-closed outer boundary.

Remaining limitations include ambient MCP configuration, readable host files
and credentials, inherited network access, remote provider effects, custom
callbacks/hooks/SDK MCP servers, alternative authentication and token refresh,
resource limits, future SDK/runtime changes, and non-Linux platforms.

Primary provider references:

- <https://platform.claude.com/docs/en/agent-sdk/overview>
- <https://platform.claude.com/docs/en/managed-agents/migration>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview>
- <https://github.com/anthropics/claude-agent-sdk-python>

## Codex SDK feasibility probe (2026-07-25)

### Tested versions and production path

The source trace and probe used Bubblewrap 0.6.3, `openai-codex` 0.144.4,
`openai-codex-cli-bin` 0.144.4, and the selected Codex CLI 0.145.0. The SDK
ships a pinned runtime, while the backend prefers its explicitly configured
agent command. The current call path is:

```text
Referee
  -> CodexSdkBackend.create_runner
  -> CodexSdkRunner / persistent CodexSdkConversation (daemon Python)
  -> openai_codex.AsyncCodex
  -> codex app-server --listen stdio:// subprocess
  -> local tool processes and remote OpenAI model service
```

The SDK launches app-server with `subprocess.Popen`; stdout and stderr are
consumed by daemon threads. The backend maps the resolved workdir, model,
provider-native sandbox preset, and optional reasoning effort. It does not
supply per-thread runtime configuration, so app-server can still read ambient
`CODEX_HOME` configuration.

### Tool and side-effect ownership

| Capability | Current production ownership |
| --- | --- |
| SDK client, event translation, and reader/drainer threads | In the unsandboxed daemon. |
| Model transport | Local app-server to the remote OpenAI service. |
| Built-in file and shell tools | Executed locally by app-server against the configured workspace. |
| Shell descendants | Children of app-server; they inherit its cwd, environment, and mount namespace. |
| MCP, hooks, skills, plugins, and subagents | Runtime-owned when enabled through ambient Codex configuration. The backend does not add or suppress them per thread. |
| Caller-side custom tool execution | The high-level Python SDK has approval handling but no backend-configured client custom-tool executor. |
| Provider web tools | Provider-hosted. Their remote filesystem and effects are outside the local Bubblewrap claim. |
| Runtime threads and state | Reader/drainer threads are in daemon Python; app-server reads and mutates `CODEX_HOME` state including config, auth, sessions, skills/package metadata, and SQLite data. |

Wrapping app-server alone would leave the SDK caller/threads in the daemon and
would not create a reliable forced-cancellation boundary. The complete runner
must move into the supervised worker namespace.

### Native controls, state, and authentication

Codex sandbox presets govern commands spawned by app-server, and descendants
inherit those provider-native restrictions. Approval policy is separate. The
standalone feasibility worker used `danger-full-access` only after Bubblewrap
established the outer boundary; the SDK's default `auto_review` approval mode
remained in effect. Neither provider-native setting is treated as proof of
outer containment.

The tested runtime required the complete `CODEX_HOME` to be writable. That
root includes authentication, configuration, sessions, skills/package
metadata, and SQLite state; a separately configured SQLite home would also
need an explicit contract. The live probe inherited credentials from the
selected state root or supported environment without reading or printing
their contents. General `HOME` remained read-only; only the complete selected
provider root and private scratch were writable.

### Wrapped worker and current architecture results

Both credential-free structural modes passed. They exercised the exact
production factory/mapping and confirmed:

- exact workspace cwd and configured runtime selection;
- model, `danger-full-access`, and low reasoning-effort mapping;
- no extra per-thread options beyond cwd/model/sandbox;
- the SDK default `auto_review` approval mode;
- the exact temporary workspace path containing spaces remained the cwd;
- tracked input was readable;
- workspace, protected-host, and general-home writes were blocked;
- provider-state writes matched the writable/read-only mode;
- private scratch was writable; and
- the action child inherited the worker mount namespace and filesystem result.

The credentialed writable-state standalone worker passed a real shell turn.
The action ran exactly once; the SDK caller, app-server, action, and action
child shared the Bubblewrap namespace and differed from the host.

The deliberate current-architecture control used the exact production backend
factory. The same action and child reached every controlled host marker,
including workspace, protected-host, general-home, and provider-state markers,
while app-server shared the caller's host mount namespace. The current
in-daemon architecture is therefore not contained.

The credentialed read-only-state comparison started app-server and then failed
before shell dispatch with a sanitized read-only-filesystem category.
Workspace, protected-host, general-home, provider-state, scratch-action, and
action markers all remained absent. This establishes that the tested app-server
startup/turn path requires mutable private runtime state.

### Cancellation and remaining work

Production turn cancellation shields the in-flight provider run until it
settles and does not issue an app-server turn interrupt. The referee can adopt
the still-running task for background reaping, while reset/close may remain
blocked on the conversation lock. SDK client close terminates and waits for
app-server, then escalates to kill, but does not perform a second wait after
that kill.

A forced one-second probe timeout killed the standalone worker process group
and confirmed that the recorded app-server descendant was gone. Production
support needs the common worker supervisor to own this forceful tree cleanup
and preserve event streaming, continuation identity, bounded shutdown, and
fail-closed startup.

Remaining limitations include ambient MCP/hooks/skills/plugins, readable host
files and credentials, inherited network access, remote provider effects,
external helpers, alternative authentication and token refresh, resource
limits, future SDK/runtime changes, and non-Linux platforms.

Primary provider references:

- <https://developers.openai.com/codex/sdk/>
- <https://developers.openai.com/codex/app-server/>
- <https://developers.openai.com/codex/security/>
- <https://github.com/openai/codex>

## SDK probe-change verification

All four probes compiled and passed Ruff lint/format checks. All meaningful
structural modes passed after formatting. The authorized Antigravity, Claude,
and Codex comparisons passed their positive and deliberate
current-architecture controls; the state and timeout negatives returned
nonzero at the expected stages. The xAI credentialed comparison was not
started because its API key was unavailable.

| Command | Outcome |
| --- | --- |
| `python3 -m py_compile probes/bubblewrap_antigravity_sdk/probe_bubblewrap_antigravity_sdk.py probes/bubblewrap_xai_sdk/probe_bubblewrap_xai_sdk.py` | Passed |
| `python3 -m py_compile probes/bubblewrap_claude_sdk/probe_bubblewrap_claude_sdk.py probes/bubblewrap_codex_sdk/probe_bubblewrap_codex_sdk.py` | Passed |
| Antigravity SDK `--preflight-only --state-mode writable` | Passed all direct, child, state, and host assertions |
| Antigravity SDK `--preflight-only --state-mode read-only` | Passed all assertions, including blocked provider-state writes |
| Antigravity SDK wrapped worker, writable state, `gemini-2.5-flash` | Passed the credentialed tool turn; caller, harness, action, and child were contained |
| Antigravity SDK current-architecture control, `gemini-2.5-flash` | Passed the deliberate negative proof: the file write reached the host and the harness shared the host namespace |
| Antigravity SDK wrapped worker, read-only state | Expected nonzero: trajectory database creation failed before model/tool dispatch |
| Antigravity SDK wrapped worker, one-second timeout | Expected nonzero: process group killed and recorded harness descendant reaped |
| xAI SDK `--preflight-only` | Passed the model-only request, no-child, namespace, and host assertions |
| xAI SDK credentialed comparison | Blocked before launch: `XAI_API_KEY` unavailable |
| Claude SDK `--preflight-only --state-mode writable` | Passed all production-mapping, direct, child, state, namespace, and host assertions |
| Claude SDK `--preflight-only --state-mode read-only` | Passed all assertions, including blocked provider-state writes |
| Claude SDK wrapped worker, writable state, `sonnet` | Passed the credentialed Bash turn; caller, Claude Code, action, and child were contained |
| Claude SDK current-architecture control, `sonnet` | Passed the deliberate negative proof: all controlled writes reached the host and Claude Code shared the host namespace |
| Claude SDK wrapped worker, read-only state | Expected nonzero: Bash event observed, then session-state `EROFS` prevented the action |
| Claude SDK wrapped worker, one-second timeout | Expected nonzero: process group killed and recorded Claude Code descendant reaped |
| Codex SDK `--preflight-only --state-mode writable` | Passed all production-mapping, direct, child, state, namespace, and host assertions |
| Codex SDK `--preflight-only --state-mode read-only` | Passed all assertions, including blocked provider-state writes |
| Codex SDK wrapped worker, writable state, `gpt-5.6-luna` | Passed the credentialed shell turn; caller, app-server, action, and child were contained |
| Codex SDK current-architecture control, `gpt-5.6-luna` | Passed the deliberate negative proof: all controlled writes reached the host and app-server shared the host namespace |
| Codex SDK wrapped worker, read-only state | Expected nonzero: app-server started, then read-only runtime state prevented shell dispatch |
| Codex SDK wrapped worker, one-second timeout | Expected nonzero: process group killed and recorded app-server descendant reaped |
| Probe Ruff lint and format checks | Passed |
| `git diff --check` | Passed |
| `./agent_collab_dev.sh test` | Passed 1,196 tests with one skip |

No probe marker, generated bytecode, credential data, provider transcript,
session identifier, machine-specific path, or raw namespace identifier was
retained.

## Authoritative implementation design

This section is the implementation contract for issue #43. The feasibility
record above is its evidence. The older investigation outline after this
section is retained as research history only; where it differs from this
section, this section wins.

### Architecture decision

Implement one backend-neutral outer-sandbox launch service owned by the runner
layer, not by `Referee`. A backend supplies immutable provider facts and an
inner execution plan. Common code validates those facts, establishes and
proves the Bubblewrap boundary, supervises the process tree, and reports the
result. Common code must never import a concrete backend package or switch on a
backend id. Backend code must never assemble Bubblewrap argv or reproduce
generic mount, handshake, timeout, or process-tree logic.

There are three execution shapes:

```text
CLI
Referee -> existing provider runner -> SandboxSupervisor
        -> bwrap -> bootstrap -> provider CLI -> every descendant

SDK with local execution
Referee -> SdkWorkerRunner -> SandboxSupervisor
        -> bwrap -> bootstrap -> SDK worker
        -> provider runtime -> tools/MCP/hooks/callbacks/subagents/descendants

SDK with proven no-local-effects surface
Referee -> audited in-process SDK runner -> remote provider
```

The third shape is a capability exception, not an OS-enforcement claim. It is
valid only while the exact request surface has no local file, shell, callback,
hook, plugin, skill, MCP, subagent, helper-process, or writable-state path.
`xai_sdk` currently meets that narrower condition. Any new local extension
invalidates the capability and makes outer `read-only` unsupported until the
backend moves to the worker shape.

The MVP guarantee is workspace write protection. The visible host root remains
readable. Network access and declared provider state remain available. This is
not an untrusted-code container. The guarantee covers filesystem operations by
the contained process tree; it does not cover writes delegated over network or
IPC to a host-local service that operates outside the namespace.

### Public policy and configuration contract

#### Caller-facing policy

Add one backend-neutral start field:

```json
{"sandbox": "read-only"}
```

`SandboxPolicy` is a string enum with exactly:

- `read-only`: require an established OS boundary for every local execution
  surface, or a positively audited `no_local_effects` backend capability;
- `none`: do not establish an agent-collab outer sandbox.

The field is independent of every provider's existing `sandbox`, permission,
approval, or mode option. Existing provider fields remain under
`backend_options.<canonical>`. `sandbox = "none"` does not alter those native
settings.

The target built-in default is platform-specific after the production
readiness gate:

| Platform with an activated enforcement engine | Built-in default |
| --- | --- |
| Linux after the Bubblewrap readiness gate | `read-only` |
| macOS, Windows, or any platform without an activated engine | `none` |

The merged `SystemConfig.sandbox_default` remains a concrete enum, not `auto`;
the built-in config layer supplies the platform value before global merge.
Global user config may set it to either value. An explicit or configured
`read-only` still fails closed for an OS-enforced member when its engine is
unavailable; the platform default never downgrades such a request. The optional
global installation constraint is:

```toml
[system]
sandbox_override = "read-only"
```

Resolution is fixed:

| Override | Explicit start | Effective value and source |
| --- | --- | --- |
| absent | omitted | `sandbox_default`, source `configured_default` |
| absent | present | explicit value, source `request` |
| present | omitted | override, source `installation_override` |
| present | same value | override, source `installation_override` |
| present | different value | reject before session creation |

An override never silently replaces a conflicting request. Unknown values are
field-path validation errors. Project config cannot set either system field;
the existing project-scope filter must strip them with a sanitized warning.

Rollout has two explicit built-in-config phases per platform. Partial
implementation releases ship `sandbox_default = "none"` and require an
explicit `read-only` request; their migration makes omitted starts inherit
`none`. Linux activation may set its built-in layer to `read-only` only after
the automated shipped agent/workflow and Bubblewrap engine readiness gates
pass. Other platforms retain built-in `none` until their own engine and
readiness gates exist. Users without an explicit global value inherit their
platform target; an explicit global `none` remains an opt-out. The migration
never writes either built-in value into user config.

A persisted session created before the field existed normalizes to
`effective = "none"` with source `legacy_session`; that records the boundary it
actually had rather than applying a new default retroactively. It may continue
with that historical policy only when no installation `read-only` override is
active. An active override rejects another turn or resume with
`outer_sandbox_legacy_session` and directs the caller to start a new contained
session. Status and settings retain the legacy source and warning. The
migration never fabricates enforcement for historical turns.

#### Operator path exceptions

Stage 1 adds no caller-supplied path field. Writable exceptions are too
security-sensitive to let an arbitrary session caller widen. If operator
exceptions are needed, they are global-user-only:

```toml
[system]
sandbox_extra_readable_dirs = []
sandbox_extra_writable_dirs = []
sandbox_alias_audit_max_entries = 1000000
sandbox_alias_audit_timeout_seconds = 10
# sandbox_scratch_root = "/absolute/daemon-owned/runtime/path"
```

Project config cannot supply them. Start requests cannot override them. A full
settings or dry-run view reports their normalized destinations and access;
compact views report only the count and access class. The audit limits are
global-operator compatibility controls, not session inputs. They must be
at least the built-in minima, are reported in full settings/health, and may be
raised for large same-device trees without weakening alias detection. Lower
values are rejected during config validation. Exhausting either limit fails
closed with remediation that names the required operator fields; it never
recommends disabling the sandbox.

Scratch allocation uses a daemon-owned anchor that will remain visible after
all mounts. The built-in resolver prefers
`$XDG_RUNTIME_DIR/agent-collab/sandbox` when that runtime directory is absolute,
owned by the daemon uid, and not symlinked; otherwise it uses
`$AGENT_COLLAB_HOME/runtime/sandbox`. The optional global-only
`sandbox_scratch_root` overrides that choice. The resolved anchor must not equal
or descend from `/tmp`, `/var/tmp`, the workspace, or any destination that the
plan later overmounts. It must pass the same owner/no-symlink private-directory
rules as created state. If neither built-in candidate is safe, start fails with
`outer_sandbox_scratch_anchor_invalid` and names the global remediation field.
Project config and start requests cannot choose scratch placement.

The read-only host root already makes ordinary host paths readable, so an
extra-readable entry primarily documents and validates a provider-visible
directory. An extra-writable entry weakens the boundary deliberately and must
be labeled `operator writable`, never provider state.

Provider arguments that expose additional directories, such as Antigravity
`--add-dir`, are parsed by that backend's adapter. An undeclared path is
normalized as readable. A provider argument that requests or implies write
access must match a configured operator-writable entry or the start fails.
Common code receives normalized mounts; it does not parse provider argv.

#### Path rules

All declared mount paths use a `ResolvedSandboxPath` carrying:

- the configured spelling for diagnostics;
- an absolute mount destination;
- the canonical source returned by strict resolution;
- access, origin, and persistence;
- whether the destination existed or was safely created.

A normalized mount operation is separate from a declaration. It carries one
canonical destination and source, access and persistence, an ordered nonempty
set of retained declaration origins, and the declarations or logical protected
roots for which it is the coverage mount. Normalization never discards
provenance merely because two paths collapse to one operation.

Rules are:

1. `SandboxContext` carries two distinct canonical paths: the resolved session
   workspace root and the resolved effective agent cwd. A relative agent `cwd`
   resolves below the session root; an absolute one stays absolute and must
   pass normal workdir/path validation. The workspace root supplies the final
   read-only bind and workspace prompt fact. The effective cwd supplies host
   subprocess cwd, Bubblewrap `--chdir`, provider cwd flags, and the current
   directory prompt fact. When they are equal they share one
   `ResolvedSandboxPath`; they are never recomputed independently.
2. Common resolution classifies the session root before building mounts. A
   top-level `.git` directory, symlink, or gitfile is a worktree candidate. A
   root-level regular `HEAD` plus `objects/` and `refs/` directories is a bare
   repository candidate. Other layouts are `not_git` and receive no implicit
   external metadata path. A candidate requires the configured minimum Git
   version; a missing/incompatible executable fails with
   `outer_sandbox_git_discovery_unavailable` rather than silently dropping
   metadata protection.

   Candidate validation runs exactly:

   ```text
   git -C <workspace> rev-parse
     --path-format=absolute
     --git-dir
     --git-common-dir
     --is-bare-repository
   ```

   as one argument vector. Its environment starts from a small executable
   discovery allowlist (`PATH`, locale, and platform-required process fields),
   removes every inherited name beginning `GIT_`, then sets
   `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
   `GIT_CONFIG_SYSTEM=/dev/null`, and `GIT_ATTR_NOSYSTEM=1`. No shell, alias,
   hook, credential helper, or repository command runs. Output is bounded and
   must contain exactly two absolute paths and one boolean in that order.

   Strict Python gitfile/`commondir` parsing is authoritative for mount inputs;
   the Git result independently validates repository kind, absolute per-worktree
   git dir, and common git dir. For a bare repository both directories must be
   the workspace. Any discrepancy fails closed. The primary object directory
   is `<common-git-dir>/objects`. Resolution recursively parses filesystem
   alternates from each `objects/info/alternates`. The file is bounded and
   parsed as one filesystem pathname per LF-delimited line; a relative entry
   resolves from the object directory that owns that `objects/info/alternates`,
   not from the repository or `objects/info`. Absolute entries remain absolute.
   Discovery emits logical Git protection records in fixed role order
   `worktree_git_dir`, `common_git_dir`, `primary_object_store`, then recursive
   `alternate_object_store`. Alternates use breadth-first traversal, preserve
   line order within each file, and parse a canonical object store at most
   once. Every duplicate or cycle edge is still retained as provenance,
   including its referring object store and line ordinal.
   Empty entries, NUL, non-filesystem URLs, decoding failures, a missing final
   line delimiter, and paths that fail common strict resolution are malformed;
   comments have no special grammar. Recursion uses cycle detection and the
   common path/audit budget. Malformed, missing, or unpinnable metadata/
   alternate paths fail closed.

   Each logical record has origin `git_metadata`, one canonical destination,
   its role, and all retained discovery provenance. The authoritative concrete
   coverage-mount set is then derived exactly once:

   1. Group exact canonical destinations. Merge their Git roles and provenance
      in the fixed role order above, then referring-root lexical order and line
      ordinal. Access remains read-only and persistence remains
      host-persistent.
   2. Map every grouped destination equal to or a component-boundary
      descendant of the canonical workspace to the final workspace mount.
      Retain origin `git_metadata`, all roles, destinations, and discovery
      provenance on that workspace operation in addition to origin `workspace`;
      do not emit a redundant Git mount. Lexical prefix is never containment:
      for workspace `/work/app`, sibling `/work/app.git` remains external.
   3. Sort the remaining external grouped destinations by component depth then
      lexical path. Select a destination as an external coverage anchor only
      when no already selected anchor is its component-boundary ancestor.
      Otherwise map it to the first, necessarily shallowest, selected ancestor.
      The selected anchor operation retains the union of every covered logical
      record's origin, roles, destinations, and provenance.
   4. Emit exactly one read-only bind per external coverage anchor after every
      writable bind. Emit the one workspace read-only bind last. A selected
      external anchor that is a component-boundary descendant of writable
      provider/operator state is the required narrowing rebind; an exact
      protected/writable destination is still rejected. A writable destination
      equal to or a component-boundary descendant of an external coverage
      anchor is rejected as an ineffective exception; the only permitted
      containment direction is a protected external anchor that is a
      component-boundary descendant of a broader writable root, narrowed by
      this later read-only bind.

   Consequently, an ordinary worktree whose `.git`, common directory, and
   objects are inside the workspace emits no extra Git bind: those logical
   roots are proved by the workspace-last operation. External gitdirs, common
   directories, object stores, and alternates emit only the deterministic
   shallowest external anchors needed to cover them. A bare repository whose
   git and common directories equal the workspace also emits only the final
   workspace bind, with the bare Git roles and descendant object-store record
   retained on it. Exact duplicates and containment never create duplicate
   argv operations, lose an origin, or change which operation must prove each
   logical root. A degenerate external Git anchor that is a component-boundary
   ancestor of the workspace is allowed: the anchor is emitted in the external
   phase and the more-specific workspace remains the final operation. Both are
   read-only, and the ordinary writable-overlap and alias rules still apply.

   The final provider environment under `read-only` unsets the closed
   repository-path redirect list `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`,
   `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, and
   `GIT_INDEX_FILE`; `sandbox = "none"` preserves them. Discovery is stricter
   and removes every inherited `GIT_*` name before setting its isolated control
   values as described above. Thus linked worktrees, `--separate-git-dir`, and
   local alternates receive the same protection as an in-workspace `.git`
   directory.

   This guarantee covers only the worktree or bare repository classified at the
   resolved session workspace root. Nested repositories are ordinary read-only
   workspace content, but their external gitdirs/object stores are not
   recursively discovered in the MVP. Settings state
   `git_metadata_scope = "session_root"`; callers that need a nested
   repository's external metadata protected must start that repository as the
   session workspace.
3. Operator paths must be absolute. Relative paths, empty paths, and
   non-existent paths are rejected. `~` may be expanded only in global user
   config before absolute validation.
4. Backend state paths may be created only when their declaration uses
   `CREATE_PRIVATE_DIRECTORY`. Creation walks from an existing trusted parent,
   refuses symlink components, creates mode `0700` with the daemon uid/gid, and
   verifies the result before launch. `MUST_EXIST` paths are never created.
5. A workspace supplied through a symlink resolves to its canonical directory;
   the canonical path becomes session identity. A writable mount destination
   or any component below its declared anchor may not be a symlink. This
   deliberately fails closed for symlinked provider-state roots until that
   backend has separate compatibility evidence.
6. Exact duplicate canonical destinations with identical access and
   persistence collapse deterministically into one operation while retaining
   every origin and covered declaration. Git logical-root duplicate and
   containment handling is the specific algorithm in rule 2 and takes
   precedence over the generic collapse. A read-only and writable duplicate
   resolves to writable only when the writable declaration is explicit,
   otherwise valid, and unrelated to a protected workspace/Git logical root.
   A writable declaration whose canonical destination equals a protected root
   is always rejected; protected provenance is never collapsed away or
   promoted to writable. The workspace/Git equality of a bare repository is
   not an access conflict: both declarations are read-only, their origins and
   roles merge, and workspace-last placement wins.
7. A writable path equal to or a component-boundary descendant of the workspace
   is rejected because the final workspace bind would make the request
   ineffective. A writable component-boundary ancestor of the workspace is also
   rejected in the MVP; a final workspace rebind would protect the workspace
   but leave an unexpectedly broad host subtree writable. Lexical-prefix
   siblings such as workspace `/srv/acme` and writable `/srv/acme-scratch` have
   no overlap.
8. A writable declaration equal to `/` or the daemon user's resolved home is
   rejected regardless of origin. Provider-state and operator-writable roots
   follow the same breadth guard. Backend compatibility checks may impose
   narrower ownership, basename, relocation, or permitted-anchor rules.
9. Before every launch, common code runs a bounded, no-symlink alias audit. It
   parses the complete host mount table. Protected containment-pair sides are
   exactly the workspace coverage operation and the emitted external Git
   coverage anchors, not each descendant logical Git record independently.
   Every logical record remains an input to the identity/inode walk through its
   selected side. For each protected side and every writable root, the audit
   first selects the enclosing mount entry by longest component-boundary
   prefix. It then examines every mountpoint equal to or below that target.
   Checking only the target or only descendant mountpoints is forbidden. Each
   entry maps to its filesystem type and underlying `(major:minor, mount root +
   relative path)` identity. A nested writable-side mount whose underlying
   identity equals, contains, or is contained by a protected workspace/Git
   coverage identity is rejected. A nested mount below the workspace is also
   rejected in the MVP unless the resulting subtree is independently included
   in the protected read-only coverage. Exact root `(st_dev, st_ino, file
   type)` equality is a second check.

   Filesystem identity is capability-gated. The MVP candidate allowlist is
   `ext4`, `xfs`, and `tmpfs`; every type must pass the version-controlled
   identity, bind, hard-link, and performance acceptance fixtures before its
   capability is enabled or the production default activates. Kernel
   OverlayFS, `fuse-overlayfs`, Btrfs, other union filesystems, NFS/CIFS, FUSE
   filesystems, and unknown types are
   `outer_sandbox_filesystem_unsupported`. In particular, the MVP does not
   infer object identity between an OverlayFS mount and its upper/work/lower
   backing paths. Remediation identifies the detected enclosing filesystem and
   asks the operator to relocate the workspace and writable state to a supported
   local filesystem, or to select explicit `none` only when installation policy
   permits it. Adding a filesystem requires a later versioned identity
   algorithm and positive conditional fixtures; parsing mount options alone is
   insufficient.

   For each writable/protected relation whose enclosing entries report the same
   supported `st_dev`, the audit walks without following symlinks and rejects
   the precise intersection of all non-directory `(st_dev, st_ino, file type)`
   entries between protected coverage and genuinely writable remainder. It
   never uses `st_nlink` as a security filter.

   One writable root may contain several protected coverage sides. The audit
   collects all such sides first, sorts them by depth then lexical path, and
   derives a non-overlapping prune set by discarding a side already covered by
   a selected component-boundary ancestor. One writable-side walk prunes the
   complete set simultaneously. The union of every logical protected tree
   covered by those sides is then compared with the writable remainder. A
   shared inode between two regions that both become protected is allowed; a
   shared inode from that protected union into the remainder fails as a
   hard-link alias.

   Protected external Git coverage below a writable root is intentionally
   allowed because the later read-only rebinds narrow that mount. The mount
   proof must verify every narrowing rebind and its logical-root coverage. A
   descendant logical record never creates a second containment relation or
   rejection; it participates in its selected coverage side's identity and
   inode comparison. No other protected/writable containment is exempt. Benign
   hard links wholly within writable provider state also remain allowed. A
   relation skips intersection only when pinned
   root stats prove different `st_dev` values and the complete mount mapping
   above proves neither side contains a bind of the other's underlying
   identity; mount id or filesystem label alone never establishes separation.
   Fresh private scratch/state created empty by this launch is safe by
   construction.

   The walk uses pinned directory descriptors and defaults to at most 1,000,000
   visited entries across the plan and ten seconds of monotonic wall time. It
   fails closed with `outer_sandbox_alias_audit_exceeded` when either limit is
   reached. Only the global operator fields above may raise the limits.
   Production-default activation benchmarks a 1,000,000-entry same-device
   workspace/state fixture below ten seconds on the supported Linux CI class;
   a separate raised-limit test covers a multi-million-entry fixture. After
   separate workspace and writable bind mounts exist, Linux rejects a new hard
   link between them with `EXDEV` even when they share a backing filesystem; a
   conditional test proves that exact runtime shape. The pre-launch audit
   closes aliases that already existed before the mounts were separated.
10. Mounts sort by destination depth and lexical path within each access class.
    The normalized external Git coverage-anchor binds from rule 2 follow every
    writable bind in that same depth/lexical order. No covered descendant emits
    another operation. The merged workspace/Git read-only bind is always the
    final filesystem operation.

These rules prevent argv order or symlink spelling from silently changing the
effective boundary. They do not prevent a symlink *inside* the read-only
workspace from reaching a declared writable external state root; that target
remains writable and is an explicit MVP limitation.

#### Availability and fail-closed behavior

An `os_enforced` `read-only` execution shape is supported initially only on
Linux with a compatible `bwrap`. macOS, Windows, and other systems reject any
start containing such a member with structured remediation. They never degrade
that member to prompt-only or provider-native controls. `sandbox = "none"`
continues to work there.

Availability is resolved per selected member before any session state is
created:

- `unsupported` rejects the complete start;
- `direct_process` and `sdk_worker` require the Linux/Bubblewrap checks;
- `no_local_effects` requires its versioned capability audit but no Bubblewrap;
- an all-`no_local_effects` start skips the platform and namespace controls;
- a mixed start runs Bubblewrap checks for every OS-enforced member and the
  capability audit for every `no_local_effects` member. One exception never
  bypasses another member's required boundary.

Session-level reporting uses engine `bubblewrap` when every member is
OS-enforced, `not_applicable` when all are `no_local_effects`, and `mixed` when
both shapes are present. Per-agent `support`, `enforcement`, and engine facts
remain authoritative.

Bubblewrap availability has two checks when at least one selected member needs
OS enforcement:

- discovery/health reports platform, resolved executable, version, and the
  result of a credential-free namespace control;
- start performs a fresh control before creating session state, and every
  real launch repeats the setup/handshake so a stale health result cannot
  authorize a permissive provider process.

The control uses the same engine flags and a private fixture, verifies a
separate mount namespace and read-only/writable mount behavior, and starts no
provider. Its read-only source contains a nested bind submount: establishment
must prove both the source and submount effectively read-only and must reject a
writable submount below the coverage destination. This version-gates
Bubblewrap's recursive read-only behavior instead of inferring it from
executable presence or version text. Missing `bwrap`, an unsupported version,
disabled user namespaces, container restrictions, setuid-policy errors,
unavailable proc ancestry interfaces, non-recursive read-only behavior, and a
failed mount proof are distinct sanitized reason codes.
`outer_sandbox_proc_identity_unavailable` explains that neither the kernel
children interface nor the same-uid pinned proc scan is usable. The initial
implementation explicitly uses `--unshare-user`; `--unshare-user-try` is
forbidden because silently retaining the host namespace violates fail-closed
policy.

#### Reporting surfaces

Normalized session settings add:

```json
{
  "sandbox": {
    "requested": "read-only",
    "effective": "read-only",
    "source": "configured_default",
    "engine": "bubblewrap",
    "establishment": "required"
  },
  "agents": {
    "codex_cli": {
      "outer_sandbox": {
        "support": "direct_process",
        "enforcement": "os_enforced",
        "provider_native_profile": {},
        "writable_exceptions": []
      }
    }
  }
}
```

Allowed `support` values are `direct_process`, `sdk_worker`,
`no_local_effects`, and `unsupported`. Allowed `enforcement` values are
`os_enforced`, `not_applicable_no_local_effects`, `disabled`, and
`unavailable`; `unavailable` is diagnostic and cannot appear on a successfully
started `read-only` session.

Every CLI, MCP, REST, TUI, health, options, status, and dry-run surface keeps
outer policy separate from provider-native mode. Dry-run performs validation
and prints the prompt-free inner command plus a redacted sandbox plan; it does
not claim the namespace was established. Full local settings may show
normalized provider-state destinations. Compact settings and logs show labels
such as `Codex state (persistent writable)` and never raw scratch paths.

The provider command event must stop persisting the full prompt-bearing argv.
It records a prompt-free, secret-free command preview, policy, cwd, mount
labels, and startup result. Environment values, control nonces, file
descriptors, provider exceptions, credentials, raw mount namespace ids, and
worker/session protocol ids do not enter events or logs.

### Common launcher boundary

#### Proposed modules and ownership

Add a backend-neutral package:

```text
agent_collab/sandbox/
  policy.py       enums, resolved policy, configuration resolution
  specs.py        protocols and immutable launch/state/environment types
  paths.py        normalization, overlap and symlink validation
  bubblewrap.py   preflight, argv and mount-plan construction
  bootstrap.py    namespace proof and exec gate
  supervisor.py   process, streams, cancellation, reaping and cleanup
  worker.py       generic framed SDK-worker transport
```

Backend packages add `sandbox.py` beside `backend.py`. The registry validates
that every backend declares a `sandbox_adapter`; lack of an adapter is
equivalent to `unsupported`, not an implicit permissive default. The built-in
`MockRunner` has a registered `MockSandboxAdapter` that declares
`no_local_effects`: it constructs in-memory events and performs no file, tool,
callback, or child execution. That adapter goes through the same typed registry
and shipped-readiness matrix as provider adapters, so common code never needs
an `agent.type == "mock"` sandbox branch. Any future mock behavior that performs
a local effect revokes the capability until it uses the normal contained
shape.

`Referee` continues to know only `AgentRunner`. `SessionManager` resolves
policy and adapter support during start validation, then passes a
`ResolvedSandboxPlan` through `RefereeConfig` to runner construction. The
runner layer owns launch enforcement because it already owns cwd, environment,
streaming, timeout cancellation, and provider lifecycle.

#### Types and protocols

The implementation should use immutable dataclasses equivalent to:

```python
class SandboxPolicy(str, Enum):
    READ_ONLY = "read-only"
    NONE = "none"

class SandboxSupport(str, Enum):
    DIRECT_PROCESS = "direct_process"
    SDK_WORKER = "sdk_worker"
    NO_LOCAL_EFFECTS = "no_local_effects"
    UNSUPPORTED = "unsupported"

class PathAccess(str, Enum):
    READ_ONLY = "read_only"
    WRITABLE = "writable"

class PathOrigin(str, Enum):
    WORKSPACE = "workspace"
    GIT_METADATA = "git_metadata"
    PROVIDER_STATE = "provider_state"
    OPERATOR = "operator"
    SCRATCH = "scratch"

class Persistence(str, Enum):
    HOST = "host_persistent"
    SESSION = "session_private"
    TURN = "turn_private"

class CreationPolicy(str, Enum):
    MUST_EXIST = "must_exist"
    CREATE_PRIVATE_DIRECTORY = "create_private_directory"

@dataclass(frozen=True)
class StateRootSpec:
    label: str
    destination: Path
    access: PathAccess
    persistence: Persistence
    creation: CreationPolicy

@dataclass(frozen=True)
class EnvironmentSpec:
    set_values: Mapping[str, str]
    unset_names: tuple[str, ...]
    secret_names: tuple[str, ...]

@dataclass(frozen=True)
class NativeSandboxProfile:
    summary: Mapping[str, JSONScalar]
    command: tuple[str, ...] | None
    sdk_options: Mapping[str, JSONScalar]

@dataclass(frozen=True)
class BackendSandboxSpec:
    support: SandboxSupport
    policies: frozenset[SandboxPolicy]
    state_roots: tuple[StateRootSpec, ...]
    provider_visible_paths: tuple[StateRootSpec, ...]
    environment: EnvironmentSpec
    native_profile: NativeSandboxProfile
    compatibility: tuple[CompatibilityCheck, ...]
    backend_prompt_augmentation: str | None

class SandboxAdapter(Protocol):
    def describe(self, context: SandboxContext) -> BackendSandboxSpec: ...
    def prepare_inner(self, plan: ResolvedSandboxPlan) -> InnerExecution: ...
```

`describe` reports provider facts. `prepare_inner` remains backend-owned
because it maps the established outer policy to provider flags or SDK options.
It does not receive a Bubblewrap executable or mount builder. Common code
normalizes the returned paths and builds all mounts.

The effective policy, not the adapter, owns the mandatory policy preamble.
`ResolvedSandboxPlan.render_prompt(user_prompt, runtime_facts)` prepends it
after turn/session scratch and per-agent enforcement are known.
`backend_prompt_augmentation` may add a bounded provider-specific note after the
common block, but `None` never disables or replaces the policy block.
`sandbox = "none"` causes the common renderer to return the user prompt
unchanged and ignores any read-only-only backend note.

`InnerExecution` is either a provider CLI argv or an SDK-worker entrypoint and
opaque backend payload. The common launcher treats both as bytes/argv and
never examines a backend name.

#### Bubblewrap plan and exact ordering

For `read-only`, the generic argv order is:

```text
bwrap
  --json-status-fd <status-fd>
  --die-with-parent
  --new-session
  --unshare-user
  --unshare-pid
  --cap-drop ALL
  --ro-bind / /
  --dev /dev
  --proc /proc
  --bind <private-scratch> <private-scratch>
  --bind <private-scratch>/system-tmp /tmp
  --bind <private-scratch>/system-var-tmp /var/tmp
  [normalized readable declarations as --ro-bind]
  [normalized writable declarations as --bind]
  [normalized external Git coverage anchors as --ro-bind]
  --ro-bind <workspace> <workspace>       # final filesystem mount
  [--setenv/--unsetenv from the resolved environment]
  --chdir <effective-cwd>
  -- <absolute-python> -I -S <absolute-bootstrap.py>
       --protocol-version 1
       --proof-fd <proof-fd>
       [--worker-fd <worker-fd>]
       --provider-stdin-fd <provider-stdin-fd>
       --provider-stdout-fd <provider-stdout-fd>
       --provider-stderr-fd <provider-stderr-fd>
       -- <inner-argv-0> [<inner-argv-1> ...]
```

The default PID-1 reaper installed by Bubblewrap is retained; do not pass
`--as-pid-1`. Network, IPC, UTS, cgroup, syscall, and seccomp isolation are not
added in the MVP. `--unshare-all --share-net` is not used because it would
change more namespaces than the documented guarantee. No `--bind-try`,
`--ro-bind-try`, or user-namespace fallback flag is allowed.
Bubblewrap already leaves no capabilities in the sandboxed process by default;
`--cap-drop ALL` makes that dependency explicit. No adapter may add a
capability.

Arguments are passed as an argv vector, never shell-quoted text. Paths with
spaces remain single values. The root bind precedes all exceptions. Read-only
declarations precede writable declarations. More-specific normalized mounts
follow less-specific mounts. The normalized external Git coverage anchors
follow all writable mounts. Workspace is last regardless of lexical order.

`HOME` is preserved unless an adapter must relocate a provider whose state
lookup is tilde-based. `TMPDIR`, `TMP`, `TEMP`, and XDG cache/config/data/state
are pointed at private scratch unless an adapter declares a compatibility
exception. The process otherwise preserves the current inherited environment
plus `AgentConfig.env`; this is a compatibility decision, not a
confidentiality claim. Common private control variables replace collisions.
Only environment names and origins are observable; values are redacted.

Each scratch root contains private `system-tmp` and `system-var-tmp`
directories created before launch with temp-directory semantics. They are
mounted at `/tmp` and `/var/tmp`, so helpers that ignore `TMPDIR` remain
ephemeral and cannot reach host temp files or sockets. `TMPDIR` still points to
a distinct private subdirectory whose exact path appears in the policy
preamble. Because the scratch anchor is forbidden below either system-temp
destination, those overmounts cannot shadow the host path named by `TMPDIR`.
CLI instances receive turn-private temp mounts; SDK workers receive
session-private temp mounts. An adapter may request stricter private temp
layout, but may not expose the host `/tmp` or `/var/tmp` writable.

The resolved absolute interpreter and standalone standard-library-only
`bootstrap.py` are installation artifacts verified as regular files below a
read-only source/install root. `-I -S` excludes the current directory, user
site, `PYTHON*` environment customization, and site initialization. The first
`--` is Bubblewrap's command separator; the second is bootstrap's required
separator before the inner argv. Empty inner argv, unknown bootstrap options,
duplicate role options, or a non-absolute inner executable fail before ACK.
Backend payload and secrets travel through provider-native channels or the
worker protocol, never through bootstrap flags.

File descriptors are an independent part of the write boundary. The supervisor
always spawns with `close_fds=True`. A frozen per-launch `BootstrapHandles`
record assigns one distinct integer greater than `2` to each role. The values
are launch-local rather than global constants so the threaded daemon never
renumbers descriptors in its own process. The decimal role-to-number mapping is
passed only in the non-secret bootstrap argv above and compared verbatim in the
hello. Its exact `pass_fds` whitelist contains:

- Bubblewrap JSON-status writer;
- the bootstrap side of the proof socket;
- the worker-protocol socket only for an SDK-worker launch;
- the three launch-fixed provider stdin/stdout/stderr transfer descriptors.

Bootstrap itself receives `/dev/null` on descriptor `0` and bounded,
supervisor-owned diagnostic captures on `1` and `2`. Those captures are never
fed to a provider parser or committed as provider events; structured bootstrap
failures travel over the proof socket. Immediately before provider/worker
`exec`, bootstrap `dup2`s the separate transfer descriptors onto `0`, `1`, and
`2`, closes their original numbers, closes the proof socket, and makes no
further Python call except `execve`. Provider output therefore has a distinct
byte channel from interpreter/import warnings or bootstrap diagnostics.

Supervisor-side socket/pipe ends and every unrelated daemon descriptor are
non-inheritable. In particular, no open file description for workspace,
session storage, logs, provider state, or daemon runtime data may cross.
Bootstrap enumerates `/proc/self/fd` before ACK and fails startup on anything
outside `0`, `1`, `2`, its proof socket, the three stdio transfer descriptors,
and the optional worker socket. The enumeration opens `/proc/self/fd` with a
known directory fd, excludes only that exact transient fd from the comparison,
and closes it before proceeding.
JSON-status belongs to Bubblewrap and must not appear in the bootstrap set.
The engine compatibility control proves this behavior for the resolved
Bubblewrap version by passing a real JSON-status fd and requiring the inert
inner bootstrap to observe only its declared set; a version that leaks the
status writer is `outer_sandbox_engine_incompatible`.
After stdio transfer, CLI `exec` permits only `0`, `1`, and `2`.
SDK-worker `exec` additionally preserves the single launch-fixed worker socket;
the worker validates that exact descriptor before its protocol `hello`. This
distinction prevents writable-FD bypass, bootstrap bytes masquerading as
provider output, and accidental closure or exposure of the worker channel.

CLI scratch is turn-private and removed after the process tree exits. An SDK
worker has session-private runtime scratch because its provider runtime spans
turns; optional turn subdirectories may be removed after each result, and the
whole runtime scratch is removed on worker close/crash. A cleanup failure is
logged by stable category and retried by a background reaper, never by exposing
the raw temporary path.

#### Bootstrap control protocol

The proof socket uses protocol version `1` and the same 4-byte big-endian
length plus UTF-8 JSON-object framing as the worker transport, with a 16 KiB
frame limit. Unknown message types, duplicate fields, non-canonical role names,
oversize frames, invalid UTF-8/JSON, and EOF fail startup. An overall
15-second monotonic establishment deadline covers status parsing, challenge,
hello, host verification, and ACK; expiry begins normal teardown.

Messages are exactly:

| Direction | Type | Required fields |
| --- | --- | --- |
| supervisor -> bootstrap | `sandbox_challenge` | `version: 1`, `nonce`: 32 random bytes encoded as 64 lowercase hex characters |
| bootstrap -> supervisor | `sandbox_hello` | `version: 1`, identical `nonce`, positive namespace-local `pid`, `fd_roles`: exact proof/worker/stdio role-to-number map |
| supervisor -> bootstrap | `sandbox_ack` | `version: 1`, identical `nonce`, `verified: true` |

The nonce is created from the OS CSPRNG and first reaches bootstrap only in the
challenge frame; it never appears in argv or environment. Bootstrap accepts one
challenge and one ACK only, drops the parsed nonce and frame buffers before
exec, closes the proof socket, and never persists or logs them. The private
socket and exact nonce echo bind both messages to this launch. The optional
worker role must be absent for CLI and present exactly once for an SDK worker.

Bubblewrap status is a JSON-lines stream, not a one-object response. A dedicated
reader starts with Bubblewrap, drains concurrently for the whole process
lifetime, permits at most 16 KiB per line and 64 KiB total, and does not wait
for EOF before establishment. The first complete line must be a UTF-8 JSON
object with one positive integer `child-pid`; it identifies Bubblewrap's
namespace PID-1 reaper. Additive keys are ignored. Later valid objects and keys
are forward-compatible; a second `child-pid` is forbidden, and at most one
integer `exit-code` is retained and reconciled with process termination. EOF is
required only after Bubblewrap exits. Invalid UTF-8/JSON, an oversize line or
stream, premature EOF before `child-pid`, a child pid that cannot be pinned, or
an exit-status contradiction fails closed. Engine readiness runs this exact
stream parser and control protocol against an inert bootstrap for the resolved
Bubblewrap executable/version.

#### Establishment proof and startup gate

Bubblewrap's fail-before-exec behavior is necessary but not the only startup
proof. The common supervisor creates:

- a Bubblewrap JSON-status pipe;
- a private bootstrap control socketpair;
- an unpredictable in-memory nonce used only by the framed challenge;
- the process group/session led by the Bubblewrap process.

The bootstrap is the first user command inside Bubblewrap's default PID-1
reaper. Before importing an SDK or execing a CLI it receives the challenge,
enumerates its fd set, and sends `sandbox_hello`, then waits for
`sandbox_ack`. The status stream's host `child-pid` identifies the reaper and
must have final `NSpid` component `1`; it is not the bootstrap pid. The
supervisor opens a pidfd for the reaper where supported and otherwise pins an
opened `/proc/<host-reaper-pid>` directory plus the process start-time tuple.

Before ACK, trusted bootstrap code has not forked. The preferred correlation
reads the pinned reaper's `task/<reaper-tid>/children`. When that optional
kernel interface is absent, the equal-authority fallback enumerates numeric
entries through one pinned `/proc` directory, opens each candidate before
inspection, restricts candidates to the daemon uid, and selects on the pinned
reaper's host pid in `PPid`. Both routes require exactly one live direct child
whose final `NSpid` component equals the hello's positive local pid (normally
`2`) and whose start time is after launch. The supervisor pins that host
bootstrap identity separately by pidfd or opened proc directory plus start
time, then rereads uid, parent, `NSpid`, and start time through the pin. Missing,
multiple, reused, changed, or non-child candidates fail closed. If neither
route is readable, availability and start fail with
`outer_sandbox_proc_identity_unavailable`. The nonce and private inherited
socket bind the hello to the pinned bootstrap; no numeric pid alone is
authority.

The supervisor reads `status` and `mountinfo` through the pinned reaper and
bootstrap identities rather than reopening numeric paths. It then verifies a
different mount namespace, read-only root/workspace, every emitted external Git
coverage anchor, writable scratch and every declared writable root, effective
cwd, zero effective/permitted/ambient capabilities, the exact resolved
destinations and access modes, and the exact hello fd-role map.

Proof consumes the frozen normalized operations, not the pre-normalization
declarations. For every coverage operation, the effective topmost mount at its
destination must be the planned bind with the planned source identity and
read-only flags. No writable submount may remain at or component-boundary below
a coverage destination. An external anchor that is a component-boundary
descendant of a writable root must be a later, more-specific read-only mount,
and the workspace operation must be the final filesystem operation. A shadowed
mount entry is never establishment evidence.

The supervisor then walks the retained coverage mapping. Every logical Git root
must be equal to or a component-boundary descendant of its recorded, verified
anchor. Through the pinned bootstrap identity's namespace/root, it stats every
logical destination without following a final symlink and requires an existing
directory whose `(st_dev, st_ino, file type)` matches the plan's pinned
pre-launch identity. Workspace-covered records may map only to the verified
final workspace operation; external records may map only to the verified
emitted anchor selected by normalization. Missing roles, origins, logical
destinations, identity matches, narrowing relations, or an extra/unplanned
coverage operation fail establishment. Only then does it send `sandbox_ack`.
The bootstrap closes control fds and execs the provider or SDK worker.

Any parse error, timeout, premature status EOF, missing or reused reaper or
bootstrap identity, inconsistent ancestry/`NSpid`, mount mismatch, or early
child exit starts the full teardown before acknowledgement. Thus a permissive
native profile cannot run when setup proof fails. Raw pids, nonce, namespace
ids, and mountinfo are never persisted.

The preflight control uses the same builder and bootstrap but a fixed inert
inner command. Unit tests may fake status/mountinfo readers; conditional Linux
tests must exercise the real gate.

#### Process and cleanup lifecycle

The supervisor starts Bubblewrap with `start_new_session=True`; the outer
process group owns launcher-side processes, but it is not claimed to contain
the inner provider directly. Bubblewrap's `--new-session` creates an inner
session. Descendant containment instead depends on `--unshare-pid`, the
Bubblewrap PID-1 reaper, and namespace teardown when the exact Bubblewrap leader
exits. `--die-with-parent` covers supervisor/daemon death. The supervisor keeps
the exact leader handle plus the status-reported child handle and never relies
on a reusable numeric pid alone. Normal completion waits for stdout/stderr
readers, inner result, worker close as applicable, the Bubblewrap leader to be
reaped, and the reported child handle to signal exit.

Cancellation and timeout use the same terminal teardown state machine:

1. stop accepting new requests and stop committing new provider events for the
   occurrence, while the protocol reader continues draining and classifying
   bounded in-flight frames;
2. for a worker, send `cancel(run_id)` and wait a short adapter-declared grace;
3. signal the complete outer process group with `SIGTERM`;
4. wait the common terminate grace and reap the exact Bubblewrap leader;
5. signal the group with `SIGKILL`;
6. wait again for the leader and status-reported child handles; transfer only an
   anomalous wait/cleanup to the existing background-reaper pattern;
7. commit the referee's timeout/interruption outcome after cleanup ownership is
   established.

`cancelled` is only a cooperative protocol acknowledgement and never suppresses
steps 3–6. A cancelled or timed-out worker-backed occurrence closes that worker
and marks its runner session terminal because killing the namespace destroys
live strict-continuation state. A later turn is rejected with
`worker_session_terminated`; it never starts a new worker or provider
conversation. Ordinary successful turns retain the worker.

Signals target the outer group, never only a provider pid, but cleanup success
requires reaping Bubblewrap and observing PID-namespace teardown rather than
assuming `killpg` reached the inner session. A provider exit, parser failure,
malformed worker frame, bootstrap failure, or daemon disconnect follows the
same cleanup path. Partial startup removes scratch and any session-private
state it created but never deletes persistent provider state. Close is
idempotent. Reset must not create a fresh provider conversation when strict
continuation identity is required.

Startup failures use stable outer-sandbox codes rather than
`provider_empty_response`. Diagnostics name the policy, failing phase, platform
or missing dependency, and remediation; they omit inner argv prompts,
environment values, raw provider exceptions, and private paths.

#### Lifecycle sequences

CLI:

```text
start request
  -> resolve default/override/request policy
  -> resolve workflow/backend/options once
  -> adapter.describe + normalize paths + compatibility checks
  -> fresh Bubblewrap preflight
  -> persist sanitized settings
turn
  -> create turn scratch
  -> common policy renderer prepends per-agent filesystem facts
  -> adapter prepares inner CLI/native profile
  -> supervisor starts bwrap/bootstrap
  -> verify mount proof; ACK; exec CLI
  -> stream parsed events through existing awaited sink
  -> wait/reap -> TurnOutcome -> commit boundary
reset
  -> stateless no-op after any owned process is gone
close/crash
  -> kill group if present -> remove scratch
```

SDK worker:

```text
start request/preflight
  -> same common resolution and proof
first turn
  -> create session-private worker state/scratch
  -> launch bwrap/bootstrap/worker; prove and ACK
  -> protocol hello
  -> open(normalized backend payload, options, workspace, policy facts)
  -> ready only after open is accepted
  -> common policy renderer builds effective prompt
  -> run(turn_id, effective_prompt, workspace)
  -> worker creates SDK client/runtime only now
  -> event(seq...) -> result(outcome, continuation facts)
later turn
  -> same worker and strict provider identity; only the new delta receives the
     common policy prefix
cancel/timeout
  -> protocol cancel grace -> group TERM/KILL -> reap bwrap/namespace
  -> mark worker runner terminal; reject later turns
reset
  -> provider reset inside worker; retain only strict continuation state
close
  -> close request/ack -> group reap -> delete session-private state
daemon disconnect/crash
  -> worker sees control EOF and exits; die-with-parent/group cleanup covers
     uncooperative descendants
```

Event streaming remains cursor-based and unchanged above the runner. The
worker boundary transports the same normalized `Event` and `TurnOutcome`
shapes; it does not create a second transcript or event store.

### SDK worker protocol

Use one full-duplex Unix socketpair inherited across bootstrap. Frames are a
four-byte unsigned big-endian length followed by UTF-8 JSON. The maximum frame
size is the existing event transport bound; zero-length, oversized,
non-object, invalid UTF-8, and invalid JSON frames are fatal. File descriptors
other than the declared control/status descriptors are closed.

Every frame has `protocol`, `type`, and, where applicable, `request_id`,
`run_id`, and monotonic `sequence`. Version mismatch is fatal. Allowed
messages are:

| Direction | Message | Required semantics |
| --- | --- | --- |
| worker -> daemon | `hello` | protocol version and random worker instance token |
| daemon -> worker | `open` | normalized backend payload, options, workspace, policy facts |
| worker -> daemon | `ready` | payload accepted; SDK/provider not used before sandbox ACK |
| daemon -> worker | `run` | unique run id, prompt, resolved cwd |
| worker -> daemon | `event` | one allow-listed normalized event, increasing sequence |
| worker -> daemon | `result` | exactly one terminal outcome for the run |
| daemon -> worker | `cancel` | cancel only the matching active run |
| worker -> daemon | `cancelled` | cancellation accepted; not proof descendants exited |
| daemon -> worker | `reset` | reset live provider transport while preserving strict identity |
| worker -> daemon | `reset_result` | success or sanitized stable failure |
| daemon -> worker | `close` | idempotent final cleanup |
| worker -> daemon | `closed` | provider close finished; worker will exit |

Only one run may be active per worker. Request ids cannot repeat. Events after
a result, results without runs, out-of-order sequences, unknown types, and
provider-controlled identity changes are protocol failures. The supervisor
then kills the whole group and returns `provider_output_invalid` or the
dedicated worker-protocol code chosen during implementation.

The daemon and worker each use dedicated reader and writer tasks; neither waits
for a terminal result while leaving its receive direction undrained. The daemon
continuously validates frames into a bounded per-run event queue measured in
both frames and bytes. The worker has a bounded outbound queue and a dedicated
writer, while a separate reader remains able to receive `cancel` or `close`.
Normal event backpressure propagates through those queues. If a queue stays full
beyond the common bounded grace, if the aggregate byte limit is exceeded, or
if the sink stops consuming, the daemon records
`worker_backpressure_exceeded` and runs terminal group/namespace teardown.
Events are never silently dropped, a `result` cannot overtake earlier events,
and synchronous socket I/O never blocks the daemon event loop. Once terminal
cancellation begins, drained in-flight events are counted as ignored
post-cancel frames and are not committed; their bounded handling cannot delay
teardown.

The worker owns SDK objects, callbacks, hook handlers, in-process MCP handlers,
provider runtime children, local stdio MCP servers, plugins, skills, custom
tools, and subagent construction. No provider SDK import or callback remains
in the daemon for a worker-backed backend. Remote HTTP MCP and provider-hosted
tools are labeled remote and are not claimed to be locally contained.

Worker events are reconstructed from an allow-list of Event fields. Provider
exception text is bounded, categorized, and stripped of credentials and
private roots before it can become an event. Tracebacks and arbitrary Python
object serialization never cross. The daemon remains authoritative for
`agent_id`, turn identity, policy, and committed outcome.

Strict continuation preserves the current semantics:

- Codex retains one thread id and never falls back fresh after rejected resume.
- Claude retains one session id and never forks or silently starts fresh.
- Antigravity retains conversation id plus trajectory state and uses only
  `RESUME`, never `CREATE_OR_RESUME`.
- A worker crash destroys live in-memory continuation. The session fails the
  active occurrence, becomes terminal, removes runner-created private state only
  after namespace teardown, and does not fabricate restart-safe resume
  capability.

### Backend adapter decisions

The table is normative for the proposed implementation. `provider state` means
the complete writable exception selected from current evidence, not that every
listed state shape has already passed its stage's credentialed release gate or
that every alternative auth flow works. In particular, the Codex probe proved
staged `auth.json` and proved that a whole read-only home fails; it did not test
the proposed complete writable production state. Stage 1 acceptance below is
the enabling evidence for that shape.

| Backend | Current local owner and remote effects | State/auth contract | Native profile only inside outer boundary | Shape and support |
| --- | --- | --- | --- | --- |
| `codex_cli` | CLI owns file/shell/MCP/hooks/skills/plugins/subagents and descendants; model/web effects may be provider-hosted | Effective complete `CODEX_HOME`, must exist, persistent writable; env auth remains readable | `--dangerously-bypass-approvals-and-sandbox`; remove conflicting owned approval/sandbox flags | direct process; `read-only` supported in Stage 1 |
| `claude_cli` | Claude Code owns built-ins, external stdio MCP, skills/subagents and descendants; server tools are remote | Complete `CLAUDE_CONFIG_DIR` (normally `~/.claude`), must exist, persistent writable | `--dangerously-skip-permissions`; transient native sandbox disabled; strict empty MCP config only where existing backend policy requires it | direct process; later supported |
| `antigravity_cli` | `agy` and helper own file/shell, configured plugins/MCP/hooks and descendants; web/image services are remote | Complete `~/.gemini`; no relocation variable, so adapter validates basename and sets `HOME` to parent; keyring remains external | skip permissions, `mode=accept-edits`, `sandbox=false` | direct process; later supported, keyring/helper compatibility checked |
| `xai_cli` | Grok owns Bash, hooks, plugins, skills, MCP and descendants; remote tools remain remote | Effective complete `$GROK_HOME` or `~/.grok`, must exist, persistent writable; env/external auth allowed | `permission_mode=bypassPermissions`, `sandbox=off` | direct process; later supported |
| `codex_sdk` | daemon currently owns SDK/threads and app-server owns local tools; ambient config can enable extensions | Complete `CODEX_HOME` plus separately configured SQLite location if outside it, persistent writable | SDK `danger-full-access`; current approval default remains explicit | complete SDK worker required; later supported |
| `claude_sdk` | daemon currently owns SDK callbacks; Claude Code owns built-ins/descendants and ambient MCP may remain | Complete `CLAUDE_CONFIG_DIR`, persistent writable | `bypassPermissions`; native sandbox disabled | complete SDK worker required; later supported |
| `antigravity_sdk` | daemon currently owns SDK callbacks; `localharness` owns built-ins, subagents, stdio MCP and descendants; provider services are remote | Session-private writable trajectory/app-data; ADC read-only; alternative Gemini/keyring flows need checks | allow-all SDK policy/capabilities selected only after outer proof | complete SDK worker required; later supported |
| `xai_sdk` | in-process gRPC request; current backend exposes no local tool, callback, MCP, plugin, hook, skill, subagent, child, or local state write | API key environment only; no writable state root | none | `no_local_effects`; `read-only` resolves to `not_applicable_no_local_effects`, revoked on surface change |

Each adapter also declares exact tested provider/runtime version facts,
required executable/interpreter, state ownership checks, environment names,
provider-visible paths inferred from argv/config, alternative auth limitations,
and cancellation grace. Compatibility failure is per backend and fail-closed;
one provider's evidence never authorizes another.

### State, authentication, and writable exceptions

Persistent state roots are resolved at session start and revalidated before
each launch. `MUST_EXIST` roots are never synthesized because an empty root
could hide credentials and produce a misleading login failure. The daemon
requires the root to be owned by its uid, not group/world writable, and not a
symlink. Alternative ownership used by managed installations requires
backend-specific evidence and an explicit adapter rule.

`HOME` remains the user's real home through the read-only root except where
Antigravity CLI's tilde-only lookup requires setting it to the parent of the
selected `.gemini`. Provider-specific variables such as `CODEX_HOME`,
`CLAUDE_CONFIG_DIR`, and `GROK_HOME` point to the exact mounted root. XDG and
temporary variables point to private scratch by default. An adapter must
declare any XDG path that must persist or any SQLite path outside the main
root; undeclared writable startup requirements make the backend incompatible,
not grounds for mounting the whole home.

Host-persistent state survives turns, resets, sessions, and daemon restarts.
The sandbox never copies it back because it is mounted directly. Worker
session-private state survives ordinary turns and reset. It is removed on
close/crash only after the process tree is reaped and the worker runner is
terminal; no subsequent turn may attempt to resume from deleted private state.
Turn-private scratch survives only its CLI turn. The design must not delete a
root it did not create.

Keyrings, credential helpers, session buses, SSH agents, and external auth
providers are not filesystem mounts. An adapter reports them as external
services, checks only non-secret availability, and makes no confidentiality or
write-isolation claim. Antigravity keyring access is therefore compatible but
explicitly outside the filesystem guarantee.

The same rule applies to any host-local daemon reachable through a Unix socket
or loopback network, including container engines, user systemd services, and
automation agents. A read-only bind does not prevent such a daemon from opening
the host workspace independently. Adapters declare known required external
services for settings; common health also warns on known high-risk container
engine sockets, but neither list is a complete service firewall. Full settings
label them `external service (outside filesystem boundary)`.

Full settings show every writable destination to the authenticated local
caller, its label, origin, persistence, and creation policy. Logs, events, and
compact settings use labels and home-relative display forms. Credential
contents, environment values, database names discovered from private config,
and provider account data never appear.

The MVP intentionally permits the provider and all contained descendants to
modify the complete declared state root, including credentials, config,
history, hooks, plugins, skills, and databases. Protecting that root requires
future copy-on-write or minimized-state work and is not part of issue #43.

### Security guarantee and non-goals

For an `os_enforced` `read-only` turn, the design guarantees:

- the resolved workspace is mounted read-only last, and every resolved
  session-root Git directory, common Git directory, primary object directory,
  and local alternate object store maps to one effective, topmost, proved
  read-only coverage mount: the final workspace operation for in-workspace or
  bare roots, or a normalized external anchor emitted after writable mounts;
- direct create, overwrite, rename, delete, chmod, timestamp, and mutating Git
  operations against the established workspace and every logical Git root
  covered by an external anchor fail at the OS boundary;
- the underlying supplied workspace and session-root Git metadata/objects
  cannot be modified through a declared writable path, inherited fd, remount,
  or writable rebind;
- provider CLI/SDK runtime, local callbacks, local tools, stdio MCP servers,
  hooks, plugins, skills, subagents, shell children, and descendants inherit
  the same mount namespace when that backend advertises support;
- provider cwd remains the authoritative resolved host path;
- only declared provider/operator state and private scratch are writable;
- no pre-existing inode alias connects the workspace or external Git protection
  trees to a genuinely writable remainder, and no unrelated writable file
  descriptor crosses the exec boundary;
- a failed namespace proof cannot start the permissive inner provider profile;
- cancellation, timeout, daemon disconnect, reset, close, and provider crash
  transfer or complete ownership of the whole process tree.

For `xai_sdk` `no_local_effects`, the guarantee is only that the audited
backend request exposes no local effect path. There is no Bubblewrap namespace
and settings say so. Provider-hosted remote storage and tools are never covered.
This is a versioned software capability, not an OS boundary. Enabling it
requires both exact request/source checks and a subprocess probe that denies
filesystem mutation and child creation while exercising client construction,
request serialization, transport setup against a fixture, and close. Merely
observing zero configured tools or zero tool events is insufficient. A runtime
or dependency version outside the audited set, an unobserved writable
cache/telemetry requirement, or an unavailable deny probe makes the capability
`unsupported`. The deny probe is a release/compatibility evidence gate, not a
Bubblewrap preflight on each user start. Runtime validation matches the exact
backend surface and dependency versions to that recorded evidence, so an
all-`no_local_effects` session remains independent of host Bubblewrap
availability.

The MVP does not guarantee:

- confidentiality of readable host files or credentials, or prevention of
  network exfiltration;
- protection of declared writable state or targets reached through workspace
  symlinks into that state;
- external gitdirs/object stores belonging only to nested repositories below
  the session workspace; the workspace files remain read-only, but full Git
  metadata protection requires selecting that nested repository as the session
  root;
- isolation of OS keyrings, credential helpers, session buses, remote MCP,
  provider-hosted filesystems, or remote tool side effects;
- network filtering, DNS filtering, CPU/memory/process quotas, syscall
  filtering, seccomp, device hardening beyond Bubblewrap's private `/dev`, or
  denial-of-service resistance;
- preventing a process from creating a nested user/mount namespace and masking
  its own workspace path with a new private writable filesystem. Such writes
  may change that process's view but must not change the underlying supplied
  workspace; removing this view-level behavior requires future syscall/user-
  namespace restrictions;
- workspace writes delegated to a host-local or remote service outside the
  namespace, such as a container engine, user service manager, or automation
  daemon. Preventing those requires future network/IPC/socket mediation; the
  `os_enforced` label describes the contained process tree's filesystem view,
  not host-wide integrity against external principals;
- rollback of remote requests, messages, stored completions, billing, or other
  provider effects;
- macOS or Windows enforcement;
- support for future provider extensions without a fresh ownership/state audit.

### Implementation sequence

#### Stage 1 — common foundation plus `codex_cli`

`codex_cli` remains the correct first vertical slice. Its whole local surface
already sits below one subprocess, the probe passed a credentialed tool turn,
its complete state root and native bypass are known, and its structured stream
fits the existing runner. No repository evidence exposes an architectural
blocker. Starting with an SDK would conflate the mount engine with the worker
protocol; starting with Antigravity would add keyring, tilde-state, helper, and
message-only terminal ambiguity before the common seam is proven.

Expected changes:

- schema/API/config migration for outer policy and operator paths;
- `agent_collab/sandbox/` types, path resolver, Bubblewrap builder, bootstrap,
  supervisor, establishment proof, diagnostics, and reporting;
- registry/backend contract addition for typed adapters;
- runner construction and `SubprocessRunner` delegation to the supervisor;
- `codex_cli/sandbox.py` state/native mapping;
- CLI, MCP, REST, TUI, options/health/settings/dry-run documentation;
- focused unit, daemon-route/tool, conditional Bubblewrap, and credentialed
  Codex integration coverage.

Stage 1 is the first vertical implementation milestone, not a production
default-activation release by itself. During the multi-stage development
series, explicit `sandbox = "read-only"` may exercise implemented adapters and
explicit `none` preserves existing behavior, but a release must not flip the
built-in Linux default while any agent selected by shipped default agents or
workflows lacks its required adapter. The automated final Linux activation gate
runs that version-controlled shipped matrix, requires every selected member to
be either OS-enforced or positively audited `no_local_effects`, and requires a
working compatible Bubblewrap engine. A platform without an activated engine
keeps built-in `none`.

Once that gate passes, omitted policy becomes enforced read-only on supported
starts and `none` is the explicit opt-out. Missing/unusable Bubblewrap or
incompatible state fails before session creation or, for a launch-time race,
before provider exec. Rollback is configuration `sandbox_default = "none"` or
an explicit allowed `sandbox = "none"`; it is visible and never automatic.
Silently exempting an unsupported workflow member would violate both the
settled default and fail-closed behavior.

Acceptance:

- common code contains no concrete backend import/id branch;
- exact argv, mount order, path rules, environment, prompt and settings are
  deterministic, including Git duplicate/containment collapse, retained
  origins/roles, workspace/bare absorption, and logical-root-to-mount proof;
- namespace proof gates the permissive Codex command;
- workspace, `.git`, child writes and symlink cases match the stated boundary;
- timeout/cancel/crash reap descendants and cleanup scratch;
- normal read/tool use and persistent `CODEX_HOME` work in an authorized live
  test using an operator-authorized dedicated complete state root across
  multiple launches. The existing staged-`auth.json` probe is not this gate.

#### Stage 2 — `claude_cli`

Add only Claude's adapter, state resolver, transient native profile, health
facts, docs and tests. Reuse the Stage 1 launcher unchanged. Verify
`CLAUDE_CONFIG_DIR`, legacy `.claude.json` read-only behavior, session-env
writes, strict MCP arguments, admin-managed-setting incompatibility, and
descendant cleanup. Credentialed verification is required for auth, Bash, and
state mutation because the probe showed mutable session state is essential.
Rollback disables outer policy explicitly; incompatibility never falls back.

#### Stage 3 — `xai_cli`

Add Grok's adapter for `$GROK_HOME`, permission bypass, native sandbox `off`,
configured provider-visible paths, and warning-only legacy temp behavior.
Hermetic tests cover argv/config/auth variants and structured terminal
failures. Conditional Bubblewrap tests cover direct and child actions.
Credentialed verification is required for one Bash turn and persistent state;
managed sandbox pins and external auth remain compatibility gates.

#### Stage 4 — `antigravity_cli`

Add Antigravity's tilde-based `.gemini`/`HOME` mapping, keyring service report,
helper-materialization check, `--add-dir` parsing, and native profile.
Hermetic tests must prove exit zero plus a reported tool failure cannot become
success. Conditional tests cover keyring-independent structure and path
identity. Credentialed verification is required for keyring/auth,
`agentapi`, shell action, and state. This follows the other CLIs because it has
the highest CLI state and terminal-fidelity risk.

#### Stage 5 — generic SDK worker plus `codex_sdk`

Implement the framed worker transport, bootstrap-to-worker handoff, worker
runner, event/outcome validation, reset/close, continuation identity, crash and
disconnect cleanup. Move the *entire* Codex SDK conversation and app-server
ownership into the worker. Ambient config extensions remain inside. Reuse
Codex state facts from Stage 1 while adding external SQLite detection.

Hermetic fake-worker tests are mandatory for every frame/lifecycle failure.
Conditional Bubblewrap tests use a fake SDK/runtime tree. Credentialed tests
must cover a real shell child, stable continuation across turns, forced
timeout, and app-server reaping. Rollback leaves `codex_sdk` outer read-only
unsupported; it must not revert to the current in-daemon implementation for a
read-only request.

#### Stage 6 — `claude_sdk`

Move SDK client, callbacks, hooks and in-process SDK MCP handlers into the
generic worker; Claude Code and external stdio MCP descend from it. Preserve
strict session resume and current stream/event semantics. Audit ambient MCP
because the current command is not strict-MCP. Hermetic tests reuse the worker
contract plus Claude-specific options and resume failures. Conditional tests
use a fake runtime child; credentialed tests cover Bash, child inheritance,
session state, continuation, cancellation and cleanup.

#### Stage 7 — `antigravity_sdk`

Move SDK policies/callbacks and `localharness` into the worker. Declare
session-private trajectory/app-data, read-only ADC, glibc/protobuf/runtime
checks, built-in subagents, and future MCP ownership. Preserve strict
conversation resume and cleanup of the runner-created trajectory root.
Hermetic tests cover dependency incompatibility and private-state ownership;
conditional tests cover fake harness and children. Credentialed Vertex
verification is required for file/shell tools, continuation, forced timeout,
and harness reaping.

#### Stage 8 — `xai_sdk`

Add the explicit `no_local_effects` adapter and an exact request-surface audit
to start validation. It must assert zero configured tools/callbacks, no local
state/process requirements, and an exact match to the versioned compatibility
evidence. A version-pinned hermetic subprocess release test must additionally
deny and observe filesystem mutation and process creation while exercising SDK
construction, serialized request transport to a local fixture, and close.
Exact request/source tests are authoritative for exposed tool paths; the deny
probe is authoritative for SDK/runtime local effects. An optional credentialed
model-only turn verifies provider transport but cannot replace either hermetic
gate or prove an unconfigured tool path. Any later local feature or dependency
version drift changes the adapter to `unsupported` until the audit is renewed
or the backend uses the generic worker. The user start does not rerun
Bubblewrap; it checks the runtime surface against the gated evidence. This
stage is last because it should consume the final capability vocabulary without
driving the worker architecture.

The order is therefore:

```text
codex_cli -> claude_cli -> xai_cli -> antigravity_cli
          -> codex_sdk -> claude_sdk -> antigravity_sdk -> xai_sdk
```

Each backend stage is independently mergeable and must update that backend's
README, health, settings, rollback behavior, and acceptance matrix. The stages
may ship together or behind an inactive default, but the built-in `read-only`
default does not activate in a production release until the shipped
agent/workflow readiness gate passes. No stage bundles several providers under
one credentialed acceptance claim.

Per-stage delivery details:

| Stage | Prerequisite and expected components | Visible behavior and adapter data | Required verification and rollback |
| --- | --- | --- | --- |
| 1 `codex_cli` | Current runner/config/API surfaces; add `sandbox/`, API/config migration, `codex_cli/sandbox.py`, runner/registry/reporting integration and matching `tests/`/`integration_tests/` | Explicit read-only requests enforce Codex workspace protection during staged development; adapter declares `CODEX_HOME`, direct shape, composite native bypass, env and compatibility | Hermetic common/adapter/route tests, conditional real boundary, credentialed complete Codex tool/state; explicit `none` is rollback and default activation waits for readiness |
| 2 `claude_cli` | Stage 1 launcher; add `claude_cli/sandbox.py`, health/settings/docs and focused tests | Claude becomes start-eligible for `read-only`; adapter declares `CLAUDE_CONFIG_DIR`, direct shape, skip-permissions and transient native sandbox mapping | Hermetic argv/state/admin-setting cases, conditional descendants, required credentialed Bash/state; unsupported remains fail-closed |
| 3 `xai_cli` | Stages 1–2 common seam; add `xai_cli/sandbox.py` and focused tests/docs | Grok becomes eligible; adapter declares `$GROK_HOME`, direct shape, bypass permission and sandbox-off mapping | Hermetic auth/config/terminal mapping, conditional boundary, required credentialed Bash/state; `none` only explicit rollback |
| 4 `antigravity_cli` | Common CLI seam proven by Stages 1–3; add `antigravity_cli/sandbox.py` and focused tests/docs | Antigravity becomes eligible; adapter declares `.gemini`/parent `HOME`, keyring external service, helper, add-dir paths and native profile | Hermetic zero-exit tool-failure and path cases, conditional structure, required credentialed keyring/helper/shell/state; incompatibility rejects |
| 5 `codex_sdk` | Stage 1 plus generic worker modules; add `codex_sdk/sandbox.py`, worker entrypoint/codec/supervisor and tests/docs | Codex SDK changes from unsupported to worker-enforced; adapter declares SDK worker, complete state/SQLite, runtime and SDK native options | Exhaustive fake-worker lifecycle, conditional fake runtime, required credentialed shell/continuation/timeout/reaping; rollback marks read-only unsupported |
| 6 `claude_sdk` | Stage 5 worker protocol; add `claude_sdk/sandbox.py` and focused worker/provider tests/docs | Claude SDK changes to worker-enforced; adapter declares complete state, runtime, callbacks/MCP ownership and native options | Hermetic resume/ambient-MCP/protocol cases, conditional fake runtime, required credentialed Bash/continuation/cancel/reaping; unsupported on rollback |
| 7 `antigravity_sdk` | Stage 5 worker protocol; add `antigravity_sdk/sandbox.py` and focused tests/docs | Antigravity SDK changes to worker-enforced; adapter declares private trajectory/app-data, ADC read, dependency/runtime checks and allow-all policy | Hermetic dependency/state/resume, conditional fake harness, required credentialed Vertex tools/continuation/timeout/reaping; unsupported on rollback |
| 8 `xai_sdk` | Final capability vocabulary and request audit; add `xai_sdk/sandbox.py` and focused tests/docs | Read-only start reports `not_applicable_no_local_effects`; adapter declares no state, native profile or local extensions | Version-pinned exact request plus mutation/child-deny subprocess audits are required, credentialed model transport optional; any surface drift immediately rolls support back to unsupported |

### Test strategy and verification matrix

Use four layers:

- **Hermetic unit/integration:** default and authoritative for policy
  resolution, specs, argv, path logic, protocol, lifecycle and failures.
- **Conditional Linux/Bubblewrap:** credential-free real namespace semantics;
  skip only with an explicit unavailable reason on unsupported environments.
- **Credentialed provider integration:** only provider startup/auth/native
  controls/tool execution/state/continuation facts that fakes cannot prove.
- **Manual probes:** retain version exploration, negative comparisons,
  alternative auth, managed config, and forensic provider behavior; do not put
  every paid or brittle probe in the regular suite.

| Assertion | Layer |
| --- | --- |
| policy default/override/request precedence; partial `none` vs gated target activation; project restrictions; migration | hermetic |
| shipped agent/workflow matrix has no unsupported default member before `read-only` activation | hermetic release gate |
| Linux engine gate activates `read-only`; macOS/Windows built-in remains `none`; explicit `read-only` still rejects there | hermetic platform/config plus conditional Linux readiness |
| legacy persisted session resolves to visible `none`; installation override rejects continuation | hermetic migration/session |
| adapter protocol accepted without backend-id imports or branches | hermetic contract |
| built-in mock resolves through its typed `no_local_effects` adapter and participates in readiness | hermetic registry/workflow |
| exact Bubblewrap argv including JSON-status fd, caps, spaces, cwd/env, deterministic order | hermetic |
| relative/nonexistent paths, safe state creation, ownership, broad-root/component-boundary-ancestor and symlink rejection, plus lexical-prefix workspace/writable siblings | hermetic |
| workspace-equal writable rejection; enclosing and nested mount discovery; all-inode intersection; supported ext4/xfs/tmpfs and rejected OverlayFS/Btrfs/network/FUSE types; audit limits | hermetic inode/mount/performance fixtures plus conditional supported mounts |
| settings, health, dry-run, CLI, MCP, REST and TUI projections | hermetic route/tool/UI |
| all-no-local-effects skips bwrap; mixed selection still checks every OS-enforced member | hermetic selection/preflight |
| missing bwrap, unsupported OS, failed preflight, unusable user namespace, missing children interface with pinned same-uid proc fallback, and fully unavailable proc identity | hermetic injected failures plus conditional real negative |
| JSON-lines `child-pid` reaper/bootstrap ancestry and `NSpid`, live stream plus final `exit-code`, zero capabilities; provider cannot exec before proof/ACK | hermetic proc/status readers plus conditional real marker |
| exact inherited-FD whitelist including transient enumeration and stdio-transfer fds; writable workspace/session FD cannot reach CLI or worker | hermetic spawn fixtures plus conditional `/proc/self/fd` |
| read-only `/`, workspace and `.git`; tracked reads; private scratch/state | conditional Bubblewrap |
| clean-env gitfile/worktree/bare/separate dirs, exact Git validation, missing/incompatible Git, strict alternate grammar/base/breadth-first recursion, exact duplicate/cycle provenance, ordinary in-workspace and bare roots absorbed by workspace-last, sibling `workspace.git` kept external by component-boundary comparison, external ancestor coverage collapse, protected anchors below writable state pruned then rebound read-only, external anchors above workspace, and nested scope reported | hermetic Git fixtures plus conditional Bubblewrap |
| frozen normalized Git coverage mapping drives argv and proof; every retained role/origin/logical destination maps to exactly one effective topmost workspace or external-anchor operation; logical-root identity/existence, recursive read-only submounts, and missing/extra operations are checked | hermetic plan/proof fixtures plus conditional Bubblewrap |
| writable destination equal to or a component-boundary descendant of an external Git coverage anchor is rejected; one or several protected anchors below writable state are accepted only with proved narrowing rebinds; the writable walk prunes the union of protected sides, covered descendant logical roots form no separate relation, hardlinks among protected sides are allowed, and a hardlink from their union into writable remainder is rejected | hermetic overlap/inode fixtures plus conditional Bubblewrap |
| scratch anchor below `/tmp`/`/var/tmp`/workspace is rejected; safe `$TMPDIR`, `/tmp`, `/var/tmp` remain private and cleaned | hermetic allocator plus conditional Bubblewrap |
| create/overwrite/delete/rename/chmod/timestamp/Git mutations blocked in the workspace and every external Git logical root | conditional Bubblewrap |
| runtime hard link from workspace bind into same-filesystem writable bind fails `EXDEV` and host content is unchanged | conditional Bubblewrap |
| nested host bind of workspace below writable state is rejected before provider exec | conditional Bubblewrap mount fixture |
| direct and nested remount/rebind cannot mutate workspace; private mask changes only inner view | conditional Bubblewrap |
| workspace symlink to ordinary host target blocked; symlink to writable state follows documented limitation | conditional Bubblewrap |
| direct action and child/grandchild mount inheritance | conditional Bubblewrap |
| timeout, stop, parser failure, crash, exact bwrap wait, PID-namespace descendant reaping and partial cleanup | hermetic process fixtures plus conditional Bubblewrap |
| worker framing, ids, `hello/open/ready/run` sequence, missing/repeated handshake, malformed/oversize/EOF/crash | hermetic fake worker |
| bounded worker queues, slow sink/backpressure failure and one ordered terminal result | hermetic daemon/shared-session |
| worker cancel/reset/close, terminal-after-cancel, daemon disconnect, forced namespace teardown | hermetic fake runtime plus conditional Bubblewrap |
| strict Codex/Claude/Antigravity continuation and no fresh fallback | hermetic provider fakes; credentialed two-turn proof |
| common prompt prefix for CLI, worker and no-local-effects shapes; `none` unchanged | hermetic runner/worker contract |
| exact bootstrap argv, descriptor-role map, challenge/hello/ACK framing, nonce transport, deadline, status JSON-lines lifetime/parser, and stdout/stderr separation | hermetic bootstrap/stream fixtures plus conditional engine control |
| native profile selected only after outer ACK | hermetic marker assertion |
| provider auth, mutable complete state, direct tool and real descendant | credentialed per backend |
| keyring/external auth, managed settings, new provider versions | manual probes unless promoted by deterministic evidence |
| xAI SDK exact zero-tool request plus filesystem-mutation/process deny probe | hermetic source/request and version-pinned subprocess; optional credentialed transport |
| provider-hosted remote filesystem behavior | out of scope; never asserted |
| host-service delegated write is labeled outside the boundary; known engine sockets warn without implying completeness | hermetic reporting plus manual service probe |

Ordinary tests must not require Bubblewrap, Linux, credentials, network, or a
provider install. Conditional tests use a separate test marker/entrypoint and
run in Linux CI where available. Credentialed tests remain under
`integration_tests/` and are explicitly selected. The existing manual probes
remain valuable for compatibility research and negative whole-state controls.

Every backend stage runs Ruff, the hermetic suite, generated API checks,
conditional boundary tests when available, and only its authorized
credentialed integration target. A green structural probe alone never turns a
backend capability on.

### Migration, rollout, observability, and failure behavior

The configuration schema migration adds outer fields centrally in
`config_migrations`; runtime consumes only the new schema. API schema changes
are additive. Old clients omitting `sandbox` receive the configured default.
Old clients that use `backend_options.*.sandbox` retain provider-native
meaning. Documentation must use “outer sandbox” whenever ambiguity exists.
Historical session records without the field normalize visibly to `none` and
never inherit the new default on a later turn. An installation `read-only`
override blocks their continuation rather than silently changing their
boundary.

Capability implementation is backend-by-backend, but production activation of
a platform's shipped default is atomic with respect to the shipped
agent/workflow selection matrix and that platform's engine. Before an adapter
is implemented, an explicit `read-only` start selecting that backend fails with
`outer_sandbox_unsupported`; `none` preserves existing behavior. Linux changes
its built-in default to `read-only` only after the readiness gate proves that no
shipped default selection contains an unsupported member and Bubblewrap is
available in the supported install path. macOS and Windows keep built-in
`none`; an explicit/global `read-only` request there still receives the normal
fail-closed unsupported-engine result. This prevents silent fallback, a
partial-rollout outage, and a cross-platform default regression. Operators can
still choose the visible global default `none` for rollback, but code never
auto-downgrades a requested or installation-enforced policy.

Health stores no namespace proof as authority. It reports the latest
credential-free observation and remediation. Start rechecks, launch proves,
and the successful session settings record only the sanitized effective fact.
Metrics/log categories should include policy source, support shape, preflight
phase, startup duration, cancellation escalation level, cleanup result, and
stable failure code. They must not include path values beyond approved display
forms.

Provider failure after a proven boundary remains a provider outcome. Namespace,
bootstrap, or worker-protocol failure remains an outer/worker outcome. A
partial provider message never changes a failed boundary outcome to completed.
No failure path retries unsandboxed, switches backend, creates a fresh strict
continuation, or broadens a mount.

### Product decisions and blockers

No unresolved product decision blocks implementation. The design makes these
choices explicitly:

- Linux-only Bubblewrap enforcement for direct/worker members; an
  all-`no_local_effects` selection is platform-independent and a mixed
  selection still rejects without every required OS boundary.
- Built-in `read-only` activates only on Linux after adapter and engine
  readiness; platforms without an engine retain built-in `none` while explicit
  `read-only` remains fail-closed.
- Complete writable provider state for the MVP.
- Operator path exceptions are global-user-only, not start fields; writable
  `/`, the user home, workspace paths, and component-boundary workspace
  ancestors are forbidden; lexical-prefix siblings do not overlap.
- Symlinked writable roots fail closed.
- The MVP filesystem allowlist is ext4/xfs/tmpfs after acceptance evidence;
  OverlayFS, Btrfs, network, union, FUSE, and unknown filesystems fail closed
  until a versioned identity algorithm and conditional fixtures are added.
- Session-root Git protection includes bare repositories and recursive local
  alternates; canonical duplicates and contained roots retain provenance while
  mapping to one deterministic coverage operation, and a repository candidate
  without compatible Git fails closed.
- SDK local execution uses a complete out-of-process worker.
- `xai_sdk` uses a revocable `no_local_effects` capability, not a nominal
  Bubblewrap claim.
- Backend adapters are implemented incrementally, but the shipped `read-only`
  default activates only after the default agent/workflow readiness gate.

Future decisions, outside issue #43, are which macOS/Windows engines to add,
whether to minimize or copy-on-write provider state, and whether to add
network/resource/syscall isolation. They do not need answers before Stage 1.

## MVP boundary decision (2026-07-25)

This section records the product-boundary rationale. The authoritative design
above controls exact ordering and all implementation details.

The first implementation should enforce a **read-only workspace**, not claim
that the entire provider process is incapable of persistent host writes.

To avoid provider-specific copies, overlays, and per-file knowledge, mount the
selected backend's complete state root writable and persistent. For Codex this
is the effective `CODEX_HOME` (normally `~/.codex`). Other CLI backends declare
their equivalent top-level state root rather than teaching the launcher which
individual databases, locks, caches, configuration files, or credential files
need writes.

The intended mount order is:

1. visible host filesystem read-only;
2. private writable scratch space;
3. normalized readable declarations;
4. the selected backend state root and authorized operator paths writable at
   their real paths;
5. every normalized external session-root Git coverage anchor read-only again,
   including any that narrows a writable root; and
6. the resolved workspace mounted read-only again as the final, more-specific
   bind, covering all in-workspace Git roots and bare-repository roles.

This is deliberately simpler than a synthetic provider home or copy-on-write
overlay and accommodates provider upgrades that add new mutable state. Its
cost is an explicitly weaker boundary: the model and every command it launches
can modify the complete provider state root, including credentials,
configuration, rules, plugins, histories, and databases. A workspace symlink
whose target is inside that writable root can also modify the target. The
boundary protects repository objects and `.git`; it does not protect declared
provider state.

Session settings, dry-run output, documentation, and the filesystem-policy
prompt must report both facts separately:

```text
Workspace access: read-only (OS-enforced)
Provider state: persistent writable (<backend-owned root>)
Scratch: writable and ephemeral ($TMPDIR)
```

Do not label this posture “host read-only” or “no persistent writes.”
Ephemeral provider homes, read-only credential binds, and copy-on-write
provider state remain possible defense-in-depth follow-ups after the simpler
workspace boundary ships.

## Required security properties

The design and tests must state exactly what is protected. For the initial
read-only posture:

- The supplied workdir and the session-root repository's resolved Git metadata
  and local object stores are readable but not writable. Every logical Git root
  retains its role and provenance and maps to exactly one effective, topmost,
  proved workspace or external-anchor coverage mount. Creating, replacing,
  renaming, deleting, chmodding, or changing timestamps there must fail.
- A child process or shell started by the provider inherits the same boundary.
- Symlinks resolved within the workspace mount remain protected. A symlink
  targeting a declared writable provider-state root can modify that external
  target and is a documented limitation of the MVP.
- Any host filesystem visible outside the workdir is read-only unless a path
  is deliberately mounted writable as backend state or private scratch.
- Writable scratch space is isolated from the repository. CLI scratch is
  discarded after its turn; SDK-worker runtime scratch is session-private and
  discarded only after worker close/crash and namespace teardown. Reusing the
  host's shared `/tmp` as an unrestricted writable mount is not sufficient.
- Provider credentials may be readable where required, but must not be copied
  into the repository, transcript, event payloads, or a new persistent store.
- The effective backend state root is writable and persistent. The path is
  backend-declared, resolved explicitly, and reported in session settings; the
  launcher does not infer a whole writable user home.
- If the requested enforcement boundary cannot be established, the read-only
  turn fails before launching the provider. It must not silently fall back to
  prompt-only or provider-only controls.
- Writable execution remains an explicit, visible opt-in in normalized session
  settings and dry-run output.
- Prompt augmentation is selected by the effective sandbox enum value, not by
  the engine. `"read-only"` receives a short, mechanically generated
  filesystem-policy preamble. `"none"` receives no sandbox text.

This boundary is for filesystem write prevention. It is not, by itself, a
complete untrusted-code container: network isolation, credential
confidentiality, CPU and memory limits, syscall filtering, and protection from
reading sensitive host files are separate concerns unless the final design
explicitly adds them.

## Sandbox-policy prompt augmentation

The sandbox and the agent must receive one authoritative resolved workspace
description. Common policy code owns prompt augmentation; an adapter may add a
bounded provider-specific note but cannot disable the policy block. Each
sandbox enum value owns its augmentation:

| Effective sandbox and per-agent enforcement | Prompt augmentation |
| --- | --- |
| `"read-only"` + `os_enforced` | prepend the filesystem-policy block below with the exact workspace, cwd, and scratch facts |
| `"read-only"` + `not_applicable_no_local_effects` | state that no local file/tool execution surface or local scratch is exposed; do not claim OS enforcement |
| `"none"` | none; preserve the provider prompt unchanged |

Every future enum value must explicitly define its prompt augmentation, which
may be empty. This keeps behavior tied to the policy's meaning rather than to
Bubblewrap or another enforcement engine.

For `"read-only"`, agent-collab should prepend a small block similar to:

```text
FILESYSTEM POLICY
Workspace root: <resolved-session-workdir>
Current directory: <resolved-agent-cwd>
Access: OS-enforced read-only.
Use $TMPDIR (<sandbox-temp-path>) for temporary files and command output.
Do not attempt workspace edits; describe proposed changes in your response.
```

The exact text remains a design detail, but its facts do not:

- the workspace root, effective cwd, mount destination, provider cwd, and
  provider workspace flag must derive from the same resolved path object;
- the sandbox should preserve the real absolute workspace path rather than
  introducing an unexplained `/workspace` translation;
- when an agent-specific `cwd` differs from the session root, both paths are
  named explicitly;
- the temporary path in the preamble is the same path supplied through
  `TMPDIR` and mounted writable inside Bubblewrap;
- the common runner injects the appropriate preamble on every CLI and SDK turn
  whose effective `sandbox` value is `"read-only"`, regardless of user prompt
  wording; it renders after the per-agent enforcement and runtime scratch are
  known;
- an SDK worker receives the already augmented delta in `run`; it does not
  independently recompute policy paths or prepend the block to saved history;
- an audited `no_local_effects` agent receives only its accurate no-local-
  surface form and no nonexistent `$TMPDIR` promise;
- `sandbox = "none"` adds no sandbox text;
- a future policy cannot reuse the read-only text implicitly and must define
  its own optional augmentation; and
- tests assert the preamble from normalized launch facts instead of matching a
  path recomputed separately in prompt construction.

The preamble improves tool behavior and avoids confusing failed writes, but it
is not part of the security proof. Bubblewrap must still reject a write when a
model ignores the instruction.

## Provider-native controls under the outer sandbox

The outer `"read-only"` policy should remove provider-native approval friction
when the backend supports doing so safely. A backend may select a verified
headless profile that bypasses permission prompts and disables or relaxes its
own filesystem sandbox, because the outer boundary—not the provider prompt
gate—is responsible for workspace integrity.

This is capability-gated, not a generic argv assumption:

- A CLI backend qualifies when its complete provider process and descendants
  run inside the outer sandbox and it exposes documented non-interactive
  permission/sandbox controls.
- An SDK backend qualifies only if all local tools it invokes are also inside
  the outer sandbox. An SDK auto-approval option alone is insufficient if tool
  execution remains in the unsandboxed daemon process or an unwrapped child.
- A backend that exposes only some controls relaxes only those verified
  controls. Missing controls are left unchanged.
- A backend that cannot contain all local execution does not support
  `sandbox = "read-only"` yet and fails closed before starting a turn.
- `sandbox = "none"` does not itself relax provider-native controls. Provider
  options retain their independently configured behavior.

In the current architecture, SDK runners execute in the daemon process rather
than through `SubprocessRunner`. The initial Bubblewrap subprocess seam
therefore contains CLI backends only. An SDK needs a proven containment path,
such as an out-of-process SDK worker launched inside the outer sandbox or a
provider-supported tool-executor hook that wraps every local operation, before
it can advertise outer `"read-only"` support.

Each backend owns the mapping from the effective outer policy to its native
profile. For example, one CLI may have a combined
approval-and-sandbox-bypass flag while another has separate permission and
sandbox options. The shared launcher must not infer provider flags or branch
on backend ids.

The permissive provider command must be the inner command of the already
validated outer launch. If namespace setup fails, that command must never
execute. Effective settings and dry-run output report both the outer policy
and any provider-native controls changed by its backend mapping.

This improves headless usability but does not expand the outer sandbox's
security claim. With approvals bypassed, the model can still read visible host
files, use inherited network access, and modify the declared writable provider
state root. Those are explicit MVP limitations.

## Historical design investigation

This outline predates the authoritative implementation design above. It is
retained to show which questions drove the probes, not as an implementation
plan.

Before changing production behavior, run and record a focused Bubblewrap spike
on a supported Linux host.

### 1. Establish the minimum namespace

Prototype a launch that:

- makes the visible host filesystem read-only by default;
- provides the required `/proc`, `/dev`, executable, library, certificate, and
  name-resolution views;
- gives the child a private writable temporary directory;
- overlays the selected backend's declared state root as writable;
- preserves the resolved working directory and command lookup behavior; and
- terminates the namespace with the supervised child.

Do not settle on a broad writable home-directory bind. Record the one
backend-owned state root, how custom environment/config values relocate it,
and what sensitive persistent content it contains. Per-file mutability inside
that root is intentionally outside the MVP contract.

The spike must cover at least:

- an ordinary read command;
- writes, deletes, renames, metadata changes, and Git mutations in the workdir;
- a write attempted through a symlink;
- a child shell attempting the same writes;
- provider startup with its normal authentication state;
- a workdir below a configured relative or absolute agent `cwd`;
- a path containing spaces;
- cancellation and forced termination; and
- failure when `bwrap` is absent or user namespaces are unavailable.

### 2. Decide availability and platform policy

Determine and document:

- how `bwrap` is discovered and health-checked;
- whether Linux installations require it, recommend it, or report the backend
  unavailable until it is installed;
- the exact fail-closed behavior when Bubblewrap exists but cannot create a
  user namespace, including common container and distribution restrictions;
- whether an explicit writable Antigravity session may run without Bubblewrap;
- how macOS and Windows report the unavailable hard-boundary capability; and
- whether running agent-collab inside an existing container or sandbox needs a
  supported escape hatch or a distinct implementation.

### 3. Configuration contract

Provider controls and agent-collab's enforcement control must not be presented
as equivalent. The caller-facing start option is a backend-neutral string enum:

```json
{
  "sandbox": "read-only"
}
```

`"read-only"` is the default. Normal callers omit `sandbox` entirely and
receive the OS-enforced read-only workspace boundary. The only other initial
value is:

```json
{
  "sandbox": "none"
}
```

`"none"` explicitly disables agent-collab's outer sandbox. It does not disable
or reinterpret provider-native permission modes or sandboxes. The string enum
leaves room for additional policies later without changing the field's shape;
no additional modes are required for the first implementation.

The sandbox engine is daemon configuration, not a start option and not
caller-visible API. For the initial Linux implementation, configuration
selects Bubblewrap and the daemon resolves `sandbox = "read-only"` into the
Bubblewrap launch policy. Callers should not need to know which engine enforces
the requested policy. Normalized session settings and diagnostics report the
effective policy and whether it was established, but need not expose the
engine as part of the stable start contract.

Users do not need to add sandbox policy configuration. The shipped code-level
configuration contains the required default:

```toml
[system]
sandbox_default = "read-only"
```

Configuration merging therefore guarantees that the effective
`sandbox_default` is always present. Global user configuration may override
it, for example:

```toml
[system]
sandbox_default = "none"
```

The separate `sandbox_override` remains optional. It accepts the same
`"read-only"` and `"none"` values and can lock every omitted or matching start
to read-only:

```toml
[system]
sandbox_override = "read-only"
```

The effective `sandbox_default` supplies the value when the caller omits
`sandbox`, but an explicit caller value still wins when no override is
configured. `sandbox_override` is an installation constraint: it supplies the
effective value when the caller omits `sandbox`, accepts a matching explicit
value, and rejects a conflicting explicit value. Distinct names let operators
distinguish default behavior from enforcement.

Both fields are global-user/daemon policy. Project configuration cannot set or
weaken them.

Resolution is:

| Effective default | Configured override | Start field | Result |
| --- | --- | --- | --- |
| required value | absent | omitted | effective default |
| required value | absent | explicit valid value | requested value |
| required value | set | omitted | configured override |
| required value | set | same explicit value | configured override |
| required value | set | different explicit value | start rejected |

A configured override therefore does not silently replace a conflicting
explicit request. Start validation rejects the mismatch before creating a
session or launching any provider and identifies the requested value and the
installation policy. This lets an operator lock an installation to
`"read-only"` while callers that omit `sandbox` continue to work normally.
When both configuration fields are present, the override necessarily controls
omitted starts and the effective `sandbox_default` has no effect until the
override is removed.

Normalized settings report the effective value and whether it came from the
request, effective configured default, or installation override.

The outer option is resolved and reported independently of provider mode.
Deriving the hard boundary solely from `mode = "plan"` would conflate two
controls. After resolving the outer policy, however, a backend may derive its
verified non-interactive native profile as described above. A provider mode
that expects writes while `sandbox = "read-only"` remains constrained by the
outer boundary; `sandbox = "none"` removes only the outer boundary and does
not automatically weaken provider-native controls.

The contract must also define how extra provider-visible directories are
handled. Antigravity currently receives one resolved `--add-dir`, but user
configured arguments could add others. Unknown or unvalidated extra paths must
not silently become writable.

## Historical proposed implementation shape

Superseded by **Authoritative implementation design**. The principles here
remain useful evidence, but its stage boundaries are not current.

The launch internals remain a direction to validate; the caller-facing
`sandbox` contract above is settled.

1. Add a backend-neutral `SandboxPolicy` value and `SandboxLauncher`. The
   launcher owns Bubblewrap discovery/preflight, private scratch, mount order,
   environment additions, prompt augmentation, fail-closed setup, and wrapping
   the complete inner provider command.
2. Define a small backend `SandboxAdapter` protocol or equivalent immutable
   specification. Each CLI backend supplies one through its existing
   `create_cli_runner` path. It owns:
   - which outer sandbox modes the backend supports;
   - resolution of the backend's persistent writable state root;
   - mapping an effective outer mode to provider-native approval, permission,
     and sandbox switches;
   - sanitized reporting of those native controls; and
   - any backend-specific compatibility validation.
3. Keep both directions free of provider leakage. Common sandbox code never
   checks a backend id or edits Claude/Codex/Antigravity/Grok arguments.
   Backend code never constructs Bubblewrap mounts or implements generic
   sandbox policy. The backend builds or transforms the inner provider command;
   the common launcher wraps that result.
4. Wire the adapter into the shared CLI runner/factory rather than duplicating
   sandbox execution in every backend. Conceptually:

   ```text
   backend command builder + SandboxAdapter
                       |
                       v
   shared SubprocessRunner / SandboxLauncher
                       |
                       v
   Bubblewrap argv + inner provider command
   ```

   A backend without an adapter advertises no outer-sandbox support and fails
   closed when a non-`"none"` policy is requested.
5. Add the Linux Bubblewrap engine behind `SandboxLauncher`. Keep namespace
   construction out of every provider argv builder so dry runs, timeout
   handling, stdout/stderr parsing, cancellation, and terminal outcome logic
   remain shared.
6. Default an omitted start `sandbox` field to `"read-only"` and resolve the
   installation override and effective policy during start validation, before
   backend option normalization/runner construction. Do not inspect prompt
   text. Let the resolved policy build its optional prompt augmentation from
   the same normalized workspace and scratch facts used by the launcher.
   Persist a sanitized summary in session settings so operators can
   distinguish behavioral mode, provider sandbox, outer filesystem
   enforcement, and the effective policy source.
7. Make preflight failure a structured start or turn failure with targeted
   remediation. Do not report it as a provider empty-response failure.
8. Apply the default policy at the shared subprocess boundary. Each supported
   CLI backend declares its persistent writable state root and receives the
   same outer policy; provider-specific integration must not change the
   caller-facing default. A backend that cannot establish the requested policy
   fails closed rather than silently launching unsandboxed.

The design should not put Bubblewrap-specific branching into the referee or
provider-specific branching into common sandbox code. The referee owns
orchestration; launch enforcement belongs at the runner-owned common launch
layer; provider differences belong in backend packages.

## Historical investigation stages

These stages describe how feasibility evidence was gathered. Production must
follow the backend-by-backend sequence in the authoritative design.

### Stage 1: Evidence and decision record

- Completed for the four CLI backends and all four SDK backends on the tested
  Linux host. The tracked probes record command shapes, state requirements,
  failure stages, and local execution ownership.
- Keep the remaining engine-availability, fallback, extra-directory, worker
  protocol, and cross-platform decisions explicit before implementation.
- Update issue #43 if the acceptance criteria or user-visible scope changes.

### Stage 2: Launch boundary

- Introduce the launch-policy seam with no behavior change for existing
  runners.
- Add contract tests proving the common launcher consumes backend adapters
  without importing or branching on concrete backend packages.
- Add hermetic tests for argv composition, cwd and environment preservation,
  enum-specific prompt augmentation, dry-run/settings reporting, preflight
  errors, and process termination.
- Keep the provider prompt after any policy-owned prefix, and event parsing,
  byte-for-byte unchanged.

### Stage 3: Bubblewrap enforcement

- Implement the Linux read-only policy with narrow writable overlays.
- Add OS-level tests for the required security properties. Tests may skip only
  when the platform cannot support Bubblewrap; the normal Linux CI path should
  exercise the real boundary so a command-construction-only test cannot create
  false confidence.
- Verify that failed setup never starts the inner provider command.

### Stage 4: Backend integration

- Make omitted `sandbox` resolve to `"read-only"` for every supported
  subprocess-backed session, including Claude, Codex, Antigravity, and Grok
  CLI backends as their probes qualify them.
- For each CLI backend, verify and apply the non-interactive provider-native
  profile used under outer `"read-only"` so ordinary inspection commands do
  not wait for approvals.
- Mark an SDK backend as outer-sandbox capable only after proving that its
  local tool execution is contained; otherwise reject `"read-only"` for that
  backend rather than relaxing its native controls.
- Implement `sandbox = "none"` as the explicit opt-out from the outer boundary
  and reject unknown enum values.
- Add global-user-only `[system].sandbox_override` validation and reject
  explicitly requested values that conflict with it before session creation.
- Add required `SystemConfig.sandbox_default`, populated as `"read-only"` by
  the built-in config layer and overridable only by global user config, so the
  merged effective configuration always contains it.
- Require each supported CLI backend to declare and test its writable state
  root. Update backend READMEs, agent configuration documentation, install
  readiness/remediation output, and regression tests.

### Stage 5: Credentialed verification

- Run a normal headless Antigravity review that uses inspection commands
  without an interactive prompt.
- Verify in the same effective configuration that attempts to write the
  repository and `.git` fail at the OS boundary.
- Record only sanitized behavioral evidence; do not commit credentials,
  provider payloads, or machine-specific paths.

## Historical verification sketch

Hermetic and Linux boundary coverage should include:

| Case | Expected result |
| --- | --- |
| Read a tracked file and run `git status --short` | succeeds |
| Create, overwrite, rename, or delete inside workdir | denied |
| Write inside `.git` or run a mutating Git command | denied |
| Write through a workdir symlink | denied |
| Spawn a child that writes to workdir | denied |
| Write to private scratch space | succeeds and is later discarded |
| Write to an unapproved host path | denied |
| Read credentials and update declared provider state | succeeds and persists |
| Workspace symlink targets declared provider state | target is writable; limitation is reported |
| `bwrap` missing or namespace creation denied | fails closed with remediation |
| Omitted `sandbox` with shipped config | resolves to `"read-only"` |
| Explicit `sandbox = "none"` | no outer boundary or read-only claim; choice visible in settings |
| Unknown `sandbox` value | rejected during start validation |
| Override set; start field omitted | override is effective |
| Override set; explicit start value matches | accepted |
| Override set; explicit start value conflicts | rejected before session creation |
| User overrides default; no override or start value | effective default is used |
| User overrides default; explicit start value | explicit value is used |
| Project config sets a default or override | ignored as forbidden daemon-global policy |
| Turn timeout, stop, and kill escalation | inner process and namespace are reaped |
| OS-enforced read-only prompt | exact workspace, cwd, and `$TMPDIR` policy is prepended |
| `no_local_effects` read-only prompt | accurately reports no local file/tool or scratch surface |
| `sandbox = "none"` prompt | no sandbox policy text is added |
| Future sandbox enum value | its own optional prompt augmentation is defined and tested |
| Capable CLI exposes bypass controls | relaxed only inside outer boundary |
| Backend lacks one native bypass control | unsupported control remains unchanged |
| SDK auto-approves but tool execution is not contained | read-only start fails closed |
| Outer namespace setup fails | permissive inner provider command never executes |
| `sandbox = "none"` | does not automatically relax provider-native controls |
| Model ignores preamble and writes anyway | OS boundary still denies the write |

The full hermetic suite, Ruff checks, and generated documentation checks must
remain green.

## Questions resolved by the authoritative design

The investigation originally left the following questions open. The
authoritative design resolves them as follows:

- Keyrings are reported as external services and are not writable filesystem
  exceptions.
- Persistent provider roots must exist; only adapter-declared private session
  roots may be created with owner-only, no-symlink rules.
- Provider extra-directory flags are parsed by adapters and normalized by
  common path code; writable access requires global operator authorization.
- Typed `support` and `enforcement` fields report outer capability separately
  from provider-native options.
- A length-prefixed, supervised SDK-worker protocol preserves streaming,
  strict continuation, reset, close, cancellation, and process-tree ownership.
- `xai_sdk` advertises a revocable `no_local_effects` capability.
- macOS and Windows reject a `read-only` selection containing an OS-enforced
  member until another engine is implemented; an all-`no_local_effects`
  selection does not require Bubblewrap.

## Done when

- Every backend selected by shipped default agents and workflows is either
  contained by the common boundary or positively audited `no_local_effects`
  before the built-in `read-only` default activates.
- The effective default Antigravity CLI review can execute ordinary inspection
  commands headlessly without an approval prompt.
- The workdir and the session-root repository's resolved worktree/bare,
  common, object, and local-alternate metadata are protected by an independently
  verified OS boundary through the deterministic normalized coverage mapping
  against direct filesystem operations by the provider and its child processes.
  Nested-repository external metadata, symlinks into declared provider state,
  and writes delegated to external host services follow the documented weaker
  MVP guarantee.
- Provider-native mode/sandbox controls and agent-collab's outer enforcement
  are represented separately and documented accurately.
- Read-only-capable backends use verified non-interactive native profiles only
  inside the established outer boundary; uncontained SDK execution fails
  closed.
- The selected backend's persistent writable state root is declared, resolved,
  tested, and visibly distinguished from the read-only workspace.
- Every outer-read-only turn receives an accurate policy preamble. OS-enforced
  turns name workspace, effective cwd, and writable temporary location;
  `no_local_effects` turns state that no local file/tool or scratch surface is
  exposed.
- Prompt augmentation is defined and tested per sandbox enum value;
  `sandbox = "none"` leaves the provider prompt unchanged.
- Missing or unusable enforcement fails closed with actionable remediation.
- Disabling the outer boundary requires explicit `sandbox = "none"` and is
  auditable, either in the start request or as an installation override.
- A global-user `[system].sandbox_override = "read-only"` prevents callers and
  project configuration from starting an unsandboxed session.
- Focused hermetic and real Bubblewrap boundary tests cover the common behavior;
  every OS-enforced backend passes its own authorized credentialed acceptance,
  and `xai_sdk` passes the versioned no-local-effects deny audit.
