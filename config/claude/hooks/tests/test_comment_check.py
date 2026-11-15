"""Tests for comment-check.py, the advice that lists comments left behind by a code change.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m pytest config/claude/hooks/tests/test_comment_check.py -q -p no:cacheprovider

Each test builds a real git repo in a temp directory, commits a base on main
and a change on a branch, and runs the check as a process with `python3 -I`.
Nothing is faked. A plain reference decision, written here and sharing no code
with the check, is compared with it on a few hundred random files and edits.

COMMENT_CHECK_UNDER_TEST names another copy of the script to test (a mutant).
Lint markers are written in two parts, so that floor-guard does not read this
file as one that adds a suppression.
"""
import hashlib
import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHECK = Path(os.environ.get("COMMENT_CHECK_UNDER_TEST")
             or Path(__file__).resolve().parent.parent / "comment-check.py")
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false",
       "-c", "core.autocrlf=false"]


def git(repo, *args, check=True):
    return subprocess.run(GIT + ["-C", str(repo), *args], check=check,
                          capture_output=True, timeout=60)


def put(repo, rel, data):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def commit_all(repo, msg="c"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def make(tmp_path, files, name="repo"):
    """A repo with `files` committed on main, then a branch `work` checked out."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    for rel, data in files.items():
        put(repo, rel, data)
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "work")
    return repo


class Res:
    def __init__(self, proc):
        self.rc = proc.returncode
        self.out = proc.stdout.decode("utf-8")
        self.err = proc.stderr.decode("utf-8", "replace")
        self.lines = self.out.splitlines()
        self.hits = [l for l in self.lines if l.startswith("stale-comment ")]


def run(*args, cwd, path=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra_env or {})
    if path is not None:
        env["PATH"] = path
    proc = subprocess.run([sys.executable, "-I", str(CHECK), *args], cwd=cwd,
                          capture_output=True, env=env, timeout=120)
    return Res(proc)


def check(repo, base="main"):
    return run("--repo", str(repo), "--base", base, cwd=repo)


def change(repo, files, msg="change"):
    for rel, data in files.items():
        put(repo, rel, data)
    commit_all(repo, msg)


def assert_clean(res):
    assert res.rc == 0, (res.out, res.err)
    assert res.lines == ["comment-check: clean"], res.out


def assert_hits(res, expected):
    """expected: the hit lines, in order, without the summary."""
    assert res.rc == 1, (res.out, res.err)
    assert res.hits == expected, res.out
    assert res.lines == expected + [f"comment-check: summary hits={len(expected)}"], res.out


# ---------------------------------------------------------------- the reviewer's path

FUNC = ("import math\n"
        "\n"
        "\n"
        "# Return the area of a circle of radius r.\n"
        "def area(r):\n"
        "    return math.pi * r * r\n")


def test_body_changed_under_unchanged_comment_is_a_hit(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    change(repo, {"src/geo.py": FUNC.replace("math.pi * r * r", "2 * math.pi * r")})
    assert_hits(check(repo), ["stale-comment src/geo.py:4 # Return the area of a circle of radius r."])


def test_same_change_with_comment_edited_is_clean(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    change(repo, {"src/geo.py": FUNC.replace("math.pi * r * r", "2 * math.pi * r")
                  .replace("the area of", "the circumference of")})
    assert_clean(check(repo))


def test_hit_api_is_importable():
    import importlib.util
    spec = importlib.util.spec_from_file_location("comment_check", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.Hit._fields == ("path", "line", "comment")
    assert callable(mod.stale_comments)


# ---------------------------------------------------------------- rule details

def test_only_deletions_under_a_comment_is_a_hit(tmp_path):
    body = "# Sum and log.\ndef f(a):\n    s = sum(a)\n    log(s)\n    return s\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("    log(s)\n", "")})
    assert_hits(check(repo), ["stale-comment a.py:1 # Sum and log."])


def test_deletion_between_block_and_code_is_a_hit(tmp_path):
    body = "x = 0\n\n# Set up.\nprepare()\nrun()\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("prepare()\n", "")})
    assert_hits(check(repo), ["stale-comment a.py:3 # Set up."])


def test_deletion_above_the_block_is_clean(tmp_path):
    body = "x = 0\ny = 1\n# Set up.\nprepare()\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("y = 1\n", "")})
    assert_clean(check(repo))


def test_one_line_of_a_block_edited_is_clean(tmp_path):
    body = "# Line one.\n# Line two.\nrun(1)\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("Line two", "Line 2").replace("run(1)", "run(2)")})
    assert_clean(check(repo))


def test_comment_edited_alone_is_clean(tmp_path):
    repo = make(tmp_path, {"a.py": "# Old words.\nrun()\n"})
    change(repo, {"a.py": "# New words.\nrun()\n"})
    assert_clean(check(repo))


def test_code_ends_at_blank_line(tmp_path):
    body = "# Prepare.\nprepare()\n\nrun(1)\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("run(1)", "run(2)")})
    assert_clean(check(repo))


def test_code_ends_at_next_comment_block(tmp_path):
    body = "# Prepare.\nprepare()\n# Run.\nrun(1)\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("run(1)", "run(2)")})
    assert_hits(check(repo), ["stale-comment a.py:3 # Run."])


def test_code_ends_at_dedent(tmp_path):
    body = "def f():\n    # Inner note.\n    a()\nb(1)\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("b(1)", "b(2)")})
    assert_clean(check(repo))


def test_deeper_code_is_under_the_comment(tmp_path):
    body = "# Loop.\nfor x in y:\n    if x:\n        go(1)\n"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body.replace("go(1)", "go(2)")})
    assert_hits(check(repo), ["stale-comment a.py:1 # Loop."])


def test_code_is_capped_at_twenty_lines(tmp_path):
    code = "".join(f"v{i} = {i}\n" for i in range(1, 22))
    repo = make(tmp_path, {"a.py": "# Values.\n" + code, "b.py": "# Values.\n" + code})
    change(repo, {"a.py": ("# Values.\n" + code).replace("v20 = 20", "v20 = 0"),
                  "b.py": ("# Values.\n" + code).replace("v21 = 21", "v21 = 0")})
    assert_hits(check(repo), ["stale-comment a.py:1 # Values."])


def test_comment_at_end_of_file_is_clean(tmp_path):
    repo = make(tmp_path, {"a.py": "x = 1\n# Trailing note.\n"})
    change(repo, {"a.py": "x = 2\n# Trailing note.\n"})
    assert_clean(check(repo))


def test_inline_comment_is_not_a_block(tmp_path):
    repo = make(tmp_path, {"a.py": "x = 1  # inline note\ny = 1\n"})
    change(repo, {"a.py": "x = 1  # inline note\ny = 2\n"})
    assert_clean(check(repo))


def test_shebang_and_coding_lines_are_not_comments(tmp_path):
    repo = make(tmp_path, {"tool": "#!/bin/sh\necho 1\n",
                           "enc.py": "# -*- coding: utf-8 -*-\nimport os\n",
                           "enc2.py": "#!/usr/bin/env python3\n# vim: set fileencoding=utf-8 :\nimport os\n"})
    change(repo, {"tool": "#!/bin/sh\necho 2\n", "enc.py": "# -*- coding: utf-8 -*-\nimport sys\n",
                  "enc2.py": "#!/usr/bin/env python3\n# vim: set fileencoding=utf-8 :\nimport sys\n"})
    assert_clean(check(repo))


def test_shebang_file_without_extension_is_read(tmp_path):
    repo = make(tmp_path, {"tool": "#!/bin/sh\n# Say one.\necho 1\n",
                           "plain": "# Say one.\necho 1\n"})
    change(repo, {"tool": "#!/bin/sh\n# Say one.\necho 2\n", "plain": "# Say one.\necho 2\n"})
    assert_hits(check(repo), ["stale-comment tool:2 # Say one."])


def test_lint_marker_lines_are_not_comments(tmp_path):
    hash_markers = ["# no" + "qa: E501", "# type: " + "ignore", "# pragma: " + "no cover"]
    files = {f"m{i}.py": f"{m}\nimport os\n" for i, m in enumerate(hash_markers)}
    files["m.ts"] = "// eslint" + "-disable-next-line no-console\nconsole.log(1);\n"
    repo = make(tmp_path, files)
    change(repo, {**{k: v.replace("import os", "import sys") for k, v in files.items() if k.endswith(".py")},
                  "m.ts": files["m.ts"].replace("log(1)", "log(2)")})
    assert_clean(check(repo))


def test_type_comment_that_is_not_a_marker_is_a_comment(tmp_path):
    repo = make(tmp_path, {"a.py": "# type: the kind of job\nkind = 1\n"})
    change(repo, {"a.py": "# type: the kind of job\nkind = 2\n"})
    assert_hits(check(repo), ["stale-comment a.py:1 # type: the kind of job"])


def test_slash_languages(tmp_path):
    exts = [".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp",
            ".java", ".kt", ".swift"]
    files = {f"s{e}": "// Start it.\nstart(1);\n" for e in exts}
    repo = make(tmp_path, files)
    change(repo, {k: "// Start it.\nstart(2);\n" for k in files})
    assert_hits(check(repo), [f"stale-comment s{e}:1 // Start it." for e in sorted(exts)])


def test_hash_languages(tmp_path):
    exts = [".py", ".sh", ".bash", ".zsh", ".rb", ".yml", ".yaml", ".toml"]
    files = {f"h{e}": "# Set it.\nkey = 1\n" for e in exts}
    files["upper.PY"] = "# Set it.\nkey = 1\n"  # its own name: macOS disks fold case
    repo = make(tmp_path, files)
    change(repo, {k: "# Set it.\nkey = 2\n" for k in files})
    want = sorted(files)
    assert_hits(check(repo), [f"stale-comment {p}:1 # Set it." for p in want])


def test_other_file_types_give_no_hit(tmp_path):
    files = {n: "# Set it.\nkey = 1\n" for n in ["a.md", "a.txt", "a.cfg", "Makefile", "a.js.bak"]}
    files["b.py.txt"] = "// Set it.\nkey = 1\n"
    repo = make(tmp_path, files)
    change(repo, {k: v.replace("key = 1", "key = 2") for k, v in files.items()})
    assert_clean(check(repo))


def test_wrong_comment_token_for_the_language_is_code(tmp_path):
    repo = make(tmp_path, {"a.py": "// not a comment\nx = 1\n", "a.go": "# not a comment\nx := 1\n"})
    change(repo, {"a.py": "// not a comment\nx = 2\n", "a.go": "# not a comment\nx := 2\n"})
    assert_clean(check(repo))


def test_long_comment_is_cut_at_80_characters(tmp_path):
    words = "# " + "abcdefghij" * 12
    repo = make(tmp_path, {"a.py": f"    {words}\n    x = 1\n"})
    change(repo, {"a.py": f"    {words}\n    x = 2\n"})
    assert_hits(check(repo), ["stale-comment a.py:1 " + words[:80]])


def test_hits_are_sorted_by_path_then_line(tmp_path):
    body = "# One.\na = 1\n\n# Two.\nb = 1\n"
    repo = make(tmp_path, {"z.py": body, "b/a.py": body, "a.py": body})
    new = body.replace("= 1", "= 2")
    change(repo, {"z.py": new, "b/a.py": new, "a.py": new})
    want = [f"stale-comment {p}:{n} {c}" for p in ["a.py", "b/a.py", "z.py"]
            for n, c in [(1, "# One."), (4, "# Two.")]]
    assert_hits(check(repo), want)


# ---------------------------------------------------------------- edge cases

def test_renamed_file_with_changes(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    (repo / "lib").mkdir()
    git(repo, "mv", "src/geo.py", "lib/shapes.py")
    change(repo, {"lib/shapes.py": FUNC.replace("math.pi * r * r", "2 * math.pi * r")})
    assert_hits(check(repo), ["stale-comment lib/shapes.py:4 # Return the area of a circle of radius r."])


def test_pure_rename_is_clean(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    (repo / "lib").mkdir()
    git(repo, "mv", "src/geo.py", "lib/shapes.py")
    commit_all(repo)
    assert_clean(check(repo))


def test_deleted_file_is_clean(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC, "keep.py": "x = 1\n"})
    git(repo, "rm", "-q", "src/geo.py")
    commit_all(repo)
    assert_clean(check(repo))


def test_added_file_is_clean(tmp_path):
    repo = make(tmp_path, {"keep.py": "x = 1\n"})
    change(repo, {"new.py": FUNC})
    assert_clean(check(repo))


def test_binary_file_is_clean(tmp_path):
    repo = make(tmp_path, {"a.py": b"# Note.\nx = 1\n\0\n", "b.py": b"# Note.\nx = 1\n"})
    change(repo, {"a.py": b"# Note.\nx = 2\n\0\n", "b.py": b"# Note.\nx = 2\n\0\n"})
    assert_clean(check(repo))


def test_nul_byte_past_the_first_8000_bytes_is_binary(tmp_path):
    # git reads only the first 8000 bytes to tell binary from text.
    tail = b"\n" + b"y = 0\n" * 2000 + b"\0\n"
    repo = make(tmp_path, {"a.py": b"# Note.\nx = 1\n" + tail})
    change(repo, {"a.py": b"# Note.\nx = 2\n" + tail})
    assert_clean(check(repo))


def test_file_that_is_not_utf8(tmp_path):
    repo = make(tmp_path, {"a.py": b"# Caf\xe9 total \xff.\nx = 1\n"})
    change(repo, {"a.py": b"# Caf\xe9 total \xff.\nx = 2\n"})
    assert_hits(check(repo), ["stale-comment a.py:1 # Caf\\xe9 total \\xff."])


def test_crlf_line_ends(tmp_path):
    crlf = FUNC.replace("\n", "\r\n")
    repo = make(tmp_path, {"src/geo.py": crlf})
    change(repo, {"src/geo.py": crlf.replace("math.pi * r * r", "2 * math.pi * r")})
    assert_hits(check(repo), ["stale-comment src/geo.py:4 # Return the area of a circle of radius r."])


def test_line_end_change_alone_is_clean(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    change(repo, {"src/geo.py": FUNC.replace("\n", "\r\n")})
    assert_clean(check(repo))


def test_no_newline_at_end_of_file(tmp_path):
    body = FUNC.rstrip("\n")
    repo = make(tmp_path, {"src/geo.py": body})
    change(repo, {"src/geo.py": body.replace("math.pi * r * r", "2 * math.pi * r")})
    assert_hits(check(repo), ["stale-comment src/geo.py:4 # Return the area of a circle of radius r."])


def test_newline_added_at_end_changes_the_last_line(tmp_path):
    body = "# Note.\nx = 1"
    repo = make(tmp_path, {"a.py": body})
    change(repo, {"a.py": body + "\n"})
    assert_hits(check(repo), ["stale-comment a.py:1 # Note."])


def test_odd_paths_and_control_characters_are_escaped(tmp_path):
    names = ["src/we ird.py", "src/q\"uo'te.py", "src/line\nbreak.py", "src/back\\slash.py",
             "src/\u00fcn\u00ef.py"]
    body = "# Esc \x1b[31m red\tand\rcr.\nx = 1\n"
    repo = make(tmp_path, {n: body for n in names})
    change(repo, {n: body.replace("x = 1", "x = 2") for n in names})
    res = check(repo)
    shown = {"src/we ird.py": "src/we ird.py", "src/q\"uo'te.py": "src/q\"uo'te.py",
             "src/line\nbreak.py": "src/line\\x0abreak.py",
             "src/back\\slash.py": "src/back\\\\slash.py", "src/\u00fcn\u00ef.py": "src/\u00fcn\u00ef.py"}
    want = [f"stale-comment {shown[n]}:1 # Esc \\x1b[31m red\\x09and\\x0dcr."
            for n in sorted(names, key=lambda s: s.encode())]
    assert_hits(res, want)
    assert len(res.lines) == len(names) + 1


def test_symlink_and_submodule_entries_are_ignored(tmp_path):
    repo = make(tmp_path, {"a.py": "x = 1\n"})
    os.symlink("# Note.\nx = 1\n", repo / "link.py")
    commit_all(repo)
    git(repo, "checkout", "-q", "-B", "main")
    git(repo, "checkout", "-q", "-b", "work2")
    (repo / "link.py").unlink()
    os.symlink("# Note.\nx = 2\n", repo / "link.py")
    oid = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},sub.py")
    commit_all(repo)
    assert_clean(check(repo))


# ---------------------------------------------------------------- what the diff is

def test_uncommitted_changes_are_not_read(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    put(repo, "src/geo.py", FUNC.replace("math.pi * r * r", "0"))
    git(repo, "add", "src/geo.py")
    put(repo, "src/other.py", "# Note.\nx = 1\n")
    assert_clean(check(repo))


def test_changes_on_the_base_after_the_branch_point_are_not_read(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC, "b.py": "x = 1\n"})
    change(repo, {"b.py": "x = 2\n"})
    git(repo, "checkout", "-q", "main")
    change(repo, {"src/geo.py": FUNC.replace("math.pi * r * r", "0")})
    git(repo, "checkout", "-q", "work")
    assert_clean(check(repo))


def test_criss_cross_history_joins_hits(tmp_path):
    repo = make(tmp_path, {"a.py": "# A.\na = 1\n", "b.py": "# B.\nb = 1\n"})
    git(repo, "checkout", "-q", "main")
    change(repo, {"a.py": "# A.\na = 2\n"}, "m1")
    git(repo, "checkout", "-q", "work")
    change(repo, {"b.py": "# B.\nb = 2\n"}, "w1")
    m1 = git(repo, "rev-parse", "main").stdout.decode().strip()
    w1 = git(repo, "rev-parse", "work").stdout.decode().strip()
    git(repo, "merge", "-q", "--no-edit", m1)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-edit", w1)
    git(repo, "checkout", "-q", "work")
    change(repo, {"c.py": "x\n"}, "w2")
    assert len(git(repo, "merge-base", "--all", "main", "work").stdout.split()) == 2
    res = check(repo)
    assert res.rc == 1, res.out
    assert res.hits == ["stale-comment a.py:1 # A.", "stale-comment b.py:1 # B."]


def test_repo_flag_from_another_folder_and_a_subfolder(tmp_path):
    repo = make(tmp_path, {"src/geo.py": FUNC})
    change(repo, {"src/geo.py": FUNC.replace("math.pi * r * r", "0")})
    want = ["stale-comment src/geo.py:4 # Return the area of a circle of radius r."]
    assert_hits(run("--repo", str(repo), "--base", "main", cwd=tmp_path), want)
    assert_hits(run("--repo", str(repo / "src"), "--base", "main", cwd=tmp_path), want)


# ---------------------------------------------------------------- the result contract

def assert_error(res, *words):
    assert res.rc == 2, (res.out, res.err)
    assert res.lines and res.lines[-1].startswith("comment-check: error "), res.out
    assert "comment-check: clean" not in res.out
    for w in words:
        assert w in res.lines[-1], res.out


def test_no_such_folder(tmp_path):
    assert_error(run("--repo", str(tmp_path / "nope"), "--base", "main", cwd=tmp_path), "no such folder")


def test_not_a_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    assert_error(run("--repo", str(tmp_path / "plain"), "--base", "main", cwd=tmp_path), "not a git")


def test_no_such_base(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    assert_error(check(repo, base="origin/nope"), "origin/nope")


def test_base_with_leading_dash_is_refused(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    assert_error(run("--repo", str(repo), "--base=--output=/tmp/x", cwd=repo), "bad base")


def test_no_merge_base(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    git(repo, "checkout", "-q", "--orphan", "lone")
    commit_all(repo, "lone")
    assert_error(check(repo), "merge base")


def test_git_missing(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    assert_error(run("--repo", str(repo), "--base", "main", cwd=repo, path=str(tmp_path / "nobin")),
                 "git")


def test_git_timeout(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nsleep 5\n")
    (fake / "git").chmod(0o755)
    res = run("--repo", str(repo), "--base", "main", "--git-timeout", "1", cwd=repo,
              path=f"{fake}:/usr/bin:/bin")
    assert_error(res, "did not finish")


def test_git_warning_fails_closed(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    # A tag and a branch of one name: rev-parse warns that the name is ambiguous.
    git(repo, "tag", "work", "main")
    assert_error(check(repo, base="work"), "warned")


def test_missing_object_fails_closed(tmp_path):
    repo = make(tmp_path, {"a.py": "# Note.\nx = 1\n"})
    change(repo, {"a.py": "# Note.\nx = 2\n"})
    oid = git(repo, "rev-parse", "HEAD:a.py").stdout.decode().strip()
    (repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
    assert_error(check(repo))


def test_partial_clone_never_fetches(tmp_path):
    repo = make(tmp_path, {"a.py": "# Note.\nx = 1\n"})
    change(repo, {"a.py": "# Note.\nx = 2\n"})
    oid = git(repo, "rev-parse", "HEAD:a.py").stdout.decode().strip()
    server = tmp_path / "server"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(server))
    marker = tmp_path / "fetched"
    prog = tmp_path / "upload.sh"
    prog.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec git-upload-pack \"$@\"\n")
    prog.chmod(0o755)
    for key, value in [("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"),
                       ("remote.origin.url", str(server)), ("remote.origin.promisor", "true"),
                       ("remote.origin.uploadpack", str(prog))]:
        git(repo, "config", key, value)
    (repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
    assert_error(check(repo))
    assert not marker.exists(), "a lazy fetch ran the upload-pack program from config"
    # The trap is live: a plain read fetches through it.
    subprocess.run(["git", "-C", str(repo), "cat-file", "-p", oid], capture_output=True, timeout=60)
    assert marker.exists()


def test_git_variables_from_the_caller_are_dropped(tmp_path):
    other = make(tmp_path, {"b.py": "# Other.\ny = 1\n"}, name="other")
    change(other, {"b.py": "# Other.\ny = 2\n"})
    repo = make(tmp_path, {"src/geo.py": FUNC})
    change(repo, {"src/geo.py": FUNC.replace("math.pi * r * r", "0")})
    res = run("--repo", str(repo), "--base", "main", cwd=repo,
              extra_env={"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)})
    assert_hits(res, ["stale-comment src/geo.py:4 # Return the area of a circle of radius r."])


def test_stdout_closed_fails_closed(tmp_path):
    repo = make(tmp_path, {"a.py": "x\n"})
    proc = subprocess.run(["sh", "-c", f'"{sys.executable}" -I "{CHECK}" --base main >&-'],
                          cwd=repo, capture_output=True, timeout=120)
    assert proc.returncode == 2, proc.stderr


def test_clean_on_base_itself(tmp_path):
    repo = make(tmp_path, {"a.py": "# Note.\nx = 1\n"})
    assert_clean(check(repo))


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
    repo = make(tmp_path, {"src/geo.py": FUNC})
    change(repo, {"src/geo.py": FUNC.replace("math.pi * r * r", "2 * math.pi * r")})
    marker = tmp_path / "ran"
    prog = tmp_path / "prog.sh"
    prog.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    prog.chmod(0o755)
    with open(repo / ".git/config", "a") as f:
        f.write(f"[diff]\n\texternal = {prog}\n"
                f"[diff \"x\"]\n\ttextconv = {prog}\n\tcommand = {prog}\n"
                f"[filter \"x\"]\n\tclean = {prog}\n\tsmudge = {prog}\n\tprocess = {prog}\n"
                f"[core]\n\tfsmonitor = {prog}\n\tpager = {prog}\n\tuntrackedCache = true\n"
                f"[pager]\n\tdiff = {prog}\n\tdiff-tree = {prog}\n"
                f"[color]\n\tui = always\n")
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/attributes").write_text("* diff=x filter=x\n")
    before = snapshot(repo)
    res = check(repo)
    after = snapshot(repo)
    assert not marker.exists(), "a program from repo config ran"
    assert_hits(res, ["stale-comment src/geo.py:4 # Return the area of a circle of radius r."])
    assert before == after, "the check wrote into the repo"
    # The trap is live: porcelain commands do run the configured programs.
    subprocess.run(["git", "-C", str(repo), "diff", "main", "work"], capture_output=True, timeout=60)
    assert marker.exists()


# ---------------------------------------------------------------- reference comparison

R_HASH = [".py", ".sh", ".rb", ".yml", ".toml", ""]
R_SLASH = [".ts", ".go", ".rs", ".c", ".java"]


def r_new_file(rng):
    """A random file and edit. Every line is unique (blank lines differ in how
    many spaces they hold), so the edit itself is the only minimal diff, and
    the reference knows which new lines were added and where lines went."""
    ext = rng.choice(R_HASH + R_SLASH)
    tok = "//" if ext in R_SLASH else "#"
    eol = "\r\n" if rng.random() < 0.2 else "\n"
    seq = [0, 0]  # next id, next blank width

    def fresh():
        seq[0] += 1
        u = seq[0]
        ind = rng.choice(["", "", "  ", "    ", "\t", "  \t"])
        r = rng.random()
        if r < 0.45:
            return f"{ind}v{u} = {u}" if tok == "#" else f"{ind}v{u}({u});"
        if r < 0.78:
            return f"{ind}{tok} c{u} says what follows"
        if r < 0.90:
            seq[1] += 1
            return " " * seq[1]
        if r < 0.95:
            return f"{ind}v{u} = 1  {tok} i{u}"
        if tok == "#":
            return f"{ind}# no" + f"qa: E{u}"
        return f"{ind}// eslint" + f"-disable-line r{u}"

    header = []
    if ext == "":
        header = ["#!/bin/sh"]
    elif ext == ".py" and rng.random() < 0.3:
        header = ["# -*- coding: utf-8 -*-"]
    ops = [("keep", h) for h in header]
    for _ in range(rng.randint(0, 22)):
        line = fresh()
        while rng.random() < 0.12:
            ops.append(("add", fresh()))
        r = rng.random()
        if r < 0.72:
            ops.append(("keep", line))
        elif r < 0.86:
            ops.append(("del", line))
        else:
            ops.append(("del", line))
            ops.append(("add", fresh()))
    while rng.random() < 0.12:
        ops.append(("add", fresh()))
    if not header and ext == "":
        raise AssertionError("unreachable")
    old_nl = rng.random() < 0.85
    new_nl = old_nl if rng.random() < 0.85 else not old_nl
    return ext, tok, eol, header, ops, old_nl, new_nl


def r_serialize(lines, eol, final_nl):
    out = []
    for i, line in enumerate(lines):
        out.append(line + (eol if i < len(lines) - 1 or final_nl else ""))
    return out


def r_diff(ops, eol, old_nl, new_nl):
    """Return (old bytes, new bytes, new lines, added, gaps) of one edit."""
    old = [l for k, l in ops if k != "add"]
    new = [l for k, l in ops if k != "del"]
    old_t = r_serialize(old, eol, old_nl)
    new_t = r_serialize(new, eol, new_nl)
    # A kept line whose line end changed is a changed line to git.
    seq, oi, ni = [], 0, 0
    for k, l in ops:
        if k == "keep":
            if old_t[oi] == new_t[ni]:
                seq.append(("keep", ni))
            else:
                seq.append(("del", None))
                seq.append(("add", ni))
            oi += 1
            ni += 1
        elif k == "del":
            seq.append(("del", None))
            oi += 1
        else:
            seq.append(("add", ni))
            ni += 1
    added, gaps = set(), set()
    i = 0
    seen = 0  # new lines before the current spot
    while i < len(seq):
        if seq[i][0] == "keep":
            seen += 1
            i += 1
            continue
        run_dels = run_adds = 0
        while i < len(seq) and seq[i][0] != "keep":
            if seq[i][0] == "del":
                run_dels += 1
            else:
                run_adds += 1
                added.add(seq[i][1] + 1)
            i += 1
        if run_dels:
            gaps.add(seen)
        seen += run_adds
    return "".join(old_t).encode(), "".join(new_t).encode(), new, added, gaps


def r_kind(n, line, tok, header):
    if n <= len(header):
        return "code"
    if line.strip(" \t") == "":
        return "blank"
    body = line.lstrip(" \t")
    if not body.startswith(tok):
        return "code"
    rest = body[len(tok):].lstrip(" \t")
    if rest.startswith("no" + "qa") or rest.startswith("eslint" + "-"):
        return "code"
    return "comment"


def r_hits(lines, tok, header, added, gaps):
    """The rule, line by line: [(first line of block, comment text)]."""
    kinds = [r_kind(n, l, tok, header) for n, l in enumerate(lines, 1)]

    def ind(s):
        return len(s) - len(s.lstrip(" \t"))

    blocks = []
    for i, k in enumerate(kinds):
        if k == "comment":
            if i > 0 and kinds[i - 1] == "comment":
                blocks[-1].append(i)
            else:
                blocks.append([i])
    out = []
    for blk in blocks:
        floor = min(ind(lines[i]) for i in blk)
        code = []
        j = blk[-1] + 1
        while j < len(lines) and len(code) < 20 and kinds[j] == "code" and ind(lines[j]) >= floor:
            code.append(j)
            j += 1
        if not code:
            continue
        if any(i + 1 in added for i in blk):
            continue
        touched = any(j + 1 in added for j in code)
        cut = any(g in gaps for g in range(blk[-1] + 1, code[-1] + 1))
        if touched or cut:
            out.append((blk[0] + 1, lines[blk[0]].lstrip(" \t")[:80]))
    return out


def run_reference_cases(repo, seed, count):
    rng = random.Random(seed)
    want = []
    olds, news = {}, {}
    for i in range(count):
        ext, tok, eol, header, ops, old_nl, new_nl = r_new_file(rng)
        name = f"f{i:03d}{ext}"
        old_b, new_b, new_lines, added, gaps = r_diff(ops, eol, old_nl, new_nl)
        olds[name], news[name] = old_b, new_b
        if old_b == new_b:
            continue
        for n, text in r_hits(new_lines, tok, header, added, gaps):
            want.append((name, n, f"stale-comment {name}:{n} {text}"))
    git(repo, "init", "-q")
    for name, data in olds.items():
        put(repo, name, data)
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "work")
    for name, data in news.items():
        put(repo, name, data)
    commit_all(repo, "edit")
    res = check(repo)
    want = [line for _, _, line in sorted(want)]
    want_rc = 1 if want else 0
    tail = [f"comment-check: summary hits={len(want)}"] if want else ["comment-check: clean"]
    return seed, res.rc == want_rc and res.lines == want + tail, want, res.lines, res.err


def test_matches_reference_on_random_files_and_edits(tmp_path):
    seeds = [3, 17, 29, 101]
    per_seed = 90
    repos = [tmp_path / f"r{s}" for s in seeds]
    for r in repos:
        r.mkdir()
    with ThreadPoolExecutor(max_workers=len(seeds)) as pool:
        results = list(pool.map(run_reference_cases, repos, seeds, [per_seed] * len(seeds)))
    total = sum(len(r[2]) for r in results)
    assert total > 60, f"too few hits to mean much: {total}"
    for seed, ok, want, got, err in results:
        if not ok:
            missing = sorted(set(want) - set(got))
            extra = sorted(set(got) - set(want))
            raise AssertionError(f"seed {seed}: missing {missing[:5]} extra {extra[:5]} err {err}")


def test_reference_itself_on_known_cases():
    # Guards the reference against drifting into agreement with a broken check.
    lines = ["# a", "x = 1", "y = 2", "", "# b", "z = 3"]
    assert r_hits(lines, "#", [], {2}, set()) == [(1, "# a")]
    assert r_hits(lines, "#", [], {1, 2}, set()) == []
    assert r_hits(lines, "#", [], set(), {1}) == [(1, "# a")]
    assert r_hits(lines, "#", [], set(), {3}) == []
    assert r_hits(lines, "#", [], {6}, set()) == [(5, "# b")]
    assert r_hits(lines, "#", [], {4}, set()) == []
    ops = [("keep", "a"), ("del", "b"), ("add", "c"), ("keep", "d")]
    assert r_diff(ops, "\n", True, True)[3:] == ({2}, {1})
    assert r_diff([("keep", "a")], "\n", False, True)[3:] == ({1}, {0})
    assert r_diff([("keep", "a"), ("del", "b")], "\n", True, True)[3:] == (set(), {1})
