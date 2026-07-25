# Bubblewrap + Claude CLI read-only probe

This manual Linux probe checks the proposed outer-sandbox shape with Claude
CLI before it is added to agent-collab's runners. Its default `writable` state
mode gives Claude:

- a temporary workspace kept at its real absolute host path and mounted
  read-only;
- that same path as Bubblewrap's cwd and the prompt's authoritative workspace;
- the invoking user's `HOME` through the read-only root mount;
- the real `CLAUDE_CONFIG_DIR` as the one persistent writable provider-state
  exception;
- a private writable `TMPDIR` plus XDG cache/state/config/data paths below that
  scratch directory;
- transient `sandbox.enabled = false` settings; and
- `--dangerously-skip-permissions`, so the probe does not wait for approval
  while Bubblewrap decides whether filesystem writes succeed.

Claude Code stores Linux credentials and its primary provider state below
`CLAUDE_CONFIG_DIR` (normally `~/.claude`). The probe mounts that complete
directory writable instead of copying credentials or teaching the launcher
which individual files Claude may update. With the environment variable
unset, the probe preserves that normal default behavior rather than setting it
to `~/.claude` again, which would change how Claude resolves legacy
`.claude.json` state. This follows the planned MVP boundary and means Claude or
a command it launches can modify credentials, settings, plugins, histories,
and other files in the selected state root.

The probe creates one uniquely named provider-state marker to verify the
writable exception and removes it before exiting. It refuses to overwrite an
existing marker. Its workspace, protected directory, and scratch markers are
all below a private temporary directory that is deleted on exit.

## Requirements

- Linux with `bwrap` on `PATH` and usable user namespaces.
- Claude CLI on `PATH`.
- An existing Claude login under `$CLAUDE_CONFIG_DIR` or `~/.claude`, or a
  supported credential environment variable.
- Network access for the Claude model call.

The Claude run may consume paid provider usage. Start with the free structural
control:

```bash
python3 probes/bubblewrap_claude/probe_bubblewrap_claude.py --preflight-only
```

Run the real Claude probe with:

```bash
python3 probes/bubblewrap_claude/probe_bubblewrap_claude.py
```

The default real run keeps the complete provider state root persistently
writable. Compare the unsupported whole-state-read-only shape with:

```bash
python3 probes/bubblewrap_claude/probe_bubblewrap_claude.py \
  --state-mode read-only
```

Select a model only when needed with `--model MODEL_ID`.

The expected default host verification is:

```text
workspace write blocked       PASS
protected host write blocked  PASS
read-only home write blocked  PASS
provider state write allowed  PASS
scratch write allowed         PASS
```

The probe returns nonzero if Claude fails, Claude does not run the requested
action script, or any filesystem expectation fails. It explicitly disables
session persistence and user/project customizations for the test, requests
only the Bash tool, disables Claude's native sandbox through transient
settings, and bypasses provider permission prompts.

## Observed result

On this Linux host, the real probe passed with Bubblewrap 0.6.3 and Claude Code
2.1.219:

- Claude authenticated from its existing state without copying credentials;
- Bash ran without an interactive approval prompt;
- the real absolute workspace path containing spaces was preserved;
- workspace, protected-host, and general-home writes were blocked;
- scratch and the selected `~/.claude` provider-state root were writable; and
- the probe removed its provider-state marker on exit.

The `--state-mode read-only` negative control authenticated but could not start
the Bash tool. Claude attempted to create a per-session directory below
`~/.claude/session-env/` and received `EROFS` before the action script ran.
Writable Claude provider state is therefore required for this tested version,
even with `--no-session-persistence`.

## What this does not prove

- The outer boundary protects workspace integrity, not provider state. The
  writable `CLAUDE_CONFIG_DIR` is deliberately persistent and sensitive.
- The read-only root remains readable. The probe inherits network access, so
  it does not provide credential confidentiality or prevent exfiltration.
- Admin-managed Claude settings may take precedence over transient settings.
  The probe does not claim that it can weaken an administrator-enforced native
  sandbox.
- It tests the installed Bubblewrap and Claude versions on this host, not every
  distribution, container, macOS, or Windows fallback.

Summarize durable findings in
`antigravity-read-only-bubblewrap-sandbox`; do not commit raw model output or
credentials.
