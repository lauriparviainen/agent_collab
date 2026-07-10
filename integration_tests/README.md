# Live integration tests

> **Antigravity SDK blocker on Oracle Linux 9:** the `google-antigravity`
> package's bundled `localharness` requires `GLIBC_ABI_DT_RELR` (glibc 2.36+),
> while Oracle Linux 9 provides glibc 2.34. The `antigravity sdk` live test
> cannot run on this host until it uses a newer host runtime or Google ships an
> EL9-compatible binary. Do not replace the system glibc manually.

This suite is credentialed and may make paid model calls. It is structurally separate from `tests/` and is never discovered by `./agent_collab.sh test`.

```bash
./agent_collab.sh integration-test
./agent_collab.sh integration-test claude_sdk
./agent_collab.sh integration-test codex_cli --strict
./agent_collab.sh integration-test xai_cli --strict
./agent_collab.sh integration-test xai_sdk --strict
```

Selection can also use comma-separated canonical names in
`AGENT_COLLAB_IT_BACKENDS`, such as `claude_sdk,codex_cli`.
`AGENT_COLLAB_IT_STRICT=1` makes missing dependencies/credentials for explicitly
selected backends exit `2`. Behavioral failures exit `1`; passes and ordinary
skips exit `0`.

The paid calls default to economical, low-latency settings: Claude `sonnet` with
low effort, Codex `gpt-5.6-luna` with low reasoning, and Antigravity
`Gemini 3.5 Flash (Low)`. xAI uses Grok Build's local `grok-build` model for the
CLI and `grok-4.5` with low effort for the remote SDK. Override models with
`AGENT_COLLAB_IT_CLAUDE_MODEL`, `AGENT_COLLAB_IT_CODEX_MODEL`, or
`AGENT_COLLAB_IT_ANTIGRAVITY_MODEL`, or `AGENT_COLLAB_IT_XAI_MODEL`; override
Claude/Codex/xAI effort with the
corresponding `AGENT_COLLAB_IT_<PROVIDER>_THINKING_LEVEL`. Native provider
authentication is used. Each turn runs in a fresh temporary workspace with an
isolated `AGENT_COLLAB_HOME`; assertions log event kinds rather than raw SDK
responses or transcripts.

The xAI CLI accepts `XAI_API_KEY` or Grok's cached local sign-in. The xAI SDK
test specifically requires `XAI_API_KEY`, emits message-only events, and asserts
response identity rather than prose.

The Antigravity SDK test uses Vertex when Google Application Default
Credentials are available. It reads the credential path from
`GOOGLE_APPLICATION_CREDENTIALS`, defaulting to gcloud's standard
`~/.config/gcloud/application_default_credentials.json`. Set
`AGENT_COLLAB_IT_ANTIGRAVITY_PROJECT` to override the active gcloud project and
`AGENT_COLLAB_IT_ANTIGRAVITY_LOCATION` to override the default `us-central1`
location. Credential contents and project values are never logged.
