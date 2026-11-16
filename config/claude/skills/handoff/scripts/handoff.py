#!/usr/bin/env python3
"""handoff: write the frame of a resume note for this work tree, and check a note.

  handoff.py frame [--repo DIR]   write .claude/handoff.md: the facts from git, and the empty parts
  handoff.py check [--repo DIR]   list what the note lacks, and where the tree is no longer as it says

The note is for a session that starts with nothing: it says what the goal is,
where the work stands and what to do first. `frame` writes what git knows (the
branch, the commit, the files that are not committed) and the headings. The
agent writes the rest. `check` reads the note and the tree again.

Read the exit code and the last line together, never one without the other:

  frame  exit 0   "handoff: wrote .claude/handoff.md"
  check  exit 0   "handoff: ok"                  the note is whole, and the tree is as it says
         exit 1   "handoff: summary gaps=N"      with one line for each gap above it
  both   exit 2   "handoff: error ..."           the script could not finish; this is not a result

The gap lines of check:

  stale-head note=<commit> now=<commit>    the tree is at another commit than the note
  stale-branch note=<name> now=<name>      the tree is on another branch
  stale-changes note=<n> now=<n>           other files are changed and not committed
  empty-section <name>                     a part is missing or holds no text (tldr is the summary on top)
  no-next-action                           "Next actions" has no open checkbox
  no-command                               the first open checkbox names no command (no code span, no code block)
  missing-path <path>                      the note names a path of this tree that is not there
  missing-path-more <n>                    that many more such paths; the count has them all

The note lives in the work tree and git does not see it: frame adds its two
names to the repo's exclude file when no rule ignores them yet. A note with
text in it is kept one time, as .claude/handoff.prev.md, when frame writes a
new one. The script only calls git and reads and writes these files. It runs
nothing of the project and uses no network.
"""
import argparse
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

NOTE = ".claude/handoff.md"
PREV = ".claude/handoff.prev.md"
SECTIONS = ("Next actions", "State now", "Environment", "Decisions, and why", "Rejected approaches", "Open questions")
SKELETON = "\n> [!tldr]\n>\n" + "".join(f"\n## {name}\n" for name in SECTIONS)
MAX_NOTE_BYTES = 1 << 20
MAX_CHANGED = 50     # entries of the fact block; `dirty` always holds the full count
MAX_MISSING = 20     # missing-path lines that check prints; the summary counts them all
GIT_TIMEOUT_S = 60

COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)\Z")
BOX = re.compile(r"\s*(?:[-*+]|[0-9]{1,9}[.)])\s+\[([ xX])\]\s+\S")
TLDR = re.compile(r">\s*\[!tldr\][-+]?(.*)\Z")
LINE_SUFFIX = re.compile(r":[0-9]+(?:-[0-9]+)?\Z")
NOT_A_PATH = set("*?[]{}<>|$\\\"'=;()&,`")


class Failed(Exception):
    """handoff could not finish."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failed(message)


@dataclass
class Facts:
    branch: object               # the name, or None when no branch is checked out
    head: object                 # the commit id, or None when the repo has no commit yet
    dirty: int                   # entries of `git status`, all of them
    changed: list = field(default_factory=list)   # the first MAX_CHANGED of them, as "XY path"
    more: int = 0                # dirty - len(changed)


def clean_text(text):
    """A name from a file or from git cannot start a new line, or move the cursor."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text))


# ── git ──────────────────────────────────────────────────────────────────────

def own_git_env():
    """The caller's GIT_ variables can point a call at another repo; none of them is handed on. A call of this
    script also takes no lock that it does not need, so check changes no file."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git(folder, *args, ok=(0,)):
    """(exit code, output) of one git call in this folder. An exit code that is not expected ends the run."""
    try:
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, env=own_git_env(),
                              timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Failed("git is not on PATH")
    except subprocess.TimeoutExpired:
        raise Failed(f"git {args[0]} did not end in {GIT_TIMEOUT_S} s")
    if done.returncode not in ok:
        reason = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Failed(f"git {args[0]} failed: {reason[-1] if reason else 'exit ' + str(done.returncode)}")
    return done.returncode, done.stdout


def find_top(repo):
    if not repo.is_dir():
        raise Failed(f"no such folder: {repo}")
    _, out = git(repo, "rev-parse", "--show-toplevel")
    top = Path(os.fsdecode(out.rstrip(b"\n")))
    if not out.strip() or not top.is_dir():
        raise Failed("git names no work tree here")
    return top


def read_facts(top):
    code, out = git(top, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
    branch = out.decode("utf-8", "surrogateescape").rstrip("\n") if code == 0 else None
    code, out = git(top, "rev-parse", "--verify", "-q", "HEAD^{commit}", ok=(0, 1))
    head = out.decode("ascii", "replace").strip() if code == 0 else None
    if head is not None and not COMMIT.match(head):
        raise Failed("git gave no commit id for HEAD")
    _, out = git(top, "status", "--porcelain=v1", "-z")
    fields = out.decode("utf-8", "surrogateescape").split("\0")
    entries, index = [], 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise Failed("git status gave a line that this script cannot read")
        if entry[0] in "RC" or entry[1] in "RC":
            index += 1          # the next field is the old name of a rename or a copy
        entries.append(entry)
    listed = entries[:MAX_CHANGED]
    return Facts(branch=branch, head=head, dirty=len(entries), changed=listed, more=len(entries) - len(listed))


def is_tracked(top, rel):
    code, _ = git(top, "ls-files", "--error-unmatch", "--", rel, ok=(0, 1))
    return code == 0


def is_ignored(top, rel):
    code, _ = git(top, "check-ignore", "-q", "--", rel, ok=(0, 1))
    return code == 0


def ensure_ignored(top):
    """Keep the note and the kept note out of git. Return the exclude file when lines were added to it."""
    need = [rel for rel in (NOTE, PREV) if not is_ignored(top, rel)]
    if not need:
        return None, 0
    _, out = git(top, "rev-parse", "--git-path", "info/exclude")
    exclude = Path(os.fsdecode(out.rstrip(b"\n")))
    if not exclude.is_absolute():
        exclude = top / exclude
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(exclude, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "r+b") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Failed("the exclude file of this repo is no plain file")
            before = handle.read()
            start = b"" if not before or before.endswith(b"\n") else b"\n"
            handle.write(start + "".join(f"/{rel}\n" for rel in need).encode("ascii"))
    except OSError as exc:
        raise Failed(f"cannot write the exclude file of this repo: {exc.strerror}")
    still = [rel for rel in need if not is_ignored(top, rel)]
    if still:
        raise Failed(f"{still[0]} cannot be kept out of git here: a rule of the repo takes it back in")
    return exclude, len(need)


# ── the note ─────────────────────────────────────────────────────────────────

def fact_block(facts, written):
    lines = ["---", "type: handoff", f"written: {json.dumps(written)}", f"branch: {json.dumps(facts.branch)}",
             f"head: {json.dumps(facts.head)}", f"dirty: {facts.dirty}"]
    if facts.changed:
        lines.append("changed:")
        lines.extend(f"  - {json.dumps(entry)}" for entry in facts.changed)
    else:
        lines.append("changed: []")
    if facts.more:
        lines.append(f"changed_more: {facts.more}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def json_text(raw, what, number, nullable=False):
    try:
        value = json.loads(raw)
    except ValueError:
        value = 0
    if not (isinstance(value, str) or (nullable and value is None and raw == "null")):
        raise Failed(f"the fact block is not as frame wrote it (line {number}: {what})")
    return value


def read_note(text):
    """(the facts of the block, the lines of the body). The block is read strictly: it is what frame wrote, line for
    line and in that order, or the note is not read at all."""
    lines = text.split("\n")
    position = 0

    def take(pattern, what):
        nonlocal position
        match = re.fullmatch(pattern, lines[position]) if position < len(lines) else None
        if match is None:
            raise Failed(f"the fact block is not as frame wrote it (line {position + 1}: {what})")
        position += 1
        return match

    take(r"---", "the first line")
    take(r"type: handoff", "type")
    json_text(take(r"written: (.*)", "written").group(1), "written", position)
    branch = json_text(take(r"branch: (.*)", "branch").group(1), "branch", position, nullable=True)
    head = json_text(take(r"head: (.*)", "head").group(1), "head", position, nullable=True)
    if head is not None and not COMMIT.match(head):
        raise Failed(f"the fact block is not as frame wrote it (line {position}: head)")
    dirty = int(take(r"dirty: (0|[1-9][0-9]{0,8})", "dirty").group(1))
    changed = []
    if take(r"changed:( \[\])?", "changed").group(1) is None:
        while position < len(lines) and lines[position].startswith("  - "):
            changed.append(json_text(lines[position][4:], "changed", position + 1))
            position += 1
        if not changed:
            raise Failed(f"the fact block is not as frame wrote it (line {position + 1}: changed)")
    more = 0
    if position < len(lines) and lines[position].startswith("changed_more: "):
        more = int(take(r"changed_more: ([1-9][0-9]{0,8})", "changed_more").group(1))
    take(r"---", "the last line")
    if dirty != len(changed) + more or len(changed) > MAX_CHANGED or (more and len(changed) != MAX_CHANGED):
        raise Failed("the fact block is not as frame wrote it (the count does not fit the list)")
    return Facts(branch=branch, head=head, dirty=dirty, changed=changed, more=more), lines[position:]


def split_body(lines):
    """(the rows above the first heading, {section name: its rows}). A row is (the line, in_code), where in_code
    is true for a fence line and for each line inside a fenced block. A heading inside a block starts no section.
    A section that comes twice is one section."""
    above, sections, fence = [], {}, None
    rows = above
    for line in lines:
        match = FENCE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = (match.group(1)[0], len(match.group(1)))
                rows.append((line, True))
            elif line.startswith("## "):
                rows = sections.setdefault(line[3:].strip(), [])
            else:
                rows.append((line, False))
            continue
        if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1] and not match.group(2).strip():
            fence = None
        rows.append((line, True))
    return above, sections


def code_spans(line):
    """The code spans of one line: a run of backticks opens a span that the next run of the same length ends."""
    spans, index, size = [], 0, len(line)
    while index < size:
        if line[index] != "`":
            index += 1
            continue
        opened = index
        while index < size and line[index] == "`":
            index += 1
        width, seek, close = index - opened, index, -1
        while seek < size:
            if line[seek] != "`":
                seek += 1
                continue
            run = seek
            while seek < size and line[seek] == "`":
                seek += 1
            if seek - run == width:
                close = run
                break
        if close != -1:
            spans.append(line[index:close])
            index = close + width
    return spans


def has_text(rows):
    return any(line.strip() for line, _ in rows)


def summary_has_text(above):
    """The summary: a `> [!tldr]` line above the first heading, with text on it or on the quote lines after it."""
    for number, (line, in_code) in enumerate(above):
        match = None if in_code else TLDR.match(line)
        if match is None:
            continue
        if match.group(1).strip():
            return True
        for later, later_in_code in above[number + 1:]:
            if later_in_code or not later.startswith(">"):
                break
            if later[1:].strip():
                return True
    return False


def action_gap(rows):
    """None, or the gap of "Next actions": no open checkbox, or a first open checkbox with no command. The first
    open action runs up to the next checkbox line."""
    boxes = [number for number, (line, in_code) in enumerate(rows) if not in_code and BOX.match(line)]
    first = next((number for number in boxes if BOX.match(rows[number][0]).group(1) == " "), None)
    if first is None:
        return "no-next-action"
    end = next((number for number in boxes if number > first), len(rows))
    for line, in_code in rows[first:end]:
        if in_code or any(span.strip() for span in code_spans(line)):
            return None
    return "no-command"


def named_path(span):
    """The path that a code span names, or None when the span is not written as a path below the top folder."""
    name = LINE_SUFFIX.sub("", span.strip())
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if any(ch.isspace() or ch in NOT_A_PATH for ch in name):
        return None
    if any(part in ("", ".", "..") for part in name.split("/")):
        return None
    return name


def missing_paths(top, body_rows):
    """Paths that the note names and that are not there. Only a path whose first part exists in the top folder
    counts, so a branch name or a path of another machine is no gap."""
    found = []
    for line, in_code in body_rows:
        if in_code:
            continue
        for span in code_spans(line):
            name = named_path(span)
            if name is None or name in found:
                continue
            try:
                if os.path.lexists(top / name.split("/")[0]) and not os.path.lexists(top / name):
                    found.append(name)
            except (OSError, ValueError):
                continue
    return found


def read_note_file(top):
    path = top / NOTE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Failed(f"no note at {NOTE}")
    except OSError as exc:
        raise Failed(f"cannot read {NOTE}: {exc.strerror}")
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Failed(f"{NOTE} is no plain file")
        data = handle.read(MAX_NOTE_BYTES + 1)
    if len(data) > MAX_NOTE_BYTES:
        raise Failed(f"{NOTE} is too large to be a note")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Failed(f"{NOTE} is not UTF-8 text")


def short(commit):
    return commit[:7] if commit else "none"


def check(top):
    noted, body = read_note(read_note_file(top))
    now = read_facts(top)
    gaps = []
    if noted.head != now.head:
        gaps.append(f"stale-head note={short(noted.head)} now={short(now.head)}")
    if noted.branch != now.branch:
        gaps.append(f"stale-branch note={noted.branch or 'detached'} now={now.branch or 'detached'}")
    if (noted.dirty, noted.changed) != (now.dirty, now.changed):
        gaps.append(f"stale-changes note={noted.dirty} now={now.dirty}")
    above, sections = split_body(body)
    if not summary_has_text(above):
        gaps.append("empty-section tldr")
    gaps.extend(f"empty-section {name}" for name in SECTIONS if not has_text(sections.get(name, [])))
    if has_text(sections.get(SECTIONS[0], [])):
        gap = action_gap(sections[SECTIONS[0]])
        if gap:
            gaps.append(gap)
    missing = missing_paths(top, above + [row for rows in sections.values() for row in rows])
    lines = gaps + [f"missing-path {name}" for name in missing[:MAX_MISSING]]
    if len(missing) > MAX_MISSING:
        lines.append(f"missing-path-more {len(missing) - MAX_MISSING}")
    count = len(gaps) + len(missing)
    if count == 0:
        return 0, ["handoff: ok"]
    return 1, lines + [f"handoff: summary gaps={count}"]


def worth_keeping(path):
    """True when the file at the note's path holds something that frame did not write itself."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        return False            # a link is replaced; what it points at is not this script's to move
    if not stat.S_ISREG(mode):
        raise Failed(f"{NOTE} is no plain file")
    try:
        data = path.read_bytes()
        if len(data) > MAX_NOTE_BYTES:
            return True
        _, body = read_note(data.decode("utf-8"))
    except (Failed, UnicodeDecodeError):
        return True             # not a note that this script can read: never thrown away
    above, sections = split_body(body)
    return has_text([row for row in above if not TLDR.match(row[0]) and row[0].strip() != ">"]) \
        or summary_has_text(above) or any(has_text(rows) for rows in sections.values())


def write_whole(path, text):
    """Write the file under another name and rename it, so a reader sees the old note or the new one, and a link
    at the path is replaced and not followed."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="handoff.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def frame(top):
    for rel in (NOTE, PREV):
        if is_tracked(top, rel):
            raise Failed(f"git tracks {rel} in this repo; frame does not replace a tracked file")
    folder, note, prev = top / ".claude", top / NOTE, top / PREV
    try:
        folder.mkdir(exist_ok=True)
    except OSError as exc:
        raise Failed(f"cannot use {folder.name}/ as a folder: {exc.strerror}")
    lines = []
    exclude, added = ensure_ignored(top)
    facts = read_facts(top)
    written = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if worth_keeping(note):
            try:
                os.replace(note, prev)
                lines.append(f"kept the last note as {PREV}")
            except FileNotFoundError:
                pass            # another frame moved it first
        write_whole(note, fact_block(facts, written) + SKELETON)
    except OSError as exc:
        raise Failed(f"cannot write {NOTE}: {exc.strerror}")
    if added:
        try:
            shown = exclude.relative_to(top)
        except ValueError:
            shown = exclude
        lines.append(f"added {added} line{'s' if added > 1 else ''} to {shown}")
    return 0, lines + [f"handoff: wrote {NOTE}"]


def main(argv=None):
    parser = Parser(prog="handoff.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("frame", "check"):
        commands.add_parser(name).add_argument("--repo", type=Path, default=Path("."))
    try:
        args = parser.parse_args(argv)
        top = find_top(args.repo)
        code, lines = frame(top) if args.command == "frame" else check(top)
    except Failed as exc:
        code, lines = 2, [f"handoff: error {exc}"]
    except Exception as exc:  # anything else: the script did not finish, and that must not read as a result
        code, lines = 2, [f"handoff: error {type(exc).__name__}: {exc}"]
    for line in lines:
        print(clean_text(line), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
