# seanock

An agent harness for Claude Code and Codex: global working rules, six workers,
twelve skills, hooks that check risky steps with code, and the tests that keep
all of it in step.

It was part of the terminal setup in
[ghostty-tmux-nvim](https://github.com/SeanoChang/ghostty-tmux-nvim) until
2026-11-12. This repo keeps that history, so an older commit id still resolves
here.

## What is in it

| Part        | Where                                 | What it does                                    |
| ----------- | ------------------------------------- | ----------------------------------------------- |
| Rules       | `config/claude/CLAUDE.md`             | Global rules, loaded into every session         |
| Codex rules | `config/codex/AGENTS.md`              | The same rules for Codex, built from the root   |
| Workers     | `config/claude/agents/`               | Builder, verifier, adversary, planner, scout, scribe |
| Skills      | `config/claude/skills/`               | Task instructions, loaded when a task needs one |
| Hooks       | `config/claude/hooks/`                | Code that runs on a tool call or a prompt       |
| Permissions | `config/claude/settings.json.tmpl`    | Deny and ask lists for risky commands           |
| Commands    | `config/claude/commands/`             | `/explore` and `/map`                           |
| Mods        | `config/claude/mods/`                 | Changes to the Claude Code interface            |
| Status line | `config/claude/statusline-command.sh` | Three lines under the prompt                    |
| Tests       | `tests/`, and beside each hook and skill | Run by `.github/workflows/ci.yml`            |

The harness has one version, in [`VERSION`](VERSION). A release is a tag
`vX.Y.Z` on `main`, and [CHANGELOG.md](CHANGELOG.md) has one entry for each.
[docs/architecture.md](docs/architecture.md) says how the parts meet and what
the three numbers mean. [docs/decisions/](docs/decisions/) holds one short
record for each choice that is hard to reverse. Minor findings that were
logged and not fixed are in [docs/known-limits.md](docs/known-limits.md).

## Requirements

- macOS, with `git`, `python3` and `jq`. The status line needs `jq`.
- [Claude Code](https://claude.com/claude-code).
- `gh`, for the merge-gate hook.
- Optional: the Codex CLI, for the `codex` group and the `codex-exec` skill.

## Install

```sh
git clone https://github.com/SeanoChang/seanock ~/dev/seanock
cd ~/dev/seanock
./install.sh --dry-run       # print the plan, change nothing
./install.sh
```

`install.sh` reads [`manifest.txt`](manifest.txt), a three-column list of
`mode`, `source` and `destination`. For each entry:

- **`link`** points the destination at the repo file, so editing a file in
  place edits the repo.
- **`merge`** keeps the destination a real folder and links each entry of the
  source into it. The machine can still add its own entries. Workers, skills,
  slash commands, hooks and mods install this way.
- A `.tmpl` source is rendered first: `__HOME__` becomes your real `$HOME`.
  Rendered output lands in `~/.dotfiles-rendered/`, outside the repo.

Anything already at a destination is **moved** to
`~/.dotfiles-backup/<timestamp>/`, with its path kept. Nothing is deleted, and
re-running is safe: correct links are left alone.

**Drift check.** Claude Code writes to its own `settings.json` (plugins, hooks,
model settings), which is a rendered copy of the template. Re-rendering would
drop those writes. So before it changes anything, the script compares each
rendered file with its template. If one has changed in place, it lists the file
and stops, and it prints the commands to see, keep or discard the changes.

```sh
./install.sh --only codex    # install one group: claude or codex
./install.sh --force         # re-render drifted templates (the live copy is backed up first)
./install.sh --help
```

**The links are live.** `~/.claude` then points into this working tree. A
branch that you check out here takes effect in the next session, so keep the
main checkout on `main` and do work in a git worktree.

## Claude Code

Everything under `config/claude/` installs into `~/.claude/`.

**Working rules.** `CLAUDE.md` holds global rules: work toward a stated
outcome and the check that proves it, reproduce a bug before fixing it, show
evidence before claiming success, review each change at a depth that follows
what it touches, never change remote infrastructure, and write in plain, short
sentences.

**Workers.** Six subagent definitions in `config/claude/agents/`. The builder
and the scribe write. The verifier, the adversary, the planner and the scout
only read and report, so a review cannot change the tree it judges. Each file
names its model and effort.

**Hooks.** `merge-gate.py` checks each `gh pr merge` against the review rules
in `CLAUDE.md`. `visual-artifacts-route.sh` points a request for a page at the
right skill. `second-brain-log-prompt.sh` logs each prompt for 30 days.
`second-brain-sweep.sh` is a read-only command that lists the logged questions
when you ask for a sweep. Each hook has tests in `config/claude/hooks/tests/`.

**Status line.** `statusline-command.sh` draws three lines under the prompt:

1. Folder, git branch, model, effort, session title and harness version. The
   version is the newest release tag that the live tree has reached, with
   `+N` for commits past it, `*` for changed files, and `@branch` when the
   tree is not on `main`.
2. Usage: context used, then the 5-hour and 7-day rate-limit windows with
   reset times.
3. Prompt-cache state: warm or cold, time left, what a cold rebuild would cost,
   the hit rate and the session cost.

Line 3 also reads a file that the `cache-keepalive` mod writes.

**Mods.** Mods are Claude Code plugins built from function hooks; they can draw
native panes and bands. `settings.json` loads them from `~/.claude/mods/` with
`CLAUDE_CODE_PLUGIN_DIRS`.

| Mod                 | What it does                                                                                               |
| ------------------- | ---------------------------------------------------------------------------------------------------------- |
| `cache-keepalive`   | Keeps an idle session's 1-hour prompt cache warm with a tiny request, at most three times per idle stretch |
| `statusline-bridge` | The status line as a compact band in the desktop app                                                       |

**Commands and skills.** Two slash commands live in `config/claude/commands/`:
`/explore` and `/map`. These skills live in `config/claude/skills/`, and Claude
loads whichever one matches the request:

| Skill                   | What it's for                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- |
| `premise-check`         | Traces each part of an idea to who asked for it, before any design or build                                    |
| `shape`                 | A time-boxed system design session, from a rough idea to a design doc                                          |
| `codex-exec`            | Hands checkable execution work to the Codex CLI, with a watchdog and a scope check                             |
| `verify-current`        | Checks the current state of anything fast-moving (model ids, CLI flags, prices) before stating it             |
| `coach`                 | Graded course work that the student must write: hints one rung at a time, checks, no solutions                 |
| `writing-markdown-docs` | House style for docs, with a reference per document type and a `check_doc.py` linter                           |
| `visual-artifacts`      | Learning pages, prototypes, guides and plan pages published as Claude Artifacts, with a render checker         |
| `brief-artifact`        | A decision or a short report as one page: markdown in, and a "Copy response" button gives the answer to paste |
| `tui-design`            | Designing and reviewing terminal UIs: layout, keybindings, colour, redraw and resize                           |
| `scaffold-docs`         | Writes a `CLAUDE.md` entry point into each meaningful folder and keeps a versioned changelog                   |
| `orient`                | Reads those scaffold docs at the start of a session instead of exploring the tree again                        |
| `ccdash-analysis`       | Turns a local [ccdash](https://github.com/SeanoChang/ccdash) archive into ranked changes to model routing      |

## Codex

`config/codex/AGENTS.md` holds the same global rules for Codex. It is built
from `config/claude/CLAUDE.md` and `config/codex/AGENTS.src.md` by
`config/codex/build_agents_md.py`, and the build stops when a rule has no
decision for Codex. After any edit to `CLAUDE.md`, run the build and
`tests/test_codex_rules.py`.

`config/codex/agents/` holds three Codex agents: builder, verifier and
adversary.

## Tests

CI runs these on every pull request, and on each push to `dev` and `main`:

```sh
python3 -m pip install -r .github/ci-requirements.txt
python3 -m playwright install chromium
python3 -m pytest tests -q -p no:cacheprovider
python3 -m pytest config/claude/hooks/tests -q -p no:cacheprovider
bash config/claude/hooks/tests/test-second-brain-hooks.sh
bash config/claude/hooks/tests/test-visual-artifacts-hook.sh
python3 -m pytest config/claude/skills/writing-markdown-docs/scripts -q -p no:cacheprovider
python3 -m pytest config/claude/skills/brief-artifact/tests -q -p no:cacheprovider
python3 -m pytest config/claude/skills/visual-artifacts/tests -q -p no:cacheprovider
```

Three groups of tests are skipped unless you set their switch:
`WORKER_CAST_SOURCE`, `COACH_LIVE` (starts a live Claude session) and
`CODEX_PROMPT` (needs the Codex CLI). The mod tests need the Claude Code CLI
and do not run in CI.

## Uninstall

Remove the symlinks that `install.sh` created. Your originals are in
`~/.dotfiles-backup/`.

## License

[MIT](LICENSE).
