#!/usr/bin/env python3
"""rules: find repeated corrections, and keep the table of rules that answer them.

  rules.py mine  --repo <dir> [--history <file> | --no-history] [--project <dir> ...]
  rules.py check --repo <dir>
  rules.py table --repo <dir>

mine   lists candidates for a rule, one JSON object per line:
         revert       a commit that reverts another
         fix-cluster  a file that fix commits touched two times or more
         history      a line of your prompt history for this project that reads
                      like a correction, with how often you typed it
       It reads git with `git log` only, and of the history only the text you
       typed (never the pasted content). A line in which anything looks like a
       secret is held back whole: it is counted, and its text is never printed.
check  reads <repo>/.claude/rules.json and lists each rule that nothing holds up:
         missing-enforcer  the file that enforces the rule is not there
         docs-only         the rule lives in text only, and no reason is given
         not-in-ci         the enforcer says it runs in CI, and no `run:` command of a
                           workflow names it
         table-stale       .claude/rules.md is not what `table` writes
table  writes <repo>/.claude/rules.md, the table that a person reads.

Read the exit code and the last line together:

  exit 0   "rules: clean"                 nothing found   (table: "rules: wrote .claude/rules.md")
  exit 1   "rules: summary ..."           candidates or gaps, one line each above
  exit 2   "rules: error ..."             it could not finish; this says nothing about the rules

The rule table, .claude/rules.json:

  {"rules": [{"id": "no-ai-attribution",
              "rule": "Commits and PR bodies carry no AI attribution.",
              "layer": "config",
              "enforcer": {"path": "tests/test_attribution.py", "runs_in": "ci"},
              "evidence": ["commit 1a2b3c4", "history 2026-11-14"]}]}

  layer     where the rule is held, strongest first:
            architecture, types, config, lint, test, hook, docs
  enforcer  the file that fails when the rule is broken, and where it runs:
            "ci", "hook", or "none" (a docs rule: the path is the text that states it)
  accepted  only for a docs rule: why no code can check it. With it, the rule is no gap.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
from pathlib import Path

LAYERS = ("architecture", "types", "config", "lint", "test", "hook", "docs")
RUNS_IN = ("ci", "hook", "none")
ROW_KEYS = {"id", "rule", "layer", "enforcer", "evidence", "accepted"}
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,59}\Z")
HISTORY = Path.home() / ".claude" / "history.jsonl"
MAX_LINE = 400       # a longer history line is a paste or a brief, not a correction
SHOWN = 160          # characters of a line that are printed
MAX_HISTORY = 200    # history candidates printed; the rest are counted
MAX_CLUSTERS = 30
MAX_COMMITS = 2000


class Failed(Exception):
    """The script could not finish."""


# ── secrets: a line with any of these is held back whole ─────────────────────

# No rule asks what stands next to the thing it looks for. A word boundary
# (\b) is not there when a secret touches a letter, a digit or "_", and a key
# word is often part of a longer name (SECRET_KEY, --db-password). A rule that
# asks for a neighbour does not see those.
HEX = "0-9a-fA-F"   # a long hex value needs no rule of its own: it is a run of letters and digits (PLAIN_RUN)
# A line with a key word is held back, whatever stands next to the word and
# also when it holds no secret: "my password is x" has no mark that a rule
# could ask for. Three rules that asked for one (a `:` or `=` that touches
# the word, one anywhere after it, a dash before it) each let a line pass.
# The short forms count too, also inside a longer word: `pass` (DB_PASS=x,
# --pass x), `pw`, `cred`. So "make the tests pass" is held back as well. A
# list that left `pass` out printed lines that the rule before held back.
WORD = r"(?:pass|pw|secret|token|api[\s_-]?key|access[\s_-]?key|private[\s_-]?key|auth|cred|bearer)"
SECRETS = [re.compile(pattern) for pattern in (
    r"sk-[A-Za-z0-9_-]{16,}",                                   # API keys of several vendors
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"A[KS]IA[0-9A-Z]{16}",
    r"xox[abeprs]-[A-Za-z0-9-]{10,}",
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}",  # a JSON web token
    r"(?i)basic[\s\\\"',]+[A-Za-z0-9+/_.=-]{16,}",
    r"(?i)" + WORD,
    r"://[^\s/@]{0,4096}:[^\s@]{1,4096}@",                         # a URL with a password, with or without a user
    rf"[{HEX}]{{8}}-[{HEX}]{{4}}-[{HEX}]{{4}}-[{HEX}]{{4}}-[{HEX}]{{12}}",   # a UUID
)]
LONG_RUN = re.compile(r"[A-Za-z0-9+/_=-]{24,}")     # held back with a small letter, a capital and a digit
PLAIN_RUN = re.compile(r"[A-Za-z0-9]{20,}")         # held back with a letter and a digit
WHITE = re.compile(r"\s+")


def closed(text):
    """The text with each run of white space that holds a line break taken out.

    A secret that was copied from a wrapped line has a break in it. One pass
    over the runs: a pattern that looks for a break inside white space backs
    up on a long run of spaces.
    """
    return WHITE.sub(lambda run: "" if "\n" in run.group() or "\r" in run.group() else run.group(), text)


def visible(text):
    """The text with full-width forms made plain, and without the characters that do not show."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if ch.isprintable() or ch.isspace())


def has_secret_form(text):
    if any(pattern.search(text) for pattern in SECRETS):
        return True
    for run in LONG_RUN.findall(text):
        if re.search(r"[a-z]", run) and re.search(r"[A-Z]", run) and re.search(r"[0-9]", run):
            return True
    for run in PLAIN_RUN.findall(text):
        if re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run):
            return True
    return False


def looks_secret(text):
    """True when any part of the text has the form of a secret.

    A line with a character that does not show (a control character, a
    zero-width character, a line separator) is held back whole: such a
    character can split a secret or join it to a word, and every way to print
    the line would guess. Tab, LF, CR and the space characters of other
    scripts (a no-break space, the wide space of Chinese text) pass, and
    count as spaces. The rest is looked at as
    typed and with each line break closed (a secret copied from a wrapped
    line), each of the two also with its spaces joined, and as `shown` prints
    it. So no line is printed in a form that was not checked. It errs to the
    safe side: a full commit id, a UUID, or a long path with capitals and
    digits is held back too.
    """
    if any(not ch.isprintable() and ch not in "\t\n\r" and unicodedata.category(ch) != "Zs" for ch in text):
        return True
    seen = visible(text)
    joined = closed(seen)
    return any(has_secret_form(form) for form in (seen, joined, printed(seen), printed(joined), printed(text)))


def printed(text):
    """The text as one line: each character that does not print is a space, and spaces are joined."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def shown(text):
    """One printable line of at most SHOWN characters, or None when it must be held back."""
    if looks_secret(text):
        return None
    return printed(text)[:SHOWN]


# ── mine: the repo ───────────────────────────────────────────────────────────

REVERT = re.compile(r"Revert\b")
FIX = re.compile(r"(?i)^(?:fix|fixup!|hotfix|bugfix)\b|\bfix(?:es|ed)?\b")


def git_log(repo):
    """[(sha, unix time, subject, [paths])] of the newest commits. Git lists no paths for a merge.

    No signature is checked and the GIT_ variables of the caller are dropped,
    so that nothing but the format below reaches stdout. Each commit starts
    with a mark that is made for this run, and the fields end with a NUL: a
    commit subject can hold neither, so it cannot stand as a commit.
    """
    mark = secrets.token_hex(16)
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        done = subprocess.run(
            ["git", "--no-pager", "-C", str(repo), "log", "-n", str(MAX_COMMITS), "--no-show-signature",
             "--name-only", "-z", f"--format={mark}%H%x00%ct%x00%s%x00"],
            capture_output=True, text=True, timeout=120, errors="replace", env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Failed("git is not installed")
    except subprocess.TimeoutExpired:
        raise Failed("git log gave no answer in 120 seconds")
    if done.returncode != 0:
        reason = (done.stderr.strip().splitlines() or ["no message"])[-1]
        if "does not have any commits" in reason:
            return []
        raise Failed("git log failed" + (": not a git repository" if "not a git repository" in reason else ""))
    blocks = done.stdout.split(mark)
    if blocks[0] != "":
        raise Failed("git log gave a shape this script does not know")
    commits = []
    for block in blocks[1:]:
        parts = block.split("\x00")
        # sha, time, subject, the end of the format, then "\n" and the paths, each with a NUL after it
        if len(parts) < 5 or not parts[1].isdigit() or parts[3] != "" or parts[-1] != "":
            raise Failed("git log gave a shape this script does not know")
        paths = parts[4:-1]
        if paths:
            if not paths[0].startswith("\n"):
                raise Failed("git log gave a shape this script does not know")
            paths[0] = paths[0][1:]
        if any(path == "" for path in paths):
            raise Failed("git log gave a shape this script does not know")
        commits.append((parts[0], int(parts[1]), parts[2], paths))
    return commits


def mine_repo(commits):
    """(candidates, how many were cut) from the commits: reverts, and files fixed two times or more."""
    found, fixed = [], {}
    for sha, _when, subject, paths in commits:
        if REVERT.match(subject):
            found.append({"kind": "revert", "sha": sha[:12], "subject": shown(subject)})
        elif FIX.search(subject):
            for path in paths:
                fixed.setdefault(path, []).append(sha[:12])
    clusters = sorted(((path, shas) for path, shas in fixed.items() if len(shas) >= 2),
                      key=lambda item: (-len(item[1]), item[0]))
    for path, shas in clusters[:MAX_CLUSTERS]:
        found.append({"kind": "fix-cluster", "path": shown(path), "count": len(shas), "commits": shas[:10]})
    return found, max(0, len(clusters) - MAX_CLUSTERS)


# ── mine: the prompt history ─────────────────────────────────────────────────

CORRECTION = re.compile(
    r"(?i)(?<![a-z])(?:don'?t|do not|never|always|stop|again|i said|i told you|not what i|shouldn'?t|should not"
    r"|must not|wrong|instead of|why did you|you forgot|remember to|from now on|no need to|i already)(?![a-z])")


def normal(text):
    """The text as it is compared: no case, the letters and digits of any script, single spaces."""
    return " ".join("".join(ch if ch.isalnum() or ch == "'" else " " for ch in visible(text).casefold()).split())


def in_project(project, roots):
    """True when the history line was typed in one of the folders, or below one."""
    if not isinstance(project, str):
        return False
    return any(project == root or project.startswith(root.rstrip("/") + "/") for root in roots)


def mine_history(path, roots):
    """(candidates, numbers of the lines held back, how many were too long, how many were cut).

    Only the field `display` is read: the text that was typed. The pasted
    content of a prompt is never read.
    """
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        raise Failed(f"the history file cannot be read: {path}")
    groups, held, long_ones = {}, [], 0
    with handle:
        for number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not in_project(row.get("project"), roots):
                continue
            text = row.get("display")
            if not isinstance(text, str) or text.startswith("/") or len(text.strip()) < 8:
                continue
            if looks_secret(text):
                held.append(number)
                continue
            if len(text) > MAX_LINE:
                long_ones += 1
                continue
            if not CORRECTION.search(text):
                continue
            stamp = row.get("timestamp")
            day = (datetime.datetime.fromtimestamp(stamp / 1000, datetime.timezone.utc).date().isoformat()
                   if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 < stamp < 1e14 else None)
            group = groups.setdefault(normal(text), {"text": shown(text), "count": 0, "days": []})
            group["count"] += 1
            if day:
                group["days"].append(day)
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["text"]))
    found = [{"kind": "history", "text": g["text"], "count": g["count"],
              "first": min(g["days"]) if g["days"] else None, "last": max(g["days"]) if g["days"] else None}
             for g in ranked[:MAX_HISTORY]]
    return found, held, long_ones, max(0, len(ranked) - MAX_HISTORY)


def mine(repo, history, roots):
    found, cut_clusters = mine_repo(git_log(repo))
    held, long_ones, cut_history = [], 0, 0
    if history is not None:
        more, held, long_ones, cut_history = mine_history(history, roots)
        found += more
    for item in found:
        emit(item)
    if held:
        emit({"kind": "held-back", "count": len(held), "lines": held[:50]})
    marks = "".join(f" {name}={value}" for name, value in (
        ("held_back", len(held)), ("too_long", long_ones), ("not_shown", cut_clusters + cut_history)) if value)
    say(f"rules: summary candidates={len(found)}{marks}" if found or held else "rules: clean")
    return 1 if found or held else 0


# ── the rule table ───────────────────────────────────────────────────────────

def inside(repo, rel):
    """The path below the repo that `rel` names, or None when it leaves the repo or is not a plain relative path."""
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "~")) or "\\" in rel or "\x00" in rel:
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    path = repo.joinpath(*parts)
    try:
        path.resolve().relative_to(repo.resolve())
    except ValueError:
        return None
    return path


def need(ok, message):
    if not ok:
        raise Failed(f"rules.json: {message}")


def load_table(repo):
    """The rows of .claude/rules.json, checked. Raises Failed on anything this script does not know."""
    path = repo / ".claude" / "rules.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Failed("no .claude/rules.json in this repo")
    except (OSError, ValueError):
        raise Failed("rules.json cannot be read as JSON")
    need(isinstance(data, dict) and set(data) == {"rules"} and isinstance(data["rules"], list),
         'the file is {"rules": [...]} and nothing else')
    seen = set()
    for number, row in enumerate(data["rules"], 1):
        where = f"rule {number}"
        need(isinstance(row, dict), f"{where} is not an object")
        extra = sorted(set(row) - ROW_KEYS)
        need(not extra, f"{where} has a key this script does not know: {extra[0]!r}" if extra else "")
        need({"id", "rule", "layer", "enforcer", "evidence"} <= set(row),
             f"{where} needs id, rule, layer, enforcer and evidence")
        need(isinstance(row["id"], str) and ID.match(row["id"]) is not None,
             f"{where}: an id is lower-case letters, digits and -, at most 60")
        where = f"rule {row['id']!r}"
        need(row["id"] not in seen, f"{where} is there two times")
        seen.add(row["id"])
        need(isinstance(row["rule"], str) and 0 < len(row["rule"].strip()) <= 300 and "\n" not in row["rule"],
             f"{where}: 'rule' is one line of text, at most 300 characters")
        need(row["layer"] in LAYERS, f"{where}: 'layer' is one of {', '.join(LAYERS)}")
        enforcer = row["enforcer"]
        need(isinstance(enforcer, dict) and set(enforcer) == {"path", "runs_in"},
             f"{where}: 'enforcer' is {{\"path\": ..., \"runs_in\": ...}}")
        need(inside(repo, enforcer["path"]) is not None, f"{where}: the enforcer path is a relative path inside the repo")
        need(enforcer["runs_in"] in RUNS_IN, f"{where}: 'runs_in' is one of {', '.join(RUNS_IN)}")
        need((enforcer["runs_in"] == "none") == (row["layer"] == "docs"),
             f"{where}: 'runs_in' is \"none\" for a docs rule, and only for a docs rule")
        need(isinstance(row["evidence"], list) and row["evidence"]
             and all(isinstance(item, str) and 0 < len(item.strip()) <= 200 and "\n" not in item for item in row["evidence"]),
             f"{where}: 'evidence' is a list of one or more short lines")
        if "accepted" in row:
            need(row["layer"] == "docs", f"{where}: 'accepted' is for a docs rule only")
            need(isinstance(row["accepted"], str) and 0 < len(row["accepted"].strip()) <= 300 and "\n" not in row["accepted"],
                 f"{where}: 'accepted' is one line that says why no code can check the rule")
    return data["rules"]


def workflow_texts(repo):
    folder = repo / ".github" / "workflows"
    texts = []
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.suffix in (".yml", ".yaml") and path.is_file():
                try:
                    texts.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    raise Failed("a workflow file cannot be read")
    return texts


RUN_KEY = re.compile(r"^(\s*(?:-\s+)?)run:(?:\s+(.*))?$")
BLOCK = re.compile(r"[|>][+-]?[1-9]?[+-]?\Z")
WORD_EDGE = re.compile(r"[\s\"'`;&|()<>]+")        # what stands between two words of a command line


def without_comment(line):
    """The line up to a `#` that starts it or follows a space."""
    return re.sub(r"(?:^|\s)#.*$", "", line)


def commands(text):
    """The command lines of the `run:` steps of a workflow file, read as text.

    Only these lines can name an enforcer. A job name, a step name or an
    input of an action cannot: "Run the tests" runs nothing. A `run:` with
    nothing after it is not a step: it is a key with keys below it, as in the
    `defaults` of a workflow.
    """
    found, lines, at = [], text.splitlines(), 0
    while at < len(lines):
        key = RUN_KEY.match(lines[at])
        at += 1
        if not key:
            continue
        column = len(key.group(1))            # where `run:` starts; the lines of its block start further right
        value = without_comment(key.group(2) or "").strip()
        if not value:
            continue
        if not BLOCK.match(value):
            found.append(value)
            continue
        while at < len(lines):
            line = lines[at]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= column:
                break
            found.append(without_comment(line))
            at += 1
    return found


def named_in(texts, rel):
    """True when a word of a workflow command is the path, or a folder above it, written as a path.

    A word is what stands between spaces, quotes and the marks of the shell.
    It is a path only with a slash in it: `tests/`, `./tests`, `config/hooks`.
    A bare `tests` is a word like any other (`npm test`, `echo "no tests"`),
    so it does not count, and neither does a path inside a longer word
    (`--ignore=tests/`, `tests/*.py`).
    """
    spellings, parts = set(), rel.split("/")
    for depth in range(1, len(parts) + 1):
        name = "/".join(parts[:depth])
        spellings.update({name + "/", "./" + name, "./" + name + "/"})
        if depth > 1:
            spellings.add(name)
    return any(word in spellings for text in texts for line in commands(text) for word in WORD_EDGE.split(line))


def cell(text):
    return " ".join(str(text).split()).replace("|", "\\|")


def table_md(rows):
    """The table that a person reads: strongest layer first."""
    lines = ["# Rules", "",
             "Written by `rules.py table` from `.claude/rules.json`. Change that file, not this one.", "",
             "| Rule | Layer | Held up by | Runs in | Evidence |", "| --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda row: (LAYERS.index(row["layer"]), row["id"])):
        held = f"`{cell(row['enforcer']['path'])}`"
        if "accepted" in row:
            held += f" (text only: {cell(row['accepted'])})"
        lines.append(f"| **{row['id']}**: {cell(row['rule'])} | {row['layer']} | {held} "
                     f"| {row['enforcer']['runs_in']} | {cell('; '.join(row['evidence']))} |")
    return "\n".join(lines) + "\n"


def gaps(rows, repo):
    """[(kind, id, detail)] for each rule that nothing holds up, and for a table that is out of date."""
    found, texts = [], workflow_texts(repo)
    for row in rows:
        path, runs_in = row["enforcer"]["path"], row["enforcer"]["runs_in"]
        target = inside(repo, path)
        if target is None or not target.is_file():
            found.append(("missing-enforcer", row["id"], path))
        elif runs_in == "ci" and not named_in(texts, path):
            found.append(("not-in-ci", row["id"], path))
        if row["layer"] == "docs" and "accepted" not in row:
            found.append(("docs-only", row["id"], path))
    try:
        current = (repo / ".claude" / "rules.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except OSError:
        raise Failed(".claude/rules.md cannot be read")
    if current != table_md(rows):
        found.append(("table-stale", "-", ".claude/rules.md"))
    return found


def check(repo):
    found = gaps(load_table(repo), repo)
    for kind, rule, detail in found:
        say(f"{kind} {rule} {detail}")
    say(f"rules: summary gaps={len(found)}" if found else "rules: clean")
    return 1 if found else 0


def write_table(repo):
    rows = load_table(repo)
    target = repo / ".claude" / "rules.md"
    partial = target.with_name(target.name + ".part")
    try:
        os.unlink(partial)
    except FileNotFoundError:
        pass
    with open(partial, "x", encoding="utf-8") as handle:  # never through a link
        handle.write(table_md(rows))
    os.replace(partial, target)
    say("rules: wrote .claude/rules.md")
    return 0


# ── the command line ─────────────────────────────────────────────────────────

def say(text):
    """Print one line. A name from a file cannot start a new line and so cannot stand as the last line."""
    print("".join(ch if ch.isprintable() else " " for ch in str(text)), flush=True)


def emit(item):
    say(json.dumps(item, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("mine", "check", "table"))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--history", type=Path, default=HISTORY)
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--project", action="append", default=[], type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.repo.is_dir():
            raise Failed(f"no such folder: {args.repo}")
        repo = args.repo.resolve()
        if args.command == "mine":
            roots = [str(repo)] + [str(path.resolve()) for path in args.project]
            return mine(repo, None if args.no_history else args.history, roots)
        if args.command == "check":
            return check(repo)
        return write_table(repo)
    except Failed as exc:
        say(f"rules: error {exc}")
        return 2
    except Exception as exc:  # anything else: it did not finish, and that must not read as "nothing found"
        say(f"rules: error {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
