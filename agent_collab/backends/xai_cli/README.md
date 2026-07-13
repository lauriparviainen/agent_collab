# xAI CLI backend

Registered as `xai_cli` (`type="xai"`, `backend="cli"`). It requires the Grok
Build `grok` command and runs headless single turns with newline-delimited
`streaming-json`; the built-in command includes `--no-auto-update`.

Authentication uses `XAI_API_KEY` or Grok's own cached sign-in under
`~/.grok/auth.json`. Agent-collab checks only for non-empty credential evidence
and never reads credential values into events or logs.

`model`, `permission_mode`, and `sandbox` map to the corresponding Grok flags.
`thinking_level` is preferred; `reasoning_effort` is an alias, and one effective
value maps to `--reasoning-effort`. Flags are inserted before `-p`/`--single`,
and the subprocess working directory is used directly without adding `--cwd`.
Headless runs default to `permission_mode=bypassPermissions` and
`sandbox=read-only`, so Grok can execute inspection commands without an
interactive approval prompt while repository writes remain blocked. The
backend also tells Grok to issue one read-only inspection command at a time
without prepending `cd`. Callers must explicitly opt into a writable sandbox.
`provider_max_turns` maps to Grok's internal `--max-turns` model/tool-loop limit;
it is separate from agent-collab's workflow `max_turns` and has no backend
default, so Grok retains its version-specific default unless a caller overrides
it.

Observed Grok 0.2.93 records map `text` to xAI messages, `thought` to verbose
status, explicit errors to transcript errors, and `end.sessionId` to the uniform
provider-session event (kind `session`). The raw `sessionId` and `requestId` are
preserved. Only `stopReason=EndTurn` is successful; cancelled and other terminal
reasons emit a structured fatal error while retaining session identity.
Streaming text deltas are coalesced into one transcript message per turn; a
partial turn is flushed at EOF. A real tool-use capture emitted no typed action
record, so tool, command, and file-change fidelity is intentionally not claimed.
Resume, interrupt, and tool-gate capabilities are all false.

The typed turn outcome uses the same evidence: `EndTurn` completes,
`Cancelled` maps to `cancelled`, other end reasons fail conservatively, and EOF
without `end` fails even after partial text. Conflicting terminal markers fail
with `provider_protocol_conflict`; identical duplicates are harmless.

Hermetic tests: `python3 -m unittest tests.backends.xai_cli.test_backend`.
Credentialed test: `./agent_collab_dev.sh integration-test xai_cli --strict`.
