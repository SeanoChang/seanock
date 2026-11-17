# Changelog

One entry per version of the harness. Each pull request into `dev` is one
version, with the date of the pull request. A version that reached `main` has
the tag `vX.Y.Z`. The number is in `VERSION`, and
[docs/architecture.md](docs/architecture.md) says how it moves. The newest
entry is first.

## 0.11.0 (2026-11-17)

Changed:

- `handoff`: the note has a folder of its own. `frame` writes
  `.claude/handoff/note.md` and keeps the last note as
  `.claude/handoff/prev.md`. It makes the folder with a `.gitignore` of one
  line, `*`, so the folder ignores itself, and it no longer writes into the
  exclude file of the repo. The script reads, writes and renames every file
  through the folder that it opened one time, with no link followed.
  Both commands end with exit 2 when git tracks a file in the folder (under
  any letter case), when git finds another git folder from inside `.claude`
  or the folder than from the top of the work tree, and when the folder
  holds files of the user and is not one that `frame` made.

Fixed:

- `handoff` of 0.8.0 wrote three names into `.git/info/exclude`, which all
  work trees of a repo share. A file of the user with one of those names in
  another work tree (`.claude/handoff.md`, or `HANDOFF.md` on a Mac) was
  gone from `git status`, and `git worktree remove` then deleted it with no
  warning. A test holds the case for six names; four of them fail on the
  script of 0.8.0.

A note that 0.8.0 wrote at `.claude/handoff.md` is not read and not moved,
and the lines of 0.8.0 in an exclude file stay. 0.8.0 was in no release.

## 0.10.0 (2026-11-17)

Added:

- Two live tests of the reference rule on a reviewer, behind
  `REFERENCE_RULE_LIVE=1`, which now starts five sessions. A session with
  the verifier's instructions gets a page that was built with two parts of
  the prototype dropped. Without the rule it does not fail the page; with
  the rule its verdict is "fail" and it names both parts. The page is one a
  real session built before the rule, kept in `tests/reference_fixture.py`.
  In 15 runs before the first review, 0 of 9 reviewers failed the page
  without the rule and 6 of 6 failed it with the rule.

Changed:

- `docs/known-limits.md`, on reference builds: the counts of the reviewer
  runs, and why the instructions of the builder and the verifier stay as
  they are. One sentence from the review of 0.9.0 is corrected: a list of
  parts that misses a view "can pass" the check, not "passes" it.

## 0.9.0 (2026-11-17)

Added:

- A rule for reference builds, in `config/claude/CLAUDE.md` and in both
  modes of the Codex file. When Sean points at a prototype, a design or a
  screenshot, or says "exactly as", that reference is the spec. The agent
  lists its parts first. Each part ends built, as an empty state when its
  data does not exist yet, or cut with his yes. No other rule gives leave to
  cut a part, and an agent with a brief builds the rest and reports the part.
  An approved design goes into the plan as a build task, a part that needs
  tier 2 review gets its own task, a brief hands over the reference itself,
  and the report starts with what is missing. The source is a measured
  session: twice in it, an agent built its own summary of an approved
  prototype and left out the parts the app had no data for, with no word of
  the cuts in its report. `tests/test_reference_rule.py` pins the sentences
  and has three live sessions behind `REFERENCE_RULE_LIVE=1`.

Changed:

- Unattended mode: after a blocked step the agent takes the next task in the
  plan. When the plan has none, it ends the run and does not pick work of
  its own. In the same session, an agent with its task blocked started a
  refactor that nobody had asked for.
- The size caps of the two rules files are raised for the new rule: the
  Claude file from 15,650 to 16,450 characters and from 230 to 241 lines,
  the Codex file from 16,000 to 16,384 bytes.

## 0.8.0 (2026-11-15)

Added:

- `handoff`, a skill that writes a resume note for the work in a work tree,
  and that starts a session from such a note. Its script `handoff.py frame`
  writes `.claude/handoff.md` with what git knows (the branch, the commit,
  each file that is not committed) and the empty parts of the hand-off
  outline of `writing-markdown-docs`. The agent writes the parts.
  `handoff.py check` lists what the note lacks (a part with no text, no open
  next action, a first action with no command, a path of the tree that is
  gone) and where the tree is no longer as the note says (another commit,
  another branch, other changes). It exits 0 with `handoff: ok`, 1 with a
  summary, and 2 when it could not finish. Git does not see the note: the
  script adds its name to the repo's exclude file. The fact block ends with
  a check value, so a block that was changed by hand is an error and the
  way from a stale note to `ok` is a new `frame`. One `frame` at a time
  works in `.claude/`. `frame` moves and replaces only a note that `frame`
  wrote, and both commands refuse a note that git tracks (in any letter
  case), a note behind a link, and a `.claude` that is a repo of its own.
  The list of changes is the one of `git status`, and the settings of the
  repo for new files, renames and submodules do not change it. The commands of the skill text start Python in
  isolated mode, so no file of the work tree is loaded as a module. A note
  carries no approval, and the skill commits and pushes nothing.

## 0.7.0 (2026-11-15)

Changed:

- `docs/known-limits.md`, on `maintain-verify-skill`: the point on a lookup
  path of the caller that leads into the project is corrected after the
  last review round of 0.6.0. It names two more files that load through an
  empty or relative entry of `PYTHONPATH` (`__future__.py` and
  `sitecustomize.py`). It no longer says that every Python script in that
  folder loads them. It says that only a start that is isolated from
  outside (`python3 -I`) covers that way, and what the fix for `PATH` has
  to test. No code changed.

## 0.6.0 (2026-11-15)

Added:

- `maintain-verify-skill`, a skill that keeps a project's verify skill true
  to the project. Its script `drift.py check` lists where the skill no
  longer fits: a path of the feature map that matches no tracked file,
  tracked files that no feature covers, a map that the current runner
  refuses, a gap that the runner's doctor finds, a step or a build that
  fails in a real run of the runner, and a runner copy that is not the
  current runner. The runner itself is asked at each stage, and the copy is
  started only when it is the current runner, byte for byte, and in
  Python's isolated mode, so no module beside it is loaded.
  `drift.py bump` replaces the runner copy, and refuses when the map is for
  another runner number. The skill text says what to do with each finding.
  A failed step is first a finding about the app: a step changes only when
  the step is shown to be wrong, and it is never weakened to get a pass.

Changed:

- `docs/known-limits.md` also gets the points that the last review rounds
  of `merge-follow-up` and `setup-repo` left open. They are in the lists
  "Minor" of those two sections.

## 0.5.0 (2026-11-15)

Added:

- `comment-check.py`, a command beside the floor guard. It reads the
  committed diff against a base and lists each comment block whose code
  changed while no line of the comment did: `stale-comment <path>:<line>
  <text>`. It reads line comments (`#` and `//`) in the common languages,
  runs no program that a repo's settings name, and exits 0 when clean, 1
  with hits, and 2 when it could not finish. The verifier runs it on each
  change (step 9 of its text). A hit is advice: a comment that still holds
  is no finding, a comment that is now false is a Nit, and it is Important
  only when a reader who trusts it would use the code wrongly. A check that
  did not run goes under "Not covered" and changes no verdict.

## 0.4.0 (2026-11-15)

Changed:

- Every pull request into `dev` is a version of its own. It raises the middle
  number of `VERSION` by one, adds its entry here with its date, and has a
  title that starts with the number, such as `0.4.0: One version per pull
  request`. Before, the number changed once per release, and the pull
  requests of a release shared one entry; 0.3.0 below is the last entry of
  that kind. A release to `main` changes no number: `main` gets the number
  that `dev` holds, and that number gets the tag. Two tests hold the
  changelog to the rule: each entry has a date, and each entry is the next
  number after the one below it.

## 0.3.0 (2026-11-15)

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
- `correct`, a skill that only you start. It turns a correction that you
  gave more than once into a rule that code holds up. Its script `rules.py`
  lists candidates from the repo (reverts, files that were fixed again and
  again) and from your prompt history for the project. A line in which
  anything looks like a secret is held back whole, and the pasted content of
  a prompt is never read. The rules of a project are one table,
  `.claude/rules.json`: each row names the rule, the layer that holds it,
  and the file that fails when the rule is broken. `rules.py check` lists
  each rule that nothing holds up, and `rules.py table` writes the table
  that you read, `.claude/rules.md`.

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
