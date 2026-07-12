# Go-public checklist

**Status: Open.**

Created 2026-07-12. Issue: none by decision — this is a release-gate
checklist, not a discrete work item; individual entries can be promoted to
issues if they grow.

## Context

The repository is private and is intended for public release. Several
settings and verifications only make sense — or are only possible — at the
moment the visibility flips. SECURITY.md (added for #2) already points
reporters at GitHub's private vulnerability reporting, but that feature
exists only on public repositories: the API returns 404 while the repo is
private, so it could not be enabled in advance.

## Checklist

Ordered; the audit comes first because the flip publishes all history at
once.

1. **Pre-flip content audit.** The entire git history, all issues, comments,
   and labels become public. Re-check them for secrets, tokens,
   machine-specific paths or hostnames, and personal data before flipping.
   The public-content guardrail in the github-issues skill has applied to new
   content for a while; this step covers everything older.
2. **Flip visibility:**

   ```bash
   gh repo edit lauriparviainen/agent_collab --visibility public
   ```

3. **Enable private vulnerability reporting** (fails with 404 until the repo
   is public):

   ```bash
   gh api -X PUT repos/lauriparviainen/agent_collab/private-vulnerability-reporting
   ```

   Verify with the matching `GET`, and confirm the
   `/security/advisories/new` link in SECURITY.md now works for a logged-in
   non-collaborator viewpoint.
4. **Enable dependency security features** (dependency graph and Dependabot
   alerts) under repository security settings if GitHub has not enabled them
   automatically on flip.
5. **Verify public rendering:** the README CI badge resolves, Actions runs
   are publicly visible, and GitHub's community profile recognizes LICENSE,
   SECURITY.md, CONTRIBUTING.md, and the bug-report issue template.
6. **Consider branch protection on `main`** (require CI to pass; restrict
   force-pushes). Optional for a single maintainer but cheap once public PRs
   are possible.
7. **Coordinate with the release procedure** in the release skill: flipping
   visibility and tagging the first public release should land together so
   the README install instructions work for outside users from day one.

## Verification

Done when every item above is checked off, at which point this document moves
to `doc/tasks_closed/`.
