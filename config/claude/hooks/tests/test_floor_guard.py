"""Tests for floor-guard.py, the check that flags a lowered quality bar.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m pytest ~/.claude/hooks/tests/test_floor_guard.py -q -p no:cacheprovider

Each test builds a real git repo in a temp directory, commits a base, changes
the working tree, and runs the guard as a process with `python3 -I`. Nothing
is faked. A brute-force reference decision, written here and sharing no code
with the guard, is compared with the guard on a few hundred random change sets.

This file names every marker the guard looks for. Each marker is built from
two parts, so that no line of this file holds a whole marker and the guard
does not flag its own tests.
"""
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "floor-guard.py"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false"]

SKIP = ["pytest.mark." + "skip", "pytest" + ".skip(", "unittest" + ".skip",
        "pytest.mark" + ".xfail", "it" + ".skip", "describe" + ".skip",
        "test" + ".skip", "." + "only(", "x" + "it(", "x" + "describe(",
        "t." + "Skip(", "#[" + "ignore", "@" + "Disabled", "@" + "Ignore"]
SUPP = ["# " + "noqa", "# type: " + "ignore", "# pragma: " + "no cover",
        "pylint: " + "disable", "@ts-" + "ignore", "@ts-" + "expect-error",
        "@ts-" + "nocheck", "eslint-" + "disable", "#[" + "allow(",
        "no" + "lint", "shellcheck " + "disable", "no" + "semgrep",
        "gitleaks:" + "allow", "# " + "nosec"]
CI_ON = "continue-on-error: " + "true"
CI_OR = "run: make " + "|| " + "true"
SKIP1 = "@" + SKIP[0]          # a decorator line
SUPP1 = "import os  " + SUPP[0]
PROSE = [".md", ".mdx", ".rst", ".txt", ".adoc"]


# ---------------------------------------------------------------- helpers

def git(repo, *args, check=True):
    return subprocess.run(GIT + ["-C", str(repo), *args], check=check,
                          capture_output=True)


def put(repo, rel, data):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def commit_all(repo, msg="c"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def make(tmp_path, files, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    for rel, data in files.items():
        put(repo, rel, data)
    commit_all(repo, "base")
    return repo


class Res:
    def __init__(self, proc):
        self.rc = proc.returncode
        self.out = proc.stdout.decode("utf-8")
        self.err = proc.stderr.decode("utf-8", "replace")
        self.lines = self.out.splitlines()
        self.hits = [l for l in self.lines
                     if l != "floor-guard: clean" and not l.startswith("floor-guard: summary")]

    def kinds(self):
        return sorted(l.split(" ")[1] for l in self.hits)


def run(*args, cwd, path=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if path is not None:
        env["PATH"] = path
    proc = subprocess.run([sys.executable, "-I", str(GUARD), *args], cwd=cwd,
                          capture_output=True, env=env, timeout=120)
    return Res(proc)


def guard(repo, *args):
    return run("--base", "HEAD", *args, cwd=repo)


def esc(text):
    out = []
    for ch in text:
        n = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch.isprintable():
            out.append(ch)
        elif n < 0x100:
            out.append("\\x%02x" % n)
        elif n < 0x10000:
            out.append("\\u%04x" % n)
        else:
            out.append("\\U%08x" % n)
    return "".join(out)


def has_hit(res, kind, path, line=None):
    head = f"floor-guard: {kind} {esc(path)}" + (f":{line} " if line else " ")
    return any(l.startswith(head) for l in res.hits)


def assert_clean(res):
    assert res.rc == 0, (res.out, res.err)
    assert res.lines == ["floor-guard: clean"], res.out


def assert_only(res, kind, path, line=None):
    assert res.rc == 1, (res.out, res.err)
    assert has_hit(res, kind, path, line), res.out
    assert set(res.kinds()) == {kind}, res.out
    assert res.lines[-1].startswith("floor-guard: summary "), res.out


# ---------------------------------------------------------------- reference

def ref_word(b):
    return 48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122 or b == 95


def ref_find(line, lit, start=0):
    """Positions where `lit` starts with no word char right before it."""
    out = []
    i = line.find(lit, start)
    while i >= 0:
        if i == 0 or not ref_word(line[i - 1]):
            out.append(i)
        i = line.find(lit, i + 1)
    return out


def ref_has(line, marker):
    m = marker.encode()
    if ref_word(m[0]):
        return bool(ref_find(line, m))
    return m in line


def ref_assert_line(line):
    if ref_find(line, b"assert") or ref_find(line, b"expect("):
        return True
    for i in ref_find(line, b"require."):
        j = i + 8
        k = j
        while k < len(line) and ref_word(line[k]):
            k += 1
        if k > j and line[k:k + 1] == b"(":
            return True
    for i in ref_find(line, b"t."):
        for word in (b"Error", b"Fatal", b"Fail"):
            if line.startswith(word, i + 2):
                k = i + 2 + len(word)
                while k < len(line) and ref_word(line[k]):
                    k += 1
                if line[k:k + 1] == b"(":
                    return True
    return False


def ref_ci_line(line):
    for lit in (b"continue-on-error:", b"||"):
        i = line.find(lit)
        while i >= 0:
            j = i + len(lit)
            while line[j:j + 1] in (b" ", b"\t"):
                j += 1
            if line.startswith(b"true", j):
                return True
            i = line.find(lit, i + 1)
    return False


def ref_rank(path):
    """2: collected by file name. 1: only a test folder on the path. 0: neither."""
    parts = path.split("/")
    n = parts[-1]
    ends = ["_test.py", "_test.go", "_test.rs", "_test.sh", ".bats",
            "Test.java", "Tests.swift", "_spec.rb", "_test.rb"]
    for ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts"):
        ends += [".test." + ext, ".spec." + ext]
    if n.startswith("test_") and (n.endswith(".py") or n.endswith(".rb")):
        return 2
    if n.startswith("test-") and n.endswith(".sh"):
        return 2
    if any(n.endswith(e) for e in ends):
        return 2
    if any(p in ("tests", "test", "__tests__", "spec") for p in parts):
        return 1
    return 0


def ref_runner_reads(path):
    """A test name, at a path no runner skips by default: no folder that starts
    with a dot, and no node_modules."""
    folders = path.split("/")[:-1]
    for f in folders:
        if f.startswith(".") or f == "node_modules":
            return False
    return ref_rank(path) == 2


def ref_ci_path(path):
    parts = path.split("/")
    return (len(parts) == 3 and parts[0] == ".github" and parts[1] == "workflows"
            and (path.endswith(".yml") or path.endswith(".yaml")))


def ref_code_path(path):
    return not any(path.lower().endswith(e) for e in PROSE)


def ref_count(data, pred):
    return sum(1 for line in data.split(b"\n") if pred(line))


def ref_decide(base, new):
    """Return the set of (kind, path) the rules give for two {path: bytes} trees."""
    def same(a, b):
        return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")

    def in_bytes(p):
        return p.encode("utf-8")

    out = set()
    gone = sorted([p for p in base if p not in new], key=in_bytes)
    fresh = sorted([p for p in new if p not in base], key=in_bytes)

    # A gone test must have moved to a new path that a runner collects by
    # name. A folder says nothing about what a runner collects.
    used = []
    for p in gone:
        if ref_rank(p) == 0:
            continue
        twins = [q for q in fresh if q not in used and same(new[q], base[p])]
        pick = None
        for q in twins:
            if ref_runner_reads(q):
                pick = q
                break
        if pick is not None:
            used.append(pick)
        elif twins:
            out.add(("test-moved-out", p))
        else:
            out.add(("deleted-test", p))

    # A gone workflow must have moved to a new workflow path.
    used = []
    for p in gone:
        if not ref_ci_path(p):
            continue
        pick = None
        for q in fresh:
            if q not in used and same(new[q], base[p]) and ref_ci_path(q):
                pick = q
                break
        if pick is not None:
            used.append(pick)
        else:
            out.add(("ci-weakened", p))

    for p in base:
        if p in new and ref_rank(p) > 0:
            if ref_count(new[p], ref_assert_line) < ref_count(base[p], ref_assert_line):
                out.add(("assert-removed", p))

    # Marker counts, each rule inside its own scope. A new path is a move only
    # when a gone path of the same scope held the same bytes; each one once.
    checks = [("skip-added", lambda l: any(ref_has(l, m) for m in SKIP), ref_code_path),
              ("suppression-added", lambda l: any(ref_has(l, m) for m in SUPP), ref_code_path),
              ("ci-weakened", ref_ci_line, ref_ci_path)]
    for kind, pred, scope in checks:
        left = [q for q in gone if scope(q)]
        for p in sorted(new, key=in_bytes):
            if not scope(p):
                continue
            if p in base:
                old = base[p]
            else:
                twin = None
                for q in left:
                    if same(base[q], new[p]):
                        twin = q
                        break
                if twin is not None:
                    left.remove(twin)
                    continue
                old = b""
            if ref_count(new[p], pred) > ref_count(old, pred):
                out.add((kind, p))
    return out


def parse_pairs(res, candidates):
    """Map each output line back to (kind, path), using the paths we wrote."""
    pairs = set()
    escaped = sorted(((esc(c), c) for c in candidates), key=lambda e: -len(e[0]))
    for line in res.hits:
        m = re.match(r"floor-guard: ([a-z-]+) (.*)$", line)
        assert m, line
        rest = m.group(2)
        for e, c in escaped:
            if rest.startswith(e) and rest[len(e):len(e) + 1] in (":", " "):
                pairs.add((m.group(1), c))
                break
        else:
            raise AssertionError(f"no known path in {line!r}")
    return pairs


# ---------------------------------------------------------------- kinds that fire, and their near misses

def test_deleted_test_fires(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1\n"})
    (repo / "tests/test_a.py").unlink()
    res = guard(repo)
    assert_only(res, "deleted-test", "tests/test_a.py")
    assert res.lines[-1] == "floor-guard: summary deleted-test=1"


def test_pure_rename_of_test_does_not_fire(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1\n"})
    (repo / "tests/unit").mkdir()
    (repo / "tests/test_a.py").rename(repo / "tests/unit/test_moved.py")
    assert_clean(guard(repo))


def test_rename_test_with_crlf_change_does_not_fire(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1\n"})
    (repo / "tests/test_a.py").unlink()
    put(repo, "tests/test_b.py", b"def test_x():\r\n    assert 1\r\n")
    assert_clean(guard(repo))


def test_rename_with_edits_is_reported_as_deleted(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1\n"})
    (repo / "tests/test_a.py").unlink()
    put(repo, "tests/test_b.py", "def test_x():\n    assert 1\n    assert 2\n")
    assert_only(guard(repo), "deleted-test", "tests/test_a.py")


def test_test_moved_out_fires(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1\n"})
    (repo / "src").mkdir()
    (repo / "tests/test_a.py").rename(repo / "src/helpers.py")
    res = guard(repo)
    assert_only(res, "test-moved-out", "tests/test_a.py")
    assert "src/helpers.py" in res.hits[0]


def test_move_to_other_test_path_is_not_moved_out(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1\n"})
    (repo / "src").mkdir()
    (repo / "tests/test_a.py").rename(repo / "src/a_test.py")
    assert_clean(guard(repo))


@pytest.mark.parametrize("marker", SKIP)
def test_skip_added_fires(tmp_path, marker):
    repo = make(tmp_path, {"src/a.py": "x = 1\n"})
    put(repo, "src/a.py", f"x = 1\n    {marker} here\n")
    res = guard(repo)
    assert_only(res, "skip-added", "src/a.py", 2)


def test_skip_moved_line_does_not_fire(tmp_path):
    repo = make(tmp_path, {"src/a.py": f"{SKIP1}\ndef f():\n    pass\n"})
    put(repo, "src/a.py", f"def f():\n    pass\n  {SKIP1}  \n")
    assert_clean(guard(repo))


@pytest.mark.parametrize("line", ["sys.exit(0)", "commit.skipper()", "myxdescribe(1)",
                                  "bit.Skip(x)", "x = 'skip'"])
def test_skip_near_misses_do_not_fire(tmp_path, line):
    repo = make(tmp_path, {"src/a.py": "x = 1\n"})
    put(repo, "src/a.py", f"x = 1\n{line}\n")
    assert_clean(guard(repo))


@pytest.mark.parametrize("ext", PROSE)
def test_markers_in_prose_do_not_fire(tmp_path, ext):
    repo = make(tmp_path, {f"docs/guide{ext}": "Intro\n"})
    put(repo, f"docs/guide{ext}", f"Intro\nUse {SKIP1} and {SUPP1}.\n")
    put(repo, f"NEW{ext.upper()}", f"{SKIP1}\n{SUPP1}\n")
    assert_clean(guard(repo))


@pytest.mark.parametrize("marker", SUPP)
def test_suppression_added_fires(tmp_path, marker):
    repo = make(tmp_path, {"src/a.ts": "let x = 1\n"})
    put(repo, "src/a.ts", f"let x = 1\nlet y = 2 // {marker}\n")
    assert_only(guard(repo), "suppression-added", "src/a.ts", 2)


def test_suppression_moved_line_does_not_fire(tmp_path):
    repo = make(tmp_path, {"src/a.py": f"{SUPP1}\nx = 1\n"})
    put(repo, "src/a.py", f"x = 1\n{SUPP1}\n")
    assert_clean(guard(repo))


def test_pure_rename_with_markers_does_not_fire(tmp_path):
    repo = make(tmp_path, {"src/a.py": f"{SUPP1}\n{SKIP1}\n"})
    (repo / "src/a.py").rename(repo / "src/b.py")
    assert_clean(guard(repo))


def test_new_file_counts_from_zero(tmp_path):
    repo = make(tmp_path, {"src/a.py": "x = 1\n"})
    put(repo, "src/new.py", f"x = 2\n{SUPP1}\n")
    assert_only(guard(repo), "suppression-added", "src/new.py", 2)


def test_assert_removed_fires(tmp_path):
    body = "def test_x():\n    assert a\n    self.assertEqual(a, b)\n    expect(c).toBe(1)\n"
    repo = make(tmp_path, {"tests/test_a.py": body})
    put(repo, "tests/test_a.py", body.replace("    assert a\n", ""))
    res = guard(repo)
    assert_only(res, "assert-removed", "tests/test_a.py")
    assert "base=3 now=2" in res.hits[0]


@pytest.mark.parametrize("line", ["require.NoError(t, err)", "t.Errorf(\"bad\")",
                                  "t.Fatal(err)", "t.FailNow()", "assert_that(x)"])
def test_assert_kinds_are_counted(tmp_path, line):
    repo = make(tmp_path, {"pkg/a_test.go": f"func TestX(t *testing.T) {{\n{line}\n}}\n"})
    put(repo, "pkg/a_test.go", "func TestX(t *testing.T) {\n}\n")
    assert_only(guard(repo), "assert-removed", "pkg/a_test.go")


def test_added_or_changed_assertion_does_not_fire(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "def test_x():\n    assert a == 1\n"})
    put(repo, "tests/test_a.py", "def test_x():\n    assert a\n    assert b\n")
    assert_clean(guard(repo))


def test_assert_count_in_non_test_file_is_ignored(tmp_path):
    repo = make(tmp_path, {"src/a.py": "assert a\nassert b\n"})
    put(repo, "src/a.py", "x = 1\n")
    assert_clean(guard(repo))


@pytest.mark.parametrize("line", [f"    {CI_ON}", f"      {CI_OR}", "      run: a ||true"])
def test_ci_weakened_fires(tmp_path, line):
    base = "jobs:\n  t:\n    steps:\n      - run: make test\n"
    repo = make(tmp_path, {".github/workflows/ci.yml": base})
    put(repo, ".github/workflows/ci.yml", base + line + "\n")
    assert_only(guard(repo), "ci-weakened", ".github/workflows/ci.yml", 5)


def test_new_workflow_counts_from_zero(tmp_path):
    repo = make(tmp_path, {"README.md": "x\n"})
    put(repo, ".github/workflows/rel.yaml", f"jobs:\n  r:\n    {CI_ON}\n")
    assert_only(guard(repo), "ci-weakened", ".github/workflows/rel.yaml", 3)


def test_ci_moved_line_and_other_yaml_do_not_fire(tmp_path):
    repo = make(tmp_path, {".github/workflows/ci.yml": f"a: 1\n{CI_ON}\n",
                           "config/ci.yml": "a: 1\n"})
    put(repo, ".github/workflows/ci.yml", f"{CI_ON}\na: 1\ncontinue-on-error: false\n")
    put(repo, "config/ci.yml", f"a: 1\n{CI_ON}\n")
    put(repo, ".github/workflows/sub/x.yml", f"{CI_ON}\n")
    assert_clean(guard(repo))


def test_several_kinds_and_summary(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\n", "tests/test_b.py": "assert 2\n",
                           "src/a.py": "x\n"})
    (repo / "tests/test_a.py").unlink()
    put(repo, "src/a.py", f"x\n{SKIP1}\n{SUPP1}\n")
    res = guard(repo)
    assert res.rc == 1
    assert res.kinds() == ["deleted-test", "skip-added", "suppression-added"]
    assert res.lines[-1] == ("floor-guard: summary deleted-test=1 skip-added=1 "
                             "suppression-added=1")


# ---------------------------------------------------------------- paths

ODD = ["tests/with space.py", "tests/ünïcödé.py", "-lead_test.py", "tests/new\nline.py",
       "tests/ctl\x1b[31m.py", "tests/back\\slash.py", "tests/tab\tname.py"]


@pytest.mark.parametrize("path", ODD)
def test_odd_test_path_deleted(tmp_path, path):
    repo = make(tmp_path, {path: "assert 1\n"})
    (repo / path).unlink()
    res = guard(repo)
    assert_only(res, "deleted-test", path)
    assert all(l.startswith("floor-guard: ") for l in res.lines), res.out
    assert len(res.lines) == 2
    assert "\x1b" not in res.out


@pytest.mark.parametrize("path", [p.replace("tests/", "src/").replace("_test", "") for p in ODD])
def test_odd_code_path_skip_added(tmp_path, path):
    repo = make(tmp_path, {path: "x = 1\n"})
    put(repo, path, f"x = 1\n{SKIP1}\n")
    assert_only(guard(repo), "skip-added", path, 2)


def test_base_with_leading_dash_is_refused(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    res = run("--base", "--output=/tmp/x", cwd=repo)
    assert res.rc == 2 and res.err
    res = run("--base=-p", cwd=repo)
    assert res.rc == 2 and res.err


# ---------------------------------------------------------------- content

def test_crlf_marker_added_fires_and_line_end_change_alone_does_not(tmp_path):
    repo = make(tmp_path, {"src/a.py": b"x = 1\r\n" + SKIP1.encode() + b"\r\n",
                           "src/b.py": b"y = 1\r\n" + SUPP1.encode() + b"\r\n"})
    put(repo, "src/a.py", b"x = 1\r\n" + SKIP1.encode() + b"\r\n" + SKIP1.encode() + b"\r\n")
    put(repo, "src/b.py", b"y = 1\n" + SUPP1.encode() + b"\n")
    assert_only(guard(repo), "skip-added", "src/a.py", 3)


def test_non_utf8_file(tmp_path):
    repo = make(tmp_path, {"src/a.py": b"x = '\xff\xfe'\n"})
    put(repo, "src/a.py", b"x = '\xff\xfe'\n\xe9t\xe9 " + SUPP1.encode() + b"\n")
    res = guard(repo)
    assert_only(res, "suppression-added", "src/a.py", 2)
    assert "\\xe9t\\xe9" in res.hits[0]


def test_binary_file_marker_added_fires(tmp_path):
    repo = make(tmp_path, {"bin/blob.dat": b"\x00\x01\x02\n\x00"})
    put(repo, "bin/blob.dat", b"\x00\x01\x02\n\x00" + SUPP[0].encode() + b"\x00\n")
    res = guard(repo)
    assert_only(res, "suppression-added", "bin/blob.dat", 2)
    assert "\x00" not in res.out


def test_binary_test_file_deleted_fires(tmp_path):
    repo = make(tmp_path, {"tests/fixture.bin": bytes(range(256)) * 4})
    (repo / "tests/fixture.bin").unlink()
    assert_only(guard(repo), "deleted-test", "tests/fixture.bin")


def test_empty_files(tmp_path):
    repo = make(tmp_path, {"tests/test_empty.py": b"", "src/e.py": b""})
    assert_clean(guard(repo))
    put(repo, "src/e.py", SKIP1)
    (repo / "tests/test_empty.py").unlink()
    res = guard(repo)
    assert res.rc == 1
    assert has_hit(res, "skip-added", "src/e.py", 1)
    assert has_hit(res, "deleted-test", "tests/test_empty.py")


def test_no_final_newline(tmp_path):
    repo = make(tmp_path, {"src/a.py": b"x = 1"})
    put(repo, "src/a.py", b"x = 1\n" + SKIP1.encode())
    assert_only(guard(repo), "skip-added", "src/a.py", 2)


def test_very_long_line(tmp_path):
    repo = make(tmp_path, {"src/a.js": b"var x = 1\n"})
    put(repo, "src/a.js", b"var x = 1\n" + b"a" * 3_000_000 + b" // " + SUPP[7].encode() + b"\n")
    res = guard(repo)
    assert_only(res, "suppression-added", "src/a.js", 2)
    assert len(res.hits[0]) < 1000


# ---------------------------------------------------------------- kinds of entry

def test_symlink_is_not_followed(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text(f"{SKIP1}\n{SUPP1}\n")
    repo = make(tmp_path, {"src/a.py": "x\n"})
    os.symlink(outside, repo / "src/link.py")
    os.symlink(outside, repo / "tests_link_test.py")
    assert_clean(guard(repo))


def test_committed_symlink_removed_and_test_replaced_by_symlink(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\n", "keep.py": "x = 1\n"})
    os.symlink("keep.py", repo / "tests/link_test.py")
    commit_all(repo)
    (repo / "tests/link_test.py").unlink()
    assert_clean(guard(repo))
    (repo / "tests/test_a.py").unlink()
    os.symlink("../keep.py", repo / "tests/test_a.py")
    assert_only(guard(repo), "deleted-test", "tests/test_a.py")


def test_file_under_symlinked_directory_is_not_read(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.py").write_text(f"{SKIP1}\n")
    repo = make(tmp_path, {"src/a.py": "x\n"})
    shutil.rmtree(repo / "src")
    os.symlink(outside, repo / "src")
    assert_clean(guard(repo))


def test_submodule_entry(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    head = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},tests/sub")
    git(repo, "commit", "-q", "-m", "sub")
    (repo / "tests/sub").mkdir(parents=True)
    put(repo, "tests/sub/inner.py", f"{SKIP1}\n")  # a checkout of another repo
    assert_clean(guard(repo))
    git(repo, "rm", "-q", "--cached", "tests/sub")
    shutil.rmtree(repo / "tests/sub")
    assert_clean(guard(repo))


def test_deleted_directory_of_tests(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\n", "tests/test_b.py": "assert 2\n",
                           "tests/unit/test_c.py": "assert 3\n", "src/a.py": "x\n"})
    shutil.rmtree(repo / "tests")
    res = guard(repo)
    assert res.rc == 1
    assert res.kinds() == ["deleted-test"] * 3
    for p in ["tests/test_a.py", "tests/test_b.py", "tests/unit/test_c.py"]:
        assert has_hit(res, "deleted-test", p)


def test_file_became_directory(tmp_path):
    repo = make(tmp_path, {"tests/test_x.py": "assert 1\n"})
    (repo / "tests/test_x.py").unlink()
    put(repo, "tests/test_x.py/inner.py", "x = 2\n")
    assert_only(guard(repo), "deleted-test", "tests/test_x.py")


# ---------------------------------------------------------------- state

def test_clean_tree_at_base(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": f"{SKIP1}\nassert 1\n", "src/a.py": f"{SUPP1}\n"})
    assert_clean(guard(repo))


def test_committed_staged_unstaged_and_untracked(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\n", "src/a.py": "a\n",
                           "src/b.py": "b\n"})
    git(repo, "checkout", "-q", "-b", "work")
    put(repo, "src/a.py", f"a\n{SKIP1}\n")
    commit_all(repo, "committed")
    put(repo, "src/b.py", f"b\n{SUPP1}\n")
    git(repo, "add", "src/b.py")
    (repo / "tests/test_a.py").unlink()
    put(repo, ".github/workflows/new.yml", f"{CI_ON}\n")
    res = run("--base", "main", cwd=repo)
    assert res.rc == 1
    assert res.kinds() == ["ci-weakened", "deleted-test", "skip-added", "suppression-added"]


def test_staged_change_reverted_on_disk_is_clean(tmp_path):
    repo = make(tmp_path, {"src/a.py": "a\n"})
    put(repo, "src/a.py", f"a\n{SKIP1}\n")
    git(repo, "add", "src/a.py")
    put(repo, "src/a.py", "a\n")
    assert_clean(guard(repo))


def test_ignored_files_are_not_scanned(tmp_path):
    repo = make(tmp_path, {".gitignore": "build/\n*.log\n"})
    put(repo, "build/gen.py", f"{SKIP1}\n")
    put(repo, "run.log", f"{SUPP1}\n")
    assert_clean(guard(repo))


def test_from_subdirectory_and_with_repo_flag(tmp_path):
    repo = make(tmp_path, {"src/deep/a.py": "x\n", "tests/test_a.py": "assert 1\n"})
    (repo / "tests/test_a.py").unlink()
    res = run("--base", "HEAD", cwd=repo / "src/deep")
    assert_only(res, "deleted-test", "tests/test_a.py")
    other = tmp_path / "elsewhere"
    other.mkdir()
    res = run("--repo", str(repo / "src"), "--base", "HEAD", cwd=other)
    assert_only(res, "deleted-test", "tests/test_a.py")


def test_detached_head(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\nassert 2\n"})
    git(repo, "checkout", "-q", "--detach", "HEAD")
    put(repo, "tests/test_a.py", "assert 1\n")
    commit_all(repo, "weaker")
    assert_only(run("--base", "main", cwd=repo), "assert-removed", "tests/test_a.py")


def test_default_base_order(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\n"})
    first = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    (repo / "tests/test_a.py").unlink()
    commit_all(repo, "drop test")
    second = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    res = run(cwd=repo)
    assert res.rc == 2 and "base" in res.err
    git(repo, "update-ref", "refs/remotes/origin/main", second)
    assert_clean(run(cwd=repo))
    git(repo, "update-ref", "refs/remotes/origin/integration", first)
    assert_only(run(cwd=repo), "deleted-test", "tests/test_a.py")
    git(repo, "update-ref", "refs/remotes/origin/dev", second)
    assert_clean(run(cwd=repo))


# ---------------------------------------------------------------- fail closed

def test_not_a_repo(tmp_path):
    res = run(cwd=tmp_path)
    assert res.rc == 2 and res.err and res.out == ""
    res = run("--repo", str(tmp_path / "missing"), "--base", "HEAD", cwd=tmp_path)
    assert res.rc == 2 and res.err


def test_unknown_base(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    res = run("--base", "refs/remotes/origin/nope", cwd=repo)
    assert res.rc == 2 and "nope" in res.err


def test_no_merge_base(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    git(repo, "checkout", "-q", "--orphan", "other")
    commit_all(repo, "unrelated")
    res = run("--base", "main", cwd=repo)
    assert res.rc == 2 and "merge base" in res.err


def test_shallow_clone_without_merge_base(tmp_path):
    src = make(tmp_path, {"a.py": "x\n"}, name="src")
    commit_all(src, "two")
    git(src, "checkout", "-q", "-b", "dev")
    put(src, "b.py", "y\n")
    commit_all(src, "dev side")
    git(src, "checkout", "-q", "main")
    put(src, "c.py", "z\n")
    commit_all(src, "main side")
    clone = tmp_path / "clone"
    subprocess.run(GIT + ["clone", "-q", "--depth", "1", "--no-single-branch",
                          (src.as_uri()), str(clone)], check=True, capture_output=True)
    res = run(cwd=clone)
    assert res.rc == 2 and "merge base" in res.err


# The mark name is built from parts: this file must hold no whole marker.
@getattr(pytest.mark, "skip" "if")(hasattr(os, "geteuid") and os.geteuid() == 0,
                                   reason="root reads anything")
def test_unreadable_file(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    put(repo, "secret.py", "x\n")
    (repo / "secret.py").chmod(0)
    try:
        res = guard(repo)
    finally:
        (repo / "secret.py").chmod(0o644)
    assert res.rc == 2 and "secret.py" in res.err


def test_git_missing(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    res = run("--base", "HEAD", cwd=repo, path=str(tmp_path / "nobin"))
    assert res.rc == 2 and res.err


def test_missing_object_fails_closed(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": "assert 1\n"})
    oid = git(repo, "rev-parse", "HEAD:tests/test_a.py").stdout.decode().strip()
    (repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
    res = guard(repo)
    assert res.rc == 2 and res.err


# ---------------------------------------------------------------- git config cannot run programs

def snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            digest = ""
            if os.path.isfile(p) and not os.path.islink(p):
                digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns, digest)
    return out


def test_repo_config_programs_never_run_and_nothing_is_written(tmp_path):
    repo = make(tmp_path, {"src/a.py": "x\n", "tests/test_a.py": "assert 1\n"})
    marker = tmp_path / "ran"
    prog = tmp_path / "prog.sh"
    prog.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    prog.chmod(0o755)
    with open(repo / ".git/config", "a") as f:
        f.write(f"[diff]\n\texternal = {prog}\n"
                f"[diff \"x\"]\n\ttextconv = {prog}\n"
                f"[filter \"x\"]\n\tclean = {prog}\n\tsmudge = {prog}\n\tprocess = {prog}\n"
                f"[core]\n\tfsmonitor = {prog}\n\tpager = {prog}\n"
                f"\tuntrackedCache = true\n")
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/attributes").write_text("* diff=x filter=x\n")
    put(repo, "src/a.py", f"x\n{SKIP1}\n")
    put(repo, "src/new.py", "y\n")
    before = snapshot(repo)
    res = guard(repo)
    after = snapshot(repo)
    assert not marker.exists(), "a program from repo config ran"
    assert_only(res, "skip-added", "src/a.py", 2)
    assert before == after, "the guard wrote into the repo"
    # The trap is live: porcelain commands do run the configured programs.
    subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True)
    assert marker.exists()


# ---------------------------------------------------------------- the guard does not flag itself

def test_own_source_and_tests_hold_no_whole_marker(tmp_path):
    files = {"config/claude/hooks/floor-guard.py": GUARD.read_bytes(),
             "config/claude/hooks/tests/test_floor_guard.py": Path(__file__).read_bytes()}
    for rel, data in files.items():
        for n, line in enumerate(data.split(b"\n"), 1):
            for m in SKIP + SUPP:
                assert not ref_has(line, m), f"{rel}:{n} holds {m!r}"
    repo = make(tmp_path, {"README.md": "x\n"})
    for rel, data in files.items():
        put(repo, rel, data)
    assert_clean(guard(repo))


# ---------------------------------------------------------------- reference comparison

NAMES = ["tests/test_a.py", "tests/test_b.py", "tests/unit/helper.py", "src/a_test.go",
         "src/b.py", "src/c.js", "web/x.test.ts", "web/y.spec.jsx", "spec/z_spec.rb",
         "lib/m.rs", "lib/m_test.rs", "scripts/test-run.sh", "README.md", "docs/guide.rst",
         "notes.txt", ".github/workflows/ci.yml", ".github/workflows/rel.yaml",
         ".github/ci.yml", "tests/we ird name.py", "-lead.py", "src/-dash_test.go",
         "src/nl\nx.py", "tests/ünï_test.py", "src/ünï.py", "tests/ta\tb.py",
         "src/FooTest.java", "App/BarTests.swift", "t/suite.bats", "src/__tests__/q.js",
         "src/a.test.tsx",
         # paths one rename away from another scope
         "tests/test_a.py.disabled", "tests/attic/notes.txt", "tests/attic/helper.py",
         "spec/b.py", "test/c.js", ".github/workflows/ci.yml.off", "ci/ci.yml",
         "docs/snip.txt", "src/snip.py", "spec/models/user_spec.rb", "spec/models/user.rb",
         "test/x_test.rb", "tests/a.test.mjs", "tests/a.mjs", ".attic/test_a.py",
         "node_modules/p/b.test.js"]
PLAIN = ["x = 1", "def f():", "    return 2", "", "sys.exit(0)", "commit.skipper()",
         "reassert(x)", "myexpect(1)", "require.(x)", "bit.Fatal(x)", "continue-on-error: false",
         "jobs:", "  # comment"]
ASSERTS = ["assert x == 1", "self.assertEqual(a, b)", "expect(a).toBe(1)",
           "require.NoError(t, err)", "t.Errorf(\"x\")", "t.FailNow()"]
CIS = [CI_ON, CI_OR, "run: a ||true", "run: b ||\ttrue"]
POOL = (PLAIN * 3 + ASSERTS * 2 + CIS + ["  " + m + " z" for m in SKIP]
        + ["y = 1  " + m for m in SUPP])


def rand_content(rng):
    lines = [rng.choice(POOL) for _ in range(rng.randint(0, 6))]
    sep = "\r\n" if rng.random() < 0.2 else "\n"
    data = sep.join(lines).encode()
    if lines and rng.random() < 0.7:
        data += sep.encode()
    r = rng.random()
    if r < 0.08:
        data += b"\xff\xfe junk\n"
    elif r < 0.12:
        data = b"\x00\x01" + data
    return data


def rand_edit(rng, data):
    lines = data.split(b"\n")
    op = rng.randrange(5)
    if op == 0:
        lines.insert(rng.randint(0, len(lines)), rng.choice(POOL).encode())
    elif op == 1 and len(lines) > 1:
        del lines[rng.randrange(len(lines))]
    elif op == 2 and len(lines) > 1:
        lines.insert(rng.randint(0, len(lines) - 1), lines.pop(rng.randrange(len(lines))))
    elif op == 3:
        return data.replace(b"\r\n", b"\n") if b"\r\n" in data else data.replace(b"\n", b"\r\n")
    else:
        lines[rng.randrange(len(lines))] = rng.choice(POOL).encode()
    return b"\n".join(lines)


def rand_case(rng):
    base = {}
    for _ in range(rng.randint(0, 6)):
        base[rng.choice(NAMES)] = rand_content(rng)
    new = dict(base)
    for _ in range(rng.randint(0, 4)):
        free = [n for n in NAMES if n not in new]
        op = rng.randrange(5)
        if op == 0 and free:
            new[rng.choice(free)] = rand_content(rng)
        elif op == 1 and new:
            del new[rng.choice(sorted(new))]
        elif op == 2 and new and free:
            new[rng.choice(free)] = new.pop(rng.choice(sorted(new)))
        elif op == 3 and new and free:
            new[rng.choice(free)] = new[rng.choice(sorted(new))]
        elif new:
            p = rng.choice(sorted(new))
            new[p] = rand_edit(rng, new[p])
    return base, new


def wipe(repo):
    for entry in os.listdir(repo):
        if entry == ".git":
            continue
        p = os.path.join(repo, entry)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


def run_cases(repo, seed, count):
    rng = random.Random(seed)
    failures = []
    for i in range(count):
        base, new = rand_case(rng)
        wipe(repo)
        for p, c in base.items():
            put(repo, p, c)
        commit_all(repo, f"case {i}")
        wipe(repo)
        for p, c in new.items():
            put(repo, p, c)
        res = guard(repo)
        want = ref_decide(base, new)
        got = parse_pairs(res, set(base) | set(new)) if res.rc in (0, 1) else None
        if res.rc != (1 if want else 0) or got != want:
            failures.append((seed, i, base, new, sorted(want), got, res.out, res.err))
            continue
        counts = {}
        for k, _ in [(l.split(" ")[1], l) for l in res.hits]:
            counts[k] = counts.get(k, 0) + 1
        if want:
            summary = "floor-guard: summary " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            if res.lines[-1] != summary:
                failures.append((seed, i, "summary", res.out))
    return failures


def test_matches_reference_on_random_change_sets(tmp_path):
    seeds = [11, 23, 37, 41]
    per_seed = 100
    repos = []
    for s in seeds:
        repo = tmp_path / f"r{s}"
        repo.mkdir()
        git(repo, "init", "-q")
        repos.append(repo)
    with ThreadPoolExecutor(max_workers=len(seeds)) as pool:
        results = list(pool.map(run_cases, repos, seeds, [per_seed] * len(seeds)))
    failures = [f for r in results for f in r]
    assert not failures, failures[:3]


def test_reference_itself_on_known_cases():
    # Guards the reference against drifting into agreement with a broken guard.
    t = "tests/test_a.py"
    assert ref_decide({t: b"assert 1\n"}, {}) == {("deleted-test", t)}
    assert ref_decide({t: b"assert 1\n"}, {"src/a.py": b"assert 1\n"}) == {("test-moved-out", t)}
    assert ref_decide({t: b"assert 1\n"}, {"lib/b_test.py": b"assert 1\n"}) == set()
    assert ref_decide({t: b"assert 1\n"}, {"spec/b.py": b"assert 1\n"}) == {("test-moved-out", t)}
    assert ref_decide({t: b"assert 1\n"}, {t + ".off": b"assert 1\n"}) == {("test-moved-out", t)}
    h = "tests/helper.py"
    assert ref_decide({h: b"x\n"}, {"spec/h.py": b"x\n"}) == {("test-moved-out", h)}
    assert ref_decide({h: b"x\n"}, {"tests/unit/helper.py": b"x\n"}) == {("test-moved-out", h)}
    assert ref_decide({h: b"x\n"}, {"lib/helper_test.py": b"x\n"}) == set()
    assert ref_decide({h: b"x\n", t: b"x\n"}, {"lib/x_test.py": b"x\n", "spec/h.py": b"x\n"}) == {
        ("test-moved-out", t)}
    assert ref_decide({t: b"x\n"}, {".attic/test_a.py": b"x\n"}) == {("test-moved-out", t)}
    assert ref_decide({t: b"x\n"}, {"node_modules/p/test_a.py": b"x\n"}) == {("test-moved-out", t)}
    assert ref_decide({".attic/test_a.py": b"x\n"}, {}) == {("deleted-test", ".attic/test_a.py")}
    r = "spec/models/user_spec.rb"
    assert ref_decide({r: b"x\n"}, {"spec/models/user.rb": b"x\n"}) == {("test-moved-out", r)}
    assert ref_decide({r: b"x\n"}, {"spec/user_spec.rb": b"x\n"}) == set()
    assert ref_decide({"tests/a.test.mjs": b"x\n"}, {"tests/a.mjs": b"x\n"}) == {
        ("test-moved-out", "tests/a.test.mjs")}
    assert ref_decide({h: b"x\n", t: b"x\n"}, {t: b"x\n"}) == {("deleted-test", h)}
    assert ref_decide({t: b" \n"}, {}) == {("deleted-test", t)}
    w, off = ".github/workflows/c.yml", ".github/workflows/c.yml.off"
    assert ref_decide({w: b"jobs:\n"}, {}) == {("ci-weakened", w)}
    assert ref_decide({w: b"jobs:\n"}, {"ci/c.yml": b"jobs:\n"}) == {("ci-weakened", w)}
    assert ref_decide({w: b"jobs:\n"}, {".github/workflows/d.yaml": b"jobs:\n"}) == set()
    assert ref_decide({off: CI_ON.encode()}, {w: CI_ON.encode()}) == {("ci-weakened", w)}
    assert ref_decide({"a.txt": SKIP1.encode()}, {"a.py": SKIP1.encode()}) == {("skip-added", "a.py")}
    assert ref_decide({"a.py": SKIP1.encode()}, {"b.py": SKIP1.encode()}) == set()
    assert ref_decide({"a.py": SKIP1.encode()},
                      {"b.py": SKIP1.encode(), "c.py": SKIP1.encode()}) == {("skip-added", "c.py")}
    assert ref_decide({"a.py": SKIP1.encode()},
                      {"a.py": SKIP1.encode(), "b.py": SKIP1.encode()}) == {("skip-added", "b.py")}
    assert ref_decide({t: b"assert 1\nassert 2\n"}, {t: b"assert 1\n"}) == {("assert-removed", t)}
    assert ref_decide({}, {"a.py": SKIP1.encode()}) == {("skip-added", "a.py")}
    assert ref_decide({}, {"a.md": SKIP1.encode()}) == set()
    assert ref_decide({"a.py": b"x"}, {"a.py": b"x\n" + SUPP1.encode()}) == {("suppression-added", "a.py")}
    assert ref_decide({}, {".github/workflows/c.yml": CI_ON.encode()}) == {
        ("ci-weakened", ".github/workflows/c.yml")}
    assert ref_decide({}, {"a.py": b"sys.exit(1)\ncommit.skipper\n"}) == set()


def test_closed_stdout_fails_closed(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.Popen([sys.executable, "-I", str(GUARD), "--base", "HEAD"], cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    proc.stdout.close()  # the guard writes only after all git calls, so this comes first
    err = proc.stderr.read().decode()
    assert proc.wait(timeout=60) == 2, err


def test_stdout_closed_at_start_fails_closed(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(["sh", "-c", 'exec "$0" -I "$1" --base HEAD >&-',
                           sys.executable, str(GUARD)],
                          cwd=repo, capture_output=True, env=env, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert b"cannot write the result" in proc.stderr


# ---------------------------------------------------------------- a move does not carry a file across a scope

TEST_BODY = "def test_x():\n    assert 1\n"
WF = "jobs:\n  t:\n    steps:\n      - run: make test\n"


def move(repo, old, new):
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    (repo / old).rename(repo / new)


@pytest.mark.parametrize("src", ["ci/x.yml", ".github/workflows/x.yml.disabled",
                                 ".github/workflows/sub/x.yml", "docs/x.txt"])
def test_file_moved_into_workflows_counts_from_zero(tmp_path, src):
    repo = make(tmp_path, {src: WF + f"    {CI_ON}\n      {CI_OR}\n", "README.md": "x\n"})
    move(repo, src, ".github/workflows/x.yml")
    res = guard(repo)
    assert res.rc == 1, res.out
    assert has_hit(res, "ci-weakened", ".github/workflows/x.yml", 5), res.out
    assert has_hit(res, "ci-weakened", ".github/workflows/x.yml", 6), res.out
    assert set(res.kinds()) == {"ci-weakened"}


def test_workflow_renamed_among_workflows_does_not_fire(tmp_path):
    repo = make(tmp_path, {".github/workflows/ci.yml": WF + f"    {CI_ON}\n"})
    move(repo, ".github/workflows/ci.yml", ".github/workflows/checks.yaml")
    assert_clean(guard(repo))


def test_workflow_removed_fires(tmp_path):
    repo = make(tmp_path, {".github/workflows/ci.yml": WF, "a.py": "x\n"})
    (repo / ".github/workflows/ci.yml").unlink()
    res = guard(repo)
    assert_only(res, "ci-weakened", ".github/workflows/ci.yml")
    assert "workflow removed" in res.hits[0]


@pytest.mark.parametrize("dest", ["ci/ci.yml", ".github/workflows/ci.yml.off",
                                  ".github/workflows/old/ci.yml", ".github/ci.yml"])
def test_workflow_moved_to_where_it_does_not_run_fires(tmp_path, dest):
    repo = make(tmp_path, {".github/workflows/ci.yml": WF})
    move(repo, ".github/workflows/ci.yml", dest)
    res = guard(repo)
    assert_only(res, "ci-weakened", ".github/workflows/ci.yml")
    assert f"moved to {dest}" in res.hits[0]


def test_strict_workflow_swapped_for_a_lax_one_fires(tmp_path):
    repo = make(tmp_path, {".github/workflows/ci.yml": WF,
                           ".github/workflows/lax.yml.off": WF + f"    {CI_ON}\n"})
    (repo / ".github/workflows/ci.yml").unlink()
    move(repo, ".github/workflows/lax.yml.off", ".github/workflows/main.yml")
    res = guard(repo)
    assert res.rc == 1
    assert has_hit(res, "ci-weakened", ".github/workflows/ci.yml"), res.out
    assert has_hit(res, "ci-weakened", ".github/workflows/main.yml", 5), res.out


def test_empty_workflow_removed_fires_too(tmp_path):
    repo = make(tmp_path, {".github/workflows/ci.yml": "\n", "a.py": "x\n"})
    (repo / ".github/workflows/ci.yml").unlink()
    assert_only(guard(repo), "ci-weakened", ".github/workflows/ci.yml")


@pytest.mark.parametrize("src", ["docs/snip.txt", "notes/snip.md", "snip.rst"])
def test_prose_renamed_to_code_counts_from_zero(tmp_path, src):
    repo = make(tmp_path, {src: f"{SKIP1}\n{SUPP1}\n", "a.py": "x\n"})
    move(repo, src, "src/snip.py")
    res = guard(repo)
    assert res.rc == 1, res.out
    assert has_hit(res, "skip-added", "src/snip.py", 1), res.out
    assert has_hit(res, "suppression-added", "src/snip.py", 2), res.out


def test_code_renamed_to_prose_and_back_is_not_a_free_pass(tmp_path):
    repo = make(tmp_path, {"src/a.py": "x\n", "docs/a.txt": f"{SKIP1}\n"})
    (repo / "src/a.py").unlink()
    put(repo, "src/b.py", f"{SKIP1}\n")     # the bytes of the prose file, at a code path
    res = guard(repo)
    assert_only(res, "skip-added", "src/b.py", 1)


def test_move_plus_copy_counts_the_copy(tmp_path):
    repo = make(tmp_path, {"src/a.py": f"{SKIP1}\n"})
    move(repo, "src/a.py", "src/b.py")
    put(repo, "src/c.py", f"{SKIP1}\n")
    assert_only(guard(repo), "skip-added", "src/c.py", 1)


def test_copy_of_a_file_that_stays_counts_from_zero(tmp_path):
    repo = make(tmp_path, {"src/a.py": f"{SUPP1}\n"})
    put(repo, "src/b.py", f"{SUPP1}\n")
    assert_only(guard(repo), "suppression-added", "src/b.py", 1)


@pytest.mark.parametrize("dest", ["tests/test_a.py.disabled", "tests/attic/notes.txt",
                                  "tests/test_a.txt", "spec/a.py", "tests/a.py"])
def test_test_renamed_so_that_no_runner_collects_it_fires(tmp_path, dest):
    repo = make(tmp_path, {"tests/test_a.py": TEST_BODY, "tests/test_b.py": "assert 2\n"})
    move(repo, "tests/test_a.py", dest)
    res = guard(repo)
    assert_only(res, "test-moved-out", "tests/test_a.py")
    assert dest in res.hits[0]


@pytest.mark.parametrize("old,new,clean", [
    # A folder does not say what a runner collects, so only a new path with a
    # test name keeps the floor of a file that was a test by its folder.
    ("tests/helper.py", "lib/helper_test.py", True),
    ("tests/foo.rs", "tests/foo_test.rs", True),
    ("tests/helper.py", "tests/unit/helper.py", False),
    ("tests/helper.py", "spec/support.py", False),
    ("tests/foo.rs", "tests/sub/foo.rs", False),          # cargo: no longer a test crate
    ("tests/helper.py", "tests/helper.py.bak", False),
    ("tests/run.sh", "tests/run.txt", False),
    ("tests/helper.py", "src/helper.py", False),
    ("src/__tests__/q.js", "src/__tests__/q.js.off", False),
    ("tests/Makefile", "tests/unit/Makefile", False),
])
def test_file_that_is_a_test_only_by_its_folder_cannot_move_quietly(tmp_path, old, new, clean):
    repo = make(tmp_path, {old: "x = 1\n", "a.py": "x\n"})
    move(repo, old, new)
    res = guard(repo)
    if clean:
        assert_clean(res)
    else:
        assert_only(res, "test-moved-out", old)


def test_duplicate_test_deleted_fires_though_a_copy_stays(tmp_path):
    repo = make(tmp_path, {"pkg_a/tests/test_x.py": TEST_BODY, "pkg_b/tests/test_x.py": TEST_BODY})
    (repo / "pkg_b/tests/test_x.py").unlink()
    assert_only(guard(repo), "deleted-test", "pkg_b/tests/test_x.py")


def test_two_tests_with_the_same_content_need_two_new_paths(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": TEST_BODY, "tests/test_b.py": TEST_BODY})
    move(repo, "tests/test_a.py", "tests/unit/test_a.py")
    (repo / "tests/test_b.py").unlink()
    assert_only(guard(repo), "deleted-test", "tests/test_b.py")
    put(repo, "tests/unit/test_b.py", TEST_BODY)
    assert_clean(guard(repo))


@pytest.mark.parametrize("old,new", [
    ("spec/models/user_spec.rb", "spec/models/user.rb"),      # RSpec
    ("test/x_test.rb", "test/x.rb"),                          # Minitest
    ("test/test_x.rb", "test/x.rb"),
    ("tests/a.test.mjs", "tests/a.mjs"),                      # Vitest, Node
    ("tests/a.spec.cts", "tests/a.cts"),
])
def test_rename_that_drops_the_test_name_fires(tmp_path, old, new):
    repo = make(tmp_path, {old: "x = 1\n", "a.py": "x\n"})
    move(repo, old, new)
    res = guard(repo)
    assert_only(res, "test-moved-out", old)
    assert new in res.hits[0]


@pytest.mark.parametrize("new", [".attic/test_a.py", "tests/.old/test_a.py",
                                 "node_modules/pkg/test_a.py", "web/node_modules/test_a.py"])
def test_test_moved_to_a_folder_that_runners_skip_fires(tmp_path, new):
    repo = make(tmp_path, {"tests/test_a.py": TEST_BODY, "a.py": "x\n"})
    put(repo, ".gitignore", "")          # node_modules must not be ignored here
    commit_all(repo, "ignore file")
    move(repo, "tests/test_a.py", new)
    res = guard(repo)
    assert_only(res, "test-moved-out", "tests/test_a.py")
    assert new in res.hits[0]


def test_test_that_lives_in_a_dot_folder_is_still_a_test_when_it_goes(tmp_path):
    repo = make(tmp_path, {".ci/test_a.py": TEST_BODY, "a.py": "x\n"})
    (repo / ".ci/test_a.py").unlink()
    assert_only(guard(repo), "deleted-test", ".ci/test_a.py")


@pytest.mark.parametrize("old,new", [
    ("spec/models/user_spec.rb", "spec/user_spec.rb"),
    ("test/x_test.rb", "test/unit/y_test.rb"),
    ("tests/a.test.mjs", "src/a.spec.mts"),
])
def test_rename_that_keeps_a_test_name_does_not_fire(tmp_path, old, new):
    repo = make(tmp_path, {old: "x = 1\n", "a.py": "x\n"})
    move(repo, old, new)
    assert_clean(guard(repo))


@pytest.mark.parametrize("body", ["", "\n", "  \n\t\n"])
def test_empty_test_file_deleted_fires_though_other_empty_files_stay(tmp_path, body):
    repo = make(tmp_path, {"tests/__init__.py": body, "tests/unit/__init__.py": body,
                           "a.py": "x\n"})
    (repo / "tests/__init__.py").unlink()
    assert_only(guard(repo), "deleted-test", "tests/__init__.py")


# ---------------------------------------------------------------- a partial clone does not fetch

def test_partial_clone_does_not_fetch_and_fails_closed(tmp_path):
    src = make(tmp_path, {"tests/test_a.py": TEST_BODY, "a.py": "x\n"}, name="src")
    git(src, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "clone"
    subprocess.run(GIT + ["-c", "protocol.file.allow=always", "clone", "-q", "--filter=blob:none",
                          "--no-checkout", src.as_uri(), str(clone)],
                   check=True, capture_output=True)
    marker = tmp_path / "fetched"
    prog = tmp_path / "upload.sh"
    prog.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    prog.chmod(0o755)
    git(clone, "config", "remote.origin.uploadpack", str(prog))
    before = snapshot(clone)
    res = guard(clone)
    assert res.rc == 2 and res.err, (res.out, res.err)
    assert not marker.exists(), "the guard fetched through a program from repo config"
    assert snapshot(clone) == before, "the guard wrote into the repo"
    # The trap is live: reading a missing blob the plain way does fetch.
    git(clone, "cat-file", "-p", "HEAD:a.py", check=False)
    assert marker.exists()


# ---------------------------------------------------------------- reading the two trees

def test_criss_cross_history_checks_every_merge_base(tmp_path):
    # dev and the work branch each merged the other's parent, so they have two
    # best common ancestors. Only one of them holds the test.
    repo = make(tmp_path, {"r.txt": "r\n"})
    git(repo, "branch", "-m", "dev")
    git(repo, "checkout", "-q", "-b", "A")
    put(repo, "tests/test_a.py", TEST_BODY)
    commit_all(repo, "a")
    git(repo, "checkout", "-q", "dev")
    git(repo, "checkout", "-q", "-b", "B")
    put(repo, "b.txt", "b\n")
    commit_all(repo, "b")
    git(repo, "checkout", "-q", "-b", "X", "A")
    git(repo, "merge", "-q", "--no-edit", "B")
    git(repo, "checkout", "-q", "-b", "Y", "B")
    git(repo, "merge", "-q", "--no-edit", "A")
    git(repo, "branch", "-f", "dev", "X")
    git(repo, "checkout", "-q", "Y")
    bases = git(repo, "merge-base", "--all", "dev", "HEAD").stdout.split()
    assert len(bases) == 2, "the history must have two merge bases for this test"
    assert_clean(run("--base", "dev", cwd=repo))
    git(repo, "rm", "-q", "tests/test_a.py")
    commit_all(repo, "del")
    assert_only(run("--base", "dev", cwd=repo), "deleted-test", "tests/test_a.py")


@getattr(pytest.mark, "skip" "if")(hasattr(os, "geteuid") and os.geteuid() == 0,
                                   reason="root reads anything")
def test_folder_that_git_cannot_open_fails_closed(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    put(repo, "newmod/b.py", f"{SKIP1}\ndef f(): pass\n")
    os.chmod(repo / "newmod", 0)
    try:
        res = guard(repo)
    finally:
        os.chmod(repo / "newmod", 0o755)
    assert res.rc == 2 and "newmod" in res.err, (res.out, res.err)
    assert_only(guard(repo), "skip-added", "newmod/b.py", 1)


def test_ambiguous_base_name_fails_closed(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": TEST_BODY})
    git(repo, "branch", "-m", "work")
    git(repo, "branch", "dev")
    put(repo, "x.py", "x\n")
    commit_all(repo, "later")
    git(repo, "tag", "dev")            # the tag and the branch now name two commits
    (repo / "tests/test_a.py").unlink()
    res = run("--base", "dev", cwd=repo)
    assert res.rc == 2 and "ambiguous" in res.err, (res.out, res.err)
    assert_only(run("--base", "refs/heads/dev", cwd=repo), "deleted-test", "tests/test_a.py")


def test_executable_test_script_deleted_fires(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    put(repo, "tests/test-run.sh", "#!/bin/sh\nexit 0\n")
    os.chmod(repo / "tests/test-run.sh", 0o755)
    commit_all(repo, "script")
    mode = git(repo, "ls-files", "-s", "tests/test-run.sh").stdout.split()[0]
    assert mode == b"100755"
    (repo / "tests/test-run.sh").unlink()
    assert_only(guard(repo), "deleted-test", "tests/test-run.sh")


def test_unexpected_git_output_fails_closed(tmp_path):
    """A crash inside the guard must end as exit 2, never as clean."""
    repo = make(tmp_path, {"tests/test_a.py": TEST_BODY})
    (repo / "tests/test_a.py").unlink()
    fake = tmp_path / "fakebin"
    fake.mkdir()
    real = shutil.which("git")
    (fake / "git").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = ls-tree ] && { printf "garbage"; exit 0; }; done\n'
        f'exec "{real}" "$@"\n')
    (fake / "git").chmod(0o755)
    res = run("--base", "HEAD", cwd=repo, path=f"{fake}{os.pathsep}{os.environ['PATH']}")
    assert res.rc == 2 and "internal error" in res.err, (res.out, res.err)
    assert res.out == ""


def test_git_that_never_answers_is_stopped(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    (repo / "sub").mkdir()
    os.mkfifo(repo / "sub" / ".gitignore")     # git waits on it for ever
    res = run("--base", "HEAD", "--git-timeout", "3", cwd=repo)
    assert res.rc == 2 and "did not finish" in res.err, (res.out, res.err)


def name_forms_are_one_file(folder):
    """True on a disk that takes the two Unicode forms of a name as one file."""
    nfd = folder / "probe-é"
    nfd.write_text("x")
    same = (folder / "probe-é").exists()
    nfd.unlink()
    return same


def test_name_listed_in_two_unicode_forms_is_one_file(tmp_path):
    if not name_forms_are_one_file(tmp_path):
        getattr(pytest, "sk" "ip")("this disk keeps the two forms of a name apart")
    body = TEST_BODY
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "core.precomposeunicode", "false")
    put(repo, "tests/test_é.py", body)            # decomposed form in the index
    put(repo, "tests/old/test_dup.py", body)
    put(repo, "src/café.py", f"{SKIP1}\n")
    commit_all(repo, "base")
    git(repo, "config", "core.precomposeunicode", "true")
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    assert listed.count(b"test_") > 2, "git must list the name twice for this test"
    assert_clean(guard(repo))                             # no marker counted twice
    (repo / "tests/old/test_dup.py").unlink()
    assert_only(guard(repo), "deleted-test", "tests/old/test_dup.py")


def test_hard_link_under_another_name_is_a_copy_not_a_twin(tmp_path):
    repo = make(tmp_path, {"src/a.py": f"{SKIP1}\n"})
    os.link(repo / "src/a.py", repo / "src/b.py")
    assert_only(guard(repo), "skip-added", "src/b.py", 1)


def branch_that_deleted_a_test(tmp_path):
    """origin/main has no test; the branch adds one and deletes it again. So
    against origin/main the run is clean, and a guard that wrongly goes on to
    origin/main shows itself by saying so."""
    repo = make(tmp_path, {"a.py": "x\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    put(repo, "tests/test_a.py", TEST_BODY)
    commit_all(repo, "t")
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "tests/test_a.py").unlink()
    commit_all(repo, "del")
    assert_clean(run(cwd=repo))
    return repo


BROKEN_REFS = {
    "an id of no object": "1234567890123456789012345678901234567890\n",
    "text that is no id": "not an id\n",
    "nothing": "",
    "a ref that is not there": "ref: refs/remotes/origin/nowhere\n",
    "a tree": None,
}


@pytest.mark.parametrize("what", sorted(BROKEN_REFS))
def test_broken_default_base_ref_fails_closed(tmp_path, what):
    # origin/dev is there but names no commit. The guard must not go on to
    # origin/main as if origin/dev were not there.
    repo = branch_that_deleted_a_test(tmp_path)
    text = BROKEN_REFS[what]
    if text is None:
        text = git(repo, "rev-parse", "HEAD^{tree}").stdout.decode()
    ref = repo / ".git/refs/remotes/origin/dev"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(text)
    res = run(cwd=repo)
    assert res.rc == 2 and "origin/dev" in res.err, (res.out, res.err)
    assert res.out == ""


def test_broken_named_base_fails_closed(tmp_path):
    repo = branch_that_deleted_a_test(tmp_path)
    ref = repo / ".git/refs/remotes/origin/dev"
    ref.write_text("1234567890123456789012345678901234567890\n")
    res = run("--base", "origin/dev", cwd=repo)
    assert res.rc == 2 and "does not resolve" in res.err, (res.out, res.err)
    assert res.out == ""


FAILED_LOOKUPS = {
    "killed": 'kill -9 $$',
    "exit 1, silent": 'exit 1',
    "exit 3, silent": 'exit 3',
    "exit 0 with a warning": 'echo "warning: odd" >&2; exit 0',
    "a git with no such option": 'echo "error: unknown option" >&2; exit 129',
}


@pytest.mark.parametrize("how", sorted(FAILED_LOOKUPS))
def test_default_base_lookup_that_fails_is_not_read_as_no_such_ref(tmp_path, how):
    # origin/dev is a good ref, and the lookup of it fails. That is neither
    # "not there" (go on to origin/main: clean) nor "there" (one hit).
    repo = branch_that_deleted_a_test(tmp_path)
    git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD~1")
    assert_only(run(cwd=repo), "deleted-test", "tests/test_a.py")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    real = shutil.which("git")
    (fake / "git").write_text(
        "#!/bin/sh\n"
        'last=; for a in "$@"; do last=$a; done\n'
        f'[ "$last" = refs/remotes/origin/dev ] && {{ {FAILED_LOOKUPS[how]}; }}\n'
        f'exec "{real}" "$@"\n')
    (fake / "git").chmod(0o755)
    res = run(cwd=repo, path=f"{fake}{os.pathsep}{os.environ['PATH']}")
    assert res.rc == 2 and "origin/dev" in res.err, (res.out, res.err)
    assert res.out == ""


def test_default_base_skips_a_ref_that_is_not_there(tmp_path):
    repo = make(tmp_path, {"tests/test_a.py": TEST_BODY})
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/dev/topic", "HEAD")   # a longer name only
    (repo / "tests/test_a.py").unlink()
    assert_only(run(cwd=repo), "deleted-test", "tests/test_a.py")


def letter_case_makes_two_files(folder):
    (folder / "probe-a").write_text("x")
    two = not (folder / "probe-A").exists()
    (folder / "probe-a").unlink()
    return two


def test_untracked_file_whose_name_differs_only_in_case_is_its_own_file(tmp_path):
    if not letter_case_makes_two_files(tmp_path):
        getattr(pytest, "sk" "ip")("this disk takes a.py and A.py as one file")
    repo = make(tmp_path, {"src/a.py": "x\n"})
    put(repo, "src/A.py", f"{SKIP1}\n")
    assert_only(guard(repo), "skip-added", "src/A.py", 1)
