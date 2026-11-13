"""The status line never runs a program that a repo's own git config names.

The script runs git in two folders on every draw: the session's working
folder, which can be any repo a person opens, and the harness tree. `git
status` can start programs named in that repo's config: the file monitor
(core.fsmonitor), a clean or process filter, the same in a submodule, and the
transport of a partial clone that is missing objects. A repo that arrives as an
archive brings its config with it.

Each test builds a real repo with a trap program wired in one of those ways.
It first proves the trap works: a plain `git status` must run it. Then it
draws the status line for that folder, as the working folder and as the
harness tree, and the trap must not run. The line must still show the branch
and the changed mark, so the protection is not bought by showing nothing.

Run: python3 -m pytest tests/test_statusline_git_safety.py -q -p no:cacheprovider
"""
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATUSLINE = REPO / "config" / "claude" / "statusline-command.sh"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("git") is None,
    reason="the status line script needs jq and git",
)


def setup_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return env


def git(folder, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(folder), *args], check=check, env=setup_env(),
        capture_output=True, text=True,
    )


def commit(folder, name, text):
    (folder / name).write_text(text, encoding="utf-8")
    git(folder, "add", name)
    git(folder, "commit", "-q", "-m", name)


class Trap:
    """A program that records each run in a file outside the repo."""

    def __init__(self, tmp_path, body="cat"):
        self.marker = tmp_path / "trap-ran"
        self.program = tmp_path / "trap.sh"
        self.program.write_text(
            f"#!/bin/sh\necho ran >> '{self.marker}'\n{body}\n", encoding="utf-8"
        )
        self.program.chmod(self.program.stat().st_mode | stat.S_IXUSR)

    def ran(self):
        return self.marker.exists()

    def reset(self):
        self.marker.unlink()


def change_keeping_size(path):
    """Change a tracked file so that only a content check can tell: same size."""
    old = path.read_text(encoding="utf-8")
    path.write_text(old.swapcase(), encoding="utf-8")
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))


def draw(tmp_path, cwd, harness_dir):
    """Line 1 of the status line, colours removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("SSH_CONNECTION", None)
    env["STATUSLINE_HARNESS_DIR"] = str(harness_dir)
    out = subprocess.run(
        ["sh", str(STATUSLINE)], input=json.dumps({"cwd": str(cwd)}),
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    return ANSI.sub("", out.splitlines()[0]) if out.strip() else ""


@pytest.fixture
def repo(tmp_path):
    """A repo on main with one commit that has the tag v1.2.3."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (tmp_path / "plain").mkdir()
    git(folder, "init", "-q", "-b", "main")
    commit(folder, "a.txt", "hello\n")
    git(folder, "tag", "v1.2.3")
    return folder


def assert_trap_is_live(repo, trap):
    """A plain git status must run the trap, or the test proves nothing."""
    git(repo, "status", check=False)
    assert trap.ran(), "the trap did not run under a plain git status"
    trap.reset()


def assert_both_draws_are_safe(tmp_path, repo, trap, changed=True):
    plain = tmp_path / "plain"
    mark = r"\*" if changed else ""

    as_cwd = draw(tmp_path, cwd=repo, harness_dir=plain)
    assert not trap.ran(), "the trap ran when the repo was the working folder"
    assert re.search(rf"\smain{mark}(\s|$)", as_cwd), as_cwd

    as_harness = draw(tmp_path, cwd=plain, harness_dir=repo)
    assert not trap.ran(), "the trap ran when the repo was the harness tree"
    assert re.search(rf"(^|\s)v1\.2\.3{mark}(\s|$)", as_harness), as_harness


def test_a_file_monitor_named_by_the_repo_does_not_run(repo, tmp_path):
    trap = Trap(tmp_path, body="exit 1")
    git(repo, "config", "core.fsmonitor", str(trap.program))
    change_keeping_size(repo / "a.txt")
    assert_trap_is_live(repo, trap)
    assert_both_draws_are_safe(tmp_path, repo, trap)


@pytest.mark.parametrize("name", ["evil", "e=v", "E.v.clean", "a b"])
@pytest.mark.parametrize("kind", ["clean", "process"])
@pytest.mark.parametrize("required", [False, True])
def test_a_filter_named_by_the_repo_does_not_run(repo, tmp_path, name, kind, required):
    trap = Trap(tmp_path, body="cat" if kind == "clean" else "exit 1")
    git(repo, "config", f"filter.{name}.{kind}", str(trap.program))
    if required:
        git(repo, "config", f"filter.{name}.required", "true")
    if " " in name:
        # an attribute value cannot hold a space, so a second filter is the one
        # in use: the odd name must not stop the script from reaching it
        git(repo, "config", f"filter.second.{kind}", str(trap.program))
        name = "second"
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(f"* filter={name}\n", encoding="utf-8")
    change_keeping_size(repo / "a.txt")
    assert_trap_is_live(repo, trap)
    assert_both_draws_are_safe(tmp_path, repo, trap)


def test_a_filter_from_an_included_config_file_does_not_run(repo, tmp_path):
    trap = Trap(tmp_path)
    extra = tmp_path / "extra.gitconfig"
    extra.write_text(f'[filter "inc"]\n\tclean = {trap.program}\n', encoding="utf-8")
    git(repo, "config", "include.path", str(extra))
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=inc\n", encoding="utf-8")
    change_keeping_size(repo / "a.txt")
    assert_trap_is_live(repo, trap)
    assert_both_draws_are_safe(tmp_path, repo, trap)


def test_programs_named_by_a_submodule_do_not_run(repo, tmp_path):
    source = tmp_path / "sub-source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    commit(source, "f.txt", "inner\n")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
    git(repo, "commit", "-q", "-m", "sub")
    git(repo, "tag", "-f", "v1.2.3")

    trap = Trap(tmp_path)
    sub = repo / "sub"
    git(sub, "config", "core.fsmonitor", str(trap.program))
    git(sub, "config", "filter.inner.clean", str(trap.program))
    inner_git = Path(git(sub, "rev-parse", "--absolute-git-dir").stdout.strip())
    (inner_git / "info").mkdir(exist_ok=True)
    (inner_git / "info" / "attributes").write_text("* filter=inner\n", encoding="utf-8")
    change_keeping_size(sub / "f.txt")

    assert_trap_is_live(repo, trap)
    # only the submodule's own files changed, and those are not looked at
    assert_both_draws_are_safe(tmp_path, repo, trap, changed=False)


def test_a_partial_clone_does_not_fetch_what_it_lacks(repo, tmp_path):
    git(repo, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "clone", "-q", "--filter=tree:0",
         "--no-checkout", f"file://{repo}", str(clone)],
        check=True, env=setup_env(), capture_output=True,
    )
    trap = Trap(tmp_path, body="exit 1")
    git(clone, "config", "remote.origin.uploadpack", str(trap.program))
    git(clone, "status", check=False)
    assert trap.ran(), "the trap did not run under a plain git status"
    trap.reset()

    plain = tmp_path / "plain"
    draw(tmp_path, cwd=clone, harness_dir=plain)
    assert not trap.ran(), "the trap ran when the clone was the working folder"
    draw(tmp_path, cwd=plain, harness_dir=clone)
    assert not trap.ran(), "the trap ran when the clone was the harness tree"


def test_a_clean_repo_still_shows_no_changed_mark(repo, tmp_path):
    plain = tmp_path / "plain"
    assert re.search(r"\smain(\s|$)", draw(tmp_path, cwd=repo, harness_dir=plain))
    assert re.search(r"(^|\s)v1\.2\.3(\s|$)", draw(tmp_path, cwd=plain, harness_dir=repo))


def test_a_new_commit_in_a_submodule_still_marks_the_tree_changed(repo, tmp_path):
    source = tmp_path / "sub-source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    commit(source, "f.txt", "inner\n")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
    git(repo, "commit", "-q", "-m", "sub")
    commit(repo / "sub", "g.txt", "more\n")
    plain = tmp_path / "plain"
    assert re.search(r"\smain\*(\s|$)", draw(tmp_path, cwd=repo, harness_dir=plain))
