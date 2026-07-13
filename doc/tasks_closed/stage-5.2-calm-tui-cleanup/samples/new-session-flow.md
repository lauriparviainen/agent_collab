# Sample: new session flow (/new wizard)

Stage 1a mockup — illustrative, not approved. Faithful to `_start_new_wizard` /
`_advance_new_wizard` (steps task -> workflow -> workdir, with an unknown-workflow
bounce-back and `Esc` cancel).

## 1. Data assumptions

- Terminal: 80 x 24, invoked via `/new` from the running `cross-review` session
  in `main-session.md` (that session stays active in the background; the header
  still shows it).
- The wizard renders as body overlay lines; the rail prompt is `[new <step>]`.
- Slash completion is disabled during the wizard (`_current_slash_completion`
  returns `None` when `new_wizard` is set).
- Workflows for this workdir: `compare, cross-review, solo-antigravity,
  solo-claude, solo-codex`; default resolves to `cross-review` (the current
  session's workflow).

## 2. Mockups

### 2a. Step 1 — task

```
 main  ~/projects/agent_collab
 review the poller race · claude:opus-4.8 · codex:gpt-5 · cross-review
────────────────────────────────────────────────────────────────────────────
 new session
 enter task



────────────────────────────────────────────────────────────────────────────
 [new task] add a smoke test for the poller▏                      new session
 new session task                                    Enter next · Esc cancel
```

Empty submit keeps the step and shows `task is required` in the message slot.

### 2b. Step 2 — workflow (default in brackets, choices listed)

```
────────────────────────────────────────────────────────────────────────────
 new session
 task: add a smoke test for the poller
 workflow [cross-review]
 choices: compare, cross-review, solo-antigravity, solo-claude, solo-codex
────────────────────────────────────────────────────────────────────────────
 [new workflow] ▏                                                 new session
 workflow [cross-review]                              Enter next · Esc cancel
```

Empty submit accepts the default `cross-review`.

### 2c. Step 3 — workdir (default in brackets)

```
────────────────────────────────────────────────────────────────────────────
 new session
 task: add a smoke test for the poller
 workflow: cross-review
 workdir [~/projects/agent_collab]
────────────────────────────────────────────────────────────────────────────
 [new workdir] ▏                                                  new session
 workdir [~/projects/agent_collab]                    Enter start · Esc cancel
```

Empty submit accepts the default workdir, then the session starts and the TUI
activates it.

### 2d. Unknown-workflow bounce-back (stale overlay — real behavior)

If the entered workflow is not valid for the chosen workdir, the wizard sets
`step` back to `workflow` and the error message, but **does not rebuild
`overlay_lines`** ([tui.py](../../../../agent_collab/tui.py) `_advance_new_wizard`).
So the body still shows the workdir-step overlay (`workflow: pair`,
`workdir [...]`) while the rail prompt has already flipped to `[new workflow]`:

```
────────────────────────────────────────────────────────────────────────────
 new session
 task: add a smoke test for the poller
 workflow: pair
 workdir [~/projects/agent_collab]
────────────────────────────────────────────────────────────────────────────
 [new workflow] ▏                                                 new session
 unknown workflow 'pair'; choices: compare, cross-review, solo-antigravity, …
```

The message matches `unknown workflow {…!r}; choices: …`. **Target delta
(flagged):** the stale body/prompt mismatch is a current-code quirk — Stage 2
should rebuild the workflow overlay on bounce so the body and prompt agree.

### 2e. Cancel

`Esc` at any step cancels: overlay clears, message slot shows
`new session cancelled`, and the prior session view returns.

## 3. Color-token intent

- Overlay title `new session`: `accent`.
- Field labels (`task:`, `workflow:`, `workdir:`) and `choices:`: `muted`;
  entered/default values in `text`; bracketed defaults `[cross-review]` in `dim`.
- Rail prompt `[new <step>]`: `accent` (distinct from the normal `[referee]`
  `muted` to signal a different mode).
- Status/hint line: message left (`muted`, `error` red for `task is required` /
  unknown-workflow); hint right in `dim`.

## 4. Keyboard behavior

- `Enter` advances the step (or starts the session on the final step).
- `Esc` cancels the whole wizard.
- Typing is captured as the field value; `/` does not open the palette here.
- Status/hint line: message is the step prompt (`new session task`,
  `workflow [cross-review]`, `workdir […]`) or an error; hint (precedence rule:
  new-session wizard — the highest) `Enter next · Esc cancel`
  (`Enter start · Esc cancel` on the final step).

## 5. Removed / simplified

- **Faithful:** the three-step sequence, default resolution, the
  unknown-workflow bounce *message* + step change (including the stale-overlay
  quirk in 2d), and cancel all match `_advance_new_wizard`.
- **Target delta:** the steps are framed as a calm titled overlay rather than
  today's terse `overlay_lines` tuples, the step prompt/mode is signaled on the
  rail, and (2d) the workflow overlay is rebuilt on bounce so body and prompt
  agree. No change to what each step collects or validates.
