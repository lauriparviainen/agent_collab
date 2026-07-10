# Sample: non-default (SDK) backend

Stage 1a mockup — illustrative, not approved. This is the one sample where an
agent runs on a **non-default backend**, so the inline backend chip actually
appears. Every other sample runs both agents on the default `cli` (registry
fallback), where nothing is appended. Same underlying session content as
`main-session.md`; only `codex`'s backend differs.

## 1. Data assumptions

- Terminal: 80 x 24, following (tailing the transcript).
- Session `daemon-80958c73c2e04baa`, status `running`.
- Workflow `cross-review`, sequence `claude -> codex -> claude`.
- Agents:
  - `claude` — type `claude`, model `opus-4.8`, backend `cli` (= default, so
    **not** shown inline). Canonical `claude_cli`.
  - `codex` — type `codex`, model `gpt-5`, **backend `sdk`** (`settings.agents.
    codex.backend = "sdk"`, promoted to a first-class backend in stage 5.1).
    Differs from the default `cli`, so `sdk` **is** appended inline. Canonical
    `codex_sdk`.
- Interactive: true. Active overlay: none.
- Row map: identical to `main-session.md` (0 context, 1 session info, 2 hairline,
  3-20 body, 21 hairline, 22 rail, 23 status/hint).

## 2. Mockup (80 x 24, following)

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 sdk · cross-review
────────────────────────────────────────────────────────────────────────────
 claude   Looking at _poll_loop — the epoch guard in _drain_events already
          drops stale batches, so the terminal-status early return is safe.
 codex    Agreed. wait_events re-reads the session on empty batches, which is
          the intended keepalive, not a bug.
 referee  note: ship it once tests pass                                  9:22
 claude   ◆ thinking…



────────────────────────────────────────────────────────────────────────────
 [referee] ▏                                                     referee note
 sent note                              ⠹ running · Enter send · / cmds · q
```

The only visible difference from `main-session.md` is the `sdk` after
`codex:gpt-5` on the session-info line. It is the **bare backend id**, not the
canonical `codex_sdk` (that lives in `/details`) — inline the agent name already
carries the provider, so `sdk` alone disambiguates the axis without widening the
line. `claude` shows no backend because `cli` is its default.

### When the info line overflows

Per the parent README truncation priority (drop right-most first: workflow ->
workdir/project -> secondary agents -> task, ellipsize last), the inline backend
chip travels **with its agent** — it is part of that agent's `name:model`
token, so `codex:gpt-5 sdk` is dropped or kept as a unit. It is never shed
separately, and the lead agent's chip survives longest. A width where the
workflow has already dropped:

```
 review the poller race · claude:opus-4.8 · codex:gpt-5 sdk
```

## 3. Color-token intent

- Identical to `main-session.md`, with one addition: the backend chip (`sdk`) is
  `dim` — quieter than the `accent` agent name and the `muted` model, so it
  reads as a secondary qualifier, not a third first-class field. It sits in the
  same `muted`/`accent` info line but degrades to `dim`.
- In `/details` (see `details-visible.md` shape), `codex`'s block gains a
  `backend=sdk` line and the panel may surface the canonical `codex_sdk`; those
  panel labels stay `dim`, values `muted`, agent id `accent`.

## 4. Keyboard behavior

- Identical to `main-session.md`. The backend chip is display-only; it changes
  no keys and no dispatch. `/details` still expands the full per-agent block.
- Status/hint line: message `sent note` (transient); hint (precedence rule:
  default / following) `⠹ running · Enter send · / cmds · q`.

## 5. Removed / simplified

- **Target delta:** surfaces the execution backend that today's `_render_header`
  never shows. `_render_header` joins only `agent-collab id status workflow
  workdir [tags]` — model and backend are invisible until `/details`. The calm
  info line adds `name:model` always and the backend id only when it differs
  from the agent's default, so the common all-`cli` session (every other sample)
  stays uncluttered while a mixed `cli`/`sdk` session is legible at a glance.
- **Faithful:** the underlying per-agent data (`type`, `model`, `backend`) is
  exactly what `format_session_details` already reads from `settings.agents`;
  this only relocates the backend field from `/details`-only to a conditional
  inline chip. No command, event, poller, or dispatch behavior changes.
