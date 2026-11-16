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
  missing-path <path>                      the note names, as code, a path that is not there, below a top
                                           folder or file of this tree that is
  missing-path-more <n>                    that many more such paths; the count has them all

The note lives in the work tree and git does not see it: frame adds its names
to the repo's exclude file when no rule ignores them yet. A note with text in
it is kept one time, as .claude/handoff.prev.md, when frame writes a new one.
A note that git tracks came with the repo: frame does not replace it and check
does not read it. This holds also for a tracked name that differs only in
letter case, which is the same file on a disk that does not keep case apart,
and for a .claude that git tracks as a link or as a submodule. No command
reads the note through a link, at the folder or at the file.

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
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

FOLDER = ".claude"
NOTE = FOLDER + "/handoff.md"
PREV = FOLDER + "/handoff.prev.md"
TEMP = FOLDER + "/handoff.tmp."    # how the name of a file starts that frame writes and then renames to the note
KEPT_OUT = (NOTE, PREV, TEMP + "*")
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
    """A name as a disk reads it that does not keep letter case apart: `HANDOFF.md` is the note's own file there,
    and so is the name with the one character for `ff` (U+FB00)."""
    return name.casefold()


def tracked_as(top):
    """None, or the name of a file that git tracks and that is the note or the kept note on some disk. The folder
    counts too when git tracks it as one entry, a link or a submodule: each file in it came with a repo then."""
    own = {fold(NOTE), fold(PREV), fold(FOLDER)}
    _, out = git(top, "ls-files", "-z", "--", f":(top,icase){FOLDER}")
    for name in out.decode("utf-8", "surrogateescape").split("\0"):
        if fold(name) in own:
            return name
    return None


def is_ignored(top, rel):
    code, _ = git(top, "check-ignore", "-q", "--", rel, ok=(0, 1))
    return code == 0


def ensure_ignored(top):
    """Keep the note, the kept note and a half-written note out of git. Return the exclude file when lines were
    added to it."""
    need = [name for name in KEPT_OUT if not is_ignored(top, name)]
    if not need:
        return None, 0
    _, out = git(top, "rev-parse", "--git-path", "info/exclude")
    exclude = one_path(out)
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
            handle.write(start + "".join(f"/{name}\n" for name in need).encode("ascii"))
    except OSError as exc:
        raise Failed(f"cannot write the exclude file of this repo: {exc.strerror}")
    still = [name for name in need if not is_ignored(top, name)]
    if still:
        raise Failed(f"{still[0]} cannot be kept out of git here: a rule of the repo takes it back in")
    return exclude, len(need)


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
    Each run is looked at one time, so a long line of runs that fit no other costs no more than its length."""
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


def read_plain(top):
    """The bytes of the plain file at the note's path, and never more than one byte past the size of a note. The
    folder and the file are each opened one time and never through a link, so no other file can stand in the
    place of the note between a look and the read. OSError when one of them cannot be opened."""
    try:
        folder = os.open(top / FOLDER, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        raise Failed(f"{FOLDER} is no plain folder")
    try:
        fd = os.open(Path(NOTE).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    finally:
        os.close(folder)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Failed(f"{NOTE} is no plain file")
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_NOTE_BYTES + 1)


def read_note_file(top):
    try:
        data = read_plain(top)
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
    tracked = tracked_as(top)
    if tracked:
        raise Failed(f"git tracks {tracked} in this repo: the note came with the repo, and it is not a note of a "
                     "session here")
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


def worth_keeping(top):
    """True when the file at the note's path holds something that frame did not write itself."""
    try:
        data = read_plain(top)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        return False            # a link is replaced; what it points at is not this script's to move
    try:
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
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=Path(TEMP).name)
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


@contextlib.contextmanager
def only_frame_in(folder):
    """One frame at a time looks at the note, keeps it and writes the new one. Without this, two frames both chose
    to keep a note with text, and the second moved the empty frame of the first over it. The lock ends with the
    process, so a frame that was killed holds nothing."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        end = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Failed(f"another frame holds {folder.name}/ for longer than {LOCK_WAIT_S} s; run frame again")
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def frame(top):
    tracked = tracked_as(top)
    if tracked:
        raise Failed(f"git tracks {tracked} in this repo; frame writes no note over or through a tracked file")
    folder, note, prev = top / FOLDER, top / NOTE, top / PREV
    try:
        folder.mkdir(exist_ok=True)
    except OSError as exc:
        raise Failed(f"cannot use {folder.name}/ as a folder: {exc.strerror}")
    lines = []
    exclude, added = ensure_ignored(top)
    facts = read_facts(top)
    written = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with only_frame_in(folder):
            if worth_keeping(top):
                os.replace(note, prev)
                lines.append(f"kept the last note as {PREV}")
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
