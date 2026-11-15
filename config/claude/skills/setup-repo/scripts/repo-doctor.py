#!/usr/bin/env python3
"""repo-doctor: list what a repo lacks for the harness's way of working.

  repo-doctor.py --repo <dir>

It only reads. It runs no network call and no `gh`, and it calls git only
with `rev-parse` and `for-each-ref`, with a timeout, locks off and fsmonitor
off. It never writes a file.

It prints one line per check, in the order below: `<state> <id> <detail>`,
with state `ok`, `gap` or `unknown`. Read the exit code and the last line
together:

  exit 0   "repo-doctor: clean"                      every check is ok
  exit 1   "repo-doctor: summary gaps=N unknown=M"   at least one check is a gap or unknown
                                                     (unknown is never a pass)
  exit 2   "repo-doctor: error ..."                  it could not finish; this says nothing about the repo

The checks:

  integration-branch  origin/dev, origin/develop, origin/development or
                      origin/integration is there (the first in that order is
                      named). Unknown when the folder is not the top of a git
                      repo, has no refs under origin, or git cannot answer.
  ci-tests            a line of .github/workflows/*.yml or *.yaml runs a test
                      command (TEST_COMMANDS). A comment line does not count.
  ci-lint             the same for a lint or type command (LINT_COMMANDS).
  root-map            the root map (CLAUDE.md, else AGENTS.md) names a command
                      for each of build, test, lint and install: a line that
                      starts with that word (after markdown marks) and has a
                      code span, or is followed within 3 lines by a fenced block.
  review-file         REVIEW.md is a file with content.
  comment-rule        the root map holds COMMENT_RULE (whitespace collapsed,
                      case ignored).
  rule-table          .claude/rules.json is an object whose only key is
                      "rules", with a list value.
  verify-skill        a folder .claude/skills/verify-<name>/ holds a file
                      `verify` and a file `features.json`.

A command matches as a whole word: the characters next to it are not
letters, digits, `_` or `-`. So `mypytest` and `pytest-cov` are no test command.

Files are read as UTF-8 with errors replaced, at most 1 MiB each. A path that
resolves outside the repo (through a symlink) counts as absent. A file that
is there but cannot be read makes its check unknown.

No text from the repo is printed: no file name, no folder name, no branch
name beyond the four fixed ones, no key and no content. Each line is built
from fixed words and counts, so a name cannot read as a result or as an
instruction to the reader. Every printed line also has its non-printable
characters replaced by spaces.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

CHECKS = ["integration-branch", "ci-tests", "ci-lint", "root-map", "review-file", "comment-rule",
          "rule-table", "verify-skill"]
INTEGRATION = ["dev", "develop", "development", "integration"]  # the order the merge rule picks in
TEST_COMMANDS = ["pytest", "go test", "cargo test", "npm test", "npm run test", "pnpm test", "yarn test",
                 "bun test", "make test", "swift test", "ctest", "tox", "nox", "gradlew test", "mvn test",
                 "bats"]
LINT_COMMANDS = ["ruff", "flake8", "pylint", "mypy", "pyright", "eslint", "biome", "tsc", "prettier --check",
                 "cargo clippy", "cargo fmt --check", "golangci-lint", "go vet", "shellcheck", "make lint",
                 "npm run lint", "pnpm lint", "yarn lint"]
MAP_WORDS = ["build", "test", "lint", "install"]
COMMENT_RULE = "Change a comment in the same commit as the code it describes."
MAX_BYTES = 1 << 20
GIT_TIMEOUT = 30

Result = namedtuple("Result", "id state detail")


class Failed(Exception):
    """The script could not finish."""


class Unreadable(Exception):
    """A file is there but cannot be read; its check is unknown."""


# ── output ───────────────────────────────────────────────────────────────────

def clean(text):
    """The text with each non-printable character (a line break among them) replaced by a space."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text))


def say(text):
    print(clean(text), flush=True)


# ── files, never outside the repo ────────────────────────────────────────────

def inside(root, rel):
    """The real path of root/rel when it resolves inside root, else None."""
    real = os.path.realpath(os.path.join(root, rel))
    return real if real == root or real.startswith(root + os.sep) else None


def regular_file(root, rel, label):
    """The real path of a regular file at root/rel inside the repo, else None.

    `label` is what an error calls the file: fixed words, never a name from the repo."""
    real = inside(root, rel)
    if real is None:
        return None
    try:
        return real if stat.S_ISREG(os.stat(real).st_mode) else None
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Unreadable(f"cannot read {label}: {exc.strerror}")


def read_text(root, rel, label):
    """(text, truncated) of the file at root/rel, or None when it is absent or outside the repo."""
    real = regular_file(root, rel, label)
    if real is None:
        return None
    try:
        with open(real, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise Unreadable(f"cannot read {label}: {exc.strerror}")
    return data[:MAX_BYTES].decode("utf-8", errors="replace"), len(data) > MAX_BYTES


def list_dir(root, rel):
    """The sorted names in the folder root/rel, or [] when it is absent or outside the repo.

    `rel` is a fixed path of this script, so an error may name it."""
    real = inside(root, rel)
    if real is None or not os.path.isdir(real):
        return []
    try:
        return sorted(os.listdir(real))
    except OSError as exc:
        raise Unreadable(f"cannot list {rel}: {exc.strerror}")


# ── git, read-only ───────────────────────────────────────────────────────────

def git(root, *args):
    """Stdout of a read-only git call, or None when git cannot answer."""
    if args[0] not in ("rev-parse", "for-each-ref"):
        raise Failed(f"refused to run git {args[0]}: this script only reads")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", root, *args], capture_output=True,
                              env=env, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or done.stderr.strip():
        return None
    return done.stdout.decode("utf-8", errors="replace")


def check_integration(root):
    top = git(root, "rev-parse", "--show-toplevel")
    if top is None or os.path.realpath(top.rstrip("\n")) != root:
        return "unknown", "no git repo at the top of this folder, or git cannot answer"
    out = git(root, "for-each-ref", "--format=%(refname)%00", "refs/remotes/origin/")
    if out is None:
        return "unknown", "git cannot list the remote-tracking refs"
    names = {line.strip("\n") for line in out.split("\0")} - {""}
    if not names:
        return "unknown", "no remote-tracking refs under origin"
    for name in INTEGRATION:
        if f"refs/remotes/origin/{name}" in names:
            return "ok", f"origin/{name}"
    return "gap", "no origin/dev, origin/develop, origin/development or origin/integration"


# ── workflows ────────────────────────────────────────────────────────────────

def command_regex(command):
    words = [re.escape(word) for word in command.split()]
    return re.compile(r"(?<![\w-])" + r"\s+".join(words) + r"(?![\w-])")


TEST_RES = [(command, command_regex(command)) for command in TEST_COMMANDS]
LINT_RES = [(command, command_regex(command)) for command in LINT_COMMANDS]


def match_command(line, regexes):
    """The first command of `regexes` that `line` runs as a whole word, else None. A comment line runs none."""
    if line.lstrip().startswith("#"):
        return None
    for command, regex in regexes:
        if regex.search(line):
            return command
    return None


def workflows(root):
    """The text of each workflow file inside the repo."""
    found = []
    for name in list_dir(root, ".github/workflows"):
        if name.endswith((".yml", ".yaml")):
            read = read_text(root, f".github/workflows/{name}", "a workflow file")
            if read is not None:
                found.append(read[0])
    return found


def check_ci(root, regexes, kind):
    files = workflows(root)
    if not files:
        return "gap", "no workflow"
    first, running = None, 0
    for text in files:
        commands = [match_command(line, regexes) for line in text.splitlines()]
        commands = [command for command in commands if command]
        if commands:
            first = first or commands[0]
            running += 1
    if first:
        return "ok", f"{first} in {running} of {len(files)} workflow file(s)"
    return "gap", f"no {kind} command in {len(files)} workflow file(s)"


# ── the root map ─────────────────────────────────────────────────────────────

MARKS = re.compile(r"^(?:[\s>*+|#_-]|\d+[.)](?=\s))*")
WORD = re.compile(r"(build|test|lint|install)(?![\w-])", re.IGNORECASE)
SPAN = re.compile(r"`[^`]+`")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def root_map(root):
    """(name, text) of CLAUDE.md, else AGENTS.md, at the repo root; None when neither is there."""
    for name in ("CLAUDE.md", "AGENTS.md"):
        read = read_text(root, name, name)
        if read is not None:
            return name, read[0]
    return None


def named_commands(text):
    """The words of MAP_WORDS that the map names a command for, in MAP_WORDS order."""
    lines = text.splitlines()
    fenced = []
    in_fence = False
    for line in lines:
        opens = FENCE.match(line) is not None
        fenced.append(in_fence or opens)
        if opens:
            in_fence = not in_fence
    found = set()
    for index, line in enumerate(lines):
        if fenced[index]:
            continue
        word = WORD.match(MARKS.sub("", line))
        if not word:
            continue
        nearby = any(FENCE.match(lines[i]) for i in range(index + 1, min(index + 4, len(lines))))
        if SPAN.search(line) or nearby:
            found.add(word.group(1).lower())
    return [word for word in MAP_WORDS if word in found]


def check_root_map(found):
    if found is None:
        return "gap", "no CLAUDE.md or AGENTS.md"
    name, text = found
    named = named_commands(text)
    missing = [word for word in MAP_WORDS if word not in named]
    if missing:
        return "gap", f"{name} missing: {', '.join(missing)}"
    return "ok", f"{name} names build, test, lint, install"


def collapse(text):
    return " ".join(text.split()).casefold()


def check_comment_rule(found):
    if found is None:
        return "gap", "no CLAUDE.md or AGENTS.md"
    name, text = found
    if collapse(COMMENT_RULE) in collapse(text):
        return "ok", f"{name} holds the comment rule"
    return "gap", f"{name} lacks: {COMMENT_RULE}"


# ── REVIEW.md, the rule table, the verify skill ──────────────────────────────

def check_review(root):
    read = read_text(root, "REVIEW.md", "REVIEW.md")
    if read is None:
        return "gap", "no REVIEW.md"
    if not read[0].strip():
        return "gap", "REVIEW.md is empty"
    return "ok", "REVIEW.md"


def check_rules(root):
    read = read_text(root, ".claude/rules.json", ".claude/rules.json")
    if read is None:
        return "gap", "no .claude/rules.json"
    text, truncated = read
    if truncated:
        return "gap", ".claude/rules.json is larger than 1 MiB"
    try:
        data = json.loads(text)
    except ValueError:
        return "gap", ".claude/rules.json is not valid JSON"
    if not isinstance(data, dict):
        return "gap", ".claude/rules.json is not a JSON object"
    if set(data) != {"rules"}:
        other = len(set(data) - {"rules"})
        return "gap", (f".claude/rules.json has {other} key(s) other than rules" if other
                       else ".claude/rules.json has no rules key")
    if not isinstance(data["rules"], list):
        return "gap", ".claude/rules.json: rules is not a list"
    return "ok", f"{len(data['rules'])} rule(s)"


def check_verify(root):
    found = 0
    for name in list_dir(root, ".claude/skills"):
        if not name.startswith("verify-") or name == "verify-":
            continue
        folder = f".claude/skills/{name}"
        if (regular_file(root, f"{folder}/verify", "the runner of a verify skill")
                and regular_file(root, f"{folder}/features.json", "the map of a verify skill")):
            found += 1
    if not found:
        return "gap", "no .claude/skills/verify-<name>/ with verify and features.json"
    return "ok", f"{found} verify skill(s)"


# ── the run ──────────────────────────────────────────────────────────────────

def guarded(check, *args):
    """(state, detail) of a check; a file that is there but cannot be read makes it unknown."""
    try:
        return check(*args)
    except Unreadable as exc:
        return "unknown", str(exc)


def check_repo(repo):
    """One Result per id of CHECKS, in that order."""
    root = os.path.realpath(repo)
    if not os.path.isdir(root):
        raise Failed(f"no such folder: {repo}")
    try:
        found_map = root_map(root)
        map_state = None
    except Unreadable as exc:
        found_map, map_state = None, ("unknown", str(exc))
    answers = {
        "integration-branch": guarded(check_integration, root),
        "ci-tests": guarded(check_ci, root, TEST_RES, "test"),
        "ci-lint": guarded(check_ci, root, LINT_RES, "lint"),
        "root-map": map_state or check_root_map(found_map),
        "review-file": guarded(check_review, root),
        "comment-rule": map_state or check_comment_rule(found_map),
        "rule-table": guarded(check_rules, root),
        "verify-skill": guarded(check_verify, root),
    }
    return [Result(ident, *answers[ident]) for ident in CHECKS]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        results = check_repo(args.repo)
        if [r.id for r in results] != CHECKS or any(r.state not in ("ok", "gap", "unknown") for r in results):
            raise Failed("a check gave no answer")
        for result in results:
            say(f"{result.state} {result.id} {result.detail}")
        gaps = sum(result.state == "gap" for result in results)
        unknown = sum(result.state == "unknown" for result in results)
        if gaps or unknown:
            say(f"repo-doctor: summary gaps={gaps} unknown={unknown}")
            return 1
        say("repo-doctor: clean")
        return 0
    except Failed as exc:
        say(f"repo-doctor: error {exc}")
        return 2
    except Exception as exc:  # anything else: it did not finish, and that must not read as clean
        say(f"repo-doctor: error {type(exc).__name__}")  # its text can hold a path of the repo
        return 2


if __name__ == "__main__":
    sys.exit(main())
