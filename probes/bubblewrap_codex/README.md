# Bubblewrap + Codex CLI read-only probe

This manual Linux probe checks the proposed outer-sandbox shape before it is
added to agent-collab's runners. Its default `read-only` home mode gives Codex
CLI:

- a temporary workspace kept at its real absolute host path and mounted
  read-only;
- that same path as Bubblewrap's cwd, Codex's `-C` workspace, and the prompt's
  authoritative workspace;
- a private read-only `HOME`;
- the real `CODEX_HOME` through the read-only host mount, without copying it;
- a private writable `TMPDIR` plus XDG cache/state/config/data paths below that
  scratch directory; and
- `--dangerously-bypass-approvals-and-sandbox`, so attempted writes are decided
  by Bubblewrap rather than Codex's provider-native sandbox.

The alternative `staged` mode copies only `auth.json` into a private writable
ephemeral `CODEX_HOME`. It exists to compare CLIs that cannot operate with
read-only provider state; it is not the preferred result.

The probe targets only temporary files it creates itself. Even if its sandbox
command is wrong, it does not ask Codex to write to the repository containing
this probe or to the user's real home. In `read-only` mode, however, Codex and
the remote model's tool process can read the real `CODEX_HOME`, including
authentication material. Read-only prevents mutation, not disclosure.

## Requirements

- Linux with `bwrap` on `PATH` and usable user namespaces.
- Codex CLI on `PATH`.
- An existing Codex login in `$CODEX_HOME/auth.json` or `~/.codex/auth.json`.
- Network access for the Codex model call.

The Codex run may consume paid provider usage. Start with the free structural
control:

```bash
python3 probes/bubblewrap_codex/probe_bubblewrap_codex.py --preflight-only
```

That runs the filesystem action script directly inside Bubblewrap, without
starting Codex or reading authentication.

Run the real Codex probe with:

```bash
python3 probes/bubblewrap_codex/probe_bubblewrap_codex.py --home-mode read-only
```

Compare the writable staged-home fallback with:

```bash
python3 probes/bubblewrap_codex/probe_bubblewrap_codex.py --home-mode staged
```

`read-only` is the default, so the flag may be omitted. Select a model only
when needed with `--model MODEL_ID`.

The expected read-only host verification is:

```text
workspace write blocked        PASS
protected host write blocked   PASS
read-only home write blocked   PASS
scratch write allowed          PASS
```

That expectation passes in `--preflight-only` mode. A real Codex 0.145.0 turn
does **not** currently reach the action script with its complete `CODEX_HOME`
read-only. Even with `--ignore-user-config --ephemeral` and writable TMP/XDG
paths, startup attempts to write `state_5.sqlite`, reports SQLite read-only
error code 8, and then fails to initialize the in-process app-server client
with `EROFS`. The `read-only` real run is therefore a tracked negative control,
not a working configuration for that Codex version.

A possible later hardening experiment is a writable ephemeral `CODEX_HOME`
with the real `auth.json` mounted into it as a read-only file. That would avoid
copying or mutating authentication while allowing Codex's state database and
app-server runtime files to remain disposable. The planned MVP instead uses
the simpler complete persistent writable provider-state root.

The probe returns nonzero if Codex fails, Codex does not run the requested
action script, or any filesystem expectation fails. Its private directory is
removed when the process exits; staged mode removes its copied authentication
with that directory.

## What this does not prove

- It invokes Codex with `--ignore-user-config`. Read-only mode still points
  `CODEX_HOME` at the real directory for authentication; staged mode copies
  only `auth.json`. Other CLIs and authentication mechanisms need
  provider-specific discovery.
- It deliberately inherits the invoking process's non-home environment and
  network namespace. Environment minimization and network isolation are
  separate design work.
- Scratch storage, and staged mode's writable home, are temporary. The probe
  does not test safe persistence or copying provider state back to the real
  home.
- It tests the installed Bubblewrap and Codex versions on this host, not every
  distribution, container, macOS, or Windows fallback.

Summarize durable findings in
`antigravity-read-only-bubblewrap-sandbox`; do not commit raw model output or
copied credentials.
