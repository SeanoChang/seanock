---
name: create-verify-skill
description: Write a verify skill into a project, so that an agent can drive the real app, feature by feature, and keep the evidence. It adds a runner, a feature map and a short skill file under the project's .claude/skills/verify-<app>/, and proves them with one real run. The user starts it with /create-verify-skill in the project. This version drives command-line steps only.
disable-model-invocation: true
---

# create-verify-skill

A project's tests show that its parts work. Nothing shows that the app works
unless someone runs it. This skill gives the project a scripted way to do
that: a feature map, a runner that follows it, and evidence that stays.

You write the feature map. A script writes the rest.

## What the project gets

```text
<project>/.claude/skills/verify-<app>/
  SKILL.md        when and how to run it, and how to read a result
  features.json   the feature map
  verify          the runner (a copy; the project does not need this skill to run it)
<project>/.verify/runs/<run>/      the evidence of each run, ignored by git
```

## Steps

1. **Read the app.** Find how it is built, how it starts, and what a user
   does with it: the README, the command list (`--help`), the routes, the
   end-to-end tests. List three to seven features in the user's words; each
   one becomes an entry of the map.
   This version runs commands only. If the app is a server or has a screen,
   cover what a command can reach, and say in your report what is not covered.

2. **Write the frame.**

   ```bash
   python3 ~/.claude/skills/create-verify-skill/scripts/scaffold.py --project <root> --app <name>
   ```

   Exit 0 and a last line that starts `create-verify-skill: wrote` means that
   the folder is there. Exit 2 means that nothing was written; the line says
   why. The script never changes a folder that exists.

3. **Write the feature map** in `features.json`. The shape:

   ```json
   {
     "app": "notes",
     "runner": 1,
     "build": ["cargo", "build", "--release"],
     "bin": "target/release",
     "fixture": "fixtures/vault",
     "env": { "NOTES_HOME": "{tmp}/vault" },
     "features": [
       {
         "id": "find",
         "paths": ["src/find/*"],
         "steps": [
           {
             "kind": "run",
             "argv": ["{bin}/notes", "find", "load balancer"],
             "expect": { "exit": 0, "stdout_has": ["2 notes"] }
           }
         ]
       }
     ]
   }
   ```

   - `paths` are the source files of the feature. They say which feature to
     run again when a file changes.
   - Each step is one command. `expect` must name the exit code. It can also
     hold `stdout_has`, `stdout_lacks`, `stderr_has` and `stderr_lacks`.
   - Expect something that is false when the feature is broken. A step that
     only expects exit 0 passes for most broken features.
   - **Keep the app away from real data.** Read where the app keeps its data
     when no variable is set. Give it that folder through `env`, as a path
     under `{tmp}`. When the app finds its data through the home folder, set
     `HOME` to a path under `{tmp}`.
   - Put the files the app needs in a `fixture` folder beside the map. Each
     feature runs in its own fresh `{tmp}`, with its own copy of that folder
     at `{tmp}/<its name>`. So no feature sees what another one wrote, and a
     feature gives the same result alone and in a full run. The build gets
     an empty `{tmp}`.
   - The doctor refuses an `env` value that is a fixed path on this machine.
     It checks nothing else. A path in `argv`, and a place that the app picks
     itself, are yours to find.
   - Optional keys: `timeout_s` (for all steps, or for one step; 120 seconds
     when absent), `build_timeout_s`, and `stdin` and `cwd` on a step.
   - The runner refuses a key that it does not know.

4. **Run it.** From the project root, each as its own command, with no pipe:

   ```bash
   python3 .claude/skills/verify-<app>/verify doctor
   python3 .claude/skills/verify-<app>/verify run
   ```

   Read the exit code and the last line together, as the project's new
   `SKILL.md` says. Fix the map until the run is exit 0 with a last line
   that starts `verify-<app>: pass`. If the app itself is broken, stop and
   report that; do not weaken the step.

5. **Show that each feature catches a broken app.** A wrong expected text
   only shows that the runner compares text. So break the app: for each
   feature, change one thing in the source that the feature promises (a
   condition, a format, a name), run that feature with `--only`, and see
   exit 1. Then undo the change, and check with `git diff` that the source is
   as it was. Do this on files with no uncommitted changes of the user's. A
   feature that passes on the broken app proves nothing: give it a step for
   that break. Think of the edges too: a limit, an empty input, a refusal.

6. **Check the evidence.** Open the run folder that the `evidence` line
   names. It must hold `run.json` and the output files, and the temp folder
   named in `run.json` must be gone. Check also that the app's real data
   folder is as it was before the first run.

7. **Point to it.** If the project has a root `CLAUDE.md`, add one line under
   its commands: `Verify the app: python3 .claude/skills/verify-<app>/verify run`.

8. **Commit** the skill folder and the `.gitignore` line on a branch, by the
   project's own review rules. If the project has none, commit on a branch
   and say so in the report. Then run once more: a pass covers a commit only
   when the pass line names that commit and has no `dirty` or `partial`.

## Report

- the features in the map, and for each one the break that you made and
  that its steps caught;
- the last line and the exit code of the final run, and its run folder;
- what the map does not cover (a server, a screen, a feature that needs real
  data or a paid service);
- anything in the app that the run showed to be broken.

## Limits

- Command-line steps only. Servers that must stay up and screens come in a
  later runner version.
- Two runs at the same time are safe for evidence and temp folders. They
  share the project's build folder, so let one build finish first.
