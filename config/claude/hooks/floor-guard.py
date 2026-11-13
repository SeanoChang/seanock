#!/usr/bin/env python3
"""floor-guard: flag a change that lowers the quality bar.

It compares the tree at merge-base(<base>, HEAD) with the working tree as it
is on disk: committed, staged and unstaged changes to tracked files, plus
untracked files that git does not ignore. Loosening is loud; tightening is
silent. It reports six kinds:

  deleted-test       a test file at the base that is gone, with no move that
                     keeps it a test (see "Moves")
  test-moved-out     a test file that is gone, and its content is now only at
                     a new path that does not keep it a test (see "Moves")
  skip-added         more lines with a skip marker in a file than at the base
  suppression-added  more lines with a lint or type suppression than at the base
  assert-removed     fewer assertion lines in a test file than at the base
  ci-weakened        more lines that let a workflow step fail silently, or a
                     workflow file that is gone or moved to where it does not run

Every rule looks at paths of one scope, and the scope comes from the path
alone:

  test      2 = the file name matches a test pattern (test_*.py, *.test.ts ...),
            which a runner collects by name; 1 = only a folder named tests,
            test, __tests__ or spec is on the path; 0 = neither. A folder does
            not say what a runner collects, so rank 1 only means "report it
            when it goes"
  workflow  .github/workflows/<name>.yml or .yaml
  code      every path that is not prose (.md, .mdx, .rst, .txt, .adoc); the
            skip and suppression rules read code only

Counts are per file and per kind, so a line moved inside a file is not a hit.
A path that is new counts from zero.

Moves. A path that is gone and a path that is new are one move when both hold
the same bytes (CRLF equals LF) and the new path keeps the rule's scope. Each
path takes part in one move for a rule. Paths are taken in byte order.
  - A gone test, of rank 1 or 2, keeps its floor only at a new path of rank
    2 with no folder on it that starts with a dot or is node_modules. So a
    helper moved from one test folder to another is reported as
    test-moved-out: the guard cannot tell whether a runner still collects it.
    The guard reads names only. It knows neither a runner's settings
    (testpaths, ignore patterns) nor the other folders a runner skips by
    default (build, venv, dist, testdata), nor a layout rule such as RSpec
    reading only spec/.
  - A gone workflow keeps its floor at a new workflow path.
  - For a marker rule, a new path in scope is a move, with nothing to count,
    when a gone path in the same scope held its bytes. So a file moved into a
    scope (a disabled workflow switched on, a text file renamed to code)
    counts from zero.
A copy is not a move: the path it came from is still there.

It is a tripwire, not a judge: a reviewer decides on each hit. There is no
in-repo exemption. It does not see a weakened assertion that keeps the count,
a rename with edits (that reports as deleted-test), or markers spelled in a
way the lists below do not name.

Safety: it calls git plumbing only, with a fixed argument list, no shell, and
config that could run a program switched off (core.fsmonitor). It never asks
git for a diff, so no external diff or textconv runs, and it never fetches: in
a partial clone a missing object ends the run with exit 2 (GIT_NO_LAZY_FETCH).
Base content comes from the object store; new content comes from disk.
Symlinks are never followed. It writes nothing (GIT_OPTIONAL_LOCKS=0).

Usage: floor-guard.py [--base <ref>] [--repo <dir>]
Exit:  0 clean, 1 at least one hit, 2 could not run or finish (reason on stderr).

Tests: PYTHONDONTWRITEBYTECODE=1 python3 -m pytest ~/.claude/hooks/tests/test_floor_guard.py -q -p no:cacheprovider
"""
import argparse
import fnmatch
import os
import re
import stat
import subprocess
import sys

DEFAULT_BASES = ["dev", "develop", "development", "integration", "main", "master"]
KINDS = ["deleted-test", "test-moved-out", "skip-added", "suppression-added",
         "assert-removed", "ci-weakened"]

# Each marker is written in two parts, so this file holds no whole marker
# and the guard does not flag its own source.
SKIP_MARKERS = [
    "pytest.mark." + "skip", "pytest" + ".skip(", "unittest" + ".skip",
    "pytest.mark" + ".xfail", "it" + ".skip", "describe" + ".skip",
    "test" + ".skip", "." + "only(", "x" + "it(", "x" + "describe(",
    "t." + "Skip(", "#[" + "ignore", "@" + "Disabled", "@" + "Ignore",
]
SUPPRESSION_MARKERS = [
    "# " + "noqa", "# type: " + "ignore", "# pragma: " + "no cover",
    "pylint: " + "disable", "@ts-" + "ignore", "@ts-" + "expect-error",
    "@ts-" + "nocheck", "eslint-" + "disable", "#[" + "allow(", "no" + "lint",
    "shellcheck " + "disable", "no" + "semgrep", "gitleaks:" + "allow",
    "# " + "nosec",
]


def marker_regex(markers):
    # A marker that starts with a letter must start a word, so a call to
    # sys.exit does not count as the Jasmine one-letter skip.
    parts = [(r"(?<!\w)" if re.match(r"\w", m) else "") + re.escape(m) for m in markers]
    return re.compile("|".join(parts).encode())


SKIP_RE = marker_regex(SKIP_MARKERS)
SUPPRESSION_RE = marker_regex(SUPPRESSION_MARKERS)
CI_RE = re.compile(rb"continue-on-error:[ \t]*" + rb"true|\|\|[ \t]*" + rb"true")
ASSERT_RE = re.compile(rb"\bassert\w*\b|\bexpect\(|\brequire\.\w+\(|\bt\.(?:Error|Fatal|Fail)\w*\(")

TEST_DIRS = {"tests", "test", "__tests__", "spec"}
TEST_GLOBS = ["test_*.py", "*_test.py", "*_test.go", "*_test.rs", "test-*.sh",
              "*_test.sh", "*.bats", "*Test.java", "*Tests.swift",
              "*_spec.rb", "*_test.rb", "test_*.rb"]
TEST_GLOBS += [f"*.{word}.{ext}" for word in ("test", "spec")
               for ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts")]
PROSE_EXTS = (b".md", b".mdx", b".rst", b".txt", b".adoc")


class GuardError(Exception):
    pass


def git(root, *args, stdin=None):
    """Run one git command; return stdout bytes or raise GuardError."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # A partial clone fetches a missing object through the transport its config
    # names, which is a program, and stores what it gets. Never fetch.
    env["GIT_NO_LAZY_FETCH"] = "1"
    cmd = ["git", "-c", "core.fsmonitor=", "-c", "core.quotepath=off", "-C", root, *args]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, env=env)
    except OSError as e:
        raise GuardError(f"cannot run git: {e}")
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise GuardError(f"git {args[0]} failed: {msg}")
    return proc.stdout


def resolve(root, ref):
    try:
        return git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}").decode().strip()
    except GuardError:
        return None


def base_tree(root, commit):
    """Return {path: bytes} for regular files at the commit."""
    blobs = {}
    for entry in git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, oid = meta.split(b" ")
        if kind == b"blob" and mode in (b"100644", b"100755"):
            blobs[path] = oid
    if not blobs:
        return {}
    oids = sorted(set(blobs.values()))
    out = git(root, "cat-file", "--batch", stdin=b"\n".join(oids) + b"\n")
    content, pos = {}, 0
    for _ in oids:
        end = out.index(b"\n", pos)
        header = out[pos:end].split(b" ")
        if len(header) != 3:
            raise GuardError(f"cannot read object: {header[0].decode(errors='replace')}")
        size = int(header[2])
        content[header[0]] = out[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return {path: content[oid] for path, oid in blobs.items()}


def work_tree(root):
    """Return {path: bytes} for regular files on disk that git tracks or would add."""
    listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    broot = os.fsencode(root)
    linked = {}  # directory prefix -> is it a symlink (or not a directory)

    def under_link(path):
        parts = path.split(b"/")[:-1]
        for i in range(1, len(parts) + 1):
            prefix = b"/".join(parts[:i])
            if prefix not in linked:
                try:
                    linked[prefix] = not stat.S_ISDIR(os.lstat(os.path.join(broot, prefix)).st_mode)
                except (FileNotFoundError, NotADirectoryError):
                    linked[prefix] = True
            if linked[prefix]:
                return True
        return False

    files = {}
    for path in set(listed.split(b"\0")):
        if not path or path.endswith(b"/") or under_link(path):
            continue
        full = os.path.join(broot, path)
        try:
            if not stat.S_ISREG(os.lstat(full).st_mode):
                continue
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                files[path] = f.read()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as e:
            raise GuardError(f"cannot read {esc(path)}: {e.strerror}")
    return files


def test_rank(path):
    """2: a runner collects the file by its name. 1: only a test folder is on
    the path. 0: not a test path."""
    parts = path.decode("utf-8", "surrogateescape").split("/")
    if any(fnmatch.fnmatchcase(parts[-1], g) for g in TEST_GLOBS):
        return 2
    return 1 if TEST_DIRS.intersection(parts) else 0


def runner_reads(path):
    """A test name, at a path that no runner skips by default."""
    folders = path.split(b"/")[:-1]
    if any(f.startswith(b".") or f == b"node_modules" for f in folders):
        return False
    return test_rank(path) == 2


def is_workflow(path):
    parts = path.split(b"/")
    return (len(parts) == 3 and parts[:2] == [b".github", b"workflows"]
            and parts[2].endswith((b".yml", b".yaml")))


def is_code(path):
    return not path.lower().endswith(PROSE_EXTS)


def norm(data):
    return data.replace(b"\r\n", b"\n")


def esc(raw):
    """Make bytes safe for one output line: escape controls, backslash and bad UTF-8."""
    out = []
    for ch in raw.decode("utf-8", "surrogateescape"):
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


def count(data, regex):
    return sum(1 for line in data.split(b"\n") if regex.search(line))


def added_lines(old, new, regex):
    """Lines (number, text) of new that match and have no twin in old."""
    pool = {}
    for line in old.split(b"\n"):
        if regex.search(line):
            pool[line.strip()] = pool.get(line.strip(), 0) + 1
    hits = []
    for n, line in enumerate(new.split(b"\n"), 1):
        if regex.search(line):
            key = line.strip()
            if pool.get(key):
                pool[key] -= 1
            else:
                hits.append((n, key))
    return hits


def floor_files(base, gone, fresh_at, in_scope, keeps):
    """Pair each gone floor file with a new path that keeps its floor.

    Yield (path, where) for each one left with no such path. `where` is a new
    path that holds its bytes but does not keep the floor, or None.
    """
    taken = set()
    for p in (p for p in gone if in_scope(p)):
        spots = [q for q in fresh_at.get(norm(base[p]), ()) if q not in taken]
        fit = next((q for q in spots if keeps(q)), None)
        if fit is not None:
            taken.add(fit)
        else:
            yield p, (spots[0] if spots else None)


def check(base, new):
    """Return hits as (kind, path, line or None, detail)."""
    hits = []
    gone = sorted(p for p in base if p not in new)
    fresh = sorted(p for p in new if p not in base)
    fresh_at = {}  # content -> the new paths that hold it, in byte order
    for q in fresh:
        fresh_at.setdefault(norm(new[q]), []).append(q)

    # A test or a workflow that is gone must have moved to where it still counts.
    lost_tests = floor_files(base, gone, fresh_at, in_scope=test_rank,
                             keeps=runner_reads)
    for p, where in lost_tests:
        if where is None:
            hits.append(("deleted-test", p, None, "gone, and no new test path has its content"))
        else:
            hits.append(("test-moved-out", p, None,
                         f"now only at {esc(where)}, which no test runner collects by name"))
    lost_workflows = floor_files(base, gone, fresh_at, in_scope=is_workflow, keeps=is_workflow)
    for p, where in lost_workflows:
        if where is None:
            hits.append(("ci-weakened", p, None, "workflow removed"))
        else:
            hits.append(("ci-weakened", p, None,
                         f"workflow moved to {esc(where)}, where it does not run"))

    for p in sorted(base):
        if test_rank(p) and p in new:
            before, after = count(base[p], ASSERT_RE), count(new[p], ASSERT_RE)
            if after < before:
                hits.append(("assert-removed", p, None, f"base={before} now={after}"))

    for kind, regex, in_scope in (("skip-added", SKIP_RE, is_code),
                                  ("suppression-added", SUPPRESSION_RE, is_code),
                                  ("ci-weakened", CI_RE, is_workflow)):
        moved_from = {}  # content -> how many gone paths in this scope held it
        for q in gone:
            if in_scope(q):
                moved_from[norm(base[q])] = moved_from.get(norm(base[q]), 0) + 1
        for p in sorted(new):
            if not in_scope(p):
                continue
            if p in base:
                old = base[p]
            elif moved_from.get(norm(new[p])):
                moved_from[norm(new[p])] -= 1
                continue  # a move inside the scope: same bytes, new path
            else:
                old = b""
            if count(new[p], regex) > count(old, regex):
                for n, text in added_lines(old, new[p], regex):
                    hits.append((kind, p, n, esc(text[:160])))
    return hits


def run(repo, base_ref):
    try:
        root = git(repo, "rev-parse", "--show-toplevel")
    except GuardError as e:
        raise GuardError(f"not a git work tree: {repo} ({e})")
    root = os.fsdecode(root[:-1] if root.endswith(b"\n") else root)
    if base_ref is not None:
        if base_ref.startswith("-"):
            raise GuardError(f"bad base ref: {base_ref}")
        base = resolve(root, base_ref)
        if base is None:
            raise GuardError(f"base ref does not resolve: {base_ref}")
    else:
        refs = ["refs/remotes/origin/" + b for b in DEFAULT_BASES]
        base = next((oid for oid in (resolve(root, r) for r in refs) if oid), None)
        if base is None:
            raise GuardError("no base ref resolves; tried " + ", ".join(refs) + "; pass --base")
    try:
        merge_base = git(root, "merge-base", base, "HEAD").decode().strip()
    except GuardError as e:
        raise GuardError(f"no merge base between {base_ref or base} and HEAD "
                         f"(a shallow clone or unrelated history?): {e}")
    return check(base_tree(root, merge_base), work_tree(root))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Flag a change that lowers the quality bar.")
    parser.add_argument("--base", help="ref to compare against (default: first of origin/"
                        + ", origin/".join(DEFAULT_BASES) + ")")
    parser.add_argument("--repo", default=".", help="repo directory (default: .)")
    args = parser.parse_args(argv)
    try:
        hits = run(args.repo, args.base)
    except GuardError as e:
        print(f"floor-guard: error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # fail closed: a crash must never read as clean
        print(f"floor-guard: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    hits.sort(key=lambda h: (KINDS.index(h[0]), h[1], h[2] or 0))
    lines = []
    for kind, path, n, detail in hits:
        where = esc(path) + (f":{n}" if n else "")
        lines.append(f"floor-guard: {kind} {where} {detail}")
    if hits:
        counts = {k: sum(1 for h in hits if h[0] == k) for k in sorted({h[0] for h in hits})}
        lines.append("floor-guard: summary " + " ".join(f"{k}={v}" for k, v in counts.items()))
    else:
        lines.append("floor-guard: clean")
    try:
        if sys.stdout is None:  # started with stdout closed (`>&-`)
            raise OSError("stdout is closed")
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
        sys.stdout.flush()
    except (OSError, ValueError) as e:  # a closed pipe: the result was not delivered
        print(f"floor-guard: error: cannot write the result: {e}", file=sys.stderr)
        os._exit(2)  # skip the flush at exit, which would fail again and change the code
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
