---
name: github-issues
description: Use when creating, updating, closing, or reconciling GitHub issues, labels, task documents under doc/tasks_open and doc/tasks_closed, changelog entries, or implementation handoffs.
---

# GitHub Issues and Task Documentation

Use this skill when work needs a tracked GitHub issue, a durable task document,
or synchronization between the two. Use the `gh` CLI for all GitHub operations
against `lauriparviainen/agent_collab`.

## Core Rule

Small tasks use a GitHub issue only. Bigger tasks use both a local task
document and a matching issue.

For small tasks (bugs, cleanups, bounded features), the issue is the source of
truth: keep its description and comments current, and do not create a task
document unless the work grows.

Use the bigger-task path when work involves design decisions, multi-component
behavior, security or auth, config or data migrations, or context future
agents need before editing code. There the task document under
`doc/tasks_open/` is the durable technical history and the issue is the
work-tracking surface. The issue carries a distilled problem statement,
acceptance criteria, and the task document's name; never paste the full
document into the issue. Refer to task documents by name only, not path,
because they move to `doc/tasks_closed/` when done.

When an issue-only task grows, promote it: create the task document and
comment its name on the issue.

## Issue Workflow

Before creating an issue, search for duplicates: `gh issue list --search`,
plus `rg` over `doc/tasks_open/` and `doc/tasks_closed/`.

Issue bodies state the problem, the intended outcome, and acceptance criteria
(a "Done when:" line works well). Include exact commands and observed output
for bugs. After any issue write, report the issue number and URL.

## Public-Content Guardrail

This repository is intended for public release; every issue, comment, and
label becomes public when it flips. Never include secrets, tokens, provider
account details, machine-specific paths or hostnames, personal data, or
session transcript contents. Describe evidence; do not paste it wholesale.
The same applies to task documents, which are already public with the repo.

## Labels

Labels describe component or topic, never process status — GitHub's
open/closed state and milestones cover process. Current set: `bug`,
`enhancement`, `documentation`, `question`, `dx` (developer experience),
`release` (public-release readiness). Add specific component labels
(for example `daemon`, `tui`, `backends`, `mcp`) when a real cluster of
issues needs one; do not create near-synonyms of existing labels.

## Task Document Workflow

Keep the existing convention: descriptive kebab-case filenames in
`doc/tasks_open/`, moved to `doc/tasks_closed/` only when the work is
actually complete or the user explicitly asks. Prefer the structure of a
strong nearby task document; otherwise use: title, status, created date,
`Issue: #N`, context, goal or scope, plan or implementation notes, decisions,
verification, open questions. Mark inference as inference.

After moving a task document, `rg` for the old path and fix stale references
in `CHANGELOG.md` and other tracked Markdown.

## Closing and Commits

Close an issue only when the work is verified, and say in the closing comment
what was verified. Prefer closing through commit-body keywords (`Fixes #N`)
so the link is recorded in history; `gh issue close N --comment "..."` is the
fallback for issues resolved without a commit.

Keep commit subjects in this repository's existing plain sentence-case style
(`Add CI and Ruff static tooling`). Issue references belong in the commit
body, not the subject.

## Completion Checklist

Before marking work complete:

- an issue exists for every active task document, and each references the
  other;
- the issue description reflects the final scope if it changed materially;
- verification is summarized in the closing comment or closing commit;
- notable completed work has a concise `CHANGELOG.md` entry referencing the
  issue number;
- completed task documents are marked closed and moved to
  `doc/tasks_closed/`, with stale references fixed.
