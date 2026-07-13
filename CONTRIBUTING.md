# Contributing

Pull requests are welcome and are accepted under the
[Apache License 2.0](LICENSE): by submitting a contribution you agree it is
licensed under the same terms as the project (see section 5 of the license).
This is a single-maintainer project, so review may take a while.

Start with [AGENTS.md](AGENTS.md) — it is the entrypoint to the design docs
and the working conventions, for humans and coding agents alike.

## Before submitting

Run both gates from the repository root; CI runs the same checks:

```bash
./agent_collab_dev.sh test          # Ruff lint + format gates, then the hermetic suite
./agent_collab_dev.sh build --check # fails if generated API artifacts are stale
```

Two constraints worth knowing up front:

- CI runs without any vendor SDKs or provider CLIs installed. Tests under
  `tests/` must stay hermetic and must never rely on locally installed
  provider packages; anything that needs credentials or a real model call
  belongs under `integration_tests/`.
- Files under `doc/daemon_api_doc/` are generated. Never edit them by hand;
  regenerate them with `./agent_collab_dev.sh build`.

## Commits

Plain sentence-case subject lines; reference issues in the body, not the
subject.
