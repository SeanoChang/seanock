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
         not-in-ci         the enforcer says it runs in CI, and no workflow names it
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
import subprocess
import sys
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

SECRETS = [re.compile(pattern) for pattern in (
    r"sk-[A-Za-z0-9_-]{16,}",                                   # API keys of several vendors
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"A[KS]IA[0-9A-Z]{16}",
    r"xox[abeprs]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY",
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}",  # a JSON web token
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/_.=-]{16,}",
    r"(?i)(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|credential)s?[\"']?\s*[:=]\s*[\"']?[^\s\"']{6,}",
    r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@",           # a URL with a name and a password
    r"\b[0-9a-fA-F]{32,}\b",                                    # a long hex value
)]
LONG_RUN = re.compile(r"[A-Za-z0-9+/_=-]{24,}")


def looks_secret(text):
    """True when any part of the text has the form of a secret.

    It errs to the safe side: a long path with capitals and digits, or a full
    commit id, is held back too.
    """
    if any(pattern.search(text) for pattern in SECRETS):
        return True
    for run in LONG_RUN.findall(text):
        if re.search(r"[a-z]", run) and re.search(r"[A-Z]", run) and re.search(r"[0-9]", run):
            return True
    return False


def shown(text):
    """One printable line of at most SHOWN characters, or None when it must be held back."""
    if looks_secret(text):
        return None
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return flat[:SHOWN]


# ── mine: the repo ───────────────────────────────────────────────────────────

REVERT = re.compile(r"Revert\b")
FIX = re.compile(r"(?i)^(?:fix|fixup!|hotfix|bugfix)\b|\bfix(?:es|ed)?\b")


def git_log(repo):
    """[(sha, unix time, subject, [paths])] of the newest commits. Git lists no paths for a merge."""
    try:
        done = subprocess.run(
            ["git", "--no-pager", "-C", str(repo), "log", "-n", str(MAX_COMMITS), "--name-only", "-z",
             "--format=%x1e%H%x1f%ct%x1f%s%x1f"],
            capture_output=True, text=True, timeout=120, errors="replace",
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C"))
    except FileNotFoundError:
        raise Failed("git is not installed")
    except subprocess.TimeoutExpired:
        raise Failed("git log gave no answer in 120 seconds")
    if done.returncode != 0:
        reason = (done.stderr.strip().splitlines() or ["no message"])[-1]
        if "does not have any commits" in reason:
            return []
        raise Failed(f"git log failed: {reason}")
    commits = []
    for block in done.stdout.split("\x1e")[1:]:
        parts = block.split("\x1f")
        if len(parts) != 4 or not parts[1].isdigit():
            raise Failed("git log gave a shape this script does not know")
        paths = [path for path in parts[3].strip("\n\x00").split("\x00") if path.strip("\n")]
        commits.append((parts[0], int(parts[1]), parts[2], [path.lstrip("\n") for path in paths]))
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
    """The text as it is compared: lower case, letters and digits only, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9']+", " ", text.lower()).split())


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
                    raise Failed(f"a workflow file cannot be read: {path.name}")
    return texts


def named_in(texts, rel):
    """True when a workflow names the path, or a folder above it, as a whole word."""
    parts = rel.split("/")
    for depth in range(len(parts), 0, -1):
        name = "/".join(parts[:depth])
        pattern = re.compile(r"(?<![\w./-])" + re.escape(name) + r"/?(?![\w.-]|/\w)")
        if any(pattern.search(text) for text in texts):
            return True
    return False


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
