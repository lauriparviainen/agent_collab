# xAI CLI backend

Registered as `xai_cli` (`type="xai"`, `backend="cli"`). It requires the Grok
Build `grok` command and runs headless single turns with newline-delimited
`streaming-json`; the built-in command includes `--no-auto-update`.

Authentication uses `XAI_API_KEY`, a model-specific configured environment
key, or Grok's cached sign-in under the complete effective `GROK_HOME`
(default `~/.grok`). Agent-collab checks environment presence or cached-file
metadata only; it does not open cached auth or put credential values in
settings, events, or logs. External authentication provider commands are
rejected under outer read-only because their undeclared filesystem
dependencies cannot be proved.

[`options.toml`](options.toml) declares accepted MCP/session options;
[`defaults.toml`](defaults.toml) owns the shipped command, option values, and
disabled Event Window target.

`model`, `permission_mode`, and `sandbox` map to the corresponding Grok flags.
The shipped normal-session model is `grok-4.5`, matching the default reported
by the installed Grok CLI's `grok models` catalog; callers can override it with
the other verified catalog suggestion, `grok-composer-2.5-fast`, or another
provider-supported model ID. Its shipped
`thinking_level=high` maps to `--reasoning-effort high`; `grok-4.5` also
supports `low` and `medium`.
`thinking_level` is preferred; `reasoning_effort` is an alias, and one effective
value maps to `--reasoning-effort`. Flags are inserted before `-p`/`--single`,
and the subprocess working directory is used directly without adding `--cwd`.
Headless runs default to `permission_mode=bypassPermissions` and
`sandbox=read-only`, so Grok can execute inspection commands without an
interactive approval prompt while repository writes remain blocked. Keep that
default: under `permission_mode=auto` a command Grok's classifier will not
auto-approve (reliably a `;`-chained pipeline) raises a permission prompt that
nothing answers headlessly, and Grok cancels the turn after 15 seconds
(`stopReason=Cancelled`, daemon outcome `provider_turn_cancelled`; the grok
model does not always obey the one-command-per-call rule that would avoid
this). The backend also tells Grok to issue one read-only inspection command
at a time without prepending `cd`. Callers must explicitly opt into a writable
sandbox.
`provider_max_turns` maps to Grok's internal `--max-turns` model/tool-loop limit;
it is separate from agent-collab's workflow `max_turns` and has no backend
default, so Grok retains its version-specific default unless a caller overrides
it.

Observed Grok records map `text` to xAI messages, `thought` to verbose status,
explicit errors to transcript errors, and `end.sessionId` to the uniform
provider-session event (kind `session`). The raw `sessionId` and `requestId` are
preserved. Successful completion is `stopReason=end_turn` (current Grok
streaming-json / ACP snake_case); legacy `EndTurn` from older captures is still
accepted. Cancel maps from `cancelled` or legacy `Cancelled`. Incomplete
terminals (`max_tokens`, `max_turn_requests`) map to
`provider_output_incomplete`; `refusal` maps to `provider_turn_refused`; other
end reasons emit a structured fatal error while retaining session identity.
Streaming text deltas are coalesced into one transcript message per turn; a
partial turn is flushed at EOF. A real tool-use capture emitted no typed action
record, so tool, command, and file-change fidelity is intentionally not claimed.
Resume, interrupt, and tool-gate capabilities are all false.

The typed turn outcome uses the same evidence: `end_turn`/`EndTurn` completes,
`cancelled`/`Cancelled` maps to `cancelled`, incomplete and refusal terminals
use their dedicated codes, other end reasons fail conservatively, and EOF
without `end` fails even after partial text. Conflicting terminal markers fail
with `provider_protocol_conflict`; identical duplicates are harmless.

## Outer read-only filesystem boundary

The top-level agent-collab `sandbox="read-only"` policy is separate from this
backend's provider-native `sandbox` option. On Linux, Stage 3 reuses the common
Bubblewrap launcher and establishment proof. Grok and every Bash, hook, plugin,
skill, MCP process, subagent, and descendant start only inside the proved PID
and mount namespace. The workspace, discovered Git storage, host root, and
home remain read-only. Common private scratch owns `/tmp`, `/var/tmp`,
`$TMPDIR`, and XDG cache/config/data/state.

The adapter declares `direct_process` support for `none` and `read-only`. For
read-only, the complete effective `GROK_HOME` is the single persistent writable
exception. The directory must already exist, be absolute, retain exact
non-symlinked path identity, be owned by the daemon uid, and not be group/world
writable. It may not be the whole home, overlap the workspace or protected Git
storage, or alias protected data. `GROK_HOME` inside the namespace is set to
the exact mounted path. Grok may update auth, configuration, sessions, indexes,
skills, plugins, hooks, and other state below it; that state is not a
confidentiality boundary.

Before provider execution the adapter rejects:

- Grok `managed_config.toml`, `requirements.toml`, system managed files, or the
  legacy managed-settings source;
- malformed config, managed sandbox pins, and symlinked project extensions;
- external auth-provider commands and configured extension filesystem paths,
  including MCP command, argument, and working-directory paths from Grok TOML
  or project `.mcp.json`, outside `GROK_HOME` or the protected workspace;
- leader, resume, worktree, restore, and prompt-file shapes that can change
  execution ownership; and
- malformed permission, native-sandbox, path-bearing, or prompt-boundary
  arguments.

Grok's ambient vendor compatibility surfaces are contained rather than
rejected. Grok resolves every `[compat.<vendor>] <surface>` cell as environment
variable > `config.toml` > default, and every cell defaults to on, so an
untouched installation scans `~/.claude`, `~/.claude.json`, `~/.cursor`, and
their project equivalents for skills, rules, agents, MCP servers, and hooks —
extension sources the adapter cannot audit and the declared `GROK_HOME` state
root does not describe. Under read-only the adapter therefore sets
`GROK_{CLAUDE,CODEX,CURSOR}_{AGENTS,HOOKS,MCPS,RULES,SESSIONS,SKILLS}_ENABLED`
to `false` in the sandboxed environment, which wins over any host or project
`config.toml`. Grok 0.2.112 documents the Codex cells other than `sessions` as
reserved and inert, so those names are set defensively rather than because
`.codex` discovery exists today. A configured `[compat]` vendor or surface
outside that matrix fails the start closed, because it is evidence of an
ambient channel these variables do not turn off.

Outer read-only ambient containment is therefore a stated contract against a
Grok build that honours the compat cells and their environment precedence,
verified here against grok 0.2.112. A Grok build that ambiently loaded vendor
material while ignoring these names would start read-only with unaudited
extension sources; re-verify with `grok inspect --json` after a major Grok
upgrade, and use outer `none` if the contract no longer holds.

`grok inspect --json` reports the resulting cells as
`enabled: false, source: "env"`. Outer `none` never applies these variables, so
ambient discovery there behaves exactly as it does outside agent-collab.

Configured `--cwd`, `--agent`, and `GROK_AGENT` paths are traced, required to
exist without symlink relocation, and declared read-only where they are
outside provider state. Project config, `.mcp.json`, and extension-symlink
checks follow the effective `--cwd` ancestor chain;
relative dependencies resolve from that effective cwd. Contained MCP, hook,
skill, plugin, and LSP processes remain allowed and inherit Bubblewrap. Remote
model/MCP/auth services are reported as external services outside the
filesystem guarantee. Grok's legacy shared-temporary session attempt remains
warning-only in common private scratch; it does not authorize another
host-persistent writable root.

Only after the common bootstrap receives its nonce-bound ACK does the adapter
remove configured permission/native-sandbox flags and force:

```text
--permission-mode bypassPermissions --sandbox off
```

`--sandbox off` above disables Grok's native Landlock profile inside the
already-established Bubblewrap namespace. It does not select outer
`sandbox="none"`. Settings, dry-run events, and live command events show the
same exact prompt-free prepared prefix.

Explicit/configured outer `none` is the rollback: it bypasses adapter
compatibility checks and preserves the exact original Grok command and
environment, including its provider-native permission and sandbox posture and
its ambient vendor compatibility discovery. A requested read-only policy never
falls back automatically.

Hermetic tests:

```bash
python3 -m unittest tests.backends.xai_cli.test_backend \
  tests.backends.xai_cli.test_sandbox
```

Credential-free namespace acceptance:

```bash
./agent_collab_dev.sh bubblewrap-test
```

The opt-in paid acceptance requires an operator-authorized complete state
directory and is not run by ordinary verification:

```bash
AGENT_COLLAB_IT_XAI_SANDBOX_STATE=/absolute/path/to/.grok \
  ./agent_collab_dev.sh integration-test xai_cli --strict
```
