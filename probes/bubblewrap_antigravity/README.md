# Bubblewrap + Antigravity CLI read-only probe

This manual Linux probe checks the proposed outer-sandbox shape with
Antigravity CLI before it is added to agent-collab's runners. Its default
`writable` state mode gives `agy`:

- a private temporary workspace whose real absolute path contains spaces and
  is mounted read-only;
- that same path as the host cwd, Bubblewrap cwd, Antigravity `--add-dir`,
  prompt workspace, and shell-reported `pwd`;
- the invoking user's `HOME` through the read-only root mount;
- the complete `~/.gemini` provider-state root as the one persistent writable
  exception;
- private writable scratch through `TMPDIR`, `TMP`, `TEMP`, and the XDG
  cache/config/data/state variables;
- `--dangerously-skip-permissions` for non-interactive approval bypass; and
- `--sandbox=false`, the documented boolean override that disables
  Antigravity's native terminal sandbox while Bubblewrap enforces the boundary.

Antigravity CLI has no documented provider-specific state-root relocation
variable in version 1.1.7. Its settings, plugins, MCP cache, conversation
history, tool helpers, and legacy file-based authentication state are below
`~/.gemini`; current authentication may instead come from the operating-system
keyring. `--state-root` therefore accepts only a path ending in `.gemini` and
uses its parent as `HOME`, preserving the CLI's real lookup contract rather
than inventing a provider environment variable.

The complete selected state root is writable in the positive control. This
means Antigravity and every command it launches can modify sensitive provider
state, including credentials, settings, plugins, histories, and MCP
configuration. The outer boundary protects the workspace, general home, and
other visible host paths; it does not protect provider state or constrain
keyring service access through the inherited session bus.

The probe creates one guarded provider-specific marker in the selected state
root and one guarded marker target in general home. It refuses to overwrite
either and removes both in `finally`, including after provider failure or
timeout. All other action files are below the private temporary fixture, never
the repository containing this probe. Host verification reads a private
scratch result file, so provider prose alone cannot make a run pass.

## Requirements

- Linux with `bwrap` on `PATH` and usable user namespaces.
- Antigravity CLI (`agy`) on `PATH`.
- An existing Antigravity sign-in in the selected state or accessible OS
  keyring.
- Network access for the Antigravity model call.

The credentialed calls may consume provider usage. Run both structural modes
first:

```bash
python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py \
  --preflight-only --state-mode writable
python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py \
  --preflight-only --state-mode read-only
```

Run the real writable-state probe and read-only comparison with:

```bash
python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py \
  --state-mode writable
python3 probes/bubblewrap_antigravity/probe_bubblewrap_antigravity.py \
  --state-mode read-only
```

Use `--model MODEL_ID`, `--state-root PATH`, `--agy COMMAND`,
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
writable-state turn passed with Antigravity CLI 1.1.7:

- existing authentication was found without copying or reading credential
  contents;
- the ordinary shell action ran without an approval prompt;
- host-side evidence confirmed the exact workspace/cwd identity and readable
  tracked input;
- workspace, protected-host, and general-home writes were blocked;
- provider-state and private-scratch writes succeeded; and
- both guarded real-host marker targets were absent after cleanup.

The real `--state-mode read-only` comparison authenticated and reached the
model, but failed before the action script. Log, crash-report, conversation,
MCP-cache, and artifact writes first received `EROFS`; command execution then
failed when Antigravity could not materialize its `antigravity-cli/bin/agentapi`
helper below the selected state root. The CLI returned zero after reporting
the tool failure, but the probe correctly returned nonzero because its
host-side action evidence was absent.

Writable complete `~/.gemini` state is therefore required for this tested
version. The successful control needed no writable filesystem path outside
that root and private scratch. Updater access to its installation directory
was read-only and skipped without preventing the turn.

Primary provider references:

- <https://antigravity.google/docs/cli/using>
- <https://antigravity.google/docs/cli/sandbox>
- <https://antigravity.google/docs/cli/permissions>
- <https://antigravity.google/docs/cli/troubleshooting>

## What this does not prove

- OS-keyring storage is outside `~/.gemini`. This probe proves that keyring
  authentication worked through the inherited service connection; it does not
  claim the file mount protects keyring contents or side effects.
- The read-only root remains readable, and network access is inherited.
  Credential confidentiality and exfiltration prevention are separate work.
- Custom hooks, MCP servers, plugins, managed settings, alternative keyrings,
  token refresh, and future CLI versions may introduce additional
  compatibility requirements.
- It tests one installed Linux/Bubblewrap combination, not other
  distributions, containers, macOS, or Windows.

Summarize only sanitized findings in
`antigravity-read-only-bubblewrap-sandbox`; never track provider output,
credentials, account details, or machine-specific paths.
