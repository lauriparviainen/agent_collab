# Bubblewrap + Grok CLI read-only probe

This manual Linux probe checks the proposed outer-sandbox shape with the Grok
Build CLI before it is added to agent-collab's runners. Its default `writable`
state mode gives `grok`:

- a private temporary workspace whose real absolute path contains spaces and
  is mounted read-only;
- that same path as the host cwd, Bubblewrap cwd, Grok `--cwd`, prompt
  workspace, and shell-reported `pwd`;
- the invoking user's `HOME` through the read-only root mount;
- the complete effective `$GROK_HOME` provider-state root as the one persistent
  writable exception;
- private writable scratch through `TMPDIR`, `TMP`, `TEMP`, and the XDG
  cache/config/data/state variables;
- `--permission-mode bypassPermissions`, the configured `xai_cli`
  non-interactive approval-bypass mode; and
- `--sandbox off`, the documented built-in profile that disables Grok's native
  Landlock sandbox while Bubblewrap enforces the boundary.

Grok's provider-owned root is `$GROK_HOME`, defaulting to `~/.grok`. It
contains cached authentication, sessions, search indexes, configuration,
skills, plugins, hooks, custom sandbox definitions, and related mutable state.
Authentication may instead use `XAI_API_KEY`, a configured environment key, or
an external authentication provider. The probe checks only path and
environment presence and never reads credential values or configuration
contents.

The complete selected state root is writable in the positive control. This
means Grok and every command it launches can modify sensitive provider state.
The outer boundary protects the workspace, general home, and other visible
host paths; it does not protect `$GROK_HOME`.

The probe creates one guarded provider-specific marker in the selected state
root and one guarded marker target in general home. It refuses to overwrite
either and removes both in `finally`, including after provider failure or
timeout. All other action files are below the private temporary fixture, never
the repository containing this probe. Host verification reads a private
scratch result file, so provider prose alone cannot make a run pass.

## Requirements

- Linux with `bwrap` on `PATH` and usable user namespaces.
- The configured Grok Build CLI command on `PATH` (`grok` by default).
- Existing authentication under `$GROK_HOME`, or another configured supported
  authentication method.
- Network access for the Grok model call.

The credentialed calls may consume provider usage. Run both structural modes
first:

```bash
python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py \
  --preflight-only --state-mode writable
python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py \
  --preflight-only --state-mode read-only
```

Run the real writable-state probe and read-only comparison with:

```bash
python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py \
  --state-mode writable
python3 probes/bubblewrap_grok/probe_bubblewrap_grok.py \
  --state-mode read-only
```

Use `--model MODEL_ID`, `--state-root PATH`, `--grok COMMAND`,
`--bwrap COMMAND`, or `--timeout SECONDS` for explicit overrides. The probe
returns nonzero for provider failure, timeout, missing or repeated action
execution, or any failed filesystem assertion.

The expected host verification for writable state is:

```text
action script ran exactly once   PASS
cwd matches authoritative path   PASS
tracked input readable           PASS
workspace write blocked          PASS
protected host write blocked     PASS
general home write blocked       PASS
provider state write allowed     PASS
scratch write allowed            PASS
```

The read-only structural control changes only the provider-state expectation
to `provider state write blocked`.

## Observed result

On 2026-07-25, both structural modes passed with Bubblewrap 0.6.3. The real
writable-state turn passed with Grok Build 0.2.111:

- existing cached authentication was found without copying or reading
  credential contents;
- the ordinary Bash action ran without an approval prompt;
- host-side evidence confirmed the exact workspace/cwd identity and readable
  tracked input;
- workspace, protected-host, and general-home writes were blocked;
- provider-state and private-scratch writes succeeded; and
- both guarded real-host marker targets were absent after cleanup.

The real `--state-mode read-only` comparison failed before the model or action
script. Startup attempted SQLite WAL/search-index maintenance and then could
not create the new local session, returning a read-only-filesystem error and a
nonzero exit.

Writable complete `$GROK_HOME` state is therefore required for this tested
version. During the successful control, a warning-only attempt to create a
legacy session folder below the shared system temporary directory was blocked;
the model turn and action still completed. No writable persistent path outside
the selected state root was required.

Primary provider references:

- <https://docs.x.ai/build/settings>
- <https://docs.x.ai/build/cli/headless-scripting>
- <https://docs.x.ai/build/features/permissions>
- <https://docs.x.ai/build/features/sandbox>

## What this does not prove

- Managed configuration under `/etc/grok`, project `.grok` configuration,
  custom hooks, MCP servers, plugins, external auth providers, token refresh,
  and future CLI versions may introduce additional compatibility requirements.
- The read-only root remains readable, and network access is inherited.
  Credential confidentiality and exfiltration prevention are separate work.
- Grok's native `off` profile is used only because Bubblewrap is already the
  outer boundary. A future managed requirement may pin a different profile.
- It tests one installed Linux/Bubblewrap combination, not other
  distributions, containers, macOS, or Windows.

Summarize only sanitized findings in
`antigravity-read-only-bubblewrap-sandbox`; never track provider output,
credentials, account details, session identifiers, or machine-specific paths.
