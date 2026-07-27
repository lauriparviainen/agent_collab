# xAI SDK backend

Registered as `xai_sdk` (`type="xai"`, `backend="sdk"`). This is the remote xAI
chat API, not the local Grok Build coding runtime. It requires `xai-sdk>=1.17,<1.18`
and `XAI_API_KEY`; imports are lazy and the async client is closed
deterministically when the runner/session closes.

## Protobuf 7 import shim (deliberate hack — read before upgrading xai-sdk)

All `xai_sdk` imports in this codebase go through
[`compat.py`](compat.py)'s `import_xai_sdk()` — never `import xai_sdk`
directly. xai-sdk 1.17 ships two generated-proto trees (`proto/v5`,
`proto/v6`) and selects one at import time by the protobuf runtime's major
version, raising `ValueError("Unsupported protobuf version: ...")` for
protobuf 7+. The durable environment runs protobuf 7.35+ (required by
`google-antigravity`'s generated code), which is inside protobuf's official
one-major-back runtime guarantee for xai-sdk's v6 gencode — only that gate
blocks it. The shim tries a plain import first and, only on that exact
`ValueError`, retries the import while `google.protobuf.__version__` is
temporarily spoofed to a 6.x string, then restores it. Verified end to end
2026-07-27 (imports, proto round-trips, and `google.antigravity` importing in
the same process); `conditional_tests/test_protobuf_coexistence.py` re-proves
it wherever the real SDKs are installed, and
`tests/backends/xai_sdk/test_compat.py` covers the shim logic without them.

**On every xai-sdk upgrade, check whether upstream resolved this**: look for
a protobuf 7 branch (or relaxed gate) in `xai_sdk/proto/__init__.py` and a
lifted `protobuf<7` dependency cap
(https://github.com/xai-org/xai-sdk-python). The shim self-retires — the
plain import succeeds and the spoof branch becomes dead code — but once
upstream is fixed, remove `compat.py`, its call sites (backend and
`common/model_discovery.py`), and the second-phase protobuf alignment in
`user_install.py`, and fold the protobuf pin back into the extras.

Dynamic model discovery uses
`AsyncClient.models.list_language_models()` and includes canonical names plus
accepted aliases returned for the authenticated API key. An agent-scoped
`XAI_API_KEY` is passed explicitly to both discovery and chat turns; otherwise
both use the SDK's normal process-environment lookup.

[`options.toml`](options.toml) declares accepted MCP/session options;
[`defaults.toml`](defaults.toml) owns the shipped option values and disabled
Event Window target.

Select it with `backend="sdk"`; the shipped normal-session model is
`grok-4.5`, currently the SDK transport's verified model selection. The schema
still requires a model after defaults are resolved, so a custom configuration
that removes the shipped default must supply one; other provider-supported
model IDs remain accepted. Normal sessions default to
`thinking_level=high`; `grok-4.5` also supports `low` and `medium`.
`thinking_level` is the preferred spelling and `reasoning_effort` is an alias;
one effective `none`, `low`, `medium`, or `high` value maps to
`chat.create(reasoning_effort=...)`. CLI-only
`permission_mode` and `sandbox` are rejected by the declarative schema.

One runner retains a serialized `AsyncClient` conversation adapter. Each turn
creates a fresh `Chat` with exactly one new `user(prompt)` message,
`store_messages=True`, and — after turn 1 — the latest
`previous_response_id`. xAI prepends the stored history server-side; no prior
messages are appended locally or replayed in the request. The captured
`response.id` changes per turn and becomes the next strict continuation point
(identity kind `response`). An unknown id fails with `NOT_FOUND`; there is no
fresh-chat fallback. Abnormal turns reset the client while retaining the id.
Final close deletes captured stored completions best-effort, then closes the
client.

The runner maps only non-empty `response.content` to an xAI message. It enables
no remote or client-side tools and emits no tool, command, or file-change
events. Event fidelity is message-only. In-session continuity is true and the
settings summary reports `conversation="persistent"`; restart-safe resume,
interrupt, and tool-gate capabilities remain false. Credential values and SDK
responses are never logged by health probes.

## Outer sandbox: `no_local_effects` (Stage 8)

Outer `sandbox = "read-only"` for this backend is **not** Bubblewrap. The
audited production path is remote model/gRPC only: no local tools, callbacks,
MCP, plugins, hooks, skills, subagents, child processes, or writable local
provider-state root. Settings report:

- `support`: `no_local_effects`
- `enforcement`: `not_applicable_no_local_effects`

An all-`no_local_effects` session uses engine `not_applicable` and skips host
Bubblewrap checks. Mixed sessions still require OS enforcement for every
member that needs it. This is a **versioned software capability** pinned to
`xai-sdk` 1.17.x and the exact `chat.create` request surface (model,
`store_messages`, `previous_response_id`, `reasoning_effort` only) — not an OS
isolation claim. Dependency series drift or a new local execution surface
revokes the capability to `unsupported` until the audit is renewed or the
backend uses the generic SDK worker. The prompt preamble under read-only
states that no local file/tool or scratch surface is exposed; it does not
promise `$TMPDIR` or OS enforcement.

Hermetic adapter and deny-probe tests:
`python3 -m unittest tests.backends.xai_sdk.test_sandbox`.

For this no-tools backend, `finish_reason=STOP` with non-empty content is the
only verified completion. Empty content, length/token limits, unexpected tool
calls, other finish reasons, SDK exceptions, and uncertain bounded close fail
conservatively. No structured refusal mapping is claimed.

Hermetic tests: `python3 -m unittest tests.backends.xai_sdk.test_backend`.
Credentialed test: `./agent_collab_dev.sh integration-test xai_sdk --strict`.
