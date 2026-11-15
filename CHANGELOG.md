# Changelog

One entry per release of the harness. The number is in `VERSION`, and
[docs/architecture.md](docs/architecture.md) says what MAJOR, MINOR and PATCH
mean. The newest entry is first.

## 0.3.0 (unreleased)

Added:

- `create-verify-skill`, a skill that only you start. It writes a verify
  skill into a project: a feature map (`features.json`), a runner (`verify`)
  that builds the app and runs the steps of each feature in a fresh temp
  folder, and a short skill file that tells an agent how to run it and how to
  read the result. The runner keeps the output of every step in
  `.verify/runs/` and names the commit that it ran on. It exits 0 on a pass,
  1 when a step failed or could not start, and 2 when the run could not
  finish, and a result counts only with the runner's own last line. This
  version runs command-line steps only. What it does not do is in
  `docs/known-limits.md`.
- `merge-follow-up`, a skill for the steps after a merge. Its script
  `pr-state.py` reads your pull requests with `gh` and reports what changed
  since the session last looked: a merge, a close, a failed check, and the
  open pull requests that are now behind their base. It only reads, and it
  refuses any other `gh` call. Each session gets each event once. The skill
  then rebases the session's own task branches, reinstalls once for all
  sessions, and takes the next task. It never merges, and it never rebases
  or pushes a branch that takes pull requests: the script marks those
  (`shared_head`). A branch name that is not a plain name is held back, so
  it cannot reach a command. For a release that was merged as one commit
  with one parent, it prepares the commit that joins the two histories
  again and hands you the push.
- `setup-repo`, a skill that prepares a repo for the rules in `CLAUDE.md`.
  Its script `repo-doctor.py` only reads. It lists eight things as `ok`,
  `gap` or `unknown`: an integration branch, a workflow that runs the tests,
  a workflow that runs a lint or type check, a root map that names the
  build, test, lint and install commands, `REVIEW.md`, the comment rule in
  the root map, the rule table, and a verify skill. The skill then fills the
  gaps that are the agent's to fill: the workflow in a pull request of its
  own at tier 2, and the four text gaps in one pull request. The first
  workflow of a repo is merged by you, and so is the choice of the paths in
  `REVIEW.md`. The script prints no text from the repo, only fixed words and
  counts.

Removed:

- `scaffold-docs`. It wrote a `CLAUDE.md` into each folder and a changelog
  of the tree, and those files went out of date with each change.
  `setup-repo` writes one root map in its place. `orient` stays and reads
  that map. A copy that is already linked into `~/.claude/skills` is not
  removed by the installer.

Fixed:

- `floor-guard.py` no longer follows a `core.worktree` setting to another
  folder. When that folder was another repo, the guard read it and printed
  `floor-guard: clean` for a tree that had lost a test. Now the top of the
  work tree that git names must be the folder that holds the `.git` of the
  tree you named; anything else ends the run with exit 2. A submodule
  checkout and a linked work tree are read as before.

## 0.2.0 (2026-11-14)

Added:

- `floor-guard.py`, a command that flags a change that lowers the quality
  bar. It compares the working tree with the merge base of the base branch
  and reports six kinds of change: a deleted test, a test moved to where no
  runner collects it, a new skip marker, a new lint or type suppression,
  fewer assertion lines in a test file, and a workflow step that may now fail
  silently. It exits 0 when clean, 1 with findings, and 2 when it could not
  finish, so a run that failed never reads as clean. The verifier runs it on
  each change it checks: a hit that the stated goal does not ask for fails the
  change. What it does not see is in `docs/known-limits.md`.

Changed:

- Claude Code adds no attribution to commits and pull request descriptions.
  The settings template sets `attribution` with an empty `commit` and `pr`
  and no session link. Before, only the rule "No AI attribution" in
  `CLAUDE.md` stood against the trailer that Claude Code adds by default, and
  a session could miss it. The rule stays, for Codex and other agents. A
  machine gets the key when `install.sh` renders the template again.
- The skills `tui-design` and `ccdash-analysis` are removed. The harness
  keeps the skills of the development cycle, and these two were for one kind
  of product and one tool. The installer does not remove a link whose source
  is gone, so on a machine that installed them, delete
  `~/.claude/skills/tui-design` and `~/.claude/skills/ccdash-analysis` by
  hand.
- Two working rules in `CLAUDE.md`. Checkpoints: you say where you review, and
  between those points the agent does not stop to ask about reversible work;
  it decides, reports the decision and goes on. The standing checkpoints are
  the design gate, a release PR and everything under Safety. The design gate:
  before the code of something new, the agent shows one page with the data
  shapes, the interfaces as signatures with empty bodies, and what the
  concurrent actors share, and waits. A fix that failed three times now ends
  in a question; two failures still mean "question the diagnosis". A question
  or a thought said out loud still gets an assessment, not a build. The size
  cap of the rules file went from 15,100 to 15,650 characters for this.

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
