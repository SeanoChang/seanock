---
name: merge-follow-up
description: Do the steps that follow a merged pull request, and learn of the merge without the user saying so. Use when the user says "merged", "merged 23", "I merged it" or "merged and reinstall", right after you open a pull request that waits for the user's merge, and after you merge a task pull request yourself. A read-only script reports what changed on the pull requests; then you rebase your own open branches, reinstall when the project has an install command, and take the next task. It never merges, and it never touches a branch of another session.
---

# merge-follow-up

After a merge, the same steps follow each time: fetch, rebase the branches
that are now behind, reinstall, go on. This skill lets a session see the
merge itself and do those steps, so the user does not have to type "merged".

This skill never merges a pull request. A task pull request is merged by the
rules in `CLAUDE.md` (review, CI, the merge gate). A release pull request is
merged by the user.

## 1. Look

Run this from anywhere, as its own command, with no pipe:

```bash
python3 ~/.claude/skills/merge-follow-up/scripts/pr-state.py --repo <project root>
```

The script reads the pull requests of the signed-in GitHub user with `gh`.
It changes nothing on GitHub and nothing in git. A session's first look
records what is there and reports nothing; each later look reports what
changed since the look before.

A Claude Code session is named by itself. Any other caller (Codex, a script)
adds `--session <a fixed name>` and uses the same name each time.

Read the exit code and the last line together:

| Exit | Last line | Meaning |
| --- | --- | --- |
| 0 | `pr-state: clean` | Nothing changed. |
| 1 | `pr-state: summary events=N` | N events, one JSON object on each line above. |
| 2 | `pr-state: error ...` | It could not finish. This says nothing about the pull requests. |

Anything else is not a result. Never read a failed look as "nothing changed".

## 2. When to look

- **Right after you open a pull request that waits for the user.** This is
  the first look. It makes the later ones useful.
- **While that pull request waits.** If the session has a tool that wakes it
  later (a schedule, loop or wake-up tool), plan one look every 5 minutes.
  Never wait with `sleep` or a loop in the shell. With no such tool, look
  when the user next writes.
- **When the user says "merged".** Look first. The script gives the facts
  (which branch, which commit, how it was merged) that the word does not.
- **Stop looking** when none of your pull requests is open.

One look costs one short turn. Do not look more often than every 5 minutes.

## 3. Act on each event

### `merged`, and `release` is false

A task pull request went into the integration branch.

1. `git fetch origin`.
2. Each `behind-base` event names an open pull request on the same base.
   Rebase the ones that **this session created**, each in its own worktree:
   `git rebase origin/<base>`.
   - On a conflict that you cannot resolve with certainty, run
     `git rebase --abort` and report the branch.
   - After a clean rebase, run that branch's checks again, then
     `git push --force-with-lease`. The new commit has no review yet: a
     reviewer that passed the branch re-checks it before it is merged.
3. **Leave every other branch alone.** A branch that you did not create
   belongs to another session or to the user. That session gets the same
   event from its own look.
4. Reinstall, once for all sessions. If the project's root `CLAUDE.md` names
   an install command, take the step first:

   ```bash
   python3 ~/.claude/skills/merge-follow-up/scripts/pr-state.py --repo <project root> --claim install:<pr number>
   ```

   Exit 0 with `pr-state: claimed ...`: run the install command. Exit 1 with
   `pr-state: summary taken ...`: another session has it, so skip it. No
   install command in the root map: skip it and say so.
5. Take the next open task of the plan, at its own tier and merge rule.

### `merged`, and `release` is true

The user merged a release. Fetch, then read `squashed_release`.

- `false`: nothing to repair. Report the merge and the commit.
- `true`: the release was merged with one parent, so the release branch and
  the integration branch share no history after it, and the next release
  will conflict. Prepare the repair and **hand the push to the user**. In
  the commands, `head_sha` is that field of the event: the last commit of
  the release pull request.

  ```bash
  git rev-parse "origin/<release>^{tree}" "<head_sha>^{tree}"
  git merge-base --is-ancestor <head_sha> origin/<integration>
  ```

  Go on only when the two trees are equal and the second command exits 0.
  Then the integration branch holds everything that the release branch
  holds, and this commit joins the two histories and changes no file:

  ```bash
  NEW=$(git commit-tree "origin/<integration>^{tree}" -p origin/<integration> -p origin/<release> -m "Merge <release> into <integration> after the release")
  git branch sync/<release>-into-<integration>-<version> "$NEW"
  ```

  Give the user this command, and do not run it yourself:
  `git push origin sync/<release>-into-<integration>-<version>:<integration>`.
  If a check above fails, build nothing: report what you saw.

Never tag a release and never push to a release or integration branch. Both
are the user's.

### `closed`

The pull request was closed without a merge. Stop work on that branch and
report it. Delete nothing: the branch and its worktree stay until the user
says otherwise.

### `checks-failed`

A check failed on the head commit of your open pull request. Read the failed
run (`gh run view --log-failed`, or the checks page), fix the cause on the
branch as normal work at the task's tier, and push. A failure on a branch
that you did not create: report it and leave it.

## Report

- each event you acted on, and what you did;
- each branch you rebased, with its new head commit, and each one you left
  (a conflict, another session's);
- whether you reinstalled, skipped it, or another session had it;
- a command that waits for the user, written out in full.

## Limits

- The script sees the 100 newest pull requests of the signed-in user.
- A look reports an event once. If your turn ends before you act on it, the
  event is in this conversation, not in a later look.
- It does not report a green check, a review or a comment.
