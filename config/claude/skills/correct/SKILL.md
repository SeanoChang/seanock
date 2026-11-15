---
name: correct
description: Turn a correction that the user gave more than once into a rule that code holds up. A script lists candidates from the repo (reverts, files fixed again and again) and from the user's prompt history for the project, and holds back every line that may contain a secret. For each rule the user picks, write the enforcer at the strongest layer that works, show it fail on the old mistake, and add a row to .claude/rules.json. The user starts it with /correct in the project.
disable-model-invocation: true
---

# correct

A correction that the user types two times is a rule that nothing holds up.
A sentence in `CLAUDE.md` is the weakest way to hold it. This skill finds
such corrections, and gives each one an enforcer: code that fails when the
rule is broken.

The rules of a project are in one table, `.claude/rules.json`, under git.
The script `rules.py` checks that table and writes the copy that a person
reads, `.claude/rules.md`.

Each command below runs as its own command, with no pipe. Read the exit code
and the last line together:

| Exit | Last line | Meaning |
| --- | --- | --- |
| 0 | `rules: clean` | Nothing found. |
| 1 | `rules: summary ...` | Candidates or gaps, one on each line above. |
| 2 | `rules: error ...` | It could not finish. This says nothing about the rules. |

Anything else is not a result. One command has a last line of its own:
`table` ends with `rules: wrote .claude/rules.md` and exit 0.

## 1. Find the candidates

```bash
python3 ~/.claude/skills/correct/scripts/rules.py mine --repo <project root>
```

By default the script reads the prompt history of Claude Code for this
project. Add `--project <folder>` for each other folder where the user
worked on this project, such as a worktree beside the repo. `--no-history`
leaves the history out, and `--history <file>` names another file.

The script prints one JSON object for each candidate:

| `kind` | What it is |
| --- | --- |
| `revert` | A commit that reverts another. Something went in that should not have. |
| `fix-cluster` | A file that fix commits touched two times or more. |
| `history` | A line that the user typed in this project and that reads like a correction, with how often (`count`) and when. |
| `held-back` | Not a candidate: the numbers of the history lines that the script did not print. |

**Do not read the prompt history yourself.** Not with a file tool, not with
`cat` or `grep`, and not the lines that `held-back` names. The file can hold
secrets that the user pasted. The script is its only reader, and it prints
no line in which anything looks like a secret.

The script finds lines; it does not understand them. Read the candidates and
group them into rules: one sentence each, in your own words, as short as
the user's. A `history` line with `count` 1 can still belong to a group of
three lines that say the same thing in other words.

## 2. Let the user pick

Show the user the rules you found, most repeated first: count each commit
and each typed line as one. For each rule give the sentence, the evidence
(commits, dates, counts), and the layer you would use. Ask one time which
ones to build. Build only those.

A text with quoted lines from the history stays in this conversation. Do not
write those lines into a file, a commit or a pull request.

## 3. Pick the layer

Take the strongest layer that works, and say in one line why each stronger
one does not.

| Layer | The rule holds because | Example |
| --- | --- | --- |
| `architecture` | the mistake cannot be written | one function owns every write to the state file |
| `types` | the type checker refuses it | a `Path` that is checked to be inside the repo |
| `config` | a setting turns the behaviour off | the attribution key in the settings file |
| `lint` | a lint rule flags it | no `print` in library code |
| `test` | a test fails on it, in CI | the text of a skill names no merge command |
| `hook` | a hook stops the action when it happens | the merge gate |
| `docs` | a sentence says so | "Lead with the main point." |

`docs` is for a rule that only a reader can judge. Such a row needs
`accepted`: one line that says why no code can check it. Without that line,
`check` reports the rule as a gap.

## 4. Write the enforcer first

1. Write the enforcer: the test, the lint rule, the hook, the type, the
   setting and its test.
2. **Show it fail on the old mistake.** Put the mistake back on a copy: a
   test fixture, or a copy of the project in a temp folder. Run the enforcer
   on the copy and keep the output. An enforcer that never failed proves
   nothing. Never put the mistake back in the working tree: a restore would
   also delete the work there that is not committed.
3. Show it pass on the code as it is now. If the code still has the mistake,
   fix it in the same change.
4. Make sure it runs without anyone thinking of it: in a CI workflow, or as
   a hook.

## 5. Add the row

```json
{"rules": [
  {"id": "no-ai-attribution",
   "rule": "Commits and PR bodies carry no AI attribution.",
   "layer": "config",
   "enforcer": {"path": "tests/test_attribution.py", "runs_in": "ci"},
   "evidence": ["commit 1a2b3c4", "history 2026-11-14"]}
]}
```

- A row has `id`, `rule`, `layer`, `enforcer` and `evidence`, and no other
  key. A `docs` row can also have `accepted`. The script refuses a row with
  a key missing or with a key that it does not know.
- `enforcer.path` is the file that fails when the rule is broken. For a
  `config` rule it is the test of the setting. For a `lint` rule it is the
  lint configuration that holds the rule; name that file in the workflow
  command. For a `docs` rule it is the file that states the rule.
- `runs_in` is `ci` or `hook`. A `docs` rule, and only a `docs` rule, has
  `none`.
- `evidence` says where the rule came from: `commit <id>` or
  `history <date>`. Write the date of a history line, never its words. For
  a line that was typed more than one time, write its first and last date.

## 6. Check, and write the table

```bash
python3 ~/.claude/skills/correct/scripts/rules.py check --repo <project root>
python3 ~/.claude/skills/correct/scripts/rules.py table --repo <project root>
```

`check` prints one line for each gap:

| Gap | Meaning | What to do |
| --- | --- | --- |
| `missing-enforcer` | The enforcer file is not there. | Write it, or correct the path. |
| `not-in-ci` | `runs_in` is `ci`, and no `run:` command of a workflow names the file, or a folder above it, as a path. | Add it to a workflow, written as a path: `pytest tests/`, not `pytest tests`. |
| `docs-only` | A `docs` rule with no `accepted` line. | Move it to a stronger layer, or say why none works. |
| `table-stale` | `.claude/rules.md` is not what `table` writes. | Run `table`. |

Run `table`, then `check` again, until the last line is `rules: clean`.
Commit `.claude/rules.json`, `.claude/rules.md` and the enforcers together,
and open a pull request by the rules of the project. Its tier comes from
what the enforcers touch: a change to a CI workflow or to a hook is review
work of the highest tier in most projects.

## Report

- each rule built: the sentence, the layer, the enforcer, and the output
  that shows it fail on the old mistake;
- each rule the user picked that you could not enforce, and why;
- the last line of `check`;
- how many history lines the script held back, as a number only.

## Limits

- The script finds a correction by its words (`don't`, `never`, `again`,
  ...). A correction in other words is not listed.
- It counts two lines as one when they differ only in case and punctuation.
  Grouping by meaning is your work.
- A line can be held back although it holds no secret: a full commit id, a
  UUID, a long word of letters and digits, a long path with capitals and
  digits, or any line with a word such as `password`, `token`, `secret` or
  `auth` in it ("the per-token price"). You get its line number, not its
  text.
- `not-in-ci` reads the `run:` commands of the workflow files as text. A
  name counts only as a word of its own with a slash in it: `tests/`,
  `./tests`, `config/hooks`, `./lint.sh`. A bare `tests` is a word like any
  other (`npm test` names no folder), so `pytest tests` does not count, and
  neither does `--ignore=tests/` or `tests/*.py`. A workflow that runs every
  test with no path named counts as not naming the file, and so does a path
  that only an input of an action names.
- `check` does not run a workflow. It shows that CI names the enforcer, not
  that the workflow passes.
- This skill adds rows. It does not rewrite `CLAUDE.md`.
