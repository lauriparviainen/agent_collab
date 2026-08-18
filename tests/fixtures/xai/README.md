# xAI fixtures

Captured 2026-07-10 on Python 3.12.13. No credential values, private repository
content, local paths, prompts beyond disposable fixture instructions, or real
provider IDs are committed.

## Grok Build CLI 0.2.93

`grok-version.txt` is the real output of `grok --version`.

`streaming-json-reasoning.ndjson` came from a real headless command using
`--no-auto-update --output-format streaming-json -p`. Thought prose and IDs are
redacted, while the observed record boundaries and field names are preserved.

`streaming-json-tooluse.ndjson` came from a real disposable Git workspace with
`--permission-mode bypassPermissions`. Grok successfully created and verified a
disposable file, but 0.2.93 emitted only thought/text/end records—no tool,
command, or file-change record. This is positive evidence to keep typed action
mapping disabled, not a synthetic action fixture.

`streaming-json-error.ndjson` is a real explicit error record produced by an
invalid disposable model name. It contains no provider ID.

`streaming-json-documented-tools.ndjson` is **not** a live capture. It is the
documented Grok 1.0.4 / user-guide `streaming-json` tool example (ACP-shaped
`tool_call` / `tool_call_update` / `usage` / `end`), with the signature
redacted. Use it for hermetic parser mapping only.

`streaming-json-live-tools-1.0.5.ndjson` is a redacted live `streaming-json`
read turn on Grok Build 1.0.5 (`grok-4.6` / low). Thought prose, ids,
signatures, host paths, and file bytes are removed. Record boundaries and
field names are preserved: start `tool_call` with `kind=read`,
`status=pending`, `title`/`toolName=read_file`, and `rawInput.target_file`;
a `tool_call_update` with `status=null`; a completed update that omits
`kind`; `text`; `usage` without `stopReason`; `end` with `end_turn`. Live
also emitted many `thought` and `available_commands` records; those are
collapsed here. This pins the current-Grok start/update sequence the
user-guide example does not show. Only `kind=read` was observed, so
`event_fidelity` stays `message_first`. This task stays on
`streaming-json`.

## xAI SDK 1.17.0

`sdk-introspection.json` records non-secret public facts captured after
installing `xai-sdk>=1.17,<1.18` into the project Python environment. `python -m
pip check` reported no broken requirements. Signatures were inspected without a
model call by constructing `AsyncClient` with a fixture API key and closing it.

`sdk-response-sample.json` is illustrative, not a paid response. It contains
only the public `.content` and `.id` shape confirmed from the installed
`xai_sdk.chat.Response` properties; values are synthetic.
