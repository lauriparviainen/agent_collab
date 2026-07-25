# Enforce read-only Antigravity CLI reviews with an outer sandbox

**Status:** Open — core policy decisions plus all four CLI and both SDK
feasibility probes are recorded; production implementation remains.

**Created:** 2026-07-25.

**Issue:** [#43](https://github.com/lauriparviainen/agent_collab/issues/43)

## Purpose

Make the default headless Antigravity review posture both usable and
OS-enforced read-only. The model must be able to run ordinary inspection
commands without an interactive permission prompt, while writes to the
supplied repository fail independently of the prompt, model behavior, and
Antigravity's own permission implementation.

The first implementation target is Linux with Bubblewrap (`bwrap`). The design
should leave a clean seam for other CLI backends and operating systems, but
this task does not claim cross-platform isolation before those implementations
exist.

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

The tracked `probes/bubblewrap_antigravity_sdk` and
`probes/bubblewrap_xai_sdk` harnesses extend the execution-ownership research
to both SDK backends. They distinguish a complete SDK worker placed inside
Bubblewrap from the current production architecture, where SDK runners live in
the daemon.

The explicit support decisions are:

| Backend | Decision | Reason |
| --- | --- | --- |
| `antigravity_sdk` | **feasible with out-of-process worker** | A complete standalone SDK worker, bundled runtime, local action, and action child were contained successfully. The current production runner and runtime instead share the unsandboxed host namespace. |
| `xai_sdk` | **no local tool execution** | The current backend constructs a model-only request with no built-in or custom tools and performs no local provider-state writes or child launches. |

Neither result implements production sandboxing. In particular,
`antigravity_sdk` must not advertise outer `read-only` support until the whole
runner is moved across a supervised worker boundary. The xAI decision must be
revisited if that backend later enables provider built-ins or client-executed
function tools.

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

### SDK probe-change verification

Both new probes compiled and passed Ruff lint/format checks. All meaningful
structural modes passed after formatting. The authorized Antigravity
comparisons passed their positive and deliberate current-architecture
controls; the state and timeout negatives returned nonzero at the expected
stages. The xAI credentialed comparison was not started because its API key
was unavailable.

| Command | Outcome |
| --- | --- |
| `python3 -m py_compile probes/bubblewrap_antigravity_sdk/probe_bubblewrap_antigravity_sdk.py probes/bubblewrap_xai_sdk/probe_bubblewrap_xai_sdk.py` | Passed |
| Antigravity SDK `--preflight-only --state-mode writable` | Passed all direct, child, state, and host assertions |
| Antigravity SDK `--preflight-only --state-mode read-only` | Passed all assertions, including blocked provider-state writes |
| Antigravity SDK wrapped worker, writable state, `gemini-2.5-flash` | Passed the credentialed tool turn; caller, harness, action, and child were contained |
| Antigravity SDK current-architecture control, `gemini-2.5-flash` | Passed the deliberate negative proof: the file write reached the host and the harness shared the host namespace |
| Antigravity SDK wrapped worker, read-only state | Expected nonzero: trajectory database creation failed before model/tool dispatch |
| Antigravity SDK wrapped worker, one-second timeout | Expected nonzero: process group killed and recorded harness descendant reaped |
| xAI SDK `--preflight-only` | Passed the model-only request, no-child, namespace, and host assertions |
| xAI SDK credentialed comparison | Blocked before launch: `XAI_API_KEY` unavailable |
| Probe Ruff lint and format checks | Passed |
| `git diff --check` | Passed |
| `./agent_collab_dev.sh test` | Passed 1,196 tests with one skip |

No probe marker, generated bytecode, credential data, provider transcript,
session identifier, machine-specific path, or raw namespace identifier was
retained.

## MVP boundary decision (2026-07-25)

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
3. the selected backend state root writable at its real path; and
4. the resolved workspace mounted read-only again as the final, more-specific
   overlay.

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

- The supplied workdir, including `.git`, is recursively readable but not
  writable. Creating, replacing, renaming, deleting, chmodding, or changing
  timestamps must fail.
- A child process or shell started by the provider inherits the same boundary.
- Symlinks resolved within the workspace mount remain protected. A symlink
  targeting a declared writable provider-state root can modify that external
  target and is a documented limitation of the MVP.
- Any host filesystem visible outside the workdir is read-only unless a path
  is deliberately mounted writable as backend state or private scratch.
- Writable scratch space is isolated from the repository and discarded after
  the turn. Reusing the host's shared `/tmp` as an unrestricted writable mount
  is not sufficient.
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
description. Each sandbox enum value owns its optional prompt augmentation:

| Effective sandbox | Prompt augmentation |
| --- | --- |
| `"read-only"` | prepend the filesystem-policy block below |
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
- the preamble is injected on every stateless CLI turn whose effective
  `sandbox` value is `"read-only"`, regardless of user prompt wording;
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

## Design investigation

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

## Proposed implementation shape

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

## Implementation stages

### Stage 1: Evidence and decision record

- Completed for the four CLI backends and both SDK backends on the tested
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

## Verification matrix

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
| Read-only prompt | exact workspace, cwd, and `$TMPDIR` policy is prepended |
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

## Open questions

- How should Antigravity's OS-keyring service access be reported and
  compatibility-checked without treating it as part of the writable
  filesystem state root?
- How should a missing backend-declared state root be created safely before
  Bubblewrap mount construction?
- How are user-supplied extra `--add-dir` values discovered, normalized, and
  mounted?
- What capability or health signal communicates hard read-only support to the
  daemon, MCP clients, CLI, and TUI without overloading provider `sandbox`?
- What backend-neutral worker/event protocol preserves SDK streaming,
  continuation identity, cancellation, and cleanup while placing the complete
  `antigravity_sdk` runner outside the daemon?
- Should the model-only `xai_sdk` backend use the common worker boundary for
  policy consistency, or advertise a distinct no-local-effects capability
  until a local tool executor is added?
- What are the macOS and Windows equivalents, and until they exist should
  read-only Antigravity be unavailable or run in a command-denied fail-closed
  mode on those platforms?

## Done when

- The effective default Antigravity CLI review can execute ordinary inspection
  commands headlessly without an approval prompt.
- The workdir and `.git` are protected by an independently verified OS
  boundary, including against child processes. Symlinks into declared
  provider state follow the documented weaker MVP guarantee.
- Provider-native mode/sandbox controls and agent-collab's outer enforcement
  are represented separately and documented accurately.
- Read-only-capable backends use verified non-interactive native profiles only
  inside the established outer boundary; uncontained SDK execution fails
  closed.
- The selected backend's persistent writable state root is declared, resolved,
  tested, and visibly distinguished from the read-only workspace.
- Every outer-read-only turn receives an accurate filesystem-policy preamble
  naming its workspace, effective cwd, and writable temporary location.
- Prompt augmentation is defined and tested per sandbox enum value;
  `sandbox = "none"` leaves the provider prompt unchanged.
- Missing or unusable enforcement fails closed with actionable remediation.
- Disabling the outer boundary requires explicit `sandbox = "none"` and is
  auditable, either in the start request or as an installation override.
- A global-user `[system].sandbox_override = "read-only"` prevents callers and
  project configuration from starting an unsandboxed session.
- Focused hermetic, real Bubblewrap boundary, and credentialed Antigravity tests
  cover the intended behavior.
