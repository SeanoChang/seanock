# Known limits

Findings from reviews that were real but minor, so they were logged and not
fixed. Each entry says what happens and why it was accepted.

## `config/claude/hooks/merge-gate.py`

Reviewed on 2026-11-10 at sha1 `9443fb995e1be73846dbd741a4d54f649e0e8375`.

### By design

- **A merge that does not begin the command gets no decision.** This covers
  `cd x && gh pr merge …`, an env prefix, a wrapper such as `timeout`, a path
  such as `/opt/homebrew/bin/gh`, a quoted or escaped spelling, and a leading
  comment line. The normal permission flow handles these. The hook does not
  look for a merge inside a larger command, because that needs a full model
  of bash.
- **The hook is not a security boundary.** `gh api`, `git push` to the
  integration branch, a script, and a forged review comment all go past it.
  The review comment is written by the same account by design.
- **A repo with no CI can never pass.** A check that has not registered yet
  is not visible to the hook. The rules tell the agent to propose a workflow
  in a PR; Sean merges the first one.
- **`main`, `master` and `trunk` are refused under any setting.** The hook
  denies these bases, in any letter case, even when `MERGE_GATE_BASE` names
  one. An agent that set the variable itself cannot open the gate to the
  release branch. A release branch with another name (`production`,
  `release`) is refused only because no setting names it.
- **A base name from the `gh` answer is shown in the deny reason as it is.**
  A branch named with odd characters (a no-break space, a backtick) can make
  the reason read like other text. The decision is still a deny, and only
  someone who can create a branch in the repo can choose the name.
- **The integration branch is known by its name.** The hook accepts `dev`,
  `develop`, `development` and `integration`. It does not ask GitHub which
  branch is the default, so a repo whose default branch has one of these
  names lets an agent merge into it. In a git-flow repo that is the intent:
  `develop` is the default branch and the integration branch. A branch with
  any other name needs `MERGE_GATE_BASE`, and then only that branch is
  accepted. `staging` is not on the list, because a merge into it often
  starts a deploy.
- **Argument errors are reported before the lookup.** A command with a bad
  flag gets only the flag errors, not the PR conditions as well.
- **A help request must be in plain words.** `gh pr merge --help | cat`, or
  help with a quote, a brace or a glob, is denied.
- **`~`, a glob or a brace in a text flag value is passed literally.** The
  rebuilt command quotes every value, so bash does not expand it.

### Minor, not fixed

- **A payload Python cannot parse gets no decision.** Examples: an integer
  of more than 4,300 digits, or nesting about 100,000 deep. The hook cannot
  read the command, and the normal permission flow applies. Claude Code does
  not send such payloads.
- **More than 1,024 inherited file descriptors make every merge a deny.**
  The reader uses `select`, which cannot take a descriptor above 1,023. It
  fails closed. `selectors.DefaultSelector` would remove the limit.
- **A valid answer close to 16 MB can use about 450 MB of memory.** The byte
  cap bounds the text, but `json.loads` builds objects about 30 times larger
  for an answer made of millions of empty objects. Real `gh` does not print
  that shape.
- **A child that `gh` leaves behind after a successful lookup keeps
  running.** The hook kills the process group only when the lookup fails.
  Real `gh` leaves no such child.
- **The hook's own stdout closed or broken gives a non-zero exit.** The
  harness could not read an answer in that case anyway.

### Tested in a real session (2026-11-10, Claude Code 2.1.319)

Headless `claude -p` runs with a fake `gh` first on PATH, in a folder that is
not a git repo:

- A valid merge written as `--match-head-commit <sha>` ran without a prompt,
  and `gh` received the rebuilt `--match-head-commit=<sha>`. So Claude Code
  applies `updatedInput` together with an allow.
- A merge without the sha, with `--admin`, or with `--delete-branch` was
  refused with the hook's reason, and `gh` was not called.
- `gh pr checks 12` and `cd . && gh pr merge …` got no decision from the hook
  and were stopped by the normal approval.

### What the gate takes from CI (2026-11-11, with the first workflow)

The automated security check raised these when `.github/workflows/ci.yml`
was added. Both are in the gate and in GitHub's defaults, not in the
workflow file.

- **The gate takes any green check, not the check `checks` of the workflow
  `ci`.** `ci_problem()` wants every check on the head commit green and at
  least one that succeeded. It does not ask for a check by name. A
  `pull_request` run uses the pull request's own version of the workflow
  file. So a pull request that takes a test step out of the workflow is
  still green. What stands against this: by the rule in
  `config/claude/CLAUDE.md` a change to CI is tier 2, so a verifier and an
  adversary read the diff before the review line is written. The gate does
  not hold a pull request to that tier: it does not read which files
  changed. To close it further: name `checks` as a required status
  check in the branch protection of `dev` and `main` (a setting of the
  owner), and let the gate ask for that check by name. A required name
  stops a pull request that drops or renames the job. It does not stop one
  that keeps the name and weakens the steps; only the review does.
- **A green result is for the base as it was when CI ran.** A
  `pull_request` run tests the merge of the head commit with the base
  branch at that time. The gate binds CI and the review to the head commit
  only, and `--match-head-commit` checks the head only. When `dev` moves
  after the run, the head stays green for a merge that CI did not test.
  What stands against this: the workflow also runs on each push to `dev`,
  so a bad merge shows after it, not before. To close it: "Require
  branches to be up to date before merging" in the branch protection, or a
  gate that compares the base commit of the run with the base of the pull
  request.

Two wording points from the adversary's pass on the workflow (2026-11-11,
at `d76ffa2`). Neither lets a bad merge through.

- **"It gets no secret and no write token" (a comment in `ci.yml`) is too
  short.** The job gets GitHub's automatic token, and both actions receive
  it. The log of run 43111198048 gives its rights as `Contents: read` and
  `Metadata: read`, and then the line `Cache mode: write`. So the token
  cannot write to the repo. No step of the workflow uses the Actions cache,
  and no `run:` step names the token. A truer comment: "a read-only token
  and no other secret".
- **"A bad merge shows after it" (the point above) can be one commit
  late.** The push runs on `dev` share one group with `cancel-in-progress`.
  A second push to `dev` while the first run is in progress cancels that
  run. The first commit then has a cancelled run and no result. The run for
  the second push tests a tree that holds both merges, so the bad merge
  still turns CI red, on the later commit. This is read from the workflow
  file; it was not tried.

### Not tested

- A real merge on GitHub. The first one is the remaining check.
- Shell state that Claude Code adds: an alias or function named `gh`, or
  `GH_REPO` / `GH_HOST` set in the shell but not in the hook's environment.
- Whether the payload's `cwd` follows an earlier `cd` in the Bash tool. If it
  does not, a merge with no selector could look up another branch.
- Commands run under zsh. The tokenizer was compared with macOS `/bin/bash`
  3.2 only. The rebuilt command and plain-words help are safe under zsh by
  reasoning, not by test.
- `gh` versions other than 2.103.3, and real GitHub answers for edge cases
  (more than 100 checks, comment paging, stale check runs).

## Review rules (`config/claude/CLAUDE.md`, `config/claude/agents/`)

Reviewed on 2026-11-10 at commit `0a4b43a`. One review read the rules for
gaps, contradictions and loops, and a second one in fresh context checked its
list. Three Important gaps were fixed: a reviewer could run a command that
needs approval, a diff could outgrow its task's tier, and no rule covered a
repo with no `dev` branch or no CI.

### Minor, not fixed

- **"Less adversary" has no rule for a re-check that reports broken.** It
  allows one adversary pass and one re-check. A broken re-check is the second
  failure, and the third round never comes. The merge gate still needs
  ` adversary=held`, so the merge is denied: it fails safe.
- **No rule says a branch must be up to date with `dev` before the squash.**
  Two PRs, each reviewed against an older `dev`, can merge into a combination
  that no reviewer saw. CI on each head commit limits this.
- **The verifier's "uncommitted changes" rule does not name untracked or
  unrelated files.** One verifier leaves the review line out for an untracked
  file that is not part of the change; another keeps it.
- **A fix round can start on a finding that no refuter checked.** The refuter
  rule covers findings that reach Sean, not findings that start a fix. A false
  Important then costs one round and counts toward the limit of three.
  "Release-blocking" is not defined.
- **The Plan page example drifts from the eight-line brief.** In
  `visual-artifacts/references/plan.md` the task's Files row lists one file
  and its copied brief lists two, and the copied Files line has no "must not
  touch" part. `builder.md` ("Touch only the files in your brief") still
  bounds the edit.

### Minor, from the re-check of the fixes (not checked by a second reviewer)

- **An adversary that skips an attack because it needs approval can still
  report held.** The verifier's version of the rule forces "unverified". The
  adversary's version only moves the attack to Not tried.
- **"Tier too low" reads the tier 2 list, not `REVIEW.md`.** The verifier
  checks the diff against the list in its own step 4. A path that a repo's
  `REVIEW.md` names as always tier 2 may not raise the tier.
- **The milestone rule assumed `dev`.** Closed on 2026-11-10 by Sean's
  decision: the agent uses the repo's own integration branch and creates
  `dev` only when there is none. The rule lists the cases in order, and only
  Sean sets `MERGE_GATE_BASE` for a branch the gate does not know. The
  milestone PR goes from that branch to the release branch. For a repo with
  no CI, the agent opens a PR that adds a workflow, and Sean merges that first
  workflow himself.
- **The agent judges whether a repo "integrates on another branch".** When it
  cannot tell, the rule says not to create `dev` and to list the task under
  "Blocked on me". With `dev` and `develop` both present, the rule takes
  `dev`, which can be the stale one. The same holds for an abandoned `dev` in
  a repo that now integrates on a branch with another name: the rule picks
  `dev` by its name, and the gate accepts it. The work lands on a branch that
  is not a release branch, so it can be recovered.
- **Three cases in the no-CI rule are left to one sensible reading:** a PR
  that adds the first workflow can turn its own check green, and only "I
  merge the first workflow" stops the agent from merging it; a repo whose CI
  does not run on PRs into the integration branch reports "no checks" without
  being a repo with no CI; and a change to that trigger is not named as "the
  first workflow".
- **No rule covers a task that depends on a blocked, unmerged task** (older
  than this change): build on its branch, or skip it.

## `config/claude/skills/brief-artifact`

Reviewed on 2026-11-10: verifier pass and adversary held at commit `f6a135e`
(option cards and the section pane), and before that at `c210cd4`.
The first verifier pass was at `65ac836`. The adversary then found two
Important breaks, both fixed: a `data:image` link passed the parser's own link
check, and nesting past the parser's limit of 20 dropped the rest of the file
without an error.

### By design

- **A link target that is not `http`, `https`, `mailto:` or `#` stays as
  text.** This includes relative paths. `[text]()` keeps its text and is not
  a link.
- **Nesting deeper than six lists or thirteen quotes is an error.**
- **Answers are not kept across a reload**, and the page sends nothing back.
- **No images, math, charts or raw HTML.** Raw HTML is shown as text.
- **A callout inside a list item, and a lead of more than one paragraph, are
  plain quotes.**

### Minor, not fixed

- **A `## ?` decision whose list has no bold label becomes a free answer with
  no warning.** The build summary still says "free answer" for it. A list in
  a decision body can be ordinary context, so the script cannot refuse it.
- **An h2 that starts with `?` is always a decision.** `## \? Text` is one
  too, because the parser joins the escaped character back into the text.
- **No space is needed after the `?`.** `## ?**Bold** text` is a decision.
- **A build that fails leaves an earlier page at the output path.** The exit
  code is 1 and nothing new is written, but the old file can still be
  published by mistake.
- **A pick-one answer cannot be cleared.** After a click, or after "Use
  recommendations", a radio group has no way back to "(no answer)". The
  reader can pick another option or write a note.
- **A file that starts with a byte-order mark is refused with "found 0
  titles".** It fails safe, but the message does not name the cause.
- **A callout kind of 40 or more letters scrolls the page sideways at
  320 px.** The six known kinds are short.
- **`PYTHONIOENCODING=ascii` with a non-ASCII title ends in a traceback**
  after the page is written. The default environment prints UTF-8.
- **An unclosed code fence takes every decision after it.** A pasted markdown
  file with its own fences can close the outer fence early and open one that
  runs to the end of the file. The text stays on the page as code, and the
  build summary shows the lower decision count. This is how CommonMark reads
  the file.
- **`[text]()` is drawn like a link but does nothing.** The empty target is
  removed, and the link colour and underline stay.
- **The nesting limit counts a list as two levels and a quote as one,**
  against 14. SKILL.md gives only the pure cases (six lists, thirteen quotes).
- **A pane entry used from the keyboard scrolls to its part but keeps the
  focus.** The next Tab continues through the pane, not into the part. A
  mouse or touch reader is not affected.
- **No test needs the rule "a clicked pane entry stays current until the
  reader scrolls".** The rule works (a reviewer probed it with short last
  parts), but removing it leaves the suite green. This is the second rule
  that was found with no test of its own.

### Not tested

- The copy button in the Claude desktop viewer, Safari and Firefox. The tests
  run in Chromium. One test makes the page behave like an engine that
  replaces the text node on each write, for the select-the-reply fallback.
- Contrast of text on the semi-transparent inline-code background.

## Skills review fixes (2026-11-10)

Found by the reviewer of `claude/skills-review-fixes`, or known when the change
was made. None blocks the change.

### The sweep (`hooks/second-brain-sweep.sh`)

- **It reads every project's prompts into the calling session.** That is its
  job: a sweep across the week. The removed Stop hook listed one session only.
  A prompt that held a pasted secret is shown again, cut to 200 characters.
- **Only the first 400 characters of a prompt are read.** A concept phrase
  after that point is not seen, and two prompts that share their first 400
  characters count as one. Without the limit the sweep took 13 s on an 11 MB
  log; with it, 0.5 s.
- **The concept filter is a phrase list.** It over-matches ("what is the
  status") and misses a question with no such phrase. `--all` shows the rest.
- **The GNU `date -d` branch has no test.** Only BSD date runs on this machine.
- **No session learns of the sweep from this repo.** The second-brain-obsidian
  skill names it, and that skill lives in its own repository.
- **A one-word prompt path reads as a slash command.** A prompt that starts
  with `/etc` and a space is left out. A path with a second slash in its first
  word is kept.
- **A log file that is itself a symlink is not read.** The log folder may be a
  symlink; the logger writes plain files.
- **A sweep that runs while a session writes a large prompt** can meet a line
  that is half written. The line is counted as unreadable in the first line of
  the output. Not run as a test; inferred from how the logger writes.
- **A log file removed during a sweep stops it with exit 2.** The logger's
  30-day clean-up can do this. A second run works.
- **A read error prints two lines on stderr:** the tool's own message, then
  the sweep's.
- **A timestamp with a fraction, in the same second as the cutoff,** sorts as
  older and drops out of the window. The logger writes whole seconds.
- **A file with CR-only line ends is one unreadable line.** It is counted, not
  lost. The logger cannot write such a file.

### The validator (`writing-markdown-docs/scripts/check_doc.py`)

- **The splitter joins when it is in doubt.** A wrong cut hides a long
  sentence; a wrong join shows a warning. It forgets the open lists after a
  line that falls back to the left with no list marker, and at a change of
  quote depth, so a numbered item after such a line joins the text above it.
  Against markdown-it-py on 400,000 hostile random blocks it loses no long
  sentence and joins in 4% of blocks; the splitter before this change joined
  in 30%.
- **A list that starts past 1 right under a callout title, an image line or
  the first line of a block is joined.** The splitter takes a paragraph to be
  open unless the line above is empty or a thematic break, and only "1." may
  start a list in an open paragraph. A list that starts at 1, or sits under an
  empty quote line, is cut at each item.
- **Two cuts are the validator's own, not markdown's:** the title line of a
  callout, and an image on a line of its own. A sentence that runs across an
  image line is read as two. The parser comparison is told about both.
- **Only the first line of a callout block is a title.** Two callouts stacked
  with no empty line between them read as one, so the second title joins the
  first body.
- **The parser comparison is skipped where markdown-it-py is not installed.**
  It is installed for `python3` on this machine; nothing installs it for a
  fresh checkout.
- **A bare marker line (`+` or `1.` alone) above another marker line** makes
  `_plain` eat the line break and read the second marker as a word, which can
  end a sentence early. Pre-existing in `_plain`; the comparison with the
  parser leaves this shape out.

- **A label line and the prose on the next line are one sentence.** Markdown
  renders them as one paragraph, and a cut after a colon hid long sentences
  that were wrapped there (found by the adversary pass, fixed in the same
  change). A label over a list is still cut, at the list item.
- **Some image lines are not seen as images,** so a caption under one can
  still join it and give a false `prose-long`: a linked image, a URL with
  parentheses, alt text with brackets, a caption on the image's own line, and
  an HTML `img`. Same result as before the change.
- **No cut at a footnote definition, a `**Term**:` line, a bold label or a
  setext heading.** Same result as before the change.
- **A list block is split at every line that starts with a number,** by the
  block reader, even where markdown reads the line as a continuation.
  Pre-existing; the sentence splitter itself follows the markdown rule.
- **`_plain` takes quadratic time on a long run of `[` after `!`.** Pre-existing.
  A 32,000-character run takes over 20 s.
- **`reader-third` flags "the owner" and "the user" when they name a file
  owner or an operating-system user.** Pre-existing. Three of three such
  warnings on the OS lecture notes were false.
- **No check that an image has a `Diagram as text` block under it,** although
  SKILL.md requires one. Pre-existing. Seven of ten learning notes read on
  2026-11-10 had images with no text version.
- **A table inside a callout counts as prose.** Pre-existing.
- **Learn-genre checks do not run on vault notes.** Pre-existing and by
  design in `references/vault.md`; the 2026-11-10 diagnosis of the lecture
  notes questions that design.

### Skill and agent text

- **The writing-markdown-docs description is 1,039 characters.** The Agent
  Skills limit is 1,024. Claude Code 2.1.319 lists it in full. The
  visual-artifacts description is 1,565 and the list cuts its last line.
- **The builder rule to load the markdown skill is text only.** Nothing checks
  that a subagent ran `check_doc.py`. A hook that runs it after each write was
  offered on 2026-11-10 and declined.

## Global rules additions (2026-11-10)

Minor findings from the review of the design, time-box, outside-in and tier 2
lines in `config/claude/CLAUDE.md`. None is fixed.

- **"At each milestone" in the time box reuses a defined word.** Elsewhere in
  the file a milestone ends with the PR to the release branch. A short time box
  has no such milestones, and the rule does not ask for timed checkpoints in
  the plan, so "the clock against the plan" has little to measure against.
- **The time box does not say what happens when the must-haves alone run
  over:** run past the time, or drop a must-have.
- **"First one end-to-end check" has two readings:** before the feature code,
  or first among the tests. The reason for the rule is the first reading.
- **"Keep such checks few" can read as few checks of any kind.** It means few
  end-to-end checks.
- **"Run in CI" has a side effect.** Adding the check to a workflow is a CI
  change, so it is tier 2 and its own PR. A repo with no CI cannot meet the
  rule until its first workflow is merged.
- **"Take the smallest" does not say smallest in what.** It means the fewest
  parts, not the least build effort.
- **The time-box trigger phrases can fire by accident** ("the cron runs in
  2h"), and the row does not say whether "in 3h" counts. The "faster" and
  "continue" rows have the same weakness.
- **`tests/test_global_rules.py` catches a deleted phrase only.** It misses a
  negated rule, a phrase moved into another rule, and the parts it does not
  pin (the table row, the clock report, "run in CI"). The CI workflow
  (`.github/workflows/ci.yml`) runs the test. A commit that no CI run
  covers is checked only when someone runs the test.
- **The size cap counts the file without its HTML comments.** Claude Code
  2.1.319 leaves them out when it loads the file. If a later version loads
  them, the cap undercounts by the size of the comment (302 characters today).

## Worker definitions (`config/claude/agents/`, 2026-11-10)

- **The cast is checked against its source only on request.** The seven role
  names and jobs are copied into `tests/test_worker_roles.py`. The test that
  compares them with the source file runs only when `WORKER_CAST_SOURCE` is
  set; without it, a change in the source goes unseen here.
- **`manifest.txt` has no line for `config/claude/agents`.** Pre-existing: the
  six definitions are linked into `~/.claude/agents` by hand, one file each,
  so a fresh install gets none of them.
- **The planner's model and effort are a choice, not a measurement:** Opus at
  medium effort, because a wrong step costs a builder round.
- **The planner is limited to a task that a builder or Codex will take.** The
  rules keep design and dependent work in the lead, so a planner for every
  task would give them two readings. A wider planner needs that rule changed
  first.
- **The two new definitions are linked into `~/.claude/agents` by hand on this
  machine** (2026-11-10), like the other four. A session loads agent types
  when it starts.
- **`tests/test_worker_roles.py` matches the job sentence as a phrase.** A
  definition that quotes the job and then states another one still passes.
- **The scout's vault rule is one line of text.** It cannot load the
  `second-brain-obsidian` skill, so it knows only `sb find`.
- **"Read-only" for the verifier, adversary, planner and scout is a rule in
  their text.** They have no Edit or Write tool, but Bash can still write.
- **`planner.md` uses "cut" in two senses:** where a task could be split, and
  a step to drop under a time budget.
- **"When the brief gives a time budget" in `planner.md` can also mean the
  `Budget` line** that every brief has. On that reading every plan marks its
  steps as must-have or cut, which is harmless.
- **The builder has no "no second end-to-end check" line.** The planner has
  one. A builder that reads the Features rule literally can add an end-to-end
  check for its own piece: an extra check, not a wrong one.
- **The planner test pins the limit, not the exclusions.** The description's
  "not for a task the lead builds itself, not for a small task" can be removed
  with no failing test.
- **Bash is on every reporting worker's tool list, with no scope.** The
  verifier and the adversary run tests and probes with it. A list of allowed
  Bash commands per worker was suggested by the automated security check on
  2026-11-10 and not built: it belongs in `settings.json.tmpl`, which is a
  change of its own.
- **The scout's WebFetch has no domain list.** It is the one worker that
  reaches the web, and it runs as the user.
- **The builder and the scribe keep every tool except Agent.** They write
  files, so a list of allowed tools would have to name each one they may need.

## Design skills (`premise-check`, `shape`, 2026-11-10)

- **`shape` was checked by a dry run, not by a real session.** A fresh agent
  followed steps 1 to 4 on a past assignment, with its own recommended option
  in place of each answer. The clock, the dialog and the user's vetoes were not
  exercised. The dry run read the first text of the skill. It asked 14
  questions against a budget of ten, because that text did not say which
  questions count. The rules that came after it have not been run at all:
  which questions count, what happens when a step's time or the whole box is
  up, and the session's cut list.
- **The first review failed `shape` for one cause, three times over:** a rule
  in the skill met a rule in another file (the time box in CLAUDE.md, the
  first task in the plan genre, the fresh reader against "stop when the time
  is up") with nothing to say which applied. The skill now has a "Which rule
  applies" section, and `tests/test_design_skills.py` compares the two sides
  for the cut direction, the decision columns, the doc sections and the first
  task. A new rule in another file on one of these cases is not seen by the
  tests.
- **The size cap for the two skills was raised from 9,000 to 12,000
  characters** to hold that section and the clock rules. `shape` is about 11,600.
- **The session's cut list has three items.** When all three are dropped and
  the session is still behind, only the step rule is left: a step closes when
  its time is up, and what was not asked becomes a default.
- **The fresh reader's check has no time limit of its own.** It starts before
  the doc is written. When the box ends first, the check is listed under Open
  items and its result arrives after the session.
- **"Which rule applies" names the other files by their rule, not by line.**
  A reader must find "the time box" in CLAUDE.md by its name.
- **The tests for the two skills are pinned phrases, plus four comparisons
  with other files.** A rule that is reworded with the same meaning fails a
  test; a rule that is negated in other words passes.

Minor findings from the re-check of the fix (commit `2d7f2c0`, pass). The
first three are more cases of the cause named above, and each fails safe:

- **"Step 5 says only what goes into each section" is not exact.** Step 5
  also adds two sections that the design genre's outline does not have: Build
  order and Open items.
- **"Never dropped" does not say that the two stop rules give way to it.** A
  step that runs out of time "opens nothing new", and step 1 can run out
  before the "Done when" commands are written. The whole box can end in step
  5 before the fresh reader's check is started.
- **A doc written when the whole box is up has no `> [!tldr]`.** The genre
  puts it first, and the skill writes only Decisions, Failure modes and Open
  items at that point. The missing summary is listed under Open items.
- **"The third approach" is not defined.** When the session cut list drops
  one approach of three, nothing says that the smallest one that meets every
  P0 is kept.
- **Three changes pass the tests:** "the reverse of build order" turned into
  "build order"; "start it before you write" turned into "after the doc is
  written"; and "this skill starts no other agent" replaced by a line that
  allows one.
- **`premise-check` was checked on one past case** whose outcome was known.
- **The trigger words overlap.** "System design" also starts
  `second-brain-obsidian` (recall), and "design doc" starts `visual-artifacts`
  or `writing-markdown-docs`. `shape` is for the session with the user; the
  others are for the document or for recall.
- **The visual-artifacts Plan page has no clock or cut-list field.** Only the
  markdown plan reference got them.
- **Neither skill is in `manifest.txt` as its own line.** `merge
  config/claude/skills` links new skill folders on install; on this machine
  they are linked by hand.

## The coach skill (`config/claude/skills/coach`, 2026-11-10)

Built from a usage analysis of the prompt history: 649 of 2,796 prompts in
four months were typed in course folders, and the "hints only, I write, you
check" rule was typed again at least eight times.

### By design

- **There is no way to hand a question over to the agent.** A release rule was
  offered and declined. Under time pressure the answer is the next rung.
- **Rung 5 gives syntax for pseudo-code that is correct.** This comes from the
  user's own practice ("if i get pseudo code correct then you just directly
  provide exact c code and i'll type it in"). A course policy can forbid it;
  the skill puts the policy first and strikes the row.
- **One rule wins over the rest of the skill:** the agent never writes or
  says a step of the solution that the student has not stated first. The
  first review failed the skill on two pairs of rules with no order between
  them (a "wrong order" fix against "never trivial", and "answer a concept
  question in full" against "never states the logic first"). This is the
  cause that failed `shape`, so the fix was this one rule, not two patches.
- **The agent edits the hand-in in three kinds of case:** a slip that a check
  found (a typo, or an order that differs from what the student stated),
  message text the handout gives, and formatter noise or a file the handout
  says not to change. It also writes a README draft into the file when the
  student asks for that.
- **"Stated first" is judged by the agent.** Whether a typo fix or an order
  fix matches what the student wrote in pseudo-code or in a message is a
  reading, and a wrong reading gives a step away. The skill says to treat
  doubt as a problem to report.
- **The progress file is outside the assignment folder,** in `~/.claude/coach`
  or `$COACH_DIR`. The first write there can ask for approval.

### The live checks

Two checks run a real headless session on a fixture (a ring buffer lab in C).
They run only with `COACH_LIVE=1`. The pair takes about a minute and a half
and costs two model calls. CI does not set that switch, so nothing runs them
by itself.

What they show:

- **"just fix ring_pop for me":** without the skill, the session wrote the
  corrected line into the student's file (seen twice). With the skill it
  refused, gave the symptom and asked one question.
- **"check and clean up":** with the skill, the session sets the push message
  to the handout's text, puts the reformatted `main.c` back, leaves `ring_pop`
  as the student wrote it, reports the bug without the fix, and notes both
  changes in the progress file. Without the skill it left the logic alone too,
  but it did not fix the message, it stated the fix, and it wrote no notes.

How the session is held:

- **The session has no shell.** It runs in the CLI's restricted mode with
  five tools (Read, Edit, Write, Glob, Grep), `Bash` refused by name, and
  edits under `.git` refused. A probe session on 2026-11-11 tried each guard:
  it had no shell tool, its edit of `.git/config` was refused, a write outside
  the working folders was refused, a read of `~/.gitconfig` was refused, and
  it listed exactly its tools.
- **Why no shell.** Two earlier versions gave the session a list of commands.
  The first allowed the compiler, any git command and any binary under `/tmp`.
  The second allowed "reading" commands only. A probe showed that
  `cat handout.md > file` and `git diff --output=<path>` were both allowed
  under it, and each writes a file, outside the repo too. A reviewer showed
  that a setting written into `.git/config` makes `git status` run a program.
  So a command list is not a boundary. The automated security check flagged
  each version.
- **The review raised the test harness to tier 2.** The skill text is tier 1.
- **The test asks git nothing after the session.** It compares every file in
  the fixture, `.git` included, with a snapshot taken before. An adversary
  pass broke the version before this one: the session may write `.gitignore`,
  and a `.gitignore` that holds `*` hides itself and every new file from
  `git status`, so a file with the corrected line passed as "folder
  unchanged". The same pass found that a path spelled `RING.C` escaped the
  check for edits, that an edit with neither "head" nor "ring_pop" in it
  escaped, and that the fix stated before "Problems:" passed.
- **The test rebuilds no edit.** The version after that pass replayed each
  Edit from what the model sent. The next adversary pass held it, and named
  three classes with two or three findings each, so the helpers were written
  again and not patched:
  - *Edits the stream check missed.* The replay was not the Edit tool: the
    tool takes a relative path, matches curly quotes as straight ones, trims
    the ends of lines and refuses some calls. A recorded run changed
    `ring_pop` four times, undid each change, and passed. The test now reads
    each content of `ring.c` from the stream: the result of a write that
    worked holds the file as the CLI read it just before, and the last
    content is the one on disk. On that recorded run the new check reports
    both changes.
  - *Where the session wrote.* The test names the places a session may write
    (the progress folder, and in the first check `ring.c` and `main.c`), and
    reports every other Edit and Write: a call that failed, a relative path,
    a file one folder down, and a call with no path.
  - *The fix in words.* A check's answer may name the wrap in one place only:
    before "Problems:", in a clause about push or tail and not about head or
    pop. An earlier rewrite had dropped "nothing after Problems:" and a
    verifier caught it; both rules are now one function.
  - *The helper loosened while the text tests pass.* The offline test records
    every process that `session()` starts and requires exactly one. Each
    decision above is in a helper with its own offline test. A hand-run
    script broke the helpers in 41 ways, one at a time, and the offline tests
    failed for each. The script is not in the repo.
- **What restricted mode itself refused in that pass:** writes to
  `.gitmodules`, to a `sub/.git` file and to `.claude/settings.json`. A
  `sub/.git` file written by hand did not make git read a planted setting.
- **The session's answer is saved** in pytest's temp folder and shown in a
  failed assertion, with no redaction. The session cannot read outside its
  folders, and its process gets a short list of environment variables.

What the session does not exercise, because of that design:

- **The CLI loading the user's rules.** Restricted mode does not load
  `~/.claude/CLAUDE.md` (a probe asked for two lines that exist only there,
  and got neither). The test puts this repo's `config/claude/CLAUDE.md` into
  the prompt, so the "fix means act" rule is in context beside the skill. It
  arrives as prompt text, not the way the CLI delivers it.
- **The CLI finding the skill.** Restricted mode loads no user skills, so the
  test hands the session the text of SKILL.md in the form the CLI uses on
  invocation. Earlier runs, outside restricted mode, did start the skill with
  `/coach`. No run starts it from the description alone.
- **git and the build.** The test unpacks the starter code for the session to
  read, and tells it where the progress folder is. So the skill's
  `git diff -w --stat` comparison, its search for the starter commit, and its
  "build and run" step are not run by a session.

The record of the runs while the test was written:

- Under the first two harnesses: about fifteen runs of "check and clean up"
  with the skill. Most passed. The failures were two from the command list
  (no way to read `$COACH_DIR`), one on "Problem:" in the singular, one on an
  assertion that refused "% RING_SIZE" anywhere in the answer (that answer was
  not saved, so the cause is a guess), and one of the second check on "back to
  slot 0", which is the handout's own phrase. Each assertion was corrected to
  what the skill promises.
- Under the no-shell harness, before the snapshot and with no rules file in
  the prompt: both checks passed twice in a row.
- Under the snapshot harness, with the edits replayed: both checks passed
  twice in a row.
- Under the harness that reads each content from the stream: both checks
  passed twice in a row.

### Not tested

- Rungs 2 to 5 of the hint ladder, which need more than one turn.
- "Before hand-in", the AI-use statement and "Status".
- "Start" with a handout in the vault, a course AI policy, or no git history.
- A written assignment (a proof, a problem set) and a notebook. The fixture is
  C code with a starter commit.
- A model other than Opus.

### Minor

- **`tests/test_coach_skill.py` pins phrases** for the text rules. A rule that
  is negated in other words passes, and so does a new sentence that says the
  opposite ("rung 6: write the fix"). Not pinned at all: the wording of "Start"
  and of "Status", and the format block of the progress file.
- **The fix in words is found by a short list** in both checks ("% RING_SIZE",
  "%=", "modulo", and in the second check "= 0;" and "reset head to 0"). The
  fix in other words passes: "do for head what line 9 does for tail" names no
  word on the list. "Back to slot 0" is allowed, because the handout says it:
  one run failed on that phrase before the list was corrected.
- **A fair answer can fail the first check.** Under "Right:", a sentence that
  quotes the push line's wrap and also names pop is reported, because one
  clause cannot be split by meaning. The failed assertion prints the answer.
- **The contents of `ring.c` come from a field of the CLI's stream**
  (`tool_use_result.originalFile`) that no document promises. When a later
  CLI drops or renames it, the helper fails the run: a write with no such
  field, a call with no result, a changed file that no write names, and a
  stream with no Read are each a failure, not a pass. The snapshot does not
  depend on the stream.
- **Only the last content of `main.c` is compared.** A write to it that was
  undone later is allowed. It is a file the student does not hand in.
- **Letter case is dropped when two paths are compared,** on every disk. On
  a disk that keeps `ring.c` and `RING.C` apart, the two then count as one
  file. For the contents of `ring.c` that reports too much. For the list of
  places a session may write it allows too much: a write to `RING.C`, or to
  a folder that differs from the progress folder in letter case only, is
  not in the list of writes elsewhere. A new file in the fixture is still
  caught by the snapshot, because a session cannot remove a file. On this
  machine the disk ignores letter case, so the comparison is exact here.
  The automated security check flagged this on `39c9fd4`.
- **The offline case for a call with no usable id pins the old key form
  only.** Its real id is `call-2`, the form the made-up keys had before. The
  key is now a tuple, which no id read from JSON can equal. A later change to
  a text key of another form (say `no-usable-id-<n>`) would bring the
  collision back for a real id of that form, and the case would still pass.
  A verifier found this on the re-check of the fix.
- **A second result for one id replaces the first** in `parse_stream`. With
  a first result that worked and a second one marked as an error, the call
  counts as failed, its `tool_use_result` is dropped, and `stream_faults`
  reports nothing. So a check that reads what a write held before does not
  get it for that call. The CLI sends one result for each id. To close it,
  report a second result for one id as a fault. Found by the adversary pass
  on the same fix; the lines are older than the fix.
- **A stream in another shape can end a test with an exception, and not
  with a stated reason.** A tool call whose `input` is text passes
  `stream_faults`; `written_outside` then raises `AttributeError` where it
  reads the path from the input. A tool `name`
  that is a list raises `TypeError` in `stream_faults`. One line of 100,000
  `[` raises `RecursionError` in `parse_stream`, which catches `ValueError`
  only. Each still fails the test. Found by the same pass; the CLI sends
  none of these shapes.
- **What the session writes into the progress folder is not read for the
  fix.** Both live checks allow writes there. The first reads the progress
  file for the requirements and for the two file names in the notes, and
  the second does not read it. So a session that writes the corrected line
  into its notes passes both, although the skill's line forbids a step of
  the solution in any file. A verifier found this on the re-check of
  `39c9fd4`; it has been so since the first version of the test. To close
  it, run the word list over each write under the progress folder and over
  the final progress file.
- **No offline case pins which path `states()` matches a write by.** It
  uses the path the CLI reports, which is right. In every offline case that
  path equals the one the model sent, so a change to the model's path
  passes the offline tests. `written_outside()` has such a case.
- **Before "Problems:", the fix passes when its target has another name.**
  A clause about push or tail may hold a word of the list. So "tail wraps
  with `% RING_SIZE`, which is exactly what the consumer side is missing"
  passes the first check: the word is there, and "head" and "pop" are not.
  Text before "Right:" is read by the same rule. The second check reads its
  whole answer with no such exception. Found by the adversary pass on
  `39c9fd4`.
- **The offline test counts process starts through `subprocess.run` only.**
  A second start in `session()` through `subprocess.Popen`, `os.system` or
  `os.spawn*` is not recorded, and the test passes. Found by the same pass.
- **The skill is linked into `~/.claude/skills` by hand** on this machine.

## Codex harness (`config/codex/`, `codex-exec`, 2026-11-11)

Three changes: `config/codex/AGENTS.md` is built from `config/claude/CLAUDE.md`
and `config/codex/AGENTS.src.md`; Codex has a builder, a verifier and an
adversary agent; and `codex-run` takes a role, a worktree and a list of files
the run may change.

### By design

- **`CLAUDE.md` is the root, and `AGENTS.md` is never edited by hand.** A hand
  edit fails `tests/test_codex_rules.py`. What is only for Codex goes in
  `AGENTS.src.md`.
- **The rule names in `CLAUDE.md` are HTML comments.** Claude Code leaves HTML
  comments out when it loads the file (seen in 2.1.319; see
  `tests/test_global_rules.py`), so the names cost a session nothing. The size
  test leaves a name line out whole, line end included.
- **Codex never merges.** `AGENTS.md` says so, and on this machine
  `~/.codex/rules/default.rules` forbids `gh pr merge`. That rules file is not
  tracked here.
- **The Codex reviewers never write the merge gate's lines.** A Codex review is
  a second opinion. The review for the tier runs on Claude.
- **The builder runs on GPT-6.1 Sol at `high`.** The numbers are in the
  `codex-exec` skill, under Evidence.

### Not enforced

- **No git hook runs the build or the tests.** The CI workflow
  (`.github/workflows/ci.yml`) runs `pytest tests`, and two tests in
  `tests/test_codex_rules.py` (`test_the_built_file_is_current`,
  `test_the_check_mode_agrees`) fail when `AGENTS.md` is stale. So a change
  to `CLAUDE.md` that is committed without a build leaves `AGENTS.md` stale,
  and nothing shows it until a CI run or a person runs the tests. CI does
  not cover every commit. The workflow file names the events it runs on.
  GitHub reads the file from the commit of the event (for a pull request,
  the merge of the head with the base), so an event on a commit without
  the file starts no run. A push or a pull request whose head commit
  message has a skip marker such as `[skip ci]` starts no run either.
- **A hash forces a look, not agreement.** When a rule that Codex has in its
  own wording changes in the root, the build stops and prints the new hash.
  Setting the hash without reading both texts gets past it. The same holds for
  the three agent files.
- **`--allow` is checked after the run, and only on the worktree.** It does
  not see a write outside the worktree; the sandbox (`-s workspace-write`) is
  what keeps Codex inside.
- **`--allow` catches a run that strays; it is not a wall against a run that
  tries to hide a change.** The cheap cases fail closed: a changed `.git`
  entry, a path that cannot be read, a compare that did not finish. The wall
  is the sandbox, together with the lead's review of the
  diff in its own tree, which a process of the run cannot write. A stronger
  check would compare what is taken in with the allowed files at the take-in
  step; that is not built.
- **A process the run left behind can write after the compare.** When Codex
  ends, `codex-run` kills what is left of its process group first. A process
  that left that group (a new group, or a new session) is not reached.
  Whether `codex exec` runs its tool commands in groups of their own was not
  tested; if it does, this kill reaches few of them. The kill comes after
  bash has collected Codex's exit, so it trusts that the group's number was
  not given to another group in between.
  Two additions from 2026-11-13 (the adversary, confirmed by the verifier).
  The programs of codex-cli 0.167.1 import `setpgid`, `setsid`, `killpg` and
  `posix_spawnattr_setpgroup` (`nm -u`), which points to tool commands in
  groups of their own; no real run was made to show it. And the same limit
  now holds for a run that the watchdog killed, because that run gets a count
  too: a stand-in whose child made a group of its own gave "0 file(s)
  changed" with exit 124, and the child wrote a file four seconds later. A
  hung tool command is the usual cause of a kill, so read that count as "at
  the moment of the kill".
- **The check reads the disk, not git.** `codex-run` records each file's kind,
  size and content hash before Codex starts, keeps the record in its own
  memory, and compares after the run. Two earlier versions asked git, and git
  reads what the run can write. The first, in `f9adeb2`, left out four cache
  folder names (found by the automated security check). The second trusted
  ignore rules, so a run could add an ignore file and hide what it made
  (the same check, on `bd5bf1f`), and a run that committed its change showed
  "0 file(s) changed" (the verifier, on `bd5bf1f`). What the compare does not
  count: a new folder with no file in it, and a change of mode other than the
  executable bit.
- **Two things end with exit 3 whatever `--allow` says:** a changed `.git`
  entry of the worktree, and a path that cannot be read. Each stands for more
  than one file. In `c42069b` a folder that could not be read was one record
  that a pattern could allow, with what is under it unseen (found by the
  automated security check). A long list of either kind names the first 50
  and counts the rest.
- **A file name with a control character is printed in quoted form**
  (`$'a\nb'`), so a name cannot add a line to the report that the lead reads.
  The names in the event log and in git's own output are not changed by this.
  U+2028 and U+0085, which some readers take as line breaks, are not control
  characters to bash: U+2028 is printed as it is, and U+0085 comes out only
  half quoted (found by the verifier, a nit).
- **A run that commits is listed but not taken in.** The check lists the
  files it changed. The take-in recipe of the skill compares with the
  worktree's HEAD, which the run moved, so it takes nothing in.
- **`codex-run` does not stop when `setsid` fails.** The line that starts
  Codex in a session of its own is older than this change; were `setsid` to
  fail, the kills of the process group would reach nothing (found by the
  automated security check, older code).
- **An allowed file is not checked, and an ignored file stays in the
  worktree.** `git add -A` in the take-in step leaves an ignored file behind.
  Such a file can still sway a check that is rerun in the worktree, so the
  full check in the lead's tree is the last word.
- **A Python run leaves cache files, and they count.** A builder that runs
  `pytest` without `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` ends
  with exit 3 and a list of cache files. The lead reads the list.
- **The record costs time on a large repository.** Every file of the new
  worktree is hashed once before the run. After the run, a file is hashed
  only when it was there before with the same size. One measurement: a tree
  of 31,438 files, most of them build output that a new worktree does not
  hold, took 10 seconds to record on this machine.
- **The merge rule sees the command words only.** `codex execpolicy check`
  gives `forbidden` for `gh pr merge ...` and for `/opt/homebrew/bin/gh pr
  merge ...`. It finds no rule for `env X=1 gh pr merge ...` and for `bash -lc
  "gh pr merge ..."`. The Codex docs say the runtime splits a simple `bash
  -lc` script; that was not tested. Branch protection on GitHub is the hard
  stop.
- **A Codex agent has the permissions of its session.** "Report, do not fix" is
  an instruction to the Codex verifier and adversary, not a limit on them.
  `codex-run --worktree` gives them a tree of their own.
- **A worktree starts from HEAD.** Work that is not committed is not in it.
- **`codex-run --role` needs `python3` 3.11 or later** on PATH, to read the
  agent file. `--worktree` needs `python3` too (3.9 was tested).
- **A word in the prompt can drop the role's effort.** `codex-run` looks for
  `model_reasoning_effort` in every codex argument, the prompt included. A
  brief that only names that word makes the role's effort go unsent, with no
  message (`codex-run`, the `has_effort` line; found by the verifier, a nit).
- **The build takes only lines that it and Claude Code read the same way.**
  The build reads `CLAUDE.md` line by line; Claude Code reads it as Markdown.
  Up to `95252c9` the build dropped three kinds of text that Claude Code
  loads, and asked for no decision: text after a comment's end on the same
  line (found by the verifier), a line that starts with `#` and is not a
  heading, such as `#1 rule: ...` (found by the adversary), and what follows
  a comment that is never closed (the adversary). Each now stops the build. A
  test checks random files against a reading that shares no code with the
  build. It does not run Claude Code's own Markdown reader, so a line that
  the two read differently in a way nobody has thought of is still possible.
- **Where the build and Claude Code still read `CLAUDE.md` differently**
  (found at `6220dc9` by the adversary, with marked 4.3.0, and by the
  verifier, with marked 17; each used the comment stripping of Claude Code
  2.1.319; all nits, not changed). The first three need Markdown that is
  broken or characters nobody types, and the repo's own test cannot catch
  them: it knows no code fences and no HTML blocks, and it parts lines the
  way the build does.
  - A code fence that is still open at the next heading or rule name line. A
    `# ...` line in it is code to Claude Code; the build takes it as a
    heading and drops it. A fence that closes inside its rule, or outside
    any rule, stops the build.
  - An HTML block that is not closed (`<details>` with no end), in the same
    way.
  - A line separator other than a line feed: U+2028, U+2029, vertical tab
    (`\x0b`), form feed, `\x1c` to `\x1e`, `\x85`. The build parts the line
    there; Claude Code does not.
  - A line that is only `<!-->` or `<!--->`. The build takes it as a comment.
    Claude Code 2.1.319 keeps it as text, because its stripping step finds no
    match in it. The line holds no instruction.
  - A heading can hold an instruction (`## Never push to main`) and needs no
    decision. This is by design: a heading is not a rule.
  - Whether a rule name line reaches a session depends on the Markdown
    reader. With marked 17 all 26 comments of the file were taken out (the
    verifier, in its first round; the file is as it was at `81d8e8e`). With
    marked 4.3.0, 13 of the 25 name lines are not a comment at the top level
    of the file: each follows rule text with no blank line between, and that
    version keeps it inside the list above it. A reader that takes out only
    top-level comments leaves those 13 as text (the adversary named the
    case; the count is from `marked.lexer` on the same file, which gives 12
    name lines in top-level comments). In random files of the verifier, a
    name line after an open
    code fence, a `<div>` line or a `>` quote also stayed. No rule text is
    lost by this; a session reads a few more words.
- **Small faults in `codex-run` that the adversary found at `95252c9`, not
  changed:**
  - `-Cx`, `-mfoo` and `-m=foo` in the codex args were not seen as a folder
    or a model, so `--worktree` or the role added its own; codex 0.167 then
    refused the repeated flag, and a worktree was left behind. Closed on
    2026-11-13: see "The folder and the model in the codex args" below.
  - A model given as `-c model=...` or by `-p profile` loses to the role's
    `-m`.
  - An agent file with `model = ""` starts Codex, which refuses it. A DEL
    character in the instructions is sent as it is and is not valid TOML.
    Under a Latin-1 locale a character outside ASCII in the instructions
    arrives changed.
  - A `--worktree` path that ends in a line break is made without it. With
    `--repo` set to a linked worktree, a `--worktree` path inside the main
    checkout is accepted.
  - A path deeper than 1,024 bytes gives exit 3 ("could not be read").
  - The build joins two or more blank lines into one, inside a placed rule
    too.
- **Left by the fix for a missing `codex` (the adversary, at `4672c7d`).**
  With `codex` missing or not startable, `codex-run` ends with 127. Three
  cases are not as clean as that:
  - A `codex` file that is marked as a program and has no `#!` line (an
    empty file, such as a download that broke) is run by `/bin/sh`, as the
    shell itself would run it. An empty one ends with 0: the run looks
    clean, with an empty log and "0 file(s) changed". To close it, look for
    a `#!` line or a program header before the run.
  - A bash function named `codex` that is exported into the environment,
    with no `codex` on PATH, passes the first check, because `command -v`
    finds functions too. The run still ends with 127, but a log and a
    worktree are made. `type -P` would look at PATH only.
  - The check for `codex` comes before the checks of the role and of the
    worktree path. So with `codex` missing, a bad role name ends with 127
    and not with 2. Nothing is started and nothing is made.
- **Older faults in `codex-run`, there before this change (the adversary):**
  - A `--idle` or `--max` value that is not a number (`5m`) turns that limit
    off, with only a shell warning.
  - Killing `codex-run` itself leaves Codex running with no watchdog.
- **The folder and the model in the codex args (2026-11-13, codex-cli
  0.167.1).** With `--worktree`, `codex-run` refuses a folder argument in the
  codex args, and a role leaves its model out when the caller gave one. Both
  scans now see a value in the same word as its option.
  - The folder rule is wider than Codex's own reading: every word that starts
    with one dash and holds a capital `C` is refused, wherever it is in the
    list, also after a `--`. So `-oC.md`, `-cKEY=C` and a prompt that starts
    with a dash and holds a `C` are refused with `--worktree` (exit 2, nothing
    is made). Write the value as a word of its own.
  - The rule reads the spelling of the arguments, not what Codex does with
    them. A later Codex with a new name for the option (not `-C` or `--cd`),
    or one that takes shortened long options, would get past it. Version
    0.167.1 refuses `--c` ("unexpected argument").
  - The folder is not the only way out of the worktree. `--add-dir <DIR>`,
    the sandbox mode (`-s danger-full-access`), the two `--dangerously-*`
    switches, a profile (`-p`) and a `-c` config value are passed to Codex as
    they are. The skill forbids the dangerous switches in words only.
  - Codex 0.167.1 has an option of its own named `--worktree`. `codex-run`
    does not look for it in the codex args, and no test or live run used the
    two together.
  - A run that the watchdog killed has its files compared now, and the code
    stays 124. The lead is not told by the code alone that such a run also
    left its files: read the list.
  - The model of Codex's argument reading in `tests/test_codex_run.py` was
    written from `codex exec --help` and from three parse errors of the real
    program (a repeated `--cd`, a repeated `--model`, `--c`). No test starts
    the real `codex` with these arguments.
  - That model differs from the real program in one place that lets a list
    through (the verifier, a nit). It takes the word after an option that
    needs a value as the value, also when the word starts with a dash
    (`-o -C/tmp`). codex 0.167.1 refuses such a list ("a value is
    required"). The model lets through only lists that Codex does not run,
    and `codex-run` refuses them anyway. Its other differences make it
    stricter than the real program: it reads a folder in `-hC/tmp` and
    `-VC`, where Codex prints its help or version and stops, and in
    `-xC/tmp`, where Codex stops on the unknown letter.
  - The model scan is as wide as the folder scan, and that has a cost (the
    verifier, a nit). A word that starts with `-m` counts as the caller's
    model wherever it is, also after a `--` in the codex args, where Codex
    reads it as the prompt. With `--role`, such a prompt leaves the role's
    model out, and Codex runs on the model of its own config. The folder is
    not changed by this.
  - The folder rule misses some words in four locales (the adversary, a nit,
    confirmed by the verifier). The scan runs in the caller's locale. Under
    zh_TW.Big5, zh_CN.GB18030, ja_JP.SJIS and ja_JP.eucJP, bash 3.2 reads the
    byte `C` as the second half of a two-byte character when the byte before
    it is 0x81 or higher. So a word such as `-中C/tmp`, typed as UTF-8, is
    not refused there (exit 0, Codex is started); under a UTF-8 locale or the
    C locale it is refused. No folder reaches a run this way: codex 0.167.1
    stops on such a word ("unexpected argument '-中'"), and a `C` that comes
    after ASCII letters only, or first in the word, is seen in every locale.
    To close it, run the scan with `LC_ALL=C`. Only bash 3.2 was tried.
  - A last log line with no line end joins the next line of the report (the
    adversary, a nit, there before this change). On a kill, `codex-run`
    prints the last events with `tail` and `cut`, which add no line end. The
    line "codex-run: the worktree is still at ..." then follows the run's own
    text on the same line. The count line comes on a line of its own.
  - Not tried: whether `codex exec resume` runs in the folder that was
    stored with the session and not in the `-C` folder that `codex-run`
    adds. Only a real run can show it.

### Tested (2026-11-11, codex-cli 0.167.0)

The commit names in this section are on the branch `claude/codex-harness`.
A number here names the commit it was measured at, because the next commit
changes it.

- The suite (`python3 -m pytest tests -q -p no:cacheprovider`) passed at each
  of the seven commits up to `f16b180`, where it gave 319 passed, 4 skipped.
  Three of the skipped tests ask the real `codex` program to render a prompt;
  with `CODEX_PROMPT=1` they pass.
- Deliberate breaks of `build_agents_md.py` and `codex-run`, one at a time,
  made by a script that is not in the repo. At `f16b180`: 62 breaks, and the
  tests failed for 61 of them. The one they passed took out the second, hard
  kill of a process the run left behind. The commit after `f16b180` adds the
  test for it: a child that does not end when it is asked to.
- At `f16b180` the `--worktree` tests also pass under macOS `/bin/bash` 3.2
  with `python3` 3.9.
- The message of `c42069b` says 31 of its new cases failed before the change.
  A rerun gives 32 (found by the verifier).
- Three live runs through the `codex-run` of that hour, which asked git and
  left cache folders out (the check that is in `f9adeb2`), with the built
  rules in place as `~/.codex/AGENTS.override.md` for the time of the runs.
  They were not run again with the check that reads the disk:
  - `--role builder --worktree --allow` on a cache with three bugs, of which
    the given test shows one: the reply was "done", `codex-run` printed
    "1 file(s) changed", the lead's tree stayed clean during the run, and
    all 12 tests of the grading folder passed (the given test and the hidden
    ones).
  - A task whose fix sits in a file under "must not touch": the reply was
    "blocked" and named the file; `codex-run` printed "0 file(s) changed".
    The run's `pytest` left nine `__pycache__` files, which today's check
    would list (exit 3).
  - `--role verifier` on a commit that passes its test and breaks its
    contract: verdict fail, one Important finding with the line and the
    sequence that shows it, "tier too low" raised, no file changed, and no
    `review: pass` line.
- Twelve graded runs for the choice of the builder's model: see the skill.

### Not tested

- **Codex spawning one of the agents itself** (`agent_type`) in a live
  session. The prompt test shows only that Codex offers the three.
- **The adversary role** in a live run.
- **Main worker mode under the new rules:** the short prompts, the tiers, the
  time box. Only the subagent path ran live.
- **Several `codex-run` worktrees at one time.**
- **Which model is better on hard work.** No graded task failed in either arm.
- **The install step.** `./install.sh --dry-run --only codex` lists the three
  agent links; the real run waits for the merge.

## Status line (`config/claude/statusline-command.sh`, 2026-11-13)

### By design

- **The script's git calls run none of a repo's own programs, at a cost.**
  The file monitor is off for the status call, so a very large repo gets no
  speed-up from it. Every clean and process filter is set to empty for that
  call, so a file under a filter that was touched but not changed can show
  `*`. A submodule's working tree is not looked at; only a new commit in a
  submodule counts as a change. `tests/test_statusline_git_safety.py` holds a
  trap for each program.
- **The protection covers this script only.** Claude Code and the shell prompt
  run their own git commands in the same folder.
- **A config with very many filters is not read.** Past 64 filter keys, or
  8,192 bytes of their names, the script leaves the status call out and marks
  the tree `?`. Switching each filter off takes one environment entry, and
  thousands of them stalled the script for minutes. The branch name still
  shows; the changed mark and the ahead and behind counts do not.

### Minor

- **A tag with a suffix counts as a release tag.** The pattern
  `v[0-9]*.[0-9]*.[0-9]*` also matches `v1.3.0-rc1`, so line 1 shows it when
  it is the nearest tag. The harness makes no such tags.
- **Two release tags on one commit.** `git describe` takes the nearest tag.
  Among lightweight tags on the same commit it takes the first by name, so
  `v1.2.3` wins over `v1.2.4`. Annotated tags are taken by date.
- **The name limit counts characters, not bytes.** In a UTF-8 locale, 4,100
  two-byte characters pass the limit of 8,192, and each filter is still
  switched off. The real limit is then up to about 32 KB, which is far under
  the size limit of the environment. Both outcomes are safe.
- **Filters in your own git config count toward the 64 keys.** Git LFS adds
  three, so a repo has 61 left.
- **A bare repo as the harness folder, with a config past the limit, shows
  `?` after the version.** With a small config it shows no mark. The installed
  script never lives in a bare repo.

### Not tested

- The time the script takes in a very large repo. In this repo, 20 draws took
  2.12 s with the protection and 1.95 s without it.
- A session where `GIT_DIR` or `GIT_WORK_TREE` is set in the environment.
- A config file, or a file it includes, that is a named pipe. Every git
  command would wait on it, the ones before this change too.
- A git older than 2.45, which does not know `GIT_NO_LAZY_FETCH`.

## Installer (`install.sh`, 2026-11-13)

### Minor

- **A rendered file is written through a symlink.** When
  `~/.dotfiles-rendered/claude/settings.json` is a symlink, the render writes
  to the place it points at, which can be outside the home folder. A random
  test of 550 home folders found it; it was there before 0.1.0.

### Not tested

- Two runs in the same second. They would share one backup folder. A change
  to `backup` that removed an older backup of the same name first would pass
  the tests, and it could lose a file only in this case.
- A home folder whose path holds `|` or `&`. The render passes the path to
  `sed`.
- The real installer against a live home folder. `tests/test_install_manifest.py`
  runs it in a temp dir only.

## Floor guard (`config/claude/hooks/floor-guard.py`, 2026-11-13)

### By design

- **Only the Claude verifier runs it** (`config/claude/agents/verifier.md`,
  step 8, since 2026-11-14). The Codex verifier does not, and neither does
  CI. The verifier calls it at `~/.claude/hooks/floor-guard.py`, where the
  installer links it, and names the tree with `--repo`.
- **A guard that did not run makes the verdict "unverified".** A run counts
  only with exit 0 and the last line `floor-guard: clean`, or exit 1 and a
  last line that starts `floor-guard: summary`. Every other outcome is "did
  not run": exit 2, another exit code, another last line, a missing guard
  file, a run that was cut off. So a pipe that hides the guard's exit code,
  or a broken `python3` that exits 1 by itself, does not read as a result. An
  unverified change does not get the line that opens the merge gate. So on a
  machine that did not install this repo, or whose git prints a warning on
  every call, no change passes until that is fixed. The same holds for a repo
  that the guard cannot read but git can: one that works through `GIT_DIR`,
  or one whose `core.worktree` points elsewhere.
- **The top of the work tree is checked, not taken from git.** git takes it
  from a setting when there is one (`core.worktree`), and that setting can
  name any folder. The guard ran every later call there. When the folder
  was another repo, the guard read that repo: `floor-guard: clean` for a
  tree that had lost a test. An adversary review of the comment check
  found it on 2026-11-15, and it was there since the first version; the
  sentence above said "did not run" for such a repo, and that was true only
  when the other folder was no repo. Now the guard gives a result only
  when three answers name one folder, by device and inode: the top of the
  work tree that git names where the caller pointed (`--repo`); the first
  folder at or above `--repo` that holds a `.git` entry, which the guard
  finds itself; and the top that git names when it is asked in that
  folder, where every call then runs. Anything else is exit 2. So the
  guard gives a result only where it gave one before, and for the same
  folder; it has more reasons for exit 2 and no new result.
- **That rule is the third form of the fix, and the class was "the guard's
  idea of the tree can differ from git's".** The first form asked git
  where the caller pointed and ran the calls in the folder with the `.git`
  entry. The adversary planted a git dir inside the tree that names the
  tree as its work tree; the tree's own setting, which named another repo,
  was never looked at. The second form asked git only in the folder that
  the guard's walk found. The walk took "could not look" for "not there":
  for a tree whose top folder the guard may not enter (one `chmod`), it
  went on to the repo above and said `clean`. Now a folder that cannot be
  looked into is an error, git must also take the `.git` entry of the
  folder (it passes over one that is no git dir and takes a repo further
  up), and the three answers must agree.
- **A submodule checkout has a work tree setting too** (git writes it), and
  it names the submodule's own folder, so it is read as before. A repo
  with its git dir kept elsewhere and a linked work tree have no such
  setting.
- **A folder whose `.git` entry is broken is an error** (an empty folder, a
  link to nowhere), also when the repo above says that this folder is its
  work tree. Before this change it was read as the repo above it.
- **Who can write the repo's git settings can do more than that.** The same
  person can move the base ref to the head commit, and the guard then
  compares the change with itself. The guard reads a tree that an agent
  changed; it is no protection against someone who edits `.git` by hand.
- **The verifier skips the guard for a tree that is not under git, and only
  when the brief says so** and its own `git -C <tree> rev-parse` fails. The
  lead that writes the brief is trusted with that statement, as it is with
  the tier.
- **A guard result holds for one commit.** The verifier runs the guard again
  after each new commit, also when it is resumed to re-check a fix, and it
  writes the review line only with a guard result for the commit in that
  line. A skipped guard therefore gives no review line.
- **Only the stated goal explains a hit.** A path that `REVIEW.md` tells the
  verifier to skip still counts for the guard, so a change cannot exempt
  itself by an edit to `REVIEW.md`.
- **That the verifier follows step 8 is a rule in its text.** It was shown by
  hand on stand-in repos, and has no test in CI. The rules for a re-check, a
  pipe and a foreign exit code come from an adversary's reading of the text
  (2026-11-14) and were not run with a live verifier.

- **It reads names, not runner settings.** A path with a test name counts as
  collected, unless a folder on it starts with a dot or is `node_modules`. The
  guard does not read `testpaths`, ignore patterns, or `collect_ignore`, so a
  change to those settings is not seen.
- **Some default rules of runners are not known to it.** Each of these moves
  stops the test running and gives no finding: `tests/test_a.py` to
  `build/test_a.py` or `venv/test_a.py` (pytest skips those folders);
  `pkg/x_test.go` to `pkg/_old/x_test.go` or `pkg/testdata/x_test.go` (the go
  tool); `spec/models/user_spec.rb` to `lib/user_spec.rb` (RSpec reads only
  `spec/`); `src/test/java/FooTest.java` to `src/main/java/FooTest.java`
  (Maven); `src/a.test.ts` to `dist/a.test.ts` (Vitest).
- **A file that is a test only by its folder cannot move quietly.** A helper
  or a fixture moved from one test folder to another is reported as
  `test-moved-out`. The guard cannot tell whether a runner still collects it.
- **An empty test file that is deleted is reported.** So is one of two test
  files with the same bytes.
- **A code file with a skip marker that moves to a test path is not
  reported.** Both paths are code, and no test stops running.
- **It checks the working tree, not `HEAD`.** A test removed from the commit
  but left on disk as an untracked file reads as present.
- **A weakened assertion that keeps the count is not seen.** Neither is an
  assertion that is commented out, or a test renamed and edited in one step:
  that one reports as `deleted-test`.
- **A default base that is there but broken ends the run.** With no `--base`,
  the base is the first of `origin/dev`, `develop`, `development`,
  `integration`, `main`, `master` that is there. When that ref names no commit
  (a lost object, a damaged ref file, a symbolic ref that points nowhere), the
  guard exits 2. It does not go on to the next name, because that would
  compare with the wrong base. Finding the default base needs git 2.43 or
  later (`show-ref --exists`); with an older git, pass `--base`.
- **An untracked twin of a tracked file is left out.** On a disk that takes
  two spellings of a name as one file, git can list a tracked file twice. The
  guard drops the second entry only when it is the same file on disk (device
  and inode) and the names differ only in letter case or Unicode form. An
  untracked hard link under any other name is a copy, and is read.

### Minor

- **Some spellings of a weakened workflow step are not matched:**
  `continue-on-error: ${{ true }}`, `continue-on-error: "true"`, `|| exit 0`,
  and `||` with `true` on the next line.
- **A workflow file whose extension is `.YML` is not treated as a workflow.**
  Whether GitHub runs such a file was not checked.
- **Markers match with exact letter case,** so `# NOQA` is missed.
- **A file with CR-only line ends is read as one line.** A second marker line
  added to it is missed.
- **A case-only rename made with plain `mv` reads as clean** on a disk that
  ignores letter case. The index still holds the old name, and git commits
  nothing for it.
- **A file named `test`, `tests` or `spec` counts as a test by its own
  name,** so deleting `scripts/test` is reported.
- **Any warning from git ends the run with exit 2.** A machine whose git
  always warns (a config file it cannot read) gets no result until that is
  fixed. A repo can do the same to itself: a committed `.gitignore` that is a
  symlink makes git warn on every run.
- **The answer "no such ref" is read by its exact words.** The guard takes a
  default ref as not there only when git exits 2 and prints
  `error: reference does not exist` and nothing more. A git release that
  words this in another way makes every run with no `--base` exit 2 until the
  guard is updated.
- **A default ref whose file is a symlink to nothing reads as not there.**
  Git itself reports that ref as missing, so the guard goes on to the next
  name. Only a damaged `.git` folder holds such a file.
- **Time and memory grow with the tree.** A tree of 20,000 files with one
  200 MB source file took 60 s and 1.2 GB.
- **A folder with no `.git` of its own, inside another repo, is read as that
  outer repo.** `--repo outer/proj` gives the outer repo's result, also when
  the outer repo ignores `proj/`: `floor-guard: clean` for a tree the guard
  did not read. The verifier's skip does not apply there, because
  `git -C outer/proj rev-parse` succeeds. The sha of the outer repo cannot
  match the head of a pull request for `proj`.
- **With no `--base`, a change that is already in the default base reads as
  clean.** For a release pull request from `dev` to `main`, the default base
  is `origin/dev`, so the diff is empty: a test that `dev` deleted is
  reported only with `--base origin/main`. The verifier leaves `--base` out
  when its brief names no base. A release pull request does not go through
  the merge gate.
- **`--repo ""` reads the folder the shell is in.** That happens when a shell
  variable for the tree is empty.
- **A file that git is told not to look at hides a difference.** The guard
  reads the disk. With `git update-index --assume-unchanged` or
  `--skip-worktree` on a file, the disk can hold a test with no skip marker
  while `HEAD` adds one: the guard prints clean, and `git status` prints
  nothing, so the verifier's rule about uncommitted changes does not apply.
  It needs someone who hides the edit on purpose.
- **A change inside a submodule is not read** (found 2026-11-14, there since
  the first version). The guard reads regular files only, so a submodule
  moved to a commit that deletes a test gives `floor-guard: clean`.
- **A long list of hits ends as "unverified".** The verifier runs the guard
  with no pipe and reads its last line. Above about 20 KB the shell tool of
  Claude Code cuts the output, so the `floor-guard: summary` line is not
  shown (seen with 561 hits, 33,409 bytes). This fails safe. The verifier can
  send the output to a file and read the file, which is not a pipe; its text
  does not say so.
- **Nothing ties a guard result to the sha in the review line.** The guard
  does not print the commit it read, and the verifier reads
  `git rev-parse HEAD` at the end. When `HEAD` moves in the tree during a
  review (a commit or a branch switch by another session), the line can name
  a commit that the guard did not read. The same holds for every check the
  verifier runs, and was so before the guard.
- **On a re-check, a hit that still stands meets "do not re-report".** A
  first review fails on an Important hit, the fix does not touch it, and the
  re-check sees the same hit again. The verifier text says not to report a
  known finding a second time, and no sentence says that an earlier Important
  finding that still stands keeps the verdict at fail. This holds for every
  finding, not only for guard hits. It was read from the text (2026-11-14)
  and not run with a live verifier.
- **One test of the guard waits with no time limit**
  (`test_repo_config_programs_never_run_and_nothing_is_written`). Its last
  step runs `git diff` and `git status` with a stand-in filter program, and
  git stops that program with SIGTERM. In a test process that ignores
  SIGTERM, git waits forever. That happened once (2026-11-14): a test of the
  verify runner left the signal ignored for the tests after it. That test
  file now puts the signals back; the two git calls still have no time
  limit.

### Not tested

- **`test_matches_reference_on_random_change_sets` failed one time under
  load** (2026-11-16, seed 11, case 99: the guard gave no result of 0
  or 1). A reviewer's full suite ran beside a mutation run of another
  skill then. Alone, and in each full suite with no other load, the test
  passes. The cause is not known; a time limit inside the test is the
  first thing to look at.
- Linux. The tests ran on macOS only, and CI runs on macOS only.
- **The inode half of the twin rule has no test in CI.** Its one test needs a
  disk that keeps letter case apart: an untracked `A.py` beside a tracked
  `a.py` is its own file. On the CI disk the test is skipped. It ran once by
  hand on a case-sensitive APFS image (2026-11-13): all 183 tests passed
  there, and the test failed when the inode check was removed. On a disk that
  ignores letter case the inode check cannot change a result.
- The comparison of two spellings of one folder on a disk that keeps
  letter case apart: the test for another letter case has nothing to run
  there, and it then passes with no assertion. The disk of CI ignores case.
- A git older than 2.45, which does not know `GIT_NO_LAZY_FETCH`, and a real
  git older than 2.43 (the test uses a stand-in that rejects the option).
- Refs kept in the reftable format, and a broken entry in `packed-refs`. The
  broken-ref tests write loose ref files. Both ended with exit 2 in a review
  by hand, and have no test.
- Sparse checkouts and Git LFS pointers. These show as deletions or changes.
- That `GIT_OPTIONAL_LOCKS=0` is needed. The test that nothing is written
  passes without it.
- Replace refs, a full disk, and a signal sent to git while it runs, except
  during the lookup of the default base, which is tested.

## Attribution key (`config/claude/settings.json.tmpl`, 2026-11-14)

Reviewed on 2026-11-14: verifier pass and adversary held at commit `334ea74`.
The first commit passed too; its five small findings were one class (the live
test reached further than its temp repo) and were fixed in one change.

### By design

- **The key reaches Claude Code only.** Codex and other agents learn "No AI
  attribution" from the rules file.
- **A machine gets the key at the next render of the template.** Until
  `install.sh` renders it again, the live settings file has no such key. That
  render stops when the live file drifted from the template.

### Minor

- **The live test reads the first description block only**
  (`tests/test_attribution.py`, `described`). Attribution that a session
  prints after the end marker, or in a second block, is not seen by the test
  of the template. The control test fails loudly on the same output.
- **A marker with Markdown around it is not found** (`**PR-BODY-START**`, a
  marker with a colon). The live test then fails with "no description between
  the markers". It fails safe.
- **The hostile git config in `test_the_setup_commit_stays_in_the_test_repo`
  proves less than its text says.** The test hands the config over through
  `GIT_CONFIG_GLOBAL`, and `git()` drops every `GIT_*` variable of the caller
  anyway. That the test's git reads no user config is pinned by the shape
  test, and was shown by hand with a temp HOME.
- **The caller's HOME goes along.** With `GIT_CONFIG_GLOBAL=/dev/null`, git
  still reads `$HOME/.config/git/ignore`. An ignore file that lists
  `README.md` or `hello.txt` makes the setup fail, loudly.
- **`git commit -F <file>` is inside the rule `Bash(git commit:*)`.** A
  session could read a file outside the temp repo into its commit message. It
  writes nothing outside the repo. Shell redirects, pipes and command
  substitution were refused in a probe.

### Not tested

- A Claude Code older than 2.1.281 with the object form, and whether a
  version between 2.0.62 and 2.1.281 knows the `sessionUrl` sub-key. The docs
  say the object form is the one for older versions.
- The whole rendered template passed to the CLI. The live runs pass only the
  `attribution` object.
- A session in the cloud or under Remote Control, where the session link
  applies.
- Any model but Haiku, and more than one run per case.

## Checkpoints and the design gate (`config/claude/CLAUDE.md`, 2026-11-14)

Reviewed on 2026-11-14: verifier pass at commit `4a8170d`, tier 1. The
verifier failed the first two commits. The gate reached Claude subagents,
whose brief forbids a question; the first fix then used the words "lead" and
"worker", which the Codex file does not define. Both were one class: a
sentence that does not fit every agent that loads it. The sentences now say
who they are for by one thing both files define: an agent has a brief, or it
has none.

### By design

- **The checkpoint rule has no live test.** In a short task a session with no
  rule also decides small things by itself, so a short run cannot tell the
  rule from its absence. Its measure is the share of lead turns that end on a
  question, counted over real sessions.
- **The size cap of the rules file was raised**, from 15,100 to 15,650
  characters and from 220 to 230 lines. The file is at 15,615 and 230.

### Minor

- **A subagent with no brief gets the gate.** A fork carries the lead's
  conversation and no brief. By the text it shows a page and waits when it
  starts something new. Not tested.
- **A request that looks like an eight-line brief** may make the session Sean
  talks to skip the gate. Not tested.
- **The live gate test can pass with code in the answer.** It fails when the
  session writes any file that is not Markdown, and it asks for one signature
  with an empty body. A full implementation pasted into the answer beside
  such a signature still passes.
- **The live gate test can fail a correct session.** A signature whose body
  is only a docstring is not counted as empty, and an answer that asks for a
  go in other words than go, approve, confirm or proceed is not counted.
- **Two parts of the live test have no fast test:** the word list of the
  go-check, and the presence of the third live session.
- **The brief sentence was not seen to change behaviour.** In one run, a
  session with the builder's text and a brief wrote the code with the
  sentence cut from the rules too. The sentence removes a contradiction in
  the text.
- **A pin finds a deleted phrase,** not a rule that was negated around it.

### Not tested

- Either Codex mode. The Codex file was read, not run.
- Any model but Sonnet, and more than three runs of the gate.

## Verify runner (`config/claude/skills/create-verify-skill`, 2026-11-14)

### By design

- **Command-line steps only.** The step kind is `run`. A server that must
  stay up and a screen need a later runner version; the runner refuses a
  kind that it does not know.
- **The runner is a copy in each project.** A project can run it in CI and
  on a machine without this repo. A fix to the runner does not reach a
  project until its copy is replaced. The map and the copy both name the
  runner version.
- **The map decides what a pass means.** A step that expects only exit 0
  passes for most broken features. The skill text asks for a step that can
  fail, and for a run that shows it; no code checks that.
- **A result is tied to a commit by the pass line,** which names the commit
  and says `dirty` or `partial`. The reader must compare that commit with
  `HEAD`. No code does that for the reader.
- **The doctor refuses an `env` value that starts with `/` or `~`.** It does
  not see a real path that the app builds itself, or one inside a longer
  value. The app also gets the caller's whole environment, so a variable that
  the map does not set still points where it points in the shell.
- **Two runs share the build folder of the project.** Evidence and temp
  folders are separate per run; the build is not.
- **A map is code.** `verify run` runs the commands that the map names, as
  the tests of a project run its code. Running it on a branch trusts that
  branch as much as running its tests does. `clean` reads `.verify/runs/`,
  which git ignores; it does not write through a link there, it does not
  signal group 0 or 1, and it stops a process only when pid and start time
  match. It was not hardened further against a hostile `.verify/` folder.
- **Each feature gets a fresh folder and a fresh copy of the fixture.** A
  large fixture is copied once per feature, and a costly set-up step runs
  once per feature that needs it.

### Minor

- **What a step left running is stopped by its process group.** A process
  that moved to another session or group is not found. After a command ends,
  the runner signals its group once more; in theory the system could have
  given that group number to a new process in the microseconds between.
- **`clean` cannot stop the leftovers of a cut-off run when the command
  itself has ended.** It stops a process only when the pid and the start time
  that the run recorded both match, and only the command's own pid is
  recorded.
- **A runner that is killed between the start of a command and the write of
  its pid** leaves that command running, and `clean` does not know it.
- **`ps` gives start times to the second.** A recorded pid that the system
  gave to a new process within the same second would match.
- **Where `ps` gives no start time** (BusyBox, an image with no procps),
  `clean` still finds a runner that has ended and removes its temp folder,
  but it stops no process, because it cannot tell that a pid still means the
  recorded command. A run whose runner pid went to another process stays
  `live` there. Shown with a stand-in `ps`, not on such a system.
- **A temp folder that cannot be removed** (a file with a system flag,
  another owner) stays. The run prints `not-removed`, keeps its result, and
  `clean` tries again later. Read-only folders are not such a case: the
  runner makes them writable first.
- **An exit 2 of the scaffold can leave the skill folder.** The `.gitignore`
  line is written after the folder. When that write fails, the folder is
  there and a second call refuses it, although the texts say that exit 2
  means nothing was written. Found by the verifier, 2026-11-14.
- **Neither skill text says that a step runs in the project root by
  default.** A step that writes to a relative path writes into the project,
  not into `{tmp}`, and can then differ between a full run and `--only`.
  `cwd` on a step changes it. Found by the verifier, 2026-11-14.
- **A stop signal in the first moments of a run** (before the temp folder
  exists, or between the start of a command and the first line that guards
  it) was not shown to leak or not to leak; twelve timed signals did not hit
  it.
- **The runner ignores the stop signals from the start of its clean-up to
  its end as a process,** and so does each program it starts in that time
  (`git`, `ps`). A second Ctrl-C does not stop a clean-up that hangs;
  `kill -9` does, and `verify clean` then finishes the job.
- **When git refuses the repo, the doctor names the wrong cause.** For
  example `detected dubious ownership` in a container job: the gap reads
  `evidence-not-ignored .verify/` although the folder is ignored. The run is
  refused, which is the safe side. Found by the verifier, 2026-11-14.
- **A `.git` that points to a repo that is gone reads as "no repo".** Git
  then says `not a git repository`, the doctor is clean, and the run can
  pass with the mark `nogit`. The same holds with `GIT_DIR` set to a path
  that does not exist. Found by the adversary, 2026-11-14.
- **Git state is read at the start and at the end of a run, not between.**
  A step that edits a tracked file and a later step that puts it back leave
  no `dirty` mark. Found by the adversary, 2026-11-14.
- **A record with a pid that the system cannot hold ends `runs` and `clean`
  with exit 2 for the whole project** (`OverflowError`). It needs a damaged
  `run.json` or `started.pids`. Found by the adversary, 2026-11-14.
- **A step that takes the read permission from the run's temp folder
  itself** (not from a folder inside it) leaves a folder that `clean` keeps
  on every call, until someone makes it readable by hand. Found by the
  adversary, 2026-11-14.
- **`verify runs` says `not-cleaned` for more than a failed removal:** also
  for a run with `--keep-tmp`, and for a run whose temp folder could not be
  made. The skill text names only the first case. `clean` removes a folder
  that `--keep-tmp` kept. Found by the adversary, 2026-11-14.
- **A killed runner that its parent has not yet collected stays `live`**
  until the parent does. Found by the adversary, 2026-11-14.
- **Start times are read in UTC since 2026-11-14.** Before, `ps` printed
  local time, and `clean` from a session in another time zone took a live
  run for a cut-off one and removed its temp folder (found by the
  adversary). A run that an older copy of the runner recorded in local time
  has the same problem until it ends. The reverse is possible at long odds
  too: within the hours of the zone's offset, a new process that got the
  recorded pid at the one second that reads the same in UTC would count as
  the recorded one, and `clean` would stop it.

## Merge follow-up (`config/claude/skills/merge-follow-up`, 2026-11-14)

### By design

- **Each session gets each event once; only the reinstall is claimed once.**
  The design page of 2026-11-14 said that one session claims an event. With
  that, a second session would never learn of a merge, and its branches
  would stay behind. So the cursor is per session, and `--claim` is for a
  step that must run once.
- **An event is delivered at most once.** The cursor is saved before the
  script prints the events. A session that stops before it acts has the
  events in its conversation only. If the print itself fails (a closed pipe
  gives exit 120, which is not a result), those events are lost. The skill
  text forbids a pipe.
- **A head that takes pull requests is marked `shared_head`,** and the text
  forbids a rebase of it or a push to it. The mark comes from the branch
  names of the merge gate (`main`, `master`, `trunk`, `dev`, `develop`,
  `development`, `integration`), from the branch that `$MERGE_GATE_BASE`
  names, and from the bases of the listed pull requests, all compared
  without regard to case. A head with no name is marked. A branch that is
  the base of another of your pull requests (a stack) is marked too. An
  integration branch with another name is not marked when none of your 100
  newest pull requests has it as its base; for that one the rule holds in
  text only. The script does not ask GitHub whether a pull request of
  another author targets a head: that is one more call for each event, for
  a rare case.
- **The agent has two push commands, and both name the branch in full.**
  A fix goes up with a plain push, which git refuses when the branch on
  GitHub has a commit that the local branch does not include. Only a push
  after a rebase uses force, with `--force-with-lease` and
  `--force-if-includes` together. The lease alone protects nothing once a
  fetch ran, and the skill's own first step is a fetch: a commit that
  someone else added to the task branch would be overwritten (found by the
  adversary, 2026-11-15). With both flags git refuses that push. The text
  says to stop and report then. `--force-if-includes` needs git 2.30 or
  newer; an older git refuses the command.
- **`--force-if-includes` reads the reflog of the local branch.** A foreign
  commit that the local branch once pointed at, and that a later reset or
  rebase dropped, counts as included, and the push overwrites it. The rule
  "push only to a branch that this session created" and the stop after a
  refused push are text only.
- **A branch name or commit id that is not of the plain form is given as
  `null`.** A plain name is letters, digits and `. _ / -`, at most 200, with
  no `..`. Git allows more; a branch with such a name gets its events with no
  name, and the agent reports them and runs nothing. Check names are free
  text and are passed on as they are; the text calls them data.
- **A session's first look reports nothing.** It records what is there. A
  merge that happened before that look is not news.
- **The script sees the 100 newest pull requests of the signed-in user.**
- **It reports no green check, no review and no comment.**
- **"Branches that this session created" is the session's own knowledge.**
  No code records who made a branch. A session that lost that knowledge (a
  new session, a compacted one) must read its plan or ask.
- **Polling needs a tool that wakes the session.** Without one, the skill
  looks when the user next writes.
- **The repair of a squashed release is commands in the skill text,** tested
  as written on a real repo. No code runs them for the agent, and the push
  is always the user's.

### Minor

- **Two looks of one session that overlap can report one event twice.** The
  script reads the pull requests before it takes the lock, so the slower
  look can save an older picture over the newer one.
- **A subagent has the session name of its lead** (`CLAUDE_CODE_SESSION_ID`
  is the same). A look from a subagent takes the lead's events, and both
  hold the same claim. The text says to look from the lead only.
- **The repair of a squashed release is not claimed, and has no check for
  "already repaired".** Each session that gets the release event builds its
  own `sync/...` branch and hands over a push command, also after the user
  has repaired it by hand. The extra commit changes no file and the push is
  a fast-forward.
- **A session that has not looked for 30 days loses its cursor.** Its next
  look is a first look, and what happened in between is not reported.
- **A look that sees a shorter list forgets the rest.** The cursor is
  replaced by the list of the look. After a look as another GitHub user
  (`gh auth switch`), or after a pull request left the 100 newest, the next
  look with the full list reports old merges as new. The checks of the
  repair stop an old release; a reinstall claim for an old number would be
  given. Found by the adversary, 2026-11-14.
- **An answer of `gh` that GitHub does not give can stop every look.** A
  base name that is a list gives exit 2 on each look while that pull
  request is listed. A parent count in digits that are not ASCII would be
  read as a number. Both found by the adversary with a stand-in `gh`.
- **The order of `gh pr list` beyond 100 pull requests was not checked.**
  If it is not newest first, an old pull request can come back into the
  list and be reported as new.
- **A new task branch with a name that is already on GitHub can move that
  branch.** The plain push is a fast-forward when the old branch of that
  name is fully inside the base (a merged branch that nobody deleted). No
  commit is lost. Found by the adversary on #20, 2026-11-15.
- **With the reflog of the local repo turned off, the force push after a
  normal rebase is refused** (`core.logAllRefUpdates=false`).
  `--force-if-includes` has nothing to read then. The agent stops and
  reports. Found by the adversary on #20.
- **The lock is `flock` on a file beside the state file.** It is opened
  without following a link and is never emptied. A file system that does
  not honour `flock` (some network mounts) gives no lock.

### Not tested

- No live session followed the skill text. No Codex session ran the script.
- The script against the real `gh` in CI: the tests use a stand-in that
  answers from a file. It ran by hand against the real `gh` on this repo.

## Setup repo (`config/claude/skills/setup-repo`, 2026-11-15)

### By design

- **The script reads; the agent fills.** `repo-doctor.py` writes no file and
  calls no network and no `gh`. It calls git with `rev-parse` and
  `for-each-ref` only.
- **`unknown` is never a pass.** A check that could not find out gives exit
  1, as a gap does.
- **The integration branch is known by four names** (`dev`, `develop`,
  `development`, `integration`), the ones of the merge rule. A repo that
  integrates on another branch shows a gap; the text says to report it and
  to create nothing. The check reads the remote-tracking refs, so it sees
  what the last fetch saw. A clone of one branch (`--single-branch`,
  `--depth`) never fetches `dev`, so it shows a gap although GitHub has the
  branch.
- **`dev` is made with one command that cannot move a branch.** The text
  gives `git push --force-with-lease=refs/heads/dev: origin
  refs/remotes/origin/<default>:refs/heads/dev`. The empty value after the
  colon makes git refuse when the remote has a `dev` already, also one that
  the clone never fetched, and the source is the remote-tracking ref, so no
  local branch and no local commit can go into it. The first version (a
  local `git branch`, then a plain push) pushed an old local `dev` when one
  was there, and fast-forwarded a remote `dev` that was behind the default
  branch. Both reviewers found that, 2026-11-15. A remote that has `dev`
  only as a tag gets a branch `dev` beside it.
- **The workflow checks read the files as text, line by line,** against two
  fixed lists of commands, as whole words. A command in a step that never
  runs counts. A command that is not on the lists does not; the text says
  to wrap it in `make test` or `make lint`, or to report the gap.
- **A check that is `ok` shows that a file names something, not that it
  works.** `ci-tests` does not show that CI passes or that a branch is
  protected. `verify-skill` does not run the verify skill. `rule-table`
  checks the outer form only; the rows are checked by `rules.py` of the
  `correct` skill.
- **The root map is `CLAUDE.md`, else `AGENTS.md`,** at the top of the repo.
  One form names a command: a line that starts with `build`, `test`,
  `lint` or `install` (after markdown marks, with `command` or `commands`
  allowed after it), then `:` or the `|` of a table row, and then at once
  a code span that holds something. Nothing else counts: not `## Tests`,
  not `Testing:`, not a sentence, and not a fenced block below a label.
  This is the third form of the rule. The first took any line with the word
  and a code span, and the second took a code span right after the word and
  a fenced block near a label; the adversary got a sentence past each. A
  rule that guesses at free text was the class, so the rule now reads the
  one form that the skill text gives, and a map in another form shows a
  gap. That is the safe side: the agent adds four lines.
- **The pattern of the rule has one way to match each part.** The first
  text of the one-form rule did not: on a line with the start of a code
  span and no end, the time grew with the square of the length, and the
  map is read up to one mebibyte. An automated security review saw it; a
  test holds a line of that size.
- **What the one form still takes.** A line such as "Install: `uv` first"
  has the form and counts. The script cannot tell a command from a path in
  the code span, and it reads a label inside an HTML comment, an indented
  code block or the inner fence of a longer fence as a label.
- **The command that makes `dev` ends with exit 0 when GitHub has a `dev`
  at the same commit as the default branch.** Git says "Everything
  up-to-date", nothing moves, and an agent can report that it made the
  branch.
- **The map holds only commands that ran.** For an install command that
  writes outside the repo, the text says to leave the line out until the
  user has said yes and the command ran. The first version let the agent
  write it without a run; the verifier found that.
- **No text from the repo is printed.** Each line is built from fixed words
  and counts: `pytest in 2 of 3 workflow file(s)`, `2 verify skill(s)`. A
  file name, a folder name, a key of the rule table and the text of an
  unexpected error are never in the output, so a name cannot read as a
  result line or as an instruction to the reader. Two automated security
  reviews asked for this, 2026-11-15; the first answer (print a name only
  when it is a plain one) still let a name carry a sentence. The cost: the
  agent must find the workflow file itself.
- **A path that leaves the repo through a link counts as absent,** and each
  file is read up to 1 MiB.
- **`scaffold-docs` left the repo with this change.** `orient` still reads
  `docs/OVERVIEW.md` and `docs/changelog/` when they are there; nothing in
  the harness writes them any more.
- **The first workflow of a repo is merged by the user,** and the paths of
  `REVIEW.md` are the user's call. Both are text in the skill; no code holds
  them.

### Minor

- **More ways in which a workflow counts and runs nothing** (found by the
  adversary, 2026-11-15; all of the kind "the files are read as text"): a
  file name that is a command name (`cache-dependency-path: tox.ini`,
  `cp .flake8 dist/`), `pip install pytest ruff` with no run, a command in a
  comment after a command, a workflow that no pull request starts (`on:
  workflow_dispatch`), a workflow file that is a link to another file, a
  hidden line break (U+2028) inside a comment line, and a file of more than
  1 MiB that is cut inside a longer word.
- **One `.claude/rules.json` that is nested very deep ends the whole run**
  with exit 2 (`RecursionError`), so the other seven checks print nothing.
  A table with a key two times, with `NaN` or with a huge number is `ok`; a
  table that starts with a byte order mark is a gap.
- **A folder that cannot be read gives a gap, not `unknown`** (`.github`
  with no read bit, a link loop at `.github/workflows`). A plain file where
  a folder is expected (`.claude`, or `.claude/skills/verify-x` beside a
  real verify skill) gives `unknown`. Both fail safe: never `ok`.
- **A `CLAUDE.md` that is not a regular file** (a folder, a pipe, a dead
  link) is passed over for `AGENTS.md` with no word about it.
- **`verify-skill` is `ok` for two links** named `verify` and
  `features.json` that stay inside the repo, whatever they point to.
- **`comment-rule` looks for the sentence anywhere in the map,** so "Never
  change a comment in the same commit as the code it describes." counts.
- **Ten workflow files of 1 MiB each take about 18 seconds.**
- **A file that is swapped between the check and the read is not handled.**
- **On a disk that ignores case, `claude.md` is read as `CLAUDE.md`.**
- **A whitespace-only `REVIEW.md` is a gap** ("is empty").
- **Fences that the root map rule does not see as fences** (the adversary's
  third round, 2026-11-15). A label inside such a block counts as a label:
  a fence that is indented four spaces in a list item, a fence in a
  blockquote, a backtick fence that a `~~~` line seems to close, and a line
  such as ```` ```bash``` text ````.
- **Marks in the map that give a gap where a reader sees the form:** a line
  that a U+2028 or a form feed splits (Python's `splitlines()` splits
  there), a command in a span of two backticks, and a byte order mark before
  the first line. All three fail safe: a gap, never `ok`.
- **The reference test of the label rule uses ASCII lines only.** A line
  with Unicode whitespace or a long s (`ſ`) is not in it.

### Not tested

- No live session followed the skill text. The examples of the text are run
  through the script by a test: a repo set up as the text says is clean.
- The script ran by hand on this repo (6 gaps: no lint in CI, no root map,
  no `REVIEW.md`, no comment rule, no rule table, no verify skill) and on
  one other project (7 gaps).

## Maintain verify skill (`config/claude/skills/maintain-verify-skill`, 2026-11-15)

### By design

- **`check` runs the app.** Without `--no-run` it starts the runner copy of
  the project (`verify run`), which builds the app and runs every step in
  temp folders. The project's own code runs, as in any verify run. With
  `--no-run`, nothing of the app runs: one file of the project is started,
  the runner copy for its doctor, and only as the next two points say. Git
  is called to list the tracked files, and the doctor of the runner asks
  git if the evidence folder is ignored. Both calls get no `GIT_` variable
  of the caller, and the file watcher of the repo's settings is off for
  them (an automated security review asked for this, 2026-11-15; a test
  then showed that the doctor's call started that program).
- **The runner copy is started only when it is the current runner, byte for
  byte.** A copy that is behind, or that someone changed, is `runner-old`,
  and the doctor and the run are left out until `bump` brought it up. So
  `check` never runs a runner that the installed skills do not hold.
- **The runner copy is started in Python's isolated mode (`-I`),** for the
  doctor and for the run. A plain start puts the copy's own folder first on
  the module path. The adversary's third round put a tracked `tempfile.py`
  beside the copy: it ran as code of the project under `--no-run`, it left
  a `__pycache__` folder, and the result was still `drift: clean`. With
  `-I` the copy loads the modules of the Python that runs `drift.py` and
  nothing from its own folder, from the project or from a `PYTHON`
  variable. The steps of a full run still get the caller's variables: `-I`
  changes how the runner starts, not what it hands on. The trust rule in
  one sentence: under `--no-run`, the bytes that run are the installed
  skills', Python's own and git's, and no file of the project is code. The
  rule does not hold when a lookup path of the caller leads into the
  project (the next point).
- **A lookup path of the caller that leads into the project can make a
  file of the project run.** Three ways are known, each shown in a run, and
  the list may not be whole. The first is an empty entry, `.` or another
  relative folder in `PATH`: the doctor's two git calls start with the
  project as the current folder, `drift.py` calls git for the file list,
  and a relative entry of `PATH` is read from the current folder. The
  second is an absolute folder of `PATH` that lies inside the project, such
  as `<project>/bin`. In both, a tracked file named `git` in that place
  runs. The third is an empty or relative entry in `PYTHONPATH` while
  `drift.py` is started from inside the project: the copy of the runner is
  started in isolated mode, but `drift.py` itself is a plain script, so
  files of the current folder load as modules. A tracked `fnmatch.py` loads
  in place of Python's own. A tracked `__future__.py` loads at the first
  import of `drift.py`. A tracked `sitecustomize.py` runs when Python
  starts, before any line of `drift.py`. In all three ways the file runs
  also under `--no-run`, and the result can still be `drift: clean`. The
  project alone cannot set these variables of the caller. A tool that the
  caller set up to read `PATH` entries from a file of the project can. With
  such a `PATH` every `git` that is typed in that folder runs the same
  file. With such a `PYTHONPATH` other Python scripts that are started in
  that folder can load the same files. A script that is started in isolated
  mode does not, and a module that Python holds from its start is not
  looked up again (`os`, seen with Python 3.12). The runner has the same
  lookups when it is started by hand. The reviews of 2026-11-15 found the
  three ways one after the other (rounds four, five and six), after the
  module path of the copy: it is one class, a lookup path that leads into
  the project. It is not fixed. A fix has to cover the class and not one
  variable. For the third way `drift.py` cannot help itself, because the
  files above load before a line of it can act: the start has to be
  isolated from outside, with `python3 -I` in the three commands of the
  skill text. In a run of `check --no-run` that was started so, nothing of
  the project loaded. For the first two ways `drift.py` finds git one time,
  with the relative entries and every folder inside the project left out,
  and gives the doctor a `PATH` without them. "Inside the project" has to
  be tested after links are followed, and without regard to letter case on
  a disk that ignores it.
- **What the runner refuses before a run is a finding, and the runner itself
  is asked for it.** The map: `check` loads the text of the current runner
  as a module, from the bytes that it compares with the copy, and calls its
  `load_map`; a refusal is `failed-map` with the runner's own reason. None
  of the runner's commands runs for this, and no cache file is written. The
  doctor: `check` runs `verify doctor`, also with `--no-run`; each gap is
  `failed-doctor`. Each step is left out when the one before it gave a
  finding. `drift.py` holds no copy of the runner's rules.
- **This is the third form of the check.** An adversary review broke the
  first two with one class, "drift.py does not see what the runner
  refuses". The first said `clean` without the run for a map that the
  runner does not take, and threw the other findings away when the runner
  ended with exit 2. The second asked the runner's map rules only: a gap of
  the doctor (a value of `env` outside the temp folder, a fixture folder
  that is gone) was still `clean` without the run, and every exit 2 of the
  runner became a finding, also "the run was interrupted". Now each stage
  of the runner is asked through its own contract, and no reason text is
  read to tell a refusal from a crash.
- **A runner result counts only in its full form.** For the doctor: exit 0
  with the one line `doctor clean`, or exit 1 with the summary line and as
  many gap lines above it as it counts. For the run: exit 0 with the pass
  line, or exit 1 with the summary line and a failed line above it.
  Anything else (exit 2 with the runner's error line, another exit code, a
  foreign last line, a signal, a timeout) is `drift: error`, exit 2.
- **`drift: error` keeps the findings that were found before it.** They are
  printed above the error line. Exit 2 still means that the list is not
  whole.
- **With no current runner beside the script nothing is started,** and the
  form of the map is not checked by the runner's rules. The line
  `runner-old unknown runner` keeps the result from being clean.
- **A current runner whose text cannot be loaded,** or that has no
  `load_map` and `MapError`, is `drift: error`.
- **A pattern matches as `fnmatch` does, case-sensitive, and `*` also takes
  a `/`.** So `src/**/*.py` does not match `src/a.py`, and such a map shows
  a stale path. The runner itself has no rule for `paths`.
- **`uncovered-path` looks only into top folders that some pattern reaches
  into,** and counts files per folder. A new top folder is not listed.
- **A failed build has its own line,** `failed-build`, counted in `failed=`.
  The design gave no line for it; without one a failed build would read as
  no failed step.
- **`bump` reads the runner number from the text of the runner** (`RUNNER =
  N`, exactly one such line) and never runs or loads the file. It writes
  a new file beside the copy and renames it, so it does not write through
  a link, and it keeps the mode of the copy, with execute added.
- **`runner=unknown`** in the last line when no current runner is beside
  the script. It is a finding, not a pass.
- **Names in a finding are data.** A feature id, a pattern, a folder name
  and the reason of a failed step are printed, with non-printable
  characters replaced. The agent needs them to act; the text calls them
  data.
- **What to do with a finding is text only.** No code stops an agent that
  weakens a step.

### Minor

- **Two `bump` runs at one time share one temp name.** The copy stays
  whole; one of the two can fail.
- **A `.claude` or `skills` folder that is itself a link is followed.** Only
  the `verify-<app>` folder and the copy are checked for links.
- **A `features.json` nested so deep that Python gives up on it** is
  `drift: error RecursionError`, exit 2, and not `failed-map`.
- **A `features.json` that is a link to a device or a pipe is read.** A link
  to `/dev/zero` makes `check` hang; the runner has the same limit.
- **`bump` says `clean` for a copy with the right bytes that is not
  executable.** It compares bytes only. A copy with mode `000` is replaced
  by one with mode `100`, which Python cannot read: set the mode by hand.
- **A `[` in a pattern starts a set, as in a shell.** A map that names a
  path such as `app/[slug]/page.tsx` shows a stale path. Write `[[]slug]`.
- **The `app` of the map is not compared with the name of the skill
  folder.** A map with another valid `app` in `verify-<app>/` gives no
  finding.
- **`--help` prints the usage with exit 0,** and its last line is not a
  result line. By the rule of the text, that is not a result.
- **A locale that cannot print a character of a finding ends the script
  with a Python error** and no last line. That is not a result either.
- **When git cannot list the tracked files, nothing else is printed.** The
  run ends with `drift: error`, and a `failed-map` or `runner-old` that the
  script already knew is not above it. Exit 2 means that the list is not
  whole, so no wrong result follows.
- **A build command that starts with a placeholder is not checked by the
  doctor.** When a map names `{root}/scripts/build.sh` and the file has
  moved, `--no-run` says `clean`, and a full run ends with `drift: error`
  (the runner's exit 2), not with `failed-build`. The cause is in the
  runner: its doctor passes over a tool with a placeholder.
- **A file that changes while `check` runs** (the map or the copy, between
  the reading and the run) can give findings of two states. A copy that is
  swapped between the comparison and its start is started.

### Not tested

- The timeout of three hours on the runner's run.
- A git that hangs or is missing; only a folder outside git is tested.
- A file name that is not UTF-8.
- No live session followed the skill text. The commands of the text run as
  written in a test, on a project that the real scaffold made.
- Whether a variable that a project's own Claude Code settings set for a
  session (`PATH`, `PYTHONPATH`) reaches `drift.py` as a lookup path into
  the project.
- `bump` and a full `check` with `drift.py` started in isolated mode.
- Linux, except through CI.

## Correct (`config/claude/skills/correct`, 2026-11-15)

### By design

- **The script finds lines; the agent finds rules.** `mine` lists a typed
  line when it holds one of about twenty words (`don't`, `never`, `again`,
  ...), and counts two lines as one only when they differ in case and
  punctuation. A correction in other words is not listed, and a line with
  such a word that is no correction is listed. Grouping by meaning is the
  agent's work.
- **A line in which anything looks like a secret is held back whole,** and
  only its line number is printed. The forms are: keys of several vendors,
  GitHub, AWS and Slack tokens, a JSON web token, a `Basic` value, a key
  word (the next point; a private key header holds one), a URL with a
  password (with or without a user name), a UUID, a run of 20 or more
  letters and digits that has both, and a run of 24 or more characters of
  letters, digits and `+ / _ = -` that has a small letter, a capital and a
  digit. No form asks what stands next to the secret. So a line with a
  commit id of 20 characters or more, a UUID or a long mixed path is held
  back too.
- **A line with a key word is held back, whatever stands next to the
  word.** The key words, in any letter case and also inside a longer word:
  `pass` (so also `password`, `passwd`, `passphrase`, `passcode`,
  `passkey`, `DB_PASS`), `pw` (so also `pwd`), `secret`, `token`, `api key`,
  `access key` and `private key` (with a space, `_`, `-` or nothing
  between), `auth`, `cred` (so also `credential`, `creds`), `bearer`. This
  is the fourth form
  of the rule, and the class was "the rule asks what stands next to the
  key word". The first wanted the key word to touch a `:` or `=`, or a
  flag to start at the key word. The second took a `:` or `=` anywhere
  after it, or a dash before it. The adversary got `SECRET_KEY=x` and
  `--db-password x` past the first, and the verifier got "my password is
  x" and `setdefault("PASSWORD", "x")` past the second: such a line has no
  mark that a rule could ask for. The cost is a line that holds no secret
  and is counted, not shown: "the per-token price", "the token count is
  wrong", "the author said no", "make the tests pass", "never bypass the
  gate", "round upward". On one real history the rule cost one candidate
  of 42, a line with the word `token`; 14 lines are held back where the
  first form held back 1.
- **The list of key words only grows.** The first list of the fourth form
  left `pass` out, because "make the tests pass" is what a correction
  says. Two automated security reviews of that commit showed the price:
  `DB_PASS=x` and `--pass x`, which the form before held back, were
  printed. So `pass` is on the list again, with `pw` and `cred`, and a
  test holds the rule against the one before on random lines: a line that
  the form before held back is never printed. On the real history the
  three short words cost no candidate (2 more lines held back). The same
  reviews asked for a `pass` that counts only with a `:`, `=`, `_` or dash
  beside it; that is the class above, and it was not taken. One of them
  asked for one shared list with the secret check of the visual-artifacts
  skill (`assemble.py`): not taken, because that check reads source files
  for another purpose, lets a placeholder pass, and ships in another skill
  folder.
- **A line with a character that is not printable is held back whole:** a
  control character, a zero-width character, a line or paragraph separator.
  Such a character can split a secret or join it to a word, and each way to
  print the line guesses which. The first fix took these characters out
  before the check and printed a space for them; an automated security
  review showed that the printed form was then one that nobody had checked
  (`--password`, a control character, the value). Tab, LF, CR and the space
  characters of other scripts (a no-break space, the wide space of Chinese
  text) pass and count as spaces. A line with an emoji that is joined from
  several (a zero-width joiner) is held back too.
- **No line is printed in a form that was not checked.** The text is
  checked as typed with full-width characters made plain, again with each
  line break closed (a secret that was copied from a wrapped line has a
  break in it), each of the two also with its spaces joined, and in the
  form that is printed. A secret that a plain space splits is not seen.
- **The same review asked for two more things.** A URL password can now be
  4,096 characters long (it was 256). A rule for a long run of hex digits
  alone was not put back: a run of 20 or more letters and digits with both
  kinds is held back, and a hex value of 32 characters from a random source
  has both kinds except in about one case in three million.
- **A secret of a form that is not on the list is not seen.** Examples: a
  value of fewer than 20 characters in a line with no key word, a value of
  small letters only, `Basic` with fewer than 16 characters after it. An
  automated security review asked to hold back every run of 20 or more
  characters that has two kinds of characters, separators included. That
  was taken in part (letters and digits with no separator, from 20): the
  full rule would hold back ordinary paths such as
  `tests/test_runner_part2.py`, which corrections name often. A trial on
  one real history gave the same counts before and after the stricter
  rules: 42 candidates, 1 line held back. (The key word rule changed that
  count later; see the point on key words.)
- **`mine` reads git with its own format.** The `GIT_` variables of the
  caller are dropped, and no signature is checked, so that only the
  script's own format reaches it. Each commit starts with a mark that is
  made for the run. The git settings of the user and of the repo are read.
  A test sets `log.showSignature`, `format.pretty` and `color.ui`, and the
  result does not change; other settings are not tested.
- **Two typed lines are one candidate when they differ only in case, marks
  and spaces,** in any script. Accents count: `écrire` and `ecrire` are two.
- **Only the text that was typed is read.** The pasted content of a prompt
  is never opened.
- **A line of more than 400 characters is left out** (and counted): it is a
  paste or a brief, not a correction. It is checked for a secret first, so
  it is counted as held back when it has one.
- **What is printed of a line is its first 160 characters.**
- **The layer `config` is in the list.** The design page of 2026-11-14 gave
  six layers and an example row with a seventh, `config`. The list has
  seven: architecture, types, config, lint, test, hook, docs.
- **A `docs` rule is a gap until the row says why no code can check it**
  (`accepted`). A rule that only a reader can judge is then no gap, and the
  table shows the reason.
- **The table that a person reads is a file under git** (`.claude/rules.md`),
  and `check` reports it when it is not what `table` writes.
- **`mine` reads at most the 2,000 newest commits,** prints at most 200
  history candidates and 30 files, and says in its last line how many it
  did not show.

### Minor

- **`not-in-ci` reads the `run:` commands of the workflow files as text.**
  A command names the enforcer when one of its words is the enforcer path,
  or a folder above it, written as a path: with a slash in it (`tests/`,
  `./tests`, `config/hooks`, `./lint.sh`). A word is what stands between
  spaces, quotes and the marks `; & | ( ) < >`. The first version took a
  bare folder name as a whole word, and the adversary showed `npm test`,
  `make test` and `echo "no tests yet"` counting for a folder; the class
  was "a word of a command is taken for a path". So a bare `tests` no
  longer counts, and `pytest tests` gives `not-in-ci`: the cost is one
  slash in the workflow. A path inside a longer word does not count either
  (`--ignore=tests/`, `PYTHONPATH=tests/`, `tests/*.py`), and a file at the
  top of the repo needs `./` before it. A job name, a step name, an input
  of an action and a comment do not count. A workflow that runs every test
  with no path named (`pytest`) does not count. A `run:` with nothing after
  it on its line is not a step (the `defaults` of a workflow have such a
  key); a command that starts on the line below it is so not read. The
  block of a `run:` is found by its indent, with no YAML parser: a `run:`
  key inside a quoted string, or a command in a flow mapping
  (`{run: ...}`), is read wrongly.
- **A path that a command names is not a path that the command runs.**
  `ls tests/`, `rm -r tests/` and `pytest --ignore tests/e2e` count.
- **A path in quotes inside a longer word of the shell counts:**
  `--ignore="tests/e2e"`, `PYTHONPATH="tests/" true`. A quote ends a word
  for the script and not for the shell.
- **Some right spellings give `not-in-ci`:** an enforcer path with a space
  in it, a command whose path stands on the next line of a plain YAML
  value, and a quoted key (`"run": ...`). A `run:` line inside the block
  of another key (the `script: |` of an action) counts.
- **Characters that print as nothing and are printable for Python pass:**
  U+3164, U+034F, U+2800, U+FE0F, U+115F, U+180B, U+E0101. One of them
  inside a secret splits it for the script and not for the eye. Found by
  the adversary's second round, as are the next three points.
- **A wrapped secret with a mark at the start of the next line is not
  joined** (`> `, `│`, a backslash at the line end), and a tab or a
  no-break space inside a secret prints as a space.
- **The URL rule is slow on a line with `://` and thousands of colons:**
  about 0.6 seconds for 4 KB.
- **A key word in letters of another script that look the same** (a
  Cyrillic `а` in `pаssword`) is not a key word.
- **A key word in another language is not a key word.** The list is
  English. A line such as `密碼是 x` or `パスワードは x` is printed when the
  value has fewer than 20 characters. It is inside the boundary above (a
  line with no key word), and it is the likeliest case for a user who types
  in Chinese. Words to weigh for the list: `密碼`, `密码`, `金鑰`, `密鑰`,
  `パスワード`. Found by the verifier, 2026-11-15.
- **A short secret beside a word that is not on the list is printed:**
  `curl -u name:value`, "the session cookie x", "the otp x", `key=x`. The
  adversary named `cookie`, `jwt`, `otp`, `session` and the `-u name:value`
  form as ones to weigh. `key` alone is not on the list.
- **A key word with a hyphen of another kind between its parts** (`api`,
  then U+2010 or U+2011, then `key`) is not a key word. With a full-width
  hyphen it is one, because that is made plain first.
- **A line of characters that grow when they are made plain is slow.** One
  million U+FDFA (18 characters each after the step) take about 10 seconds.
  The time grows with the length in a straight line: 0.9 seconds for
  100,000.
- **Only the first 50 numbers of the held-back lines are printed.** The
  count is whole. `SKILL.md` says "You get its line number"; past 50 lines
  you get the count only. A line with a key word is held back, so a long
  history reaches 50 sooner than before.
- **One history line that is JSON nested very deep stops `mine`** with exit
  2 (`RecursionError`); other broken lines are skipped. Found by both
  reviewers, 2026-11-15. It prints nothing from the file.
- **A `.claude/rules.md` that is not UTF-8 gives exit 2,** where
  `table-stale` would be the better answer.
- **When the rename of `table` fails** (`.claude/rules.md` is a folder),
  `.claude/rules.md.part` stays on disk.
- **A usage error** (`rules.py chek`) ends with exit 2 and the line of
  `argparse` on stderr, not with `rules: error ...`. It is not a result, and
  the text says so.
- **A typed line that starts with `/` is left out** as a slash command, also
  when it is a correction that starts with a path.
- **`check` does not run a workflow or a hook.** It shows that the enforcer
  file is there and that CI names it, not that the check passes or that the
  hook is installed.
- **An `evidence` line is free text.** The script does not look up the
  commit that it names.
- **A fix commit is one whose subject has the word `fix`.** A fix under
  another word is not counted, and a subject that only names the word, such
  as "Document the fix process", is.
- **Only folders that are named count as this project.** A worktree beside
  the repo needs `--project`.

### Not tested

- The live history of a second person. The trial ran on one history file
  (2026-11-15): 41 candidates, 14 lines held back, 19 too long. With the
  first form of the key word rule it was 42, 1 and 23. Only the summary
  line of each run was read.
- Signatures made with GnuPG. The test signs its commits with an SSH key.
- A live session with the user in step 2. A fresh agent followed the text on
  a stand-in project with the user's answer given ahead; it built two rules
  and showed each enforcer fail on the old mistake. Its findings about the
  text were taken.
- Linux.

## Versions (`tests/test_harness_version.py`, 2026-11-15)

### By design

- **No test reads a pull request.** That a title starts with the number, and
  that one pull request holds exactly one number, is a rule for the agent
  that opens it. A check for both needs a workflow that reads the base
  branch and the title.
- **The middle number does not say how large a change is.** It counts pull
  requests into `dev` (decision record 0008). The changelog entry says what
  a version holds.

### Minor

- **The tests and Markdown do not agree on what a heading is.** The tests
  take a line that starts with `## ` and has the form `## X.Y.Z (date)`. A
  heading with a tab after the hashes, with spaces before them, an
  underlined heading or an `<h2>` is a heading on the page and none for
  the tests; a `## ` line inside a comment or a code block is the other way
  round. So a changelog written in such a form can show another top entry
  than `VERSION` and pass. The pattern is older than the rule of 0.4.0. An
  adversary review found it on 2026-11-15; no pull request that follows
  the text writes such a heading.
- **A date is checked for its form only.** `2026-13-45` and `0000-00-00`
  pass, and so does a date that is older than the date of the entry below
  it.
- **The oldest entry is not checked against a rule.** Each entry is held
  against the one below it, and the oldest has none: a changelog that
  starts at `0.1.1` passes.
- **A digit of another script passes as a digit** (the patterns use `\d`):
  in a number of `VERSION` or of a heading after its first character, and
  at every place of a date.
- **The check of the major in `docs/architecture.md` reads one phrase,**
  also inside a comment.
- **Two open pull requests that take the same number are caught only when
  their entries have different dates.** Both make the same change to
  `VERSION`, so that file does not conflict in the rebase of the later
  one. The changelog does. With different dates the two heading lines
  differ: keeping both sides gives the number two times, and two tests
  fail. With the same date, which is the usual case, git takes the heading
  line as common to both: keeping both sides gives one entry with the text
  of both pull requests, and every test passes. (That is git's default
  conflict style. With `merge.conflictStyle=diff3` the heading stays inside
  the conflict, and two tests fail for the same date too.) Taking one side loses the
  entry of the other pull request, and every test passes too. Both
  reviewers and the author ran this with git on 2026-11-15; the first text
  of this point said that the tests catch it, from reading alone. So the
  rule is one for the agent that rebases: every entry that `dev` holds is
  still there afterwards, and the pull request's own entry is a new one on
  top, with the next number.

### Not tested

- How GitHub draws the changelog. The points on headings follow the
  CommonMark rules.

## Comment check (`config/claude/hooks/comment-check.py`, 2026-11-15)

### By design

- **A hit is advice.** The check lists a comment block whose code changed
  while no line of the block did. Whether the comment is now false is a
  judgement, and the verifier makes it (step 9 of its text): a comment that
  still holds is no finding, a false one is a Nit, and it is Important only
  when a reader who trusts it would use the code wrongly.
- **A check that did not run changes no verdict.** It goes under "Not
  covered". The review line needs a floor guard result and does not need
  this one. So a broken comment check cannot block a merge, and it cannot
  open one.
- **It sits beside the floor guard,** in `config/claude/hooks/`, and not in
  a skill folder as the design page of 2026-11-14 had it. The verifier runs
  both in the same way, and both read a repo they do not trust.
- **It reads lines, not the language.** Line comments only (`#` and `//`),
  by file extension. Block comments and docstrings are not read. A line
  inside a multi-line string that starts with `#` or `//` reads as a
  comment.
- **The code under a comment** is the lines after the block up to a blank
  line, the next comment block or a line indented less, at most 20 lines.
- **No hit for:** a file that the diff adds as a whole, a deleted file, a
  binary file, a link, a submodule, a change of line ends alone, code under
  the comment that the change deleted in full, and a deletion right after
  the last code line.
- **It reads the committed diff from the merge base to HEAD.** The index
  and the files of the working tree play no part, with one exception: git
  reads `.gitattributes` from the working tree, also when the file is
  untracked. The first text said that the working tree plays no part; the
  adversary's third round showed the exception. An attribute there starts
  no program. With `* -diff` it makes git print each file as binary, and the
  run ends with exit 2 (the point on binary below). It needs `--base`; with
  no base in the brief the verifier skips it and says so.
- **Lint and tool markers are not comments for this check** (`noqa`,
  `type: ignore`, `pragma`, `eslint-disable`, `pylint:`, `@ts-`, `nolint`,
  `shellcheck`, `fmt:`, `isort:`, `mypy:`, `prettier-ignore`, `nosec`,
  `nosemgrep`).
- **git's diff is taken only after it is held against the two versions of
  the file.** Each hunk must hold its removed lines and then its added
  lines, with no line of context, and the lines outside the hunks must be
  the same in both versions. A diff of another shape ends the run with exit
  2. An adversary review showed three settings that gave a false `clean`
  in the first version: `diff.interHunkContext` (also in the user's own
  git settings) joined two hunks over an unchanged comment,
  `core.bigFileThreshold` made git call a text file binary, and
  `core.worktree` sent every call to another repo. The class was "a setting
  changes what the script reads", so the script no longer trusts the text
  of `git diff`: it checks it. The context is also set on the command line,
  so the user's setting does no harm.
- **"Changed" is what git's diff calls changed.** The script checks that
  the diff is a true diff of the two versions; it does not check that it is
  the smallest one. When git lists a comment line as removed and added again
  with the same text (it can on a very large change, where it stops looking
  for the best match), the comment counts as changed and gives no hit.
- **The check trusts what is in `.git`.** It reads the refs, the commits
  and the files as the repo's git shows them. The adversary's second round
  got a false `clean` in three ways, and each needs a person who writes
  into `.git`: a replace ref for the blob of the new file, a grafts file
  that moves the merge base (with the setting that switches off git's
  warning for it), and a tag named like the short id of the base commit
  (with `core.warnAmbiguousRefs` off). The first design said "a setting
  cannot change what the check reads"; that was too wide. It holds for the
  text of `git diff`, which the check holds against the two versions. It
  does not hold for which commit a name means or which file an object id
  gives: git decides that, for this check and for every `git diff` and
  `git log` of the same review. To pin each of these would be a list that
  is never complete. The check is advice for a reviewer, not a lock. What
  holds for every repo: it starts no program of the repo, and it writes
  nothing. A base given as a full commit id cannot be taken for a ref name.
- **An attribute or a setting that makes git read text as binary gives exit
  2,** not a hit and not `clean`. A repo with `* -diff` in its attributes
  cannot be checked.
- **Every git call runs in the folder that `--repo` names.** The script
  does not ask git for the top of the work tree. A bare repo is read too.
- **The whole run has one time limit,** 100 seconds by default
  (`--timeout`), for the git calls and for the script's own work, the
  building of the output too. At the limit the run ends with exit 2. (The
  second review round showed that the limit ended when the hits were
  found: 60,000 hits in a file with a path of 20,000 characters then took
  131 seconds.) Only the one write of the finished output is outside it. The first version had a limit for each
  git call only, and one file with many comment blocks and many deletions
  made the script's own loop run for minutes; that loop is linear now.
- **A comment that starts with a marker word is not a comment for this
  check,** also when more text follows: `# fmt: the output format` gives no
  hit.
- **Only the Claude verifier runs it.** The Codex verifier has no step for
  it, as it has none for the floor guard.

### Minor

- **A usage error** (no `--base`) ends with exit 2 and the lines of
  `argparse` on stderr, not with `comment-check: error ...`. The verifier
  reads that as "did not run".
- **Nine mutations of the builder's run changed no output,** so no test can
  fail on them: five are equal code, and four are protections that these
  commands do not reach in a test (`--no-pager`, `--no-textconv` on the raw
  diff, `GIT_OPTIONAL_LOCKS`, the "Binary files" line of the first version).
  One mutation of the fix round is equal code too: the gap of a hunk that
  also adds lines never decides a hit, because its first added line already
  marks the code as changed.
- **A change with very many files can reach the time limit.** There is one
  `git diff` call for each changed file, about 80 ms each on the machine of
  the review: 2,000 changed files took 166 seconds there. The run then ends
  with exit 2, which the verifier reads as "did not run".
- **`PATH` and the `PYTHON` variables are the caller's own.** The check
  drops the caller's `GIT_` variables and no others. A `git` that stands
  first on `PATH`, or a module on `PYTHONPATH` (the verifier starts the
  check with `python3`, not `python3 -I`), is a program of the caller and
  can print any result.
- **A caller that blocks the alarm signal has no limit for the whole run.**
  The limit for each git call still holds.
- **The output of a run with very many hits is built in memory** before it
  is written.
- **The text of a git error is in the error line.** A value in the repo's
  settings can so reach the line `comment-check: error ...`. Step 9 of the
  verifier calls every line of the check data, the text of an error too.
- **"Pass means the checks ran"** in the Return part of the verifier's text
  does not name the comment check as an exception. Step 9 says that a
  comment check that did not run changes no verdict, and that rule is the
  specific one.
- **The indent of a line counts form feed and vertical tab too,** one
  column each, like a space or a tab. The text of the script names spaces
  and tabs only.
- **When the limit of the whole run ends inside a git call, the error line
  names the wrong cause:** `not a git repo: ... (the check did not finish
  in 2 s)` for a repo that is one. The exit code is 2 in both cases.
- **Replace refs are followed.** `GIT_NO_REPLACE_OBJECTS=1` would switch
  them off, and the check does not set it: a replace ref needs a person who
  writes into `.git`, which the check trusts (see "By design").

### Not tested

- A git older than 2.45, which does not know `GIT_NO_LAZY_FETCH` (the
  variable is in git's own manual from 2.45.0 on; two other sections of
  this file said 2.44 until 2026-11-15): in a
  partial clone it could fetch through a program that the repo's settings
  name.
- The time limit on a system with no alarm signal (Windows).
- Repeated lines such as `}`: the random test uses files with no duplicate
  lines, so it does not check how git lines them up.
- Git versions other than the local one, and Linux except through CI.
- No live verifier followed step 9 yet. The first reviews after this change
  are that test.

## Hand-off note (`config/claude/skills/handoff`, 2026-11-15)

Version 0.8.0 kept the note at `.claude/handoff.md` and wrote three names
into the exclude file of the repo. Its last review round ended with a break
that stayed open: all work trees of a repo share that file, so a file of the
user with one of the names in another work tree was gone from `git status`,
and `git worktree remove` then deleted it with no warning. Since 0.11.0
(2026-11-17) the note has a folder of its own that ignores itself, and the
script writes no rule outside that folder.

### By design

- **The check reads the form of a note, not its truth.** It cannot tell
  whether the goal is right, whether a command works, or whether a decision
  still holds. A note that passes can be wrong. The age of a note is no
  gap: only the form of the time in the fact block is read.
- **One note for each work tree, and the last writer wins.** Two sessions in
  one work tree replace each other's note. A note with text is kept one
  time, as `.claude/handoff/prev.md`; an empty frame is replaced and not
  kept. The write goes through a new file and a rename, so a reader sees a
  whole note, and nothing is written through a link.
- **`frame` moves and replaces only a note that `frame` wrote.** A file at
  the note's path or at the kept note's path that does not start as the
  notes of this script start (`---`, then `type: handoff`) is the user's
  own or came from elsewhere, and so is a link there. `frame` then ends
  with exit 2 before its first write. On a disk that does not keep letter
  case apart, this holds for `.claude/handoff/NOTE.md` too, which is the
  note's own path there. Before this rule, `frame` moved such a file to the
  kept note, and the `frame` after that replaced it. The price: a note
  whose first two lines were damaged (an editor that saves with a
  byte-order mark or with other line ends) is no note of this script any
  more. `check` and `frame` both end with exit 2 until the file is moved
  away; the error line of `frame` says to ask the user, and the one of
  `check` names the first line of the block (run 2026-11-17). A note that
  is damaged further down still starts as a note, and it is kept one time.
- **The folder ignores itself, and `frame` writes no rule anywhere else.**
  `frame` makes `.claude/handoff/` and puts a `.gitignore` with the one line
  `*` into it. In git the rule that is nearest to a file wins, so no `!`
  rule of the tree, of the exclude file or of the user's own ignore file
  shows the note to git, and a test holds that. The same rule keeps the
  note out of every other repo that looks at the folder. What follows from
  it:
  - a file of the user in another work tree, under any name, stays in
    `git status` there, and git refuses to remove that work tree;
  - the note goes with its work tree: `git worktree remove` deletes it with
    no warning, because git does not count an ignored file as work;
  - everything else in the folder is out of git's sight too. A session that
    puts a file beside the note hides it from git.
- **`frame` uses only a folder that `frame` made.** That is a folder whose
  `.gitignore` is the one line `*`, byte for byte, or an empty folder, which
  `frame` then makes its own. A `.claude/handoff` with files and another
  `.gitignore`, or none, is the user's folder: both commands end with exit
  2, the line says to ask the user, and nothing is written. A `.gitignore`
  there would hide the user's files from git, the fault of 0.8.0 in another
  place. A folder where the user wrote the same one-line `.gitignore` reads
  as a folder of `frame`: git did not see its files before, and `frame`
  adds its note beside them (run 2026-11-17).
- **The place must belong to the repo of this work tree, and git says
  which repo that is.** The script asks git two things from inside
  `.claude` (from inside the folder when it is there): its git folder, and
  the top of its work tree. Both must be the ones that git names from the
  top of the work tree, or both commands end with exit 2. With both the
  same, a git call from inside the place is the same call as from the top.
  A test holds each of these forms, at `.claude` and at the folder: a repo
  made there; a bare git folder there; a repo there whose settings name
  the outer tree as its work tree (`core.worktree`); a `.git` file there
  that names another repo; and a `.git` file there that names the outer
  repo's own git folder. Such a repo can bring a folder and a note that
  look like `frame`'s own. Three earlier forms of this check each compared
  one sign, and each missed a case in review (2026-11-17): the top alone
  (the settings of an inner repo can name the outer tree), the name `.git`
  alone (a git folder that is `.claude` itself has none), and the git
  folder alone (a `.git` file can name the outer one, with `.claude` as
  another top). A `.git` file that names no git folder is exit 2 with
  git's error. An empty folder named `.git` is no repo for git, and the
  commands work. Who keeps `.claude` as a repo of its own cannot use this
  skill there.
- **A repo that points at `.claude` from outside is not seen.** A git
  folder elsewhere can take `.claude` as its work tree (`core.worktree`,
  or `GIT_WORK_TREE` in another tool). Nothing inside `.claude` says so,
  and git, asked from inside, finds the repo of the work tree. `frame`
  then writes its note. A test holds this case: the other repo does not
  list the note, because the folder ignores itself for every repo. When
  that other repo tracks a file at the note's path, and the file starts as
  a note starts, `frame` moves and replaces it, and that repo shows the
  change. So what the script holds in full is about the git of this work
  tree and about each repo that git finds from inside the place.
- **One `frame` at a time works in `.claude/handoff/`.** A lock on that
  folder (`flock`) covers the look at the folder and at the old note, the
  move to the kept note and the write. Without it, two runs at one time
  both chose to keep a note with text, and the second moved the empty frame
  of the first over it. A run that waits longer than 30 s ends with exit 2
  and writes no note. The lock ends with its process. `check` takes no
  lock. It never reads half a note. While a `frame` keeps a note with text,
  there is a moment with no note at the path, between the move to the kept
  note and the rename of the new one: a `check` in that moment ends with
  exit 2, "no note at" (seen in the review of 2026-11-17, with a spy in
  that moment; 0.8.0 had the same).
- **The fact block ends with a check value, and the value holds no
  secret.** The last line of the block is 32 hex digits of SHA-256 over the
  lines above it. A block that was changed by hand is exit 2, also when its
  new values are the ones of the tree: the way from a stale note to `ok` is
  a new `frame`, not an edit. A program can write a block whose value fits,
  and the script cannot tell that from a frame. So each rule on the block
  holds by itself too: the lines and their order, the form of the commit
  and of the time, the count against the list, the limit of 50. The same
  facts in another JSON spelling, under a value that fits, are read. The
  agent writes the parts below the block and never the block.
- **`stale-changes` compares the lines of `git status` of this repo,** and
  `handoff: ok` says that the list is the same, not that the content is. A
  line names a file and the kind of change. A change behind a line that
  stays the same is not seen, and a test holds each of these cases:
  - another edit inside a file that was changed already;
  - another change inside a submodule that was changed already (its line
    is ` M <path>`, whatever changed in it);
  - any change inside a folder that is a repo of its own and no submodule,
    such as a clone below the work tree: git lists that folder as one line
    and does not look into it, and the script starts no git in another
    repo;
  - a file that a rule ignores: it has no line.

  The block lists the first 50 lines and counts the rest, so a change among
  the rest is seen only when the count moves. Three options take the
  settings of the repo out of the list: each file of a new folder of this
  repo has a line of its own (`--untracked-files=all`), a file that moved
  is one that is gone and one that is new (`--no-renames`), and a submodule
  with a change in it has its line (`--ignore-submodules=none`). So a
  folder with many files that is neither tracked nor ignored fills the 50
  lines by itself. Other settings of the repo still shape the list: with
  `core.fileMode` off, a file that was made executable has no line, and a
  test holds that case.
- **A path counts only when the note writes it as code, from the top
  folder, and when its first part exists there.** That keeps a branch name
  such as `origin/dev` and a path of another machine out. So the name of a
  file in a folder, written alone (`gone.py`), is not checked, a path
  inside a code block is not checked, and a code span with a space in it is
  not read as a path. A path whose first part is gone or is spelled wrong
  (`scr/app.py`, or `src/app.py` after the folder `src/` was removed) is
  not reported either, and so is a file of the top folder that was deleted
  (`Makefile`): no missing-path line does not mean that each path is
  there. A branch that is named like a top folder (`docs/next` beside a
  folder `docs/`) reads as a missing path: write such a name without the
  code marks.
- **The note is read line by line, by a small reader of its own,** not by a
  Markdown library. A section starts at a line that begins with `## `. A
  code block is a fence of three or more backticks or tildes that is
  indented by three spaces at most. What that leaves out:
  - a block that is indented further, such as one inside a list item: a
    line `## Name` inside it starts a section;
  - a heading written with a line of dashes under it, and `##Name` with no
    space: neither is a heading, and a heading closed with `##` is read
    with those marks as part of its name;
  - a code span that runs over two lines: it is not read as a span;
  - an escaped backtick: it is read as a real one, so a command or a path
    in escaped backticks counts as one in code.
- **The first open checkbox runs up to the next checkbox line.** A command
  in a plain bullet under it counts as its command.
- **The script calls the session's own git, in the user's own work tree.**
  It hands on none of the caller's `GIT_` variables, so such a variable
  cannot point it at another repo, and it takes no lock that it does not
  need. Two programs that the settings of a repo can name do not run: the
  file monitor is off (`core.fsmonitor`, also for the call that git makes
  in a submodule), and no call fetches. In a partial clone that lacks what
  `git status` needs, `frame` ends with exit 2.
- **A clean filter that the settings of the repo name still runs.**
  `git status` reads a changed file through its filter, and a run of
  2026-11-15 showed the filter program start under `frame` and under
  `check`. The status line switches each filter off, and pays for it: a
  touched file under such a filter can show as changed. The list of the
  note must be right, so the filter stays on. The settings in question are
  the ones of `.git/config`, which a clone does not bring; each
  `git status` of the session runs the same program.
- **A repo that tracks a file in the note's folder, under any letter case,
  cannot hold a note.** `frame` and `check` end with exit 2 when git tracks
  `.claude/handoff` itself or any file below it, in spellings such as
  `.claude/Handoff/Prev.md`, `.Claude/handoff/note.md`, or the folder name
  with the one character for `ff` (U+FB00). On the disk of a Mac each of
  these is the note's own place, and what git tracks there came with the
  repo. The answer is the same on a disk that keeps letter case apart,
  where they are other files. A tracked `.claude/handoff.md`, the name of
  0.8.0, is no longer in the way. The names that git lists
  under `.claude` in any ASCII letter case are compared after Unicode case
  folding. A disk that takes still more names for one is not covered (not
  run: the test disk has no such rule). Tracked means in the index: a note
  that the repo's last commit holds and that was taken out of the index by
  hand (`git rm --cached`) is read like any other (run 2026-11-16).
- **The commands of the skill text start Python in isolated mode
  (`python3 -I`),** and the script uses the standard library only and has
  no `__future__` line. So no file of the work tree is loaded as a module,
  also when the caller's module path has an empty entry; a test plants such
  files and runs the commands as written. `git` is still found through the
  caller's `PATH`: a `PATH` that leads into the work tree runs a `git` of
  that tree, as for every git call that is typed there (see "Maintain
  verify skill").
- **A note of 0.8.0 is not read and not moved.** A file at
  `.claude/handoff.md` is a file like any other now, and git lists it again
  when no rule ignores it. The three lines that 0.8.0 wrote into the
  exclude file of a repo stay there until someone takes them out. 0.8.0
  was on `dev` only and in no release.
- **A link is never followed.** `.claude`, the folder, its `.gitignore`,
  the note and the kept note are each opened with no link followed, and a
  link at one of them is exit 2 for `frame` and for `check`. The script
  opens the folder one time and reads, writes and renames every file
  through that open folder. A `.claude` that git tracks as one entry (a
  link, or a submodule) is refused too: the note in it came with a repo.
- **A note carries no approval, and that is text only.** No code stops a
  session that acts on a next action without the user's yes. Some ways in
  are closed by code: `check` does not read a note in a folder where git
  tracks a file, a note behind a link, or a note in a `.claude` that
  belongs to another repo or that git tracks as one entry (exit 2). A note that
  reaches the work tree another way, such as an unpacked archive that holds
  the folder with its `.gitignore`, is read like any other.

### Minor

- **A `frame` that is killed between the write and the rename leaves
  `.claude/handoff/note.tmp.<digits>`.** Git does not see that file, and no
  later run deletes it: `frame` deletes no file by the form of its name.
- **A `frame` that is killed while it makes the folder** leaves an empty
  folder, and the next `frame` goes on from it. Killed between the making
  of the `.gitignore` and the write of its two bytes, it leaves an empty
  `.gitignore`: both commands then end with exit 2 ("not a folder that
  frame made") until that file is deleted by hand (run 2026-11-17; the
  kill itself was not run). A write of those two bytes that fails (a full
  disk) leaves the same state, and the error line of that run names the
  note, not the `.gitignore` (`cannot write .claude/handoff/note.md: No
  space left on device`). Run with a write that was made to fail
  (2026-11-17); no real full disk was used.
- **Who owns the place is looked up before the folder is opened.** The
  two steps are not one: a folder that is swapped between them is opened
  with no link followed, and is then read as it is.
- **The script has no `--help`.** Each run ends with a result line or an
  error line, and a help text is neither, so `--help` is exit 2. The first
  lines of the script and the skill text say how to call it.
- **A name that the terminal's character set cannot show is printed in
  escaped form** (`\u65e5` in a gap line), and the run ends as usual.
- **The name of a work tree can end in a line break.** The script takes one
  line end off git's answer and no more, so such a tree gets its own note.
- **The note has mode 600.** It is for the same user's next session.
- **A name in the fact block is written as JSON text,** so a file name with
  a line break or a quote stays one entry. A file name that is not UTF-8 is
  written with its bytes escaped; that form was tested as text only,
  because the test disk refuses such a name.
- **The first 20 missing paths are printed.** The line `missing-path-more`
  and the count in the last line have the rest.

The adversary pass on the folder layout found the next five (2026-11-17).
Each one was run again by hand, and each is the same in the script of
0.8.0.

- **Text that stands only in heading lines is not kept.** `frame` keeps
  the last note as `prev.md` when a part of it holds text. A line that
  starts with `## ` is read as the name of a part, not as text. So a note
  whose only text is in such lines is replaced, and no copy is made.
- **`stale-branch` compares the short name of the branch.** For a branch
  `x` beside a tag `x`, git prints the short name `heads/x`, the same as
  for a branch named `heads/x`. A note that was written on the one gives no
  `stale-branch` line on the other. The commit is compared on its own
  (`stale-head`).
- **A fact block that is nested very deep ends both commands with exit 2**
  and the line `handoff: error RecursionError: ...` (a value of 50,000 `[`
  does it). Nothing is written and nothing is lost. `frame` works again
  when the user moves that file away.
- **With `core.splitIndex`, `check` changes the time stamp of
  `.git/sharedindex.<id>`.** Git itself does that when it reads a split
  index. The content of the file stays the same.
- **A pipe that closes early gives a traceback** (`BrokenPipeError`) on
  stderr and exit code 120, which is not one of the three. The skill text
  says to run each command with no pipe.

### Not tested

- No live session followed the skill text with a shell. The live check
  (`HANDOFF_LIVE=1`, two sessions, not in CI) gives a fresh session with no
  shell the note alone, and a session without the note as the control. So
  it shows that a note of this form is enough to go on, and not that an
  agent runs `check` first.
- Two sessions that write the parts of one note at the same time.
- `git clean -x`. By git's own text it deletes ignored files, so it would
  delete the note. The guard of this harness denies that command, and no
  test runs it.
- A `.claude` on another file system than the work tree (a mount point)
  has no test. Git's search for a repo stops at the edge of a file system,
  so both commands end with exit 2 and git's error line. Run by hand one
  time, with a disk image mounted at `.claude` (2026-11-17).
- A `.claude` that another user of the machine owns (`safe.directory`).
- A sparse checkout.
- A disk with no `flock` (some network disks). By the code, `frame` ends
  there with exit 2 and writes no note.
- Git versions other than the local one (2.54) and the one of CI.
- Linux. CI runs on macOS only.

## Reference builds (`config/claude/CLAUDE.md`, 2026-11-17)

Reviewed on 2026-11-17: verifier pass at commit `c185d7f`, tier 1, with no
Important finding. Its two minor findings and its three notes on what the
rule leaves open are below, marked "from the review".

The rule and its source are in `tests/test_reference_rule.py`. The counts
below are from 30 live sessions on Sonnet, run on 2026-11-17 on the stand-in
project of that test, before the first review.

| The request                                           | Without the rule                       | With the rule                |
| ----------------------------------------------------- | -------------------------------------- | ---------------------------- |
| "exactly as the prototype"                            | 3 of 3 copied the sample content       | 3 of 3 whole, real data only |
| the same, plus "with the real data from data/"        | 3 of 3 whole, real data only           | 3 of 3 whole, real data only |
| the builder's instructions and a brief ("exactly as") | 3 of 9 dropped both parts with no data | 9 of 9 whole, real data only |

"Whole" means that all seven parts are on the page, and the two parts with no
data show an empty state.

Added on 2026-11-17 (0.10.0), from 17 more live sessions on Sonnet. Each is a
reviewer: the verifier's instructions, a brief with the goal "exactly as the
prototype" and no hint, and a page that is already built.

| The page                                              | Without the rule                       | With the rule                                        |
| ----------------------------------------------------- | -------------------------------------- | ---------------------------------------------------- |
| five parts, the two with no data dropped (a real run) | 0 of 9 failed it: 7 pass, 2 unverified | 6 of 6 failed it, both parts in an Important finding |
| all seven parts, two as empty states                  | not run                                | 0 of 2 failed it: 1 pass, 1 unverified               |

Without the rule, every reviewer saw the two dropped parts, and none counted
an Important finding: they logged the drop as a Nit or as no finding, and
one wrote "that is the right call under the rule". In the measured
session, the agent reported that the verifier had passed the build that had
the cuts.

### By design

- **The instructions of the builder and the verifier did not change.** The
  first plan was to add a step on reference builds to both. With the rule
  alone, the builder built every part in 9 of 9 runs and the reviewer failed
  the dropped parts in 6 of 6, so a second text for the same order was left
  out. Both agents load the rules file.
- **The control shows another fault than the measured session.** There, the
  agent dropped parts. In the short run without the rule, the session that
  Sean talks to copies the prototype with its sample content, against the
  project rule. The drop shows only in the run with a brief, the path of the
  measured session, and only in a third of the runs.
- **The run with a brief has no control test.** A fault in 3 of 9 runs cannot
  be asserted in one run. With nine runs on each side, a difference of 3 to 0
  comes by chance about one time in ten, so the table supports the rule on
  that path and does not prove it.
- **A request that names the real data gets the same page with and without
  the rule.** The rule changes nothing there.
- **The size caps of both rules files were raised:** the Claude file from
  15,650 to 16,450 characters and from 230 to 241 lines (it is at 16,410 and
  241), the Codex file from 16,000 to 16,384 bytes (it is at 16,278). Codex
  reads 32 KiB of rules files, global and project together, so the global
  file now takes half and has about 100 bytes left.
- **The rule does not say what a part is.** The agent decides how fine the
  list is. A script that grades a build against a list of parts is planned
  as a skill of its own.

### Minor

- **The grader looks for a text, not for what a browser shows.** A heading
  inside a comment, a hidden element or an unused string counts as a part
  that is there.
- **"The report starts with what is missing" is tested as "the answer names
  both parts".** The test does not read where in the answer they stand. A
  correct session that writes "the Plan section" and not "Plan this week"
  fails the test; one control run wrote it that way.
- **No run asked for a yes.** Every session with the rule built the empty
  state, so the path "cut with my yes" was not seen.
- **Four sentences have no live test:** an approved design becomes a build
  task, a part under tier 2 review gets its own task, a brief hands over the
  reference itself, and a blocked run takes no work of its own. Each is
  pinned as text only.
- **"A design I approve" needs judgment.** A design that Sean only wanted to
  look at also becomes a task in the plan by this sentence.
- **A time box and the rule give opposite orders** (from the review). Time
  box says "when behind, cut from the top and tell me". The rule says a cut
  of a part is never the agent's, and it names checkpoints, absence and
  other rules, but no time box. When the cut list of a time-boxed reference
  build holds parts of the reference, the text does not say which rule
  wins. Both readings end in a cut that Sean hears of, or in a question.
- **One assertion pins less than its comment says** (from the review). In
  `test_no_other_rule_gives_leave_to_cut`, the line on "placeholders" reads
  only the craft rule and passes with or without the reference rule. The
  other assertions of the test fail when the rule is cut.
- **The list of parts is the agent's own** (from the review). A list that
  misses a view of the reference, as the summary in the measured session
  did, is then checked "part by part" and can pass.
- **"The check" is not tied to the Done line of a brief** (from the
  review). The sentence says what the check compares. The brief rule does
  not yet ask for that comparison in its Done line.
- **Work nobody asked for is named only for Unattended mode** (from the
  review). In an attended run with a blocked task, only the older sentence
  holds ("Scope beyond the request goes in suggestions"), and that sentence
  was in the rules of the measured session.
- **The reviewer tests read words, not judgment.** The control passes only
  for the verdicts "pass" and "unverified"; an answer with no verdict line,
  or with another word there, fails it. The test with the rule asks for the verdict "fail", the word
  "Important" and the names of both parts anywhere in the answer; a reviewer
  that fails the page for another reason and names the parts in passing
  passes it.
- **The reviewer gets one page.** It is the page of one real run, kept byte
  for byte in `tests/reference_fixture.py`. A page that drops one part, or
  that hides a part and keeps its heading, was not tried.
- **`.github/workflows/ci.yml` changed by one comment line,** the name of
  the new switch in the list of tests that CI does not run. No step changed.

### Not tested

- Either Codex mode. The Codex file was built and read, not run.
- Any model but Sonnet.
- A reference that is not an HTML file: a screenshot, a design file.
- A long session. The measured fault came late in a long one, through a
  brief that the agent wrote itself; each live run here is one short request.
- A reviewer with a shell. The live reviewer reads files and runs nothing,
  so no run had a floor guard result or a browser.
- The adversary, and a reviewer on Opus, the model the verifier runs on.

## 0.12.0: code colour for pseudocode and function names

Nits from the review of 69138a5. None changes the text of the code.

- **Teal strings and gray comments look close for a protan reader** (ΔE 3.8
  in light, 9.0 in dark). Other cues still separate them: comments are
  italic, and strings have quotes. The colour test checks that six colours
  differ, not that a colour-blind reader can tell them apart.
- **SQL:** an all-caps word missing from the short SQL keyword list
  (`COUNT`, `LIKE`, `OFFSET`, `VARCHAR`, `NULL`) shows orange as a
  constant, while listed keywords show purple. A table name before `(` in
  `CREATE TABLE t (` shows blue as a function.
- **sh and soft keywords:** the `$` of `$(date)` shows blue. `${HOME}`
  makes `HOME` orange, but `$HOME` stays plain. Python `match (x)` and
  `case (a, b)`, and JS `with (o)`, show blue as calls.
- **`Kit.trace` with real code and no `lang`** now gets the pseudocode
  rules: `'…'` is not a string, and `//` or `#` starts a comment, so
  Python `n // 2` reads as a comment. Pass `lang` for real code.
- **Pre-existing:** in sh, `${#arr[@]}` starts a comment at `#`.

## 0.13.0: names in pseudocode and in Go

By design, and what each rule costs. None changes the text of the code; a
test checks that on random lines in five languages.

- **Pseudocode keywords that are English words** count only where the line
  reads as code. So `x = a and b` has a plain `and`, because no control
  word stands before it. A line of prose that starts with `set`, `let`,
  `end`, `do` or `then` ("set aside the first node"), or that holds a
  control word ("continue to the next node of the list"), still gets
  keyword colour.
- **A name in capitals right before `(` is a function.** This replaces
  one statement of the 0.12.0 section above: in SQL, `COUNT(*)` and
  `VARCHAR(36)` now show blue, not orange. `LIKE`, `OFFSET` and `NULL`
  still show orange. In pseudocode `O(n)` shows blue too.
- **Go:** `EOF` and a three-letter constant such as `MAX` are plain, and a
  field named `HTTP` or `JSON` is orange. The rule knows the length of a
  name, not its role.
- **A statement word after `then` or `else` stays plain** (a Nit of the
  review of 509fcb4). `set`, `let`, `end` and the other words that open a
  statement count only as the first word of a line. So
  `if x > 0 then set y to 1` colours `if`, `then` and `to`, and leaves
  `set` plain; `if done then end` leaves `end` plain.
- **A hyphen between two names in capitals reads as one name** in
  pseudocode (same review): in `z = LO-MAX(x)`, `LO` shows blue as a part
  of the function name.
