# Live integration tests

This suite is credentialed and may make paid model calls. It is structurally separate from `tests/` and is never discovered by `./agent_collab.sh test`.

```bash
./agent_collab.sh integration-test
./agent_collab.sh integration-test claude sdk
./agent_collab.sh integration-test codex cli --strict
```

Selection can also use comma-separated `AGENT_COLLAB_IT_PROVIDERS` and `AGENT_COLLAB_IT_BACKENDS`. `AGENT_COLLAB_IT_STRICT=1` makes missing dependencies/credentials for explicitly selected providers exit `2`. Behavioral failures exit `1`; passes and ordinary skips exit `0`.

Override models with `AGENT_COLLAB_IT_CLAUDE_MODEL`, `AGENT_COLLAB_IT_CODEX_MODEL`, or `AGENT_COLLAB_IT_ANTIGRAVITY_MODEL`. Native provider authentication is used. Each turn runs in a fresh temporary workspace with an isolated `AGENT_COLLAB_HOME`; assertions log event kinds rather than raw SDK responses or transcripts.
