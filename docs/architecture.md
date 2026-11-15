---
title: The parts of the agent harness, where each lives, and how it is versioned
date: 2026-11-13
type: lookup
status: current
---

# Harness architecture

> [!tldr]
>
> The harness is the set of rules, workers, skills, hooks and checks under
> `config/claude/` and `config/codex/`. It has one version, in `VERSION` at the
> repo root. This document describes major version 0.

## Scope

The harness is this whole repo: the two folders above, their tests, the
installer, and the CI workflow that runs the tests.

Not covered, and not part of the harness version:

- Skills that are kept outside this repo.
- Plugins from a marketplace, and MCP servers.
- The memory notes that Claude Code keeps for each project.
- Terminal, editor and shell settings. They stayed in the repo that the harness
  came from (see [decision 0007](decisions/0007-own-repo.md)).

## Parts

The path of one change shows how the parts meet.

```mermaid
flowchart TD
  A[Request] --> B[Lead session reads the rules]
  B --> C[Lead builds, workers take split pieces]
  C --> D[Commit on a work branch]
  D --> E[Verifier, fresh context]
  E --> F{Tier 2}
  F -- yes --> G[Adversary until held]
  F -- no --> H[Pull request to dev]
  G --> H
  H --> I[CI runs the tests]
  I --> J[merge-gate hook checks the merge]
  J --> K[dev]
  K --> L[Release pull request to main]
  L --> M[Tag vX.Y.Z]
```

**What to notice:** only two steps are enforced by code, CI and the merge-gate
hook. Every other step is a rule that the agent must follow.

| Part        | Where                                 | Checked by                        |
| ----------- | ------------------------------------- | --------------------------------- |
| Rules       | `config/claude/CLAUDE.md`             | `tests/test_global_rules.py`      |
| Codex rules | `config/codex/AGENTS.md`              | `tests/test_codex_rules.py`       |
| Workers     | `config/claude/agents/` (6 files)     | `tests/test_worker_roles.py`      |
| Skills      | `config/claude/skills/` (13 folders)  | `tests/test_skill_frontmatter.py` |
| Hooks       | `config/claude/hooks/`                | `config/claude/hooks/tests/`      |
| Permissions | `config/claude/settings.json.tmpl`    | Not tested                        |
| Commands    | `config/claude/commands/` (2 files)   | Not tested                        |
| Mods        | `config/claude/mods/` (2 folders)     | Their own tests, not run in CI    |
| Status line | `config/claude/statusline-command.sh` | `tests/test_harness_version.py`   |
| CI          | `.github/workflows/ci.yml`            | The merge-gate hook reads it      |
| Install     | `install.sh`, `manifest.txt`          | `tests/test_install_manifest.py`  |

The [README](../README.md) says what each part does. The status line has two
test files: `tests/test_harness_version.py` covers the version segment of line
1, and `tests/test_statusline_git_safety.py` checks that its git calls run no
program named by a repo's config. Lines 2 and 3 are not tested.

Three hooks are wired in the settings template: `merge-gate.py` on
`gh pr merge`, `visual-artifacts-route.sh` on the Artifact tool, and
`second-brain-log-prompt.sh` on each prompt. `second-brain-sweep.sh`,
`floor-guard.py` and `comment-check.py` run on demand. The floor guard and
the comment check read a repo they do not trust, so they call git plumbing
only and treat every git warning as a failure.

The reasons for the main choices are in [decisions/](decisions/), one short
record each. Minor findings that were logged and not fixed are in
[known-limits.md](known-limits.md).

## Conventions

### Versions

The number in `VERSION` has three parts
(see [decision 0008](decisions/0008-a-version-per-pull-request.md)).

| Part  | It changes when                                    | Example                 |
| ----- | -------------------------------------------------- | ----------------------- |
| MAJOR | The architecture changes, or a habit stops working | The rule root moves     |
| MINOR | A pull request merges into `dev`: one step each    | 0.4.0 comes after 0.3.0 |
| PATCH | No step of this flow changes it; it stays 0        | 0.4.0, never 0.4.1      |

The middle number counts pull requests. It does not say how large a change is;
the changelog entry of the version says what it holds.

These count as MAJOR: a worker or a skill that is removed, a skill that is
renamed, and a short prompt that changes its meaning. While the major is 0,
the harness is still taking shape. A change that would be MAJOR takes the next
MINOR number like every other pull request, and its changelog entry lists it
under "Changed". Version 1.0.0 is the first version whose habits are meant to
stay.

### Releases

1. Work merges into `dev` through task pull requests. Each one raises MINOR by
   one in `VERSION`, adds the top entry of [CHANGELOG.md](../CHANGELOG.md) with
   its date, and has a title that starts with its number, such as
   `0.4.0: One version per pull request`. One pull request can take the next
   MAJOR in place of the next MINOR (1.0.0 after 0.9.0): the one that you
   name for it.
2. Two open pull requests cannot both hold the next number. The one that
   merges later is rebased onto `dev` and takes the number after it.
3. The release pull request from `dev` to `main` changes no number. `main`
   gets the number that `dev` holds.
4. After the merge, the tag `vX.Y.Z` goes on `main` for that number. The
   numbers between two releases have a changelog entry and no tag.
5. On a MAJOR version, this document is rewritten for the new major.

A test fails when `VERSION` and the top changelog entry differ, when an entry
has no date, when an entry is not the next number after the one below it, or
when this document names another major. No test reads a pull request: that a
title starts with the number, and that one pull request holds one number, is a
rule for the agent that opens it.

### Decision records

A record is one file in `decisions/`, named `NNNN-short-name.md`. It holds the
choice in one to three sentences and what was rejected. Write one only for a
choice that is hard to reverse. A record is never edited to change its meaning:
a later record replaces it and says so.

## Gotchas

- **Live files follow the checked-out branch.** `~/.claude` holds symlinks into
  this repo's working tree. A work branch checked out in the main tree is live
  at once. The status line shows the harness version and marks this state.
- **A session loads `CLAUDE.md` without its HTML comments.** A rule that sits
  only in a comment is not a rule. The size cap counts the loaded text.
- **`settings.json` is a rendered copy.** Claude Code writes to the live file,
  so it can differ from the template. `install.sh` stops when it sees that.
- **The merge gate is not a security boundary.** It checks the normal path,
  `gh pr merge` at the start of a command.
- **Two folder names are older than the repo.** Rendered files go to
  `~/.dotfiles-rendered/` and backups to `~/.dotfiles-backup/`. The live
  settings file is a rendered copy in the first folder, so a new name would
  leave it behind.
