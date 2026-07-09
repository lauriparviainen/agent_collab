# Stage 5.2 - Calm TUI Cleanup

Status: draft, not approved for implementation.

## Goal

Clean up the agent-collab TUI so it is easier to understand, calmer, and more
purposeful. The first screen should make the current session, available actions,
and input target obvious without feeling busy.

This is a two-stage task:

1. Stage 1: create CLI/TUI samples only, then review and approve them with the
   user.
2. Stage 2: implement the approved direction.

Do not start Stage 2 until Stage 1 samples are explicitly approved.

## References

- Grok CLI screenshot reference:
  [assets/grok-cli-reference.svg](assets/grok-cli-reference.svg)
- Screenshot note:
  [assets/README.md](assets/README.md)
- David AI design system:
  `/home/devel/projects/david_ai_git/doc/david_ai_design_system`

The Grok CLI reference is layout inspiration only. The agent-collab TUI palette
must come from the David AI design system.

## Design System Inputs

Use the David AI dark, warm-charcoal system as the source of truth:

- `--page #0D0C0A`: app floor.
- `--terminal #0A0906`: darkest inset for terminal/log material.
- `--floor #211C15`: grouping band.
- `--panel #322A20`: default standalone surface.
- `--raised #423629`: nested raised tile.
- `--text #F6EFE2`: primary text.
- `--muted #C2B6A3`: secondary text.
- `--dim #8C8170`: tertiary/caption text.
- `--teal #30AB92`: one primary accent.
- `--hairline rgba(255,255,255,.09)`: section separators.

Terminal color support is limited, so Stage 1 should include both token intent
and a practical terminal-color mapping.

## Inspiration To Keep

From the Grok CLI screenshot:

- A quiet top context line: branch/project on the left, usage/status on the
  right.
- A clear user-message band with timestamp.
- Assistant output as readable prose, not over-framed cards.
- Thinking/status metadata kept subdued.
- A bottom command palette that appears near the input, with selected row and
  short descriptions.
- A focused input rail at the bottom with a small mode/status area.
- Sparse separators and restrained contrast.

Do not copy the screenshot palette. Do not add decorative UI.

## Stage 1 - Samples For Approval

Deliver static CLI samples under `samples/`. These can be text mockups,
terminal-recording notes, or screenshot-ready fixtures. They should show the
same content at realistic terminal widths.

Required samples:

- Main session view with active transcript and input.
- Slash command palette.
- Session picker.
- New session flow.
- Awaiting-input state.
- Error/failed state.
- Narrow terminal fallback.

Each sample should call out:

- layout structure,
- color-token intent,
- keyboard behavior,
- what was removed or simplified from the current TUI.

Approval checkpoint:

- Review samples with the user.
- Adjust until the direction is approved.
- Only then open Stage 2 implementation.

## Stage 2 - Implementation

After approval:

- Refactor TUI rendering around the approved layout.
- Keep command/event behavior unchanged unless the approved samples require a
  specific interaction change.
- Add focused tests for formatting helpers, command palette behavior, session
  picker behavior, and narrow-terminal fallback.
- Run the full test suite before closing.

## Initial Open Questions

- Should Stage 1 samples be plain text fixtures, generated ANSI screenshots, or
  both?
- Should the default TUI first screen prioritize the latest session or a
  session picker?
- Which status items belong in the top line versus the bottom input rail?
- How much of the current slash-command help should remain visible by default?
