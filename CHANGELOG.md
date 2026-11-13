# Changelog

One entry per release of the harness. The number is in `VERSION`, and
[docs/architecture.md](docs/architecture.md) says what MAJOR, MINOR and PATCH
mean. The newest entry is first.

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
