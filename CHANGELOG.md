# Changelog

One entry per release of the harness. The number is in `VERSION`, and
[docs/architecture.md](docs/architecture.md) says what MAJOR, MINOR and PATCH
mean. The newest entry is first.

## 0.2.0 (unreleased)

Added:

- `floor-guard.py`, a command that flags a change that lowers the quality
  bar. It compares the working tree with the merge base of the base branch
  and reports six kinds of change: a deleted test, a test moved to where no
  runner collects it, a new skip marker, a new lint or type suppression,
  fewer assertion lines in a test file, and a workflow step that may now fail
  silently. It exits 0 when clean, 1 with findings, and 2 when it could not
  finish, so a run that failed never reads as clean. What it does not see is
  in `docs/known-limits.md`.

Changed:

- The skills `tui-design` and `ccdash-analysis` are removed. The harness
  keeps the skills of the development cycle, and these two were for one kind
  of product and one tool. The installer does not remove a link whose source
  is gone, so on a machine that installed them, delete
  `~/.claude/skills/tui-design` and `~/.claude/skills/ccdash-analysis` by
  hand.

Fixed:

- `codex-run --worktree` refuses a folder argument in the codex args in every
  spelling. Before, only `-C <dir>`, `--cd <dir>` and `--cd=<dir>` were seen,
  and `-C<dir>` went on to Codex. The rule is wide on purpose: a word that
  starts with one dash and holds a capital `C` is refused.
- `codex-run --role` leaves the role's model out when the caller's model is
  written as `-m<model>` or `-m=<model>`. Before, both models were sent and
  Codex refused the run.
- A run that the watchdog killed has its files compared with `--allow`, like
  any other run. Before, `codex-run` ended with 124 and printed no list. The
  code is still 124.

## 0.1.0 (2026-11-13)

The first release of the harness as its own repo. Before this it was part of
the terminal setup repo `ghostty-tmux-nvim`; this repo keeps that history, and
the tree starts from its commit `ba0b418`.

Carried over, with no change in behaviour:

- Global rules in `CLAUDE.md`, with review tiers, a merge rule and delegation
  rules, and the Codex rules built from them.
- Six workers: builder, verifier, adversary, planner, scout and scribe.
- Twelve skills, three wired hooks, two commands and two mods.
- The merge-gate hook, and a CI workflow that runs the tests on each pull
  request.

Added:

- A version for the harness: `VERSION`, this changelog, and a test that keeps
  the two in step.
- `docs/architecture.md`: the parts of the harness, where each lives, and what
  checks it.
- `docs/decisions/`: seven short records of the choices that are hard to
  reverse.
- The status line shows the harness version, and marks a working tree that is
  ahead of the tag, changed, or on another branch than `main`.

- Tests that run the real `install.sh` against a home folder in a temp dir,
  and a test that fails when a part under `config/` has no manifest row.

Changed:

- `install.sh` and `manifest.txt` install only the `claude` and `codex` groups.
  The terminal, editor and shell groups stayed in `ghostty-tmux-nvim`.

Fixed:

- `install.sh` now installs the six workers into `~/.claude/agents`. The
  manifest had no row for them, so a new machine got none.
- The status line no longer runs a program that a repo's own git config
  names: a file monitor, a clean or process filter, the same in a submodule,
  or the transport of a partial clone. It ran `git status` in the session's
  working folder, which can be any repo. A config with more filters than the
  script can switch off is not read, and the tree is marked `?`. The cost is
  in `docs/known-limits.md`.
