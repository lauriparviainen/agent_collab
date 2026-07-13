# Go-public checklist

**Status:** Open — publication blocker.

**Created:** 2026-07-12. Final audit remediation merged 2026-07-13.

**Issue:** [#14](https://github.com/lauriparviainen/agent_collab/issues/14)

**Related:**
[workdir-limits-and-workspace-trust.md](../tasks_closed/workdir-limits-and-workspace-trust.md)
(issue #13), [SECURITY.md](../../SECURITY.md), and
[release/SKILL.md](../../.claude/skills/release/SKILL.md).

## Context

The repository is private and is intended for public release. Several settings
and verifications only make sense — or are only possible — when visibility
changes. `SECURITY.md` already points reporters at GitHub's private
vulnerability reporting, but that feature exists only on public repositories:
the API returns 404 while the repository is private, so it cannot be enabled
in advance.

A final audit was run while the repository was still private. It covered the
tracked tree, all reachable Git history, GitHub issues and comments, release
notes, retained Actions logs, package contents, public-facing documentation,
repository metadata, and the then-untracked task drafts in the working tree.

No real API key, token, password, private key, or other credential was found.
Signature-based scans and `detect-secrets` found only deliberate test fixture
values after review. The repository is nevertheless not ready to become public:
the audit found a security boundary that needs a broader fix, publication
privacy decisions, onboarding defects, and repository-hygiene work.

This document is both the durable remediation record for issue #14 and the
ordered visibility-flip checklist. The narrower workspace-trust design is
recorded in the closed `workdir-limits-and-workspace-trust.md`; issue #14 owns
the combined release gate and must stay synchronized with material scope
changes.

## Audit baseline

Evidence captured on 2026-07-13:

- 227 tracked files and all 116 reachable commits were scanned for known
  credential signatures, credential-bearing URLs, private keys, JWTs, and
  high-entropy or keyword-based secret candidates;
- GitHub issue and pull-request bodies, issue comments, review comments,
  commit comments, and release notes had no unexplained secret candidates;
- logs from all 44 retained GitHub Actions runs had no secret candidates, and
  the repository had no uploaded Actions artifacts;
- the then-untracked task drafts had no credential candidates; private-side
  integration design material has since been removed from this working tree;
- the built wheel contained only the intended package modules, backend
  manifests/readmes, MCP guidance, license, and distribution metadata;
- `./agent_collab_dev.sh test` passed Ruff lint, Ruff formatting, and 695 hermetic
  tests;
- `./agent_collab_dev.sh build --check` passed;
- an isolated SDK-free wheel installation imported and rendered CLI help;
- the latest CI run on `main` passed; and
- `git fsck --full --no-dangling` reported no repository-integrity errors.

This baseline is point-in-time evidence, not a substitute for rerunning the
checks on the final pre-publication commit.

## Pre-flip blockers

### 1. Close the complete project-config workspace-trust gap

**Completed 2026-07-13.** Issue #13 and its closed task document cover the
complete field set. The original narrow finding was that project
`.agent-collab/config.toml`
could replace an enabled agent's `command` and `args`, while
`agent_collab/config.py::_merge_agent` also accepts:

- `type`, `enabled`, and `backend`;
- `env` and `cwd`;
- backend-specific static configuration; and
- dynamic backend `options`.

Those fields affect executable lookup, process/module loading, working
directory, provider selection, cost, and provider permission posture. Current
option schemas include values equivalent to bypassing permission prompts and
granting full filesystem access. Stripping only `command` and `args` therefore
does not establish a workspace-trust boundary.

The implemented coherent rule is simpler: project config never alters
execution-relevant agent settings and cannot define new agents. Those settings
belong in global user config, with no trusted-workspace exception. Project
config may set display names and compose workflows only from agents already
enabled globally. Optional user-global `[workdir].restrict_workdir_roots` confinement
treats a missing or empty list as unrestricted, accepts broad roots or one
specific exceptional directory, and cannot be widened by project config.

Completed follow-through includes protected-category tests with environment and
permission-option examples, built-in and project-only agent cases, safe workflow
composition, sanitized start/discovery warnings, and updated security and
configuration documentation. A `workdir` is documented as a config root and
default cwd, not an operating-system containment boundary.

### 2. Keep private integration design material outside this repository

**Resolved for the current working tree on 2026-07-13.** The private-side task
drafts found during the audit were removed from this repository. The final
content audit must still verify that no David AI design documents are present
in the tree or anywhere in history. Those integration design documents live in
the David AI repository by decision; the only permitted public references are
the README's "Built alongside David AI" section, the matching changelog line,
and generically framed connector issues.

Do not use an unreviewed `git add -A` before publication. Review
`git status --short`, inspect every new file selected for the final commit, and
rerun the public-content and secret scans. Public issues and task documents
must describe integration work only by public-safe category; do not copy
private architecture, credentials, local paths, or personal data into them.

### 3. Decide how to handle historical identity and machine-local metadata

Reachable commit and annotated-tag metadata contains a non-noreply
organizational email address. Current and historical content also contains
machine-local absolute paths, including references to another local project.
Affected current-tree categories include closed task documents, one captured
CLI fixture, and a TUI test; deleted versions of README/design files retain
additional occurrences in history.

**Decision recorded 2026-07-13.** The maintainer accepts the author identity
and historical machine-local paths as intentional public metadata. Preserve
the existing commit history, annotated tags, and GitHub Releases; do not
perform history/privacy remediation. Historical versions will remain unchanged;
current documentation, fixtures, and tests were separately subject to
neutralization before the visibility flip.

Do not rewrite history, move tags, delete releases, or re-tag as an incidental
cleanup. Existing releases and annotated tags make that an outward-facing
release operation governed by the release skill, and the repository rule is
never to force-move a published tag. If the disclosure is unacceptable, stop
and agree on a dedicated migration procedure before changing any ref.

**Current-tree cleanup completed 2026-07-13.** Closed task-document examples
and the TUI wrapping test now use neutral paths. The captured CLI fixture's
machine-local documentation link was replaced with neutral prose; its exact
target was not part of the parser behavior under test.

### 4. Make the documented MCP installation path work

**Completed 2026-07-13.** Direct authenticated Streamable HTTP is the preferred
installed-user transport: Claude and Codex connect straight to the running
daemon's loopback `/mcp` endpoint using the permanent user-config token. The
README now documents both clients and calls out that their saved headers are
credentials.

For users who do not want to configure direct HTTP headers, the secondary
stdio adapter is `agent-collab mcp`. It reads the daemon URL and token from
agent-collab configuration and still connects to the daemon; it does not own
sessions. The package and durable installer expose one public executable,
`agent-collab`. The separate `agent-collab-mcp` console script and hidden
`--mcp-server` flag were removed before public release, with no compatibility
alias by decision.

CLI dispatch/help and MCP behavior are covered by hermetic tests. A fresh
isolated installation verified that the package exposes only the intended
console command and that both `agent-collab mcp` stdio initialization and
authenticated Streamable HTTP initialization succeed.

## Repository hygiene and documentation

### Broken links

**Resolved 2026-07-13.** Corrected the seven broken relative links found in
these committed documents:

- `doc/tasks_closed/daemon-permanent-config-token.md`;
- `doc/tasks_closed/stage-5.4-daemon-robustness-and-code-health.md`;
- `doc/tasks_closed/stage-5.2-calm-tui-cleanup/samples/directed-input.md`;
- `doc/tasks_closed/stage-5.2-calm-tui-cleanup/samples/error-state.md`; and
- `doc/tasks_closed/stage-5.2-calm-tui-cleanup/samples/new-session-flow.md`.

A repository-wide filesystem check confirmed that every relative Markdown link
target now exists. Adding the same check to the local or CI gate remains an
optional follow-up if it can stay small and dependency-light.

### Prevent accidental secret and generated-file commits

**Completed 2026-07-13.** `.gitignore` now covers local virtual environments,
Python tool caches, `.env` variants, coverage output, OS metadata, and common
private-key/container extensions. Intentional `.env.example` and
`.env.*.example` files remain allowlisted, and no broad configuration filename
is ignored. Representative paths were verified with `git check-ignore`; a
tracked-file scan found no intended fixture in the newly protected categories.

After the repository becomes public, enable and verify GitHub secret scanning
and push protection when available, alongside the dependency graph and
Dependabot alerts.

### Public repository metadata and tracking

- **Description completed 2026-07-13.** GitHub now describes agent-collab as a
  local CLI and daemon for supervising collaboration between Claude Code,
  Codex, Grok, Gemini, and other AI coding agents over MCP.
- **Topics completed 2026-07-13.** Added `ai-agents`, `multi-agent`,
  `developer-tools`, `model-context-protocol`, `claude-code`, `codex`, `python`,
  and `local-first`.
- **Release tracking verified 2026-07-13.** Closed issue #13 and open issue #14
  are both in the `Public release` milestone; keep #14 there until this
  document closes.
- **Task tracking reconciled 2026-07-13.** Every active task document has a
  matching issue. The older `sdk-session-control.md` design now links issue #20
  and prominently requires a refresh against the current codebase before
  implementation.
- After the flip, verify the CI badge, community profile, vulnerability-report
  link, dependency/security settings, and branch protection.

## Implementation sequence

1. Expand issue #13 and implement the complete untrusted-project-config policy
   with tests and documentation.
2. Decide the historical identity/path disposition; apply only the approved
   current-tree or history-level remediation.
3. Correct the MCP installation contract and verify both documented client
   registrations.
4. Fix ignore rules, task tracking, and public metadata.
5. Rerun the complete verification matrix below on the final commit.
6. Choose the next version according to the release skill: fixes alone may be
   a patch; a new user-visible trust/confinement feature requires a pre-1.0
   minor release.
7. Only after explicit approval, perform the visibility flip and post-flip
   operations below.

## Visibility-flip checklist

Ordered; the audit comes first because the flip publishes all history at once.

1. **Complete every pre-flip blocker above.** Record explicit maintainer
   decisions for historical identity/path metadata and the MCP install
   contract.
2. **Rerun the pre-flip content audit.** Check the entire reachable Git history,
   all issues, comments, labels, retained Actions logs, and final package for
   secrets, tokens, machine-specific paths or hostnames, and personal data.
   Confirm that no private integration design document is present.
3. **Run the final release gates.** The final commit must pass the verification
   matrix below and CI. Coordinate the version, changelog, tag, and GitHub
   Release under the release skill so outside users receive a coherent first
   public release.
4. **Flip visibility only after explicit approval:**

   ```bash
   gh repo edit lauriparviainen/agent_collab --visibility public
   ```

5. **Enable private vulnerability reporting** (fails with 404 until public):

   ```bash
   gh api -X PUT repos/lauriparviainen/agent_collab/private-vulnerability-reporting
   ```

   Verify with the matching `GET`, and confirm the
   `/security/advisories/new` link in `SECURITY.md` works for a logged-in
   non-collaborator viewpoint.
6. **Enable repository security features.** Verify GitHub secret scanning and
   push protection, dependency graph, and Dependabot alerts.
7. **Verify public rendering.** Confirm the README CI badge resolves, Actions
   runs are publicly visible, and GitHub's community profile recognizes
   `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, and the bug-report issue
   template.
8. **Configure public repository metadata.** Verify the description, topics,
   default branch, and release presentation.
9. **Consider branch protection on `main`.** Requiring CI and restricting
   force-pushes is optional for a single maintainer but inexpensive once public
   pull requests are possible.
10. **Close the release gate.** Close issue #14 and the `Public release`
    milestone only after every required item is verified. Mark this document
    closed and move it to `doc/tasks_closed/`, then repair any stale links.

## Verification

Required before closing issue #14 or changing repository visibility:

```bash
./agent_collab_dev.sh test
./agent_collab_dev.sh build --check
git status --short
git fsck --full --no-dangling
```

Also verify:

- project-scope config tests prove every execution- and permission-affecting
  field is ignored/rejected unless explicit user trust is established;
- a clean source install can initialize the preferred authenticated Streamable
  HTTP endpoint and launch the secondary `agent-collab mcp` stdio adapter using
  the documented contracts;
- the built wheel contains only intended files and an SDK-free isolated install
  can run `agent-collab --help`;
- relative Markdown links resolve;
- current tree plus reachable Git history have no unexplained secret-scanner
  candidates;
- GitHub issues/comments/releases and retained Actions logs have no unexplained
  secret candidates;
- the final CI run on the release commit is green;
- package version, changelog, tag, and GitHub Release remain aligned; and
- every checklist item is completed or explicitly deferred by the maintainer.

Credentialed integration tests are required only for behavior changed in a
provider backend; otherwise keep the final gate hermetic and avoid paid model
calls.
