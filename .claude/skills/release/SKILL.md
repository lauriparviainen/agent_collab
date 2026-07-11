---
name: release
description: Use when cutting an agent-collab release, bumping the version, tagging, publishing GitHub release notes, or planning versions with milestones.
---

# Versioning and Releases

Read this together with `.claude/skills/github-issues/SKILL.md`.

## Model

- Versions are three-part SemVer (`0.3.0`), pre-1.0: minor bumps for features
  and behavior changes, patch bumps for fixes only. Tags are `v`-prefixed
  (`v0.3.0`).
- Everything releases from `main`. There are no release branches; create
  `release/0.x` from an existing tag only if an old version ever genuinely
  needs a backported fix.
- A shipped version is an annotated git tag plus a GitHub Release whose notes
  are that version's `CHANGELOG.md` section.
- A planned version is a GitHub **milestone** (for example `0.3.0`) holding
  the issues intended for it. Milestones track "when it ships"; labels track
  component/topic only — never use labels for versioning.
- The `Public release` milestone tracks work required before the repository
  flips public; it is not tied to a version number.

## Release Procedure

1. Confirm `main` is green: `./agent_collab.sh test` and
   `./agent_collab.sh setup --check` pass, and CI on the release commit is
   green.
2. Bump the version in **both** `pyproject.toml` and
   `agent_collab/__init__.py` — they must stay identical.
3. Convert the CHANGELOG `[Unreleased]` section to
   `## [X.Y.Z] - YYYY-MM-DD - <short theme>` and start a fresh empty
   `[Unreleased]` heading above it.
4. Commit (subject like `Release 0.3.0`), then tag:
   `git tag -a vX.Y.Z -m "agent-collab X.Y.Z — <short theme>"`.
5. Push the branch and tag, then publish:
   `gh release create vX.Y.Z --title "X.Y.Z — <theme>" --notes-file <extracted changelog section>`.
6. Close the matching milestone; move any unfinished issues to the next one
   explicitly rather than letting them dangle.

Do steps 4–5 only after the user approves the release: tags and GitHub
Releases are published, outward-facing state.

## Guardrails

- Never re-tag or force-move a published tag; a bad release gets a new patch
  version.
- Release notes come from the changelog — do not write separate, diverging
  prose for the GitHub Release.
- The changelog's latest released version, the package version, and the
  latest tag must always agree; if they diverge, reconcile before any new
  release work.
