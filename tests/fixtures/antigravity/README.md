# Antigravity spike fixtures (Stage 4.9, step 7)

Captured 2026-07-08 to drive `parse_antigravity_line` (cli) and the SDK
event mapper. Parsers/mappers are written against these samples, not guessed.

## CLI (`agy`) — CONFIRMED live

- Binary: `agy`, version **1.1.0** (`agy-version.txt`).
- Command: `agy -p --mode accept-edits "<prompt>"` in a throwaway git repo,
  signed in via the cached `~/.gemini` OAuth token.
- `agy-print-sample.stdout.txt` — real stdout. `agy-print-sample.stderr.txt` —
  real stderr (empty).

**Finding (matches the plan's "Verified provider facts"):** print mode emits
**free-form plain text / Markdown prose** — multiple lines, blank lines, `###`
headers, `*` bullet lists, and fenced code blocks. There is **no** JSON, no
NDJSON, and **no stable per-line event marker**. There is therefore no
tool/command/file-change structure to reconstruct from stdout.

So `parse_antigravity_line` emits one `antigravity` `message` event per
non-empty stdout line (message-only, low fidelity). The referee still emits the
`command` start and `status` exit events it emits for every subprocess runner.

## SDK (`google-antigravity`) — BLOCKED on live capture

The live SDK half of the spike could not run in this environment:

- System Python is **3.9.25**; the SDK requires **>= 3.10**.
- `pip install "google-antigravity>=0.1,<1"` in a fresh venv fails with
  `No matching distribution found for google-antigravity` (no installable
  distribution reachable here). It cannot be imported, so the real
  `ChatResponse` / `ToolCall` / `Step` attribute names, the async-iteration
  surface, and whether a stable conversation id is exposed **cannot be
  captured**.

Per the stage plan and AGENTS.md, we do **not** guess SDK object shapes into
production code. Instead the `sdk` backend (step 9):

- is implemented against the plan's *hypothesized* API, structured so a fake
  `google.antigravity` module can be injected via `sys.modules` and driven by
  `sdk-hypothesis.json` below (clearly labelled as a hypothesis, not a capture);
- **degrades to message-only** if typed tool events are not present — the same
  honest fidelity as the cli path;
- captures **no** conversation id (`agent_sessions` is not shipped) because the
  spike could not confirm a stable one exists;
- rejects `antigravity_options.mode` on `sdk` (no confirmed `LocalAgentConfig`
  equivalent);
- has one fully real, tested path: when the module is absent, `find_spec`
  returns `None` and the start fails with the `antigravity-sdk` extra install
  hint.

`sdk-hypothesis.json` documents the assumed event shapes used by the fake-module
tests. **When a machine with Python >= 3.10 and the installable SDK is
available, re-run the SDK half of the spike, replace the hypothesis fixture with
a real capture, and reconcile the mapper.**
