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

Observed Grok 0.2.93 records map `text` to xAI messages, `thought` to verbose
status, explicit errors to transcript errors, and `end.sessionId` to the uniform
provider-session event (kind `session`). The raw `sessionId` and `requestId` are
preserved. Streaming text deltas are coalesced into one transcript message per
turn; a partial turn is flushed at EOF. A real tool-use capture emitted no typed
action record, so tool, command, and file-change fidelity is intentionally not
claimed. Resume, interrupt, and tool-gate capabilities are all false.

Hermetic tests: `python3 -m unittest tests.backends.xai_cli.test_backend`.
Credentialed test: `./agent_collab_dev.sh integration-test xai_cli --strict`.
