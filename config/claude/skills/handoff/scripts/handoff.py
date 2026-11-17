#!/usr/bin/env python3
"""handoff: write the frame of a resume note for this work tree, and check a note.

  handoff.py frame [--repo DIR]   write .claude/handoff/note.md: the facts from git, and the empty parts
  handoff.py check [--repo DIR]   list what the note lacks, and where the tree is no longer as it says

The note is for a session that starts with nothing: it says what the goal is,
where the work stands and what to do first. `frame` writes what git knows (the
branch, the commit, the files that are not committed) and the headings. The
agent writes the rest. `check` reads the note and the tree again.

Read the exit code and the last line together, never one without the other:

  frame  exit 0   "handoff: wrote .claude/handoff/note.md"
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
  missing-path <path>                      the note names, as code, a path that is not there, below a top
                                           folder or file of this tree that is
  missing-path-more <n>                    that many more such paths; the count has them all

The note lives in the work tree, in a folder of its own, .claude/handoff/.
Frame makes that folder and puts a .gitignore with the one line `*` into it,
so git sees nothing in it. Frame writes no other file: not the repo's exclude
file, which all work trees of a repo share, and no rule outside the folder.
A note with text in it is kept one time, as .claude/handoff/prev.md, when
frame writes a new one.

The folder must be this work tree's own, and one that frame made. Each of
these is exit 2, for frame and for check:

  - git tracks .claude/handoff or a file in it, also under a name that
    differs only in letter case (the same place on a disk that does not keep
    case apart), or git tracks .claude as one entry (a link, a submodule);
  - .claude or the folder holds a .git of its own, as a folder or as a file:
    it is the top of another repo, and a note there is a file of that repo;
  - .claude, the folder, its .gitignore, the note or the kept note is a link;
  - the folder holds files and its .gitignore is not the one line `*`: it is
    a folder of the user, and a .gitignore there would hide those files.

For frame also: the file at the note's path or at the kept note's path does not
start as a note of this script starts. Frame then writes nothing at all.

The fact block ends with a check value over its lines. A block that was
changed by hand is an error: when the tree moved, run frame again.

The script only calls git and reads and writes these files. It runs nothing of
the project and uses no network.
"""
import argparse
import bisect
import contextlib
import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

FOLDER = ".claude"
PLACE = FOLDER + "/handoff"        # the folder that frame makes; everything of this script is in it
NOTE = PLACE + "/note.md"
PREV = PLACE + "/prev.md"
IGNORE = PLACE + "/.gitignore"
IGNORE_ALL = b"*\n"                # what frame writes there: git then sees no file of the folder, this one included
TEMP = "note.tmp."                 # how the name of a file starts that frame writes and then renames to the note
MARK = b"---\ntype: handoff\n"     # how each note starts that frame wrote
SECTIONS = ("Next actions", "State now", "Environment", "Decisions, and why", "Rejected approaches", "Open questions")
SKELETON = "\n> [!tldr]\n>\n" + "".join(f"\n## {name}\n" for name in SECTIONS)
MAX_NOTE_BYTES = 1 << 20
MAX_CHANGED = 50     # entries of the fact block; `dirty` always holds the full count
MAX_MISSING = 20     # missing-path lines that check prints; the summary counts them all
GIT_TIMEOUT_S = 60
LOCK_WAIT_S = 30

COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
WRITTEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)\Z")
BOX = re.compile(r"\s*(?:[-*+]|[0-9]{1,9}[.)])\s+\[([ xX])\]\s+\S")
TLDR = re.compile(r">\s*\[!tldr\][-+]?(.*)\Z")
LINE_SUFFIX = re.compile(r":[0-9]+(?:-[0-9]+)?\Z")
RUN = re.compile(r"`+")
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
    script also takes no lock that it does not need, so check changes no file, and it never fetches: in a partial
    clone a fetch runs a program that the repo's config names."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def git(folder, *args, ok=(0,)):
    """(exit code, output) of one git call in this folder. An exit code that is not expected ends the run. The
    file monitor is off: it is a program that the repo's config names."""
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(folder), *args], capture_output=True,
                              env=own_git_env(), timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Failed("git is not on PATH")
    except subprocess.TimeoutExpired:
        raise Failed(f"git {args[0]} did not end in {GIT_TIMEOUT_S} s")
    if done.returncode not in ok:
        reason = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Failed(f"git {args[0]} failed: {reason[-1] if reason else 'exit ' + str(done.returncode)}")
    return done.returncode, done.stdout


def one_path(out):
    """The path that git printed: all of it but the line end, for a name can end in a line break itself."""
    return Path(os.fsdecode(out[:-1] if out.endswith(b"\n") else out))


def find_top(repo):
    if not repo.is_dir():
        raise Failed(f"no such folder: {repo}")
    _, out = git(repo, "rev-parse", "--show-toplevel")
    top = one_path(out)
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
    # Each option takes one setting of the repo out of the answer: every file of a new folder is listed, a file
    # that moved is one that is gone and one that is new, and a change inside a submodule counts.
    _, out = git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                 "--ignore-submodules=none")
    entries = [entry for entry in out.decode("utf-8", "surrogateescape").split("\0") if entry]
    if any(len(entry) < 4 or entry[2] != " " for entry in entries):
        raise Failed("git status gave a line that this script cannot read")
    listed = entries[:MAX_CHANGED]
    return Facts(branch=branch, head=head, dirty=len(entries), changed=listed, more=len(entries) - len(listed))


def fold(name):
    """A name as a disk reads it that does not keep letter case apart: `HANDOFF` is the folder's own name there,
    and so is the name with the one character for `ff` (U+FB00)."""
    return name.casefold()


def refuse_another_owner(top):
    """The folder must be this work tree's own. What git tracks there came with the repo, under its own name or
    under one that is the same place on some disk, and so did each file of a `.claude` that git tracks as one
    entry (a link, a submodule). A `.claude` or a folder with a `.git` in it is the top of another repo, whatever
    the settings of that repo say about its work tree. No session of this work tree wrote a note there: check
    does not read it, and frame does not write there."""
    place = fold(PLACE)
    _, out = git(top, "ls-files", "-z", "--", f":(top,icase){FOLDER}")
    for name in out.decode("utf-8", "surrogateescape").split("\0"):
        if fold(name) in (fold(FOLDER), place) or fold(name).startswith(place + "/"):
            raise Failed(f"git tracks {name} in this repo: a note at that place came with the repo, and this script "
                         "neither reads nor replaces it")
    for rel in (FOLDER, PLACE):
        if os.path.lexists(top / rel / ".git"):
            raise Failed(f"{rel} is a repo of its own: a note there is a file of that repo, and this script neither "
                         "reads nor writes it")


# ── the folder ───────────────────────────────────────────────────────────────

def open_folder(parent, part, rel, create):
    """A handle of the folder `part` in the open folder `parent`, never through a link. None when it is not there
    and may not be made."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            return os.open(part, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                return None
            with contextlib.suppress(FileExistsError):   # another frame made it in this moment
                os.mkdir(part, dir_fd=parent)
            return os.open(part, flags, dir_fd=parent)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        raise Failed(f"{rel} is no plain folder")


def open_place(top, create):
    """A handle of .claude/handoff, opened part by part, or None. Every file of the note is then opened, written
    and renamed through this handle, so no other folder can stand in its place between a look and a write."""
    handle = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    for part, rel in ((FOLDER, FOLDER), (Path(PLACE).name, PLACE)):
        try:
            deeper = open_folder(handle, part, rel, create)
        finally:
            os.close(handle)
        if deeper is None:
            return None
        handle = deeper
    return handle


def read_plain(place, rel):
    """The bytes of the plain file with this path in the open folder, and never more than one byte past the size
    of a note. The file is opened one time and never through a link. OSError when it cannot be opened."""
    fd = os.open(Path(rel).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=place)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Failed(f"{rel} is no plain file")
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_NOTE_BYTES + 1)


def own_place(place, make):
    """End with Failed unless the open folder is one that frame made: its .gitignore holds the one line `*`. With
    `make`, an empty folder becomes one. A folder with files and no such .gitignore is the user's own. Frame
    writes no .gitignore there: it would hide those files from git."""
    try:
        rule = read_plain(place, IGNORE)
    except FileNotFoundError:
        rule = None
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise Failed(f"{IGNORE} is a link")
    if rule == IGNORE_ALL:
        return
    if rule is None and not os.listdir(place):
        if not make:
            raise Failed(f"no note at {NOTE}")
        fd = os.open(Path(IGNORE).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=place)
        with os.fdopen(fd, "wb") as handle:
            handle.write(IGNORE_ALL)
        return
    raise Failed(f"{PLACE} is not a folder that frame made: its .gitignore is not the one line `*`. This script "
                 "neither reads nor writes there; ask the user what to do with that folder")


# ── the note ─────────────────────────────────────────────────────────────────

def seal(lines):
    """The check value of the block lines above it. It holds no secret: it shows an edit by hand, and it does
    not stop a program that writes a block of its own."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]


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
    lines.append(f'seal: "{seal(lines)}"')
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
    line and in that order, with the check value of these lines, or the note is not read at all."""
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
    written = json_text(take(r"written: (.*)", "written").group(1), "written", position)
    if not WRITTEN.match(written):
        raise Failed(f"the fact block is not as frame wrote it (line {position}: written)")
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
    if take(r'seal: "([0-9a-f]{32})"', "seal").group(1) != seal(lines[:position - 1]):
        raise Failed("the fact block is not as frame wrote it (it was changed after that; when the tree moved, "
                     "run frame again)")
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
    """The code spans of one line: a run of backticks opens a span that the next run of the same length ends.
    A run finds its partner by a search in a sorted list, so a long line of runs that fit no other is not read
    again for each of them."""
    runs = [match.span() for match in RUN.finditer(line)]
    by_width = {}
    for number, (opened, closed) in enumerate(runs):
        by_width.setdefault(closed - opened, []).append(number)
    spans, number = [], 0
    while number < len(runs):
        opened, closed = runs[number]
        same = by_width[closed - opened]
        later = bisect.bisect_right(same, number)
        if later == len(same):
            number += 1
            continue
        partner = same[later]
        spans.append(line[closed:runs[partner][0]])
        number = partner + 1
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
    found, seen = [], set()
    for line, in_code in body_rows:
        if in_code:
            continue
        for span in code_spans(line):
            name = named_path(span)
            if name is None or name in seen:
                continue
            seen.add(name)
            try:
                if os.path.lexists(top / name.split("/")[0]) and not os.path.lexists(top / name):
                    found.append(name)
            except (OSError, ValueError):
                continue
    return found


def read_note_file(place):
    try:
        data = read_plain(place, NOTE)
    except FileNotFoundError:
        raise Failed(f"no note at {NOTE}")
    except OSError as exc:
        raise Failed(f"cannot read {NOTE}: {exc.strerror}")
    if len(data) > MAX_NOTE_BYTES:
        raise Failed(f"{NOTE} is too large to be a note")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Failed(f"{NOTE} is not UTF-8 text")


def short(commit):
    return commit[:7] if commit else "none"


def check(top):
    refuse_another_owner(top)
    try:
        place = open_place(top, create=False)
        if place is None:
            raise Failed(f"no note at {NOTE}")
        try:
            own_place(place, make=False)
            text = read_note_file(place)
        finally:
            os.close(place)
    except OSError as exc:
        raise Failed(f"cannot read {NOTE}: {exc.strerror}")
    noted, body = read_note(text)
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


def own_file(place, rel):
    """The bytes of the note or of the kept note in the open folder, or None when there is no file. Frame moves
    and replaces only a note that frame wrote. A file that starts in another way is the user's own or came from
    elsewhere, and so did a link: Failed, and it is left as it is."""
    try:
        data = read_plain(place, rel)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise Failed(f"{rel} is a link, and frame replaces only a note that frame wrote")
    if not data.startswith(MARK):
        raise Failed(f"{rel} is not a note that frame wrote, and frame does not replace it; ask the user what to do "
                     "with that file")
    return data


def worth_keeping(data):
    """True when a note holds something that frame did not write itself."""
    try:
        if len(data) > MAX_NOTE_BYTES:
            return True
        _, body = read_note(data.decode("utf-8"))
    except (Failed, UnicodeDecodeError):
        return True             # not a note that this script can read: never thrown away
    above, sections = split_body(body)
    return has_text([row for row in above if not TLDR.match(row[0]) and row[0].strip() != ">"]) \
        or summary_has_text(above) or any(has_text(rows) for rows in sections.values())


def write_whole(place, rel, text):
    """Write the file under another name in the open folder and rename it, so a reader sees the old note or the
    new one, and nothing is written through a link."""
    temp = TEMP + secrets.token_hex(8)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=place)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp, Path(rel).name, src_dir_fd=place, dst_dir_fd=place)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=place)
        raise


@contextlib.contextmanager
def only_frame_in(place):
    """One frame at a time looks at the folder, keeps the note and writes the new one. Without this, two frames
    both chose to keep a note with text, and the second moved the empty frame of the first over it. The lock is on
    the open folder and ends with the process, so a frame that was killed holds nothing."""
    end = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(place, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= end:
                raise Failed(f"another frame holds {PLACE}/ for longer than {LOCK_WAIT_S} s; run frame again")
            time.sleep(0.02)
    try:
        yield
    finally:
        fcntl.flock(place, fcntl.LOCK_UN)


def frame(top):
    refuse_another_owner(top)
    # Before the folder is there: a new folder with its .gitignore adds no line to `git status`, and a run that
    # ends here leaves nothing behind.
    facts = read_facts(top)
    written = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = []
    try:
        place = open_place(top, create=True)
    except OSError as exc:
        raise Failed(f"cannot use {PLACE}/ as a folder: {exc.strerror}")
    try:
        with only_frame_in(place):
            # Each look comes before the first change: with a folder or a file of the user in the way, frame
            # writes nothing at all.
            own_place(place, make=True)
            old = own_file(place, NOTE)
            own_file(place, PREV)
            if old is not None and worth_keeping(old):
                os.replace(Path(NOTE).name, Path(PREV).name, src_dir_fd=place, dst_dir_fd=place)
                lines.append(f"kept the last note as {PREV}")
            write_whole(place, NOTE, fact_block(facts, written) + SKELETON)
    except OSError as exc:
        raise Failed(f"cannot write {NOTE}: {exc.strerror}")
    finally:
        os.close(place)
    return 0, lines + [f"handoff: wrote {NOTE}"]


def main(argv=None):
    # No --help: each run ends with a result line or an error line, and a help text is neither.
    parser = Parser(prog="handoff.py", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("frame", "check"):
        commands.add_parser(name, add_help=False).add_argument("--repo", type=Path, default=Path("."))
    try:
        args = parser.parse_args(argv)
        top = find_top(args.repo)
        code, lines = frame(top) if args.command == "frame" else check(top)
    except Failed as exc:
        code, lines = 2, [f"handoff: error {exc}"]
    except Exception as exc:  # anything else: the script did not finish, and that must not read as a result
        code, lines = 2, [f"handoff: error {type(exc).__name__}: {exc}"]
    # A name that the terminal's character set cannot show is printed in escaped form, and the run still ends.
    getattr(sys.stdout, "reconfigure", lambda **_: None)(errors="backslashreplace")
    for line in lines:
        print(clean_text(line), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
