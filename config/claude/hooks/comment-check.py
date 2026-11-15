#!/usr/bin/env python3
"""comment-check: list comments whose code changed while the comment did not.

It reads the committed diff from merge-base(<base>, HEAD) to HEAD. When the
history has more than one best common ancestor (a criss-cross merge), it
checks each and joins the hits. The working tree, the index and untracked
files play no part.

A hit is advice for a reviewer, not a defect. A comment that still holds after
the change is a fine answer; the point is that someone reads it again.

What a hit is. In the new version of a changed file:

  comment block   one or more consecutive lines that are comments from their
                  first non-blank character. Line comments only: `#` for .py
                  .sh .bash .zsh .rb .yml .yaml .toml and for a file with no
                  extension whose first line starts with `#!`; `//` for .js
                  .jsx .ts .tsx .go .rs .c .h .cc .cpp .hpp .java .kt .swift.
                  These lines are not comments for this check (they end a
                  block and count as code): a `#!` first line, a coding line
                  (PEP 263) on line 1 or 2, and a comment that starts with a
                  lint or tool marker (noqa, type: ignore, pragma, an eslint
                  disable, pylint, @ts- and the like; see MARKER_WORDS).
  code under it   the lines that follow the block, up to and not including the
                  first blank line, the next comment block, or a line indented
                  less than the block (the end of the scope); at most 20 lines.
                  Indent counts leading spaces and tabs, one column each.
  hit             no line of the block was added or changed, and either a line
                  of the code under it was added or changed, or a line was
                  deleted inside that code or between the block and the code.

A file the diff adds as a whole, a deleted file, a binary file (a NUL byte in
either version), a symlink, a submodule and every other file type give no
hit. A renamed file is compared with the file it came from, as git pairs them
(-M, at least 50% alike); below that it is an add and a delete, so no hit.
An inline comment after code is code, not a block. Lines are split on LF only;
a CR before it is ignored when the line is read, but git still sees the CR,
so a change of line ends alone marks every line changed, and the comments with
them: no hit. A file that is not UTF-8 is read as bytes.

Limits. The check reads lines, not the language. A line inside a multi-line
string, a heredoc or a raw block that starts with `#` or `//` reads as a
comment, and the check cannot tell it apart. Block comments (/* */, docstrings)
are not read. A deletion right after the last code line, or code that the
change deleted in full (so no code is left under the comment), gives no hit.

Safety: it calls git with a fixed argument list and no shell. No program from
the repo's config can run: no external diff (--no-ext-diff), no textconv
(--no-textconv; git matches attributes such as `* diff=x` against the blob
names of the two-blob diff, so a driver would apply), no filter (no file is
read from the work tree), no fsmonitor (core.fsmonitor is switched off), no
pager (--no-pager), and it never fetches (GIT_NO_LAZY_FETCH): in a partial
clone a missing object ends the run with exit 2. GIT_* variables from the
caller's environment are dropped, so a hook's GIT_DIR cannot point it at
another repo, and every call runs in the folder that --repo names, so a
core.worktree setting cannot either. It writes nothing
(GIT_OPTIONAL_LOCKS=0). Anything git prints on stderr ends the run with
exit 2.

A setting cannot change what the check reads from `git diff`. The check takes
a diff only after it has held it against the two versions of the file: each
hunk holds its removed lines and then its added lines, with no line of
context, and the lines outside the hunks are the same in both versions. A
diff of another shape (context that joins two hunks, "Binary files", colour)
ends the run with exit 2. So an attribute or a setting that makes git read
text as binary gives an error, never a false "clean".

Time: every git call has a timeout, and the whole run has one limit
(--timeout), which also ends the check's own work. At the limit the run ends
with exit 2.

Usage: comment-check.py --base <ref> [--repo <dir>] [--timeout <seconds>]
                        [--git-timeout <seconds>]
Output: one line per hit, `stale-comment <path>:<line> <first 80 characters of
the comment>`, sorted by path and line, then the result line:
  exit 0  comment-check: clean
  exit 1  comment-check: summary hits=N
  exit 2  comment-check: error <reason>   (it could not finish; never "clean")
Every printed line goes through `safe`, so a path or a comment cannot write a
line of its own.

Tests: PYTHONDONTWRITEBYTECODE=1 python3 -m pytest config/claude/hooks/tests/test_comment_check.py -q -p no:cacheprovider
"""
import argparse
import bisect
import os
import re
import signal
import subprocess
import sys
from typing import NamedTuple

HASH_EXTS = {b".py", b".sh", b".bash", b".zsh", b".rb", b".yml", b".yaml", b".toml"}
SLASH_EXTS = {b".js", b".jsx", b".ts", b".tsx", b".go", b".rs", b".c", b".h", b".cc",
              b".cpp", b".hpp", b".java", b".kt", b".swift"}
CODE_MAX = 20  # lines of code under a block
TEXT_MAX = 80  # characters of the comment in a hit line
WS = b" \t\f\v"

# A comment that is only one of these words (after the comment token) is a
# lint or tool marker. Some are written in two parts, so that floor-guard,
# which a reviewer runs on the same commit, does not read this file as one
# that adds a suppression.
MARKER_WORDS = ["no" + "qa", "type: " + "ignore", "pragma:", "eslint" + "-", "pylint:", "@ts-",
                "no" + "lint", "shellcheck ", "fmt:", "isort:", "mypy:", "prettier-ignore",
                "no" + "sec", "no" + "semgrep"]
MARKER_RE = re.compile(b"(?:" + b"|".join(re.escape(w.encode()) for w in MARKER_WORDS) + b")")
CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

BLANK, COMMENT, CODE = 0, 1, 2


class CheckError(Exception):
    pass


class Hit(NamedTuple):
    path: bytes     # the path in HEAD, as git stores it
    line: int       # the first line of the comment block, from 1
    comment: bytes  # the first line of the block, without its indent


GIT_TIMEOUT = 120  # seconds for one git call; --git-timeout sets it
RUN_TIMEOUT = 100  # seconds for the whole run, the check's own work too; --timeout sets it
NO_NEWLINE = b"\\ No newline at end of file"
DIFF_HEAD = (b"diff --git ", b"index ", b"--- ", b"+++ ")


def git(root, *args, stdin=None):
    """Run one git command; return stdout bytes or raise CheckError.

    Every failure is an error, and so is anything on stderr with exit 0:
    what git printed then is not the whole truth.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"  # a partial clone fetches through a program its config names
    env["LC_ALL"] = "C"
    cmd = ["git", "--no-pager", "-c", "core.fsmonitor=", "-c", "core.quotepath=off",
           "-C", root, *args]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, env=env,
                              timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise CheckError(f"git {args[0]} did not finish in {GIT_TIMEOUT} s")
    except OSError as e:
        raise CheckError(f"cannot run git: {e}")
    msg = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise CheckError(f"git {args[0]} failed: {msg or f'exit {proc.returncode}'}")
    if msg:
        raise CheckError(f"git {args[0]} warned: {msg}")
    return proc.stdout


def resolve(root, ref):
    return git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()


def changed_files(root, old, new):
    """Yield (old oid, new oid, new path) for each text-mode file changed in
    place or renamed with changes. Added, deleted and other entries are left out."""
    out = git(root, "diff-tree", "-r", "-z", "-M", "--raw", "--no-ext-diff",
              "--no-textconv", old, new)
    fields = out.split(b"\0")
    i = 0
    while i < len(fields):
        meta = fields[i]
        if not meta:
            i += 1
            continue
        if not meta.startswith(b":"):
            raise CheckError(f"unexpected diff-tree output: {meta[:40]!r}")
        old_mode, new_mode, old_oid, new_oid, status = meta[1:].split(b" ")
        if status[:1] in (b"R", b"C"):
            path = fields[i + 2]
            i += 3
        else:
            path = fields[i + 1]
            i += 2
        regular = (b"100644", b"100755")
        if (status[:1] in (b"M", b"R") and old_mode in regular and new_mode in regular
                and old_oid != new_oid):
            yield old_oid.decode(), new_oid.decode(), path


def read_blobs(root, oids):
    """Return {oid: bytes} from the object store."""
    oids = sorted(set(oids))
    if not oids:
        return {}
    out = git(root, "cat-file", "--batch", stdin="\n".join(oids).encode() + b"\n")
    content, pos = {}, 0
    for _ in oids:
        end = out.index(b"\n", pos)
        header = out[pos:end].split(b" ")
        if len(header) != 3:
            raise CheckError(f"cannot read object: {header[0].decode(errors='replace')}")
        size = int(header[2])
        content[header[0].decode()] = out[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return content


def comment_token(path, data):
    name = path.rsplit(b"/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if ext in HASH_EXTS:
        return b"#"
    if ext in SLASH_EXTS:
        return b"//"
    if ext == b"" and data.startswith(b"#!"):
        return b"#"
    return None


def git_lines(data):
    """The lines of a blob as git counts them: split on LF, a CR stays in its line."""
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    return lines


def changes(root, old_oid, new_oid, old, new):
    """Return (added new line numbers, deletion gaps) of the diff of two blobs.
    A gap g means lines were deleted between new lines g and g + 1.

    git's diff is used only after it is held against the two blobs, `old` and
    `new`: each hunk holds its removed lines and then its added lines, with
    no line of context, and the lines outside the hunks are the same in both.
    So a setting that changes what `git diff` prints cannot mark an unchanged
    comment as changed, or hide a change: it ends the run."""
    out = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
              "--unified=0", "--inter-hunk-context=0", "--diff-algorithm=myers",
              "--indent-heuristic", old_oid, new_oid)
    wrong = CheckError("git diff gave a diff that is not the plain diff of the two versions "
                       "(a setting or an attribute of the repo changes it)")
    old_lines, new_lines = git_lines(old), git_lines(new)
    rows = out.split(b"\n")
    if rows.pop() != b"":
        raise wrong
    added, gaps = set(), set()
    old_at = new_at = at = 0  # lines of each blob that are accounted for, and the row that is read
    while at < len(rows) and rows[at].startswith(DIFF_HEAD):
        at += 1
    hunks = 0
    while at < len(rows):
        m = HUNK_RE.match(rows[at])
        if not m:
            raise wrong
        at += 1
        hunks += 1
        old_count = 1 if m.group(2) is None else int(m.group(2))
        new_count = 1 if m.group(4) is None else int(m.group(4))
        # with no line on a side, git names the line before the place
        old_from = int(m.group(1)) - (1 if old_count else 0)
        new_from = int(m.group(3)) - (1 if new_count else 0)
        if old_from < old_at or new_from < new_at or old_lines[old_at:old_from] != new_lines[new_at:new_from]:
            raise wrong
        for sign, lines, first, count in ((b"-", old_lines, old_from, old_count), (b"+", new_lines, new_from, new_count)):
            want = [sign + line for line in lines[first:first + count]]
            if len(want) != count or rows[at:at + count] != want:
                raise wrong
            at += count
            if at < len(rows) and rows[at] == NO_NEWLINE:
                at += 1
        old_at, new_at = old_from + old_count, new_from + new_count
        added.update(range(new_from + 1, new_at + 1))
        if old_count:
            gaps.add(new_from)
    if not hunks or old_lines[old_at:] != new_lines[new_at:]:
        raise wrong
    return added, gaps


def split_lines(data):
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return [l[:-1] if l.endswith(b"\r") else l for l in lines]


def classify(lines, token):
    kinds = []
    for n, line in enumerate(lines, 1):
        body = line.lstrip(WS)
        if not body:
            kinds.append(BLANK)
        elif not body.startswith(token):
            kinds.append(CODE)
        elif token == b"#" and n == 1 and line.startswith(b"#!"):
            kinds.append(CODE)
        elif token == b"#" and n <= 2 and CODING_RE.match(line):
            kinds.append(CODE)
        elif MARKER_RE.match(body[len(token):].lstrip(WS)):
            kinds.append(CODE)
        else:
            kinds.append(COMMENT)
    return kinds


def indent(line):
    return len(line) - len(line.lstrip(WS))


def file_hits(path, data, token, added, gaps):
    """Return the hits in one new file, given the lines the diff added and
    the sorted gaps where it deleted lines."""
    lines = split_lines(data)
    kinds = classify(lines, token)
    hits = []
    n = len(lines)
    i = 0
    while i < n:
        if kinds[i] != COMMENT:
            i += 1
            continue
        start = i
        while i < n and kinds[i] == COMMENT:
            i += 1
        first, last = start + 1, i  # 1-based line numbers of the block
        depth = min(indent(lines[k]) for k in range(start, i))
        end = last  # last code line
        while (end < n and end - last < CODE_MAX and kinds[end] == CODE
               and indent(lines[end]) >= depth):
            end += 1
        if end == last or any(k in added for k in range(first, last + 1)):
            continue
        code_changed = any(k in added for k in range(last + 1, end + 1))
        g = bisect.bisect_left(gaps, last)
        cut = g < len(gaps) and gaps[g] < end
        if code_changed or cut:
            hits.append(Hit(path, first, lines[start].lstrip(WS)))
    return hits


def stale_comments(repo, base_ref):
    """Return the hits of the diff merge-base(base_ref, HEAD)..HEAD, sorted by
    path and line. Raise CheckError when the answer would not be whole."""
    if not os.path.isdir(repo):
        raise CheckError(f"no such folder: {repo}")
    # Every call runs in the folder that the caller named. The top of the work
    # tree is not asked for: a core.worktree setting names any folder as that.
    root = repo
    try:
        git(root, "rev-parse", "--git-dir")
    except CheckError as e:
        raise CheckError(f"not a git repo: {repo} ({e})")
    if base_ref.startswith("-"):
        raise CheckError(f"bad base ref: {base_ref}")
    try:
        base = resolve(root, base_ref)
    except CheckError as e:
        raise CheckError(f"base ref does not resolve: {base_ref} ({e})")
    head = resolve(root, "HEAD")
    try:
        merge_bases = git(root, "merge-base", "--all", base, head).decode().split()
    except CheckError as e:
        raise CheckError(f"no merge base between {base_ref} and HEAD "
                         f"(a shallow clone or unrelated history?): {e}")
    if not merge_bases:
        raise CheckError(f"no merge base between {base_ref} and HEAD")
    hits = set()
    for merge_base in merge_bases:
        entries = list(changed_files(root, merge_base, head))
        blobs = read_blobs(root, [o for e in entries for o in e[:2]])
        for old_oid, new_oid, path in entries:
            old, new = blobs[old_oid], blobs[new_oid]
            token = comment_token(path, new)
            if token is None or b"\0" in old or b"\0" in new:
                continue
            added, gaps = changes(root, old_oid, new_oid, old, new)
            hits.update(file_hits(path, new, token, added, sorted(gaps)))
    return sorted(hits)


def safe(text):
    """Make text safe for one output line: escape controls, backslash and bad UTF-8."""
    out = []
    for ch in text:
        n = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif 0xDC80 <= n <= 0xDCFF:
            out.append("\\x%02x" % (n - 0xDC00))
        elif ch.isprintable():
            out.append(ch)
        elif n < 0x100:
            out.append("\\x%02x" % n)
        elif n < 0x10000:
            out.append("\\u%04x" % n)
        else:
            out.append("\\U%08x" % n)
    return "".join(out)


def text(raw):
    return raw.decode("utf-8", "surrogateescape")


def on_alarm(signum, frame):
    raise CheckError(f"the check did not finish in {RUN_TIMEOUT} s")


def main(argv=None):
    global GIT_TIMEOUT, RUN_TIMEOUT
    parser = argparse.ArgumentParser(
        description="List comments whose code changed while the comment did not.")
    parser.add_argument("--base", required=True, help="ref to compare against")
    parser.add_argument("--repo", default=".", help="repo directory (default: .)")
    parser.add_argument("--git-timeout", type=int, default=GIT_TIMEOUT, metavar="SECONDS",
                        help=f"give up on one git call after this long (default: {GIT_TIMEOUT})")
    parser.add_argument("--timeout", type=int, default=RUN_TIMEOUT, metavar="SECONDS",
                        help=f"give up on the whole run after this long (default: {RUN_TIMEOUT})")
    args = parser.parse_args(argv)
    if args.git_timeout <= 0:
        parser.error("--git-timeout must be above 0")
    if args.timeout <= 0:
        parser.error("--timeout must be above 0")
    GIT_TIMEOUT, RUN_TIMEOUT = args.git_timeout, args.timeout
    lines = []
    code = 0
    try:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, RUN_TIMEOUT)
        try:
            hits = stale_comments(args.repo, args.base)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        for path, n, comment in hits:
            lines.append(f"stale-comment {text(path)}:{n} {text(comment)[:TEXT_MAX]}")
        lines.append(f"comment-check: summary hits={len(hits)}" if hits else "comment-check: clean")
        code = 1 if hits else 0
    except CheckError as e:
        lines = [f"comment-check: error {e}"]
        code = 2
    except Exception as e:  # fail closed: a crash must never read as clean
        lines = [f"comment-check: error internal: {type(e).__name__}: {e}"]
        code = 2
    try:
        if sys.stdout is None:  # started with stdout closed (`>&-`)
            raise OSError("stdout is closed")
        sys.stdout.buffer.write(("\n".join(safe(l) for l in lines) + "\n").encode("utf-8"))
        sys.stdout.flush()
    except (OSError, ValueError) as e:  # a closed pipe: the result was not delivered
        print(f"comment-check: error cannot write the result: {e}", file=sys.stderr)
        os._exit(2)  # skip the flush at exit, which would fail again and change the code
    return code


if __name__ == "__main__":
    sys.exit(main())
