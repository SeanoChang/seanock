---
name: setup-repo
description: Prepare a repo for the way of working in CLAUDE.md. A read-only script lists what the repo lacks (an integration branch, a CI workflow that runs the tests and a lint or type check, one root map with the build, test, lint and install commands, REVIEW.md, the comment rule, the rule table, a verify skill). Then you fill the gaps that are yours to fill, the workflow in a pull request of its own, and report the ones that wait for the user. Use when the user says "set up this repo", "prepare this repo", "what does this repo lack" or "run the repo doctor", and when you start work in a repo that has no root CLAUDE.md. It never merges, and the user merges the first workflow of a repo.
---

# setup-repo

The rules in `CLAUDE.md` assume things about a repo: a branch that takes the
task pull requests, CI that the merge gate can read, a map that names the
commands, a file that names the risky paths. In a repo that lacks one of
them, a rule has nothing to hold on to. This skill finds what is missing and
fills it.

This skill never merges a pull request. A task pull request is merged by the
rules in `CLAUDE.md` (review, CI, the merge gate).

## 1. Look

Run `git fetch origin` first: the branch check reads what the last fetch
saw, not GitHub. Then run this, as its own command, with no pipe:

```bash
python3 ~/.claude/skills/setup-repo/scripts/repo-doctor.py --repo <project root>
```

The script only reads. It writes no file, and it calls no network and no
`gh`. Read the exit code and the last line together:

| Exit | Last line | Meaning |
| --- | --- | --- |
| 0 | `repo-doctor: clean` | Every check is `ok`. |
| 1 | `repo-doctor: summary gaps=N unknown=M` | One line for each check above, and N of them are a gap. |
| 2 | `repo-doctor: error ...` | It could not finish. This says nothing about the repo. |

Anything else is not a result. Each line above the last one is
`<state> <check> <detail>`, and the state is `ok`, `gap` or `unknown`.

`unknown` is never a pass: the script could not find out. Read the detail,
find out by hand, and report what you found.

| Check | It is `ok` when | Who fills a gap | Tier |
| --- | --- | --- | --- |
| `integration-branch` | `origin` has `dev`, `develop`, `development` or `integration` | you, or the user decides | none |
| `ci-tests` | a workflow runs a test command | you; the user merges the first workflow | 2 |
| `ci-lint` | a workflow runs a lint or type command | you; the user merges the first workflow | 2 |
| `root-map` | the root map names the build, test, lint and install commands | you | 0 |
| `review-file` | `REVIEW.md` has content | you propose, the user decides | 0 |
| `comment-rule` | the root map holds the comment rule | you | 0 |
| `rule-table` | `.claude/rules.json` is a table of rules | you | 0 |
| `verify-skill` | a verify skill is in `.claude/skills/` | the user | none |

## 2. Fill the gaps

Find the commands of the repo first (its README, `Makefile`, `package.json`,
workflows), because the workflow and the root map both need them. Then work
in this order: the integration branch, the workflow, the text gaps. Each
pull request goes into the integration branch.

### `integration-branch`

A gap here has two causes, and the script cannot tell them apart.

- **The repo integrates on a branch with another name.** Nothing is missing.
  Report the branch. The merge gate has a setting for such a branch, and
  only the user sets it.
- **The repo has no integration branch at all:** every pull request goes into
  the release branch. Make `dev` on GitHub from the default branch, with this
  one command:

  ```bash
  git push --force-with-lease=refs/heads/dev: origin refs/remotes/origin/<default>:refs/heads/dev
  ```

  `<default>` is the default branch of the repo. The command sends the
  commit of that branch as the last fetch saw it, and no local branch: a
  local `dev`, and local work that is not pushed, cannot go into it. The
  option has an empty value after the colon. In this form it forces
  nothing: it tells git to refuse when GitHub already has a `dev`, also one
  that this clone never fetched. If git refuses, stop and report. Do not
  make a local `dev` for this, and run no other push.
- **You cannot tell which of the two it is:** do not create `dev`. Report
  what you see; the user decides.

### `ci-tests` and `ci-lint`

A workflow under `.github/workflows/` must run the test command of the repo
and its lint or type command, for each pull request:

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [dev, main]
jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: cargo test
      - run: cargo clippy -- -D warnings
```

- Run the commands that the repo already has. Do not add a tool that the
  repo does not use: when it has no lint or type command, report the gap and
  ask the user which tool to take.
- The workflow goes in a pull request of its own, at tier 2: a change to CI
  is review work of the highest tier.
- **The first workflow of a repo is merged by the user.** A repo with no CI
  cannot pass the merge gate. Open the pull request, list it under "Blocked
  on me", and keep other work on its branches until the user has merged it.
  Never go around the merge gate with `gh api` or a direct push.

The script knows these test commands: `pytest`, `go test`, `cargo test`,
`npm test`, `npm run test`, `pnpm test`, `yarn test`, `bun test`,
`make test`, `swift test`, `ctest`, `tox`, `nox`, `gradlew test`,
`mvn test`, `bats`, and these lint commands: `ruff`, `flake8`, `pylint`,
`mypy`, `pyright`, `eslint`, `biome`, `tsc`, `prettier --check`,
`cargo clippy`, `cargo fmt --check`, `golangci-lint`, `go vet`,
`shellcheck`, `make lint`, `npm run lint`, `pnpm lint`, `yarn lint`. A
command that is not on these lists is not seen. When the repo's command is
another one, give it a `make test` or `make lint` target, or report the gap
as one that stays.

### `root-map`

The root map is the first file an agent reads in the repo. It says what the
project is, where things are, and how to build, test, lint and install it.
The script reads one form only: a line that starts with the word, then a
colon, and then at once the command in a code span:

```markdown
## Commands

- Build: `cargo build --release`
- Test: `cargo test`
- Lint: `cargo clippy -- -D warnings`
- Install: `cargo install --path .`

## Rules

- Change a comment in the same commit as the code it describes.
```

- **One root map.** Do not write a `CLAUDE.md` into each folder. A map in
  each folder goes out of date with each change, and nobody reads it.
- Write only a command that you ran and saw work. Do not run an install
  command that writes outside the repo without the user's yes: leave the
  line out until the user has said yes and the command ran, and report the
  gap as one that waits for the user.
- When the repo has no command of one kind (no build step, for example),
  leave that line out and report the gap as one that stays.
- Nothing else counts. A sentence such as "Build output goes to `dist/`"
  names no command, and a command in a fenced block below a heading does
  not count. When a map names its commands in another way, add the four
  lines; do not rewrite the map.
- The file is `CLAUDE.md` at the top of the repo: the script reads
  `CLAUDE.md`, and `AGENTS.md` only when there is no `CLAUDE.md`. When the
  repo has a map already, add to it; do not write a second one.

### `review-file`

`REVIEW.md` at the top of the repo names the paths that are always tier 2
in this repo, with one line of reason each:

```markdown
# Review

Always tier 2 in this repo:

- `migrations/`: a migration cannot be taken back after it ran.
- `src/auth/`: sign-in and tokens.
- `.github/workflows/`: CI and release.
```

Propose the paths from what the repo holds (auth, billing, secrets, parsers,
migrations, data deletion, CI, release, installers). Which paths are on the
list is a call about risk: say in the pull request body that the list is
your proposal, and that the user decides.

### `comment-rule`

Add this sentence to the root map, word for word:

> Change a comment in the same commit as the code it describes.

### `rule-table`

`.claude/rules.json` holds the rules of the project that code holds up. Start
it empty:

```json
{"rules": []}
```

Only the user starts `/correct`, the skill that adds rows to it. Do not add a
row here.

### `verify-skill`

A verify skill runs the real app and keeps the evidence. Only the user starts
`/create-verify-skill`, which writes one. Report the gap and name that
command.

### The pull requests

- The workflow goes in a pull request of its own (tier 2, above).
- The four text gaps go in one pull request at tier 0: the root map, the
  comment rule, `REVIEW.md` and the rule table. Your own check is the script:
  run it again on the branch and put its lines into the pull request body.
- A repo with no workflow yet cannot merge the second one before the user
  has merged the first. Open both, and say so.

## 3. Look again

Run the script again when the work is done, and after each merge.

## Report

- each check, with its state before and after;
- each pull request that you opened, with its tier and who merges it;
- each gap that stays, and why: it waits for the user (the first workflow, a
  verify skill, the paths of `REVIEW.md`, the integration branch, an install
  command that writes outside the repo), or the repo has no such command;
- each `unknown`, and what you found by hand.

## Limits

- The script reads the workflow files as text, line by line. A test command
  in a step that never runs counts, and a command that the script does not
  know does not.
- It shows that a workflow names a test command, not that CI passes, and not
  that the branch is protected.
- It does not see an integration branch with another name.
- It reads the remote-tracking refs. Without a fresh fetch it can miss a
  branch, or see one that is gone. A clone of one branch never fetches
  `dev`, so the script shows a gap although GitHub has it; the command that
  makes `dev` is then refused.
- `root-map` is `ok` when each label has a code span right after its colon.
  The script cannot tell a command from a path in that span, and a map that
  names its commands in another form shows a gap.
- `verify-skill` is `ok` when the two files of a verify skill are there. The
  script does not run it.
- `rule-table` is `ok` for a table of the right outer form. The rows are
  checked by the script of `/correct`.
