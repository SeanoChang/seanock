#!/usr/bin/env python3
"""floor-guard: flag a change that lowers the quality bar.

It compares the tree at merge-base(<base>, HEAD) with the working tree as it
is on disk: committed, staged and unstaged changes to tracked files, plus
untracked files that git does not ignore. Loosening is loud; tightening is
silent. It reports six kinds:

  deleted-test       a test file at the base whose content no test-path file
                     now has (a pure move to another test path is fine)
  test-moved-out     a test file whose content now lives only at a path that
                     a test runner does not collect
  skip-added         more lines with a skip marker in a file than at the base
  suppression-added  more lines with a lint or type suppression than at the base
  assert-removed     fewer assertion lines in a test file than at the base
  ci-weakened        more lines that let a workflow step fail silently

Counts are per file and per kind, so a line moved inside a file is not a hit.
A new file counts from zero. A file whose exact content was at a path that is
now gone counts as moved, not new. Skip and suppression counts ignore prose
files (.md, .mdx, .rst, .txt, .adoc).

It is a tripwire, not a judge: a reviewer decides on each hit. There is no
in-repo exemption. It does not see a weakened assertion that keeps the count,
a rename with edits (that reports as deleted-test), or markers spelled in a
way the lists below do not name.

Safety: it calls git plumbing only, with a fixed argument list, no shell, and
config that could run a program switched off (core.fsmonitor). It never asks
git for a diff, so no external diff or textconv runs. Base content comes from
the object store; new content comes from disk. Symlinks are never followed.
It writes nothing (GIT_OPTIONAL_LOCKS=0).

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
TEST_GLOBS = ["test_*.py", "*_test.py", "*_test.go", "*.test.js", "*.test.jsx",
              "*.test.ts", "*.test.tsx", "*.spec.js", "*.spec.jsx", "*.spec.ts",
              "*.spec.tsx", "*_test.rs", "test-*.sh", "*_test.sh", "*.bats",
              "*Test.java", "*Tests.swift"]
PROSE_EXTS = (b".md", b".mdx", b".rst", b".txt", b".adoc")


class GuardError(Exception):
    pass


def git(root, *args, stdin=None):
    """Run one git command; return stdout bytes or raise GuardError."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
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


def is_test(path):
    parts = path.decode("utf-8", "surrogateescape").split("/")
    if TEST_DIRS.intersection(parts):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], g) for g in TEST_GLOBS)


def is_workflow(path):
    parts = path.split(b"/")
    return (len(parts) == 3 and parts[:2] == [b".github", b"workflows"]
            and parts[2].endswith((b".yml", b".yaml")))


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


def check(base, new):
    """Return hits as (kind, path, line or None, detail)."""
    hits = []
    new_tests = {norm(c) for p, c in new.items() if is_test(p)}
    new_where = {}
    for p, c in sorted(new.items()):
        new_where.setdefault(norm(c), p)
    for p, c in base.items():
        if not is_test(p):
            continue
        if p in new:
            before, after = count(c, ASSERT_RE), count(new[p], ASSERT_RE)
            if after < before:
                hits.append(("assert-removed", p, None, f"base={before} now={after}"))
        elif norm(c) not in new_tests:
            if norm(c) in new_where:
                hits.append(("test-moved-out", p, None,
                             f"now only at {esc(new_where[norm(c)])}, not a test path"))
            else:
                hits.append(("deleted-test", p, None, "no test file has this content"))
    gone = {norm(c) for p, c in base.items() if p not in new}
    for p, c in new.items():
        if p in base:
            old = base[p]
        elif norm(c) in gone:
            continue  # a pure move: same content, new path
        else:
            old = b""
        rules = []
        if not p.lower().endswith(PROSE_EXTS):
            rules += [("skip-added", SKIP_RE), ("suppression-added", SUPPRESSION_RE)]
        if is_workflow(p):
            rules.append(("ci-weakened", CI_RE))
        for kind, regex in rules:
            if count(c, regex) > count(old, regex):
                for n, text in added_lines(old, c, regex):
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
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
        sys.stdout.flush()
    except OSError as e:  # a closed pipe: the result was not delivered
        print(f"floor-guard: error: cannot write the result: {e}", file=sys.stderr)
        os._exit(2)  # skip the flush at exit, which would fail again and change the code
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
