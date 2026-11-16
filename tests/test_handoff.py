"""handoff.py: the frame of a resume note, and the check of a note.

The script runs as a program on real git repos in temp folders. The first test
is the user's path: pause (frame, fill, check), then resume in a tree that
moved. The rest holds each part: the facts that frame reads from git, where the
note lives and that git does not see it, what check reads as a gap, and what it
refuses to read.

Run: python3 -m pytest tests/test_handoff.py -q -p no:cacheprovider
"""
import fcntl
import importlib.util
import json
import os
import random
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "config" / "claude" / "skills" / "handoff"
SCRIPT = SKILL / "scripts" / "handoff.py"
PY = sys.executable
NOTE = ".claude/handoff.md"
PREV = ".claude/handoff.prev.md"
SECTIONS = ("Next actions", "State now", "Environment", "Decisions, and why", "Rejected approaches", "Open questions")
BODY = {
    "tldr": "Add the `double` function. The test for it is written and fails.",
    "Next actions": "- [ ] Write `double` in `src/app.py`, then run `python3 -m unittest`.\n- [ ] Open the pull request.",
    "State now": "Half-done: `src/app.py` has `greet`; the test in `tests/test_app.py` fails.",
    "Environment": "Branch `main`. No service and no secret is needed.",
    "Decisions, and why": "| Decision | Why |\n| --- | --- |\n| Plain functions | The app has no state. |",
    "Rejected approaches": "None.",
    "Open questions": "None.",
}


class Repo:
    """A git repo in a temp folder, with a home of its own so that no setting of the user plays a part."""

    def __init__(self, tmp_path, name="proj"):
        self.home = tmp_path / f"home-{name}"
        self.home.mkdir()
        self.root = tmp_path / name
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "LANG": "C.UTF-8"}
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        (self.root / "src" / "app.py").write_text("def greet():\n    return 'hello'\n")
        (self.root / "tests" / "test_app.py").write_text("import unittest\n")
        self.commit("the app")

    def git(self, *args, check=True):
        done = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, env=self.env, timeout=60)
        assert not check or done.returncode == 0, done.stderr
        return done.stdout

    def commit(self, message="more"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def run(self, *args, cwd=None, env=None, repo=True):
        argv = [PY, str(SCRIPT), *args] + (["--repo", str(self.root)] if repo else [])
        done = subprocess.run(argv, cwd=cwd or self.root.parent, capture_output=True, text=True,
                              env=env or self.env, timeout=120)
        return done.returncode, done.stdout.splitlines()

    @property
    def note(self):
        return self.root / NOTE

    def block(self):
        text = self.note.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        return yaml.safe_load(text[4:text.index("\n---\n", 4)])

    def fill(self, **changes):
        """Write the body of the note under the fact block that frame wrote."""
        assert set(changes) <= set(BODY), changes
        body = dict(BODY, **changes)
        text = self.note.read_text(encoding="utf-8")
        head = text[:text.index("\n---\n", 4) + 5]
        parts = [head, "\n> [!tldr]\n>\n> " + body["tldr"] + "\n" if body["tldr"] is not None else "\n"]
        for name in SECTIONS:
            if body[name] is not None:
                parts.append(f"\n## {name}\n\n{body[name]}\n")
        self.note.write_text("".join(parts), encoding="utf-8")

    def framed(self, **changes):
        assert self.run("frame")[0] == 0
        self.fill(**changes)
        return self


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


class Trap:
    """A program that records each run in a file outside the repo."""

    def __init__(self, tmp_path, body="exit 1"):
        self.marker = tmp_path / "trap-ran"
        self.program = tmp_path / "trap.sh"
        self.program.write_text(f"#!/bin/sh\necho ran >> '{self.marker}'\n{body}\n", encoding="utf-8")
        self.program.chmod(self.program.stat().st_mode | stat.S_IXUSR)

    def ran(self):
        return self.marker.exists()

    def live_under(self, call):
        """A plain git call must run the trap, or the test proves nothing."""
        call()
        assert self.ran(), "the trap did not run under a plain git call"
        self.marker.unlink()


def git_in(folder, env, *args, check=True):
    return subprocess.run(["git", "-C", str(folder), *args], check=check, env=env, capture_output=True, timeout=60)


def add_submodule(repo, tmp_path, at="sub"):
    inner = Repo(tmp_path, "inner")
    (inner.root / "handoff.md").write_text("a note that came with the inner repo\n")
    inner.commit("a note")
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner.root), at)
    repo.commit("a submodule")
    return repo.root / at


def load_script():
    spec = importlib.util.spec_from_file_location("handoff_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # the dataclass of the script looks its module up by name
    spec.loader.exec_module(module)
    return module


# ── the user's path ──────────────────────────────────────────────────────────

def test_pause_writes_a_note_that_checks_and_resume_sees_that_the_tree_moved(repo):
    (repo.root / "src" / "app.py").write_text("def greet():\n    return 'hello'\n\n# double: next\n")
    code, lines = repo.run("frame")
    assert (code, lines[-1]) == (0, "handoff: wrote .claude/handoff.md")

    # the frame alone is no note: every part is still to write
    code, lines = repo.run("check")
    assert code == 1 and lines[-1] == "handoff: summary gaps=7"
    assert lines[:-1] == ["empty-section tldr"] + [f"empty-section {name}" for name in SECTIONS]

    repo.fill()
    assert repo.run("check") == (0, ["handoff: ok"])
    assert repo.git("status", "--porcelain").splitlines() == [" M src/app.py"], "git must not see the note"

    # a later session: the tree is as the note says
    assert repo.run("check") == (0, ["handoff: ok"])
    # ... and then someone commits: the note is for another commit and another set of changes
    old = repo.block()["head"]
    new = repo.commit("double")
    code, lines = repo.run("check")
    assert (code, lines) == (1, [f"stale-head note={old[:7]} now={new[:7]}", "stale-changes note=1 now=0",
                                 "handoff: summary gaps=2"])


# ── the facts that frame reads from git ──────────────────────────────────────

def test_the_fact_block_names_the_branch_the_commit_and_each_change(repo):
    head = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "-q", "-c", "claude/some-task")
    (repo.root / "src" / "app.py").write_text("changed\n")
    (repo.root / "new.txt").write_text("new\n")
    repo.git("mv", "tests/test_app.py", "tests/test_main.py")
    assert repo.run("frame")[0] == 0
    block = repo.block()
    assert block["type"] == "handoff" and block["branch"] == "claude/some-task" and block["head"] == head
    assert block["dirty"] == 4, "a rename is one file that is gone and one that is new, whatever the settings say"
    assert sorted(block["changed"]) == [" M src/app.py", "?? new.txt", "A  tests/test_main.py", "D  tests/test_app.py"]
    assert isinstance(block["written"], str) and block["written"].endswith("Z") and len(block["written"]) == 20
    assert "changed_more" not in block


def test_a_clean_tree_a_detached_head_and_a_repo_with_no_commit(repo, tmp_path):
    assert repo.run("frame")[0] == 0
    assert (repo.block()["dirty"], repo.block()["changed"]) == (0, [])
    repo.git("checkout", "-q", "--detach")
    assert repo.run("frame")[0] == 0
    assert repo.block()["branch"] is None

    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "-C", str(empty), "init", "-q", "-b", "main", "."], check=True, env=repo.env)
    done = subprocess.run([PY, str(SCRIPT), "frame", "--repo", str(empty)], capture_output=True, text=True, env=repo.env)
    assert done.returncode == 0, done.stdout
    block = yaml.safe_load((empty / NOTE).read_text().split("\n---\n")[0][4:])
    assert (block["head"], block["branch"]) == (None, "main")


def test_more_changes_than_the_block_lists_are_counted_and_the_count_says_so(repo):
    for number in range(57):
        (repo.root / f"f{number:02}.txt").write_text("x\n")
    assert repo.run("frame")[0] == 0
    block = repo.block()
    assert (block["dirty"], len(block["changed"]), block["changed_more"]) == (57, 50, 7)
    repo.fill()
    assert repo.run("check") == (0, ["handoff: ok"])
    (repo.root / "f56.txt").unlink()   # one of the seven that the block does not list
    code, lines = repo.run("check")
    assert (code, lines[0]) == (1, "stale-changes note=57 now=56")


def test_a_file_name_with_a_line_break_or_a_quote_stays_one_entry(repo):
    (repo.root / "a\nb.txt").write_text("x\n")
    (repo.root / 'say "hi".txt').write_text("x\n")
    assert repo.run("frame")[0] == 0
    block = repo.block()
    assert block["dirty"] == 2 and sorted(block["changed"]) == ['?? a\nb.txt', '?? say "hi".txt']
    repo.fill()
    assert repo.run("check") == (0, ["handoff: ok"])


def test_the_callers_git_variables_do_not_pick_the_repo(repo, tmp_path):
    other = Repo(tmp_path, "other")
    (other.root / "extra.txt").write_text("x\n")
    env = dict(repo.env, GIT_DIR=str(other.root / ".git"), GIT_WORK_TREE=str(other.root))
    assert repo.run("frame", env=env)[0] == 0
    assert repo.block()["dirty"] == 0 and not (other.root / NOTE).exists()


def test_the_repo_is_the_one_of_the_current_folder_when_none_is_named(repo):
    code, lines = repo.run("frame", cwd=repo.root / "src", repo=False)
    assert (code, lines[-1]) == (0, "handoff: wrote .claude/handoff.md")
    assert repo.note.is_file() and not (repo.root / "src" / ".claude").exists()


# ── where the note lives ─────────────────────────────────────────────────────

def test_git_does_not_see_the_note_and_the_exclude_lines_are_written_once(repo):
    code, lines = repo.run("frame")
    assert code == 0 and "added 3 lines to .git/info/exclude" in lines
    exclude = (repo.root / ".git" / "info" / "exclude").read_text()
    assert exclude.endswith("/.claude/handoff.md\n/.claude/handoff.prev.md\n/.claude/handoff.tmp.*\n")
    repo.fill()
    code, lines = repo.run("frame")
    assert code == 0 and not any(line.startswith("added") for line in lines)
    assert (repo.root / ".git" / "info" / "exclude").read_text() == exclude
    assert repo.git("status", "--porcelain") == "" and (repo.root / PREV).is_file()


def test_a_repo_that_ignores_the_note_already_gets_no_exclude_line(repo):
    (repo.root / ".gitignore").write_text(".claude/\n")
    repo.commit("ignore")
    before = (repo.root / ".git" / "info" / "exclude").read_bytes()
    code, lines = repo.run("frame")
    assert code == 0 and lines == ["handoff: wrote .claude/handoff.md"]
    assert (repo.root / ".git" / "info" / "exclude").read_bytes() == before


def test_an_exclude_file_with_no_line_end_keeps_its_last_line(repo):
    exclude = repo.root / ".git" / "info" / "exclude"
    exclude.write_text("*.log")
    assert repo.run("frame")[0] == 0
    assert exclude.read_text() == "*.log\n/.claude/handoff.md\n/.claude/handoff.prev.md\n/.claude/handoff.tmp.*\n"


def test_a_linked_work_tree_has_a_note_of_its_own(repo, tmp_path):
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-q", str(linked), "-b", "claude/other")
    done = subprocess.run([PY, str(SCRIPT), "frame", "--repo", str(linked)], capture_output=True, text=True, env=repo.env)
    assert done.returncode == 0, done.stdout
    assert (linked / NOTE).is_file() and not repo.note.exists()
    status = subprocess.run(["git", "-C", str(linked), "status", "--porcelain"], capture_output=True, text=True, env=repo.env)
    assert status.stdout == ""
    assert "/.claude/handoff.md\n" in (repo.root / ".git" / "info" / "exclude").read_text()


def test_a_note_that_git_tracks_is_never_replaced(repo):
    repo.note.parent.mkdir()
    repo.note.write_text("a tracked file\n")
    repo.commit("someone tracks it")
    code, lines = repo.run("frame")
    assert code == 2 and lines[-1].startswith("handoff: error ") and "tracks" in lines[-1]
    assert repo.note.read_text() == "a tracked file\n"


def test_a_rule_of_the_repo_that_keeps_the_note_visible_is_an_error(repo):
    (repo.root / ".gitignore").write_text("!/.claude/handoff.md\n")
    repo.commit("a rule that takes the note back in")
    code, lines = repo.run("frame")
    assert code == 2 and "out of git" in lines[-1]
    assert not repo.note.exists()


def test_a_note_with_text_is_kept_once_and_an_empty_frame_is_not(repo):
    repo.framed(tldr="the first note")
    first = repo.note.read_text()
    code, lines = repo.run("frame")
    assert code == 0 and lines[0] == "kept the last note as .claude/handoff.prev.md"
    assert (repo.root / PREV).read_text() == first
    # the new note is an empty frame: a third frame replaces it and leaves the kept note alone
    code, lines = repo.run("frame")
    assert code == 0 and lines == ["handoff: wrote .claude/handoff.md"]
    assert (repo.root / PREV).read_text() == first


def test_a_note_that_is_too_large_to_read_is_kept_and_not_thrown_away(repo):
    repo.framed()
    large = repo.note.read_bytes() + b"x" * (1 << 20)
    repo.note.write_bytes(large)
    code, lines = repo.run("frame")
    assert code == 0 and lines[0] == "kept the last note as .claude/handoff.prev.md"
    assert (repo.root / PREV).read_bytes() == large


def test_an_error_that_the_script_did_not_expect_is_exit_2_and_never_a_result(repo, capsys, monkeypatch):
    script = load_script()
    def broken(_):
        raise RuntimeError("something new")
    monkeypatch.setattr(script, "find_top", broken)
    assert script.main(["check", "--repo", str(repo.root)]) == 2
    assert capsys.readouterr().out == "handoff: error RuntimeError: something new\n"


def test_a_note_that_frame_wrote_and_that_cannot_be_read_any_more_is_kept_too(repo):
    """It starts as a note of this script starts, so it is one. The repo has no exclude line yet."""
    repo.note.parent.mkdir()
    repo.note.write_text("---\ntype: handoff\nthe rest of the block is lost, and this text is not\n")
    assert repo.run("frame")[0] == 0
    assert (repo.root / PREV).read_text() == "---\ntype: handoff\nthe rest of the block is lost, and this text is not\n"
    assert repo.block()["dirty"] == 0, "the facts must be read after the note is kept out of git"


def keeps_letter_case_apart(folder):
    (folder / "probe-a").write_text("x")
    return not (folder / "PROBE-A").exists()


@pytest.mark.parametrize("name", ["handoff.md", "handoff.prev.md", "HANDOFF.md", "Handoff.Prev.md"])
@pytest.mark.parametrize("content", ["my own notes, not a frame\n", ""])
def test_a_file_that_frame_did_not_write_is_never_moved_or_replaced(repo, tmp_path, name, content):
    """A file of the user at the note's path or at the kept note's path, also under a name in other letter case
    on a disk that takes it for the same file. Frame moved such a file to the kept note, and the frame after that
    replaced it there. Now frame ends before its first write: an exclude line would hide the file from git."""
    same_file = name in ("handoff.md", "handoff.prev.md") or not keeps_letter_case_apart(tmp_path)
    mine = repo.root / ".claude" / name
    mine.parent.mkdir()
    mine.write_text(content)
    exclude = (repo.root / ".git" / "info" / "exclude").read_text()
    for _ in range(2):
        code, lines = repo.run("frame")
        if same_file:
            assert code == 2 and len(lines) == 1 and "is not a note that frame wrote" in lines[0], lines
        else:
            assert code == 0
            repo.fill()
    assert mine.read_text() == content and name in os.listdir(mine.parent)
    if same_file:
        assert os.listdir(mine.parent) == [name]
        assert (repo.root / ".git" / "info" / "exclude").read_text() == exclude
        assert repo.git("status", "--porcelain", "--untracked-files=all") == f"?? .claude/{name}\n"


@pytest.mark.parametrize("rel", [NOTE, PREV])
def test_a_link_at_the_path_of_the_note_or_of_the_kept_note_is_never_replaced_or_followed(repo, tmp_path, rel):
    repo.framed()
    whole = repo.note.read_bytes()
    target = tmp_path / "target.md"
    target.write_bytes(whole)
    if rel == NOTE:
        repo.note.unlink()
    (repo.root / rel).symlink_to(target)
    code, lines = repo.run("frame")
    assert (code, lines) == (2, [f"handoff: error {rel} is a link, and frame replaces only a note that frame wrote"])
    assert (repo.root / rel).is_symlink() and target.read_bytes() == whole
    assert sorted(os.listdir(repo.note.parent)) == sorted({"handoff.md", Path(rel).name})


def test_a_claude_folder_that_is_a_repo_of_its_own_holds_no_note(repo, tmp_path):
    """The exclude lines keep the note out of one repo. In a `.claude` with its own `.git`, the other repo sees the
    note, can commit it, and can track the file that frame replaces."""
    shared = Repo(tmp_path, "shared")
    subprocess.run(["git", "clone", "-q", str(shared.root), str(repo.root / ".claude")], check=True, env=repo.env,
                   capture_output=True)
    exclude = (repo.root / ".git" / "info" / "exclude").read_text()
    inner = sorted(os.listdir(repo.root / ".claude"))
    (repo.root / ".claude" / "handoff.md").write_text("---\ntype: handoff\n")
    for command in ("frame", "check"):
        code, lines = repo.run(command)
        assert code == 2 and len(lines) == 1 and lines[0].startswith("handoff: error .claude is a repo of its own"), lines
    assert sorted(os.listdir(repo.root / ".claude")) == sorted(inner + ["handoff.md"])
    assert (repo.root / ".git" / "info" / "exclude").read_text() == exclude


def test_a_link_at_the_exclude_file_or_at_the_claude_folder_is_an_error_and_nothing_is_written(repo, tmp_path):
    shared = tmp_path / "shared-exclude"
    shared.write_text("*.log\n")
    exclude = repo.root / ".git" / "info" / "exclude"
    exclude.unlink()
    exclude.symlink_to(shared)
    code, lines = repo.run("frame")
    assert code == 2 and "exclude file" in lines[-1]
    assert shared.read_text() == "*.log\n" and not repo.note.exists()

    other = Repo(tmp_path, "other")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (other.root / ".claude").symlink_to(elsewhere)
    assert other.run("frame") == (2, ["handoff: error .claude is no plain folder"])
    assert list(elsewhere.iterdir()) == []
    assert "handoff" not in (other.root / ".git" / "info" / "exclude").read_text()


def test_a_folder_at_the_note_path_or_a_file_at_the_claude_path_is_an_error(repo, tmp_path):
    (repo.root / NOTE).mkdir(parents=True)
    for command in ("frame", "check"):
        assert repo.run(command) == (2, [f"handoff: error {NOTE} is no plain file"]), command
    other = Repo(tmp_path, "other")
    (other.root / ".claude").write_text("a file\n")
    code, lines = other.run("frame")
    assert code == 2 and lines[-1].startswith("handoff: error ")


def test_frame_leaves_no_temp_file_and_many_at_once_leave_one_whole_note(repo):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: repo.run("frame"), range(16)))
    assert all(code == 0 for code, _ in results), results
    assert sorted(entry.name for entry in repo.note.parent.iterdir()) == ["handoff.md"]
    code, lines = repo.run("check")
    assert code == 1 and lines[-1] == "handoff: summary gaps=7", "the note must be one whole frame"


# ── two frames at one time, other names for the note, the settings of the repo ──

def test_two_frames_at_one_time_do_not_lose_a_note_with_text(repo):
    """Both read the note with text and chose to keep it. The first moved it to the kept note and wrote its empty
    frame; the second then moved that empty frame over the kept note. With a note of this size the text was lost
    in about half of the runs."""
    repo.framed(**{"State now": "UNIQUE-TEXT-MARKER " + "a line of state that takes time to read\n" * 16000})
    text = repo.note.read_bytes()
    for _ in range(8):
        repo.note.write_bytes(text)
        (repo.root / PREV).unlink(missing_ok=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: repo.run("frame"), range(2)))
        assert all(code == 0 for code, _ in results), results
        assert (repo.root / PREV).read_bytes() == text, "the note with text must be the kept note"
        assert repo.run("check")[1][-1] == "handoff: summary gaps=7"


@pytest.mark.parametrize("tracked", [".claude/HANDOFF.md", ".Claude/handoff.md", ".claude/Handoff.Prev.md",
                                     ".claude/hando\ufb00.md"])
def test_a_tracked_file_whose_name_differs_only_in_letter_case_is_never_replaced(repo, tracked):
    """On a disk that does not keep letter case apart, that file is the note's own path. The last name has the
    one character for `ff`: the disk of a Mac reads it as the two letters."""
    path = repo.root / tracked
    path.parent.mkdir(exist_ok=True)
    path.write_text("a tracked document of the team\n")
    repo.commit("someone tracks it")
    for command in ("frame", "check"):
        code, lines = repo.run(command)
        assert code == 2 and len(lines) == 1 and f"git tracks {tracked}" in lines[0], (command, lines)
    assert path.read_text() == "a tracked document of the team\n"
    assert repo.git("status", "--porcelain") == ""


def test_a_note_behind_a_link_or_in_a_submodule_at_the_claude_path_is_never_read(repo, tmp_path):
    """A repo can track `.claude` as a link to one of its folders, or as a submodule. The note in it came with a
    repo then, and no session of this work tree wrote it. A link that git does not track is not followed either."""
    repo.framed()
    whole = repo.note.read_bytes()
    assert repo.run("check") == (0, ["handoff: ok"])
    (repo.root / "docs").mkdir()
    (repo.root / "docs" / "handoff.md").write_bytes(whole)
    os.rename(repo.note.parent, repo.root / ".claude.real")
    (repo.root / ".claude").symlink_to("docs")
    assert repo.run("check") == (2, ["handoff: error .claude is no plain folder"]), "a link that git does not track"
    repo.commit("the repo tracks the link and the note behind it")
    for command in ("check", "frame"):
        code, lines = repo.run(command)
        assert code == 2 and len(lines) == 1 and lines[0].startswith("handoff: error git tracks .claude in"), lines
    assert (repo.root / "docs" / "handoff.md").read_bytes() == whole

    other = Repo(tmp_path, "other")
    add_submodule(other, tmp_path, at=".claude")
    for command in ("check", "frame"):
        code, lines = other.run(command)
        assert code == 2 and len(lines) == 1 and lines[0].startswith("handoff: error git tracks .claude in"), lines
    assert other.note.read_text() == "a note that came with the inner repo\n"


def test_other_tracked_files_of_the_claude_folder_do_not_stop_a_note(repo):
    names = (".claude/settings.json", ".claude/handoff.txt", ".claude/skills/handoff.md", "docs/.claude/handoff.md")
    for name in names:
        path = repo.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    repo.commit("the team's files")
    repo.framed()
    assert repo.run("check") == (0, ["handoff: ok"])
    assert repo.git("status", "--porcelain") == ""


def test_a_frame_waits_while_another_one_works_in_the_folder(repo):
    """The test holds the lock of a frame. A second frame must not look at the note, move it or write before the
    first is done."""
    repo.framed()
    text = repo.note.read_bytes()
    held = os.open(repo.note.parent, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        waiting = subprocess.Popen([PY, str(SCRIPT), "frame", "--repo", str(repo.root)], stdout=subprocess.PIPE,
                                   text=True, env=repo.env)
        time.sleep(2)
        assert waiting.poll() is None, "the second frame did not wait"
        assert repo.note.read_bytes() == text and not (repo.root / PREV).exists()
    finally:
        os.close(held)
    out, _ = waiting.communicate(timeout=60)
    assert (waiting.returncode, out.splitlines()) == (0, [f"kept the last note as {PREV}", f"handoff: wrote {NOTE}"])
    assert (repo.root / PREV).read_bytes() == text


def test_a_frame_that_does_not_get_the_folder_in_time_is_an_error_and_writes_nothing(repo, monkeypatch):
    script = load_script()
    monkeypatch.setattr(script, "LOCK_WAIT_S", 0.3)
    monkeypatch.setenv("HOME", str(repo.home))
    repo.framed()
    text = repo.note.read_bytes()
    held = os.open(repo.note.parent, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(script.Failed, match=r"another frame holds \.claude/ for longer than 0\.3 s"):
            script.frame(repo.root)
        assert 0.3 <= time.monotonic() - start < 10
    finally:
        os.close(held)
    assert repo.note.read_bytes() == text and not (repo.root / PREV).exists()
    assert [path.name for path in repo.note.parent.iterdir()] == ["handoff.md"]


def test_a_file_monitor_that_the_settings_of_the_repo_name_does_not_run(repo, tmp_path):
    """Also not the one of a submodule: git hands the switch on to the call that it makes there."""
    sub = add_submodule(repo, tmp_path)
    trap = Trap(tmp_path)
    for folder in (repo.root, sub):   # each of the two alone starts the trap under a plain status of the repo
        git_in(folder, repo.env, "config", "core.fsmonitor", str(trap.program))
        (folder / "src" / "app.py").write_text("changed\n")
        trap.live_under(lambda: repo.git("status", check=False))
        git_in(folder, repo.env, "config", "--unset", "core.fsmonitor")
    for folder in (repo.root, sub):
        git_in(folder, repo.env, "config", "core.fsmonitor", str(trap.program))
    repo.framed()
    assert sorted(repo.block()["changed"]) == [" M src/app.py", " M sub"]
    assert repo.run("check") == (0, ["handoff: ok"])
    assert not trap.ran()


def test_a_partial_clone_does_not_fetch_what_it_lacks(repo, tmp_path):
    """A fetch runs a program that the settings of the clone name. Frame ends with an error line instead."""
    repo.git("config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "clone"
    subprocess.run(["git", "-c", "protocol.file.allow=always", "clone", "-q", "--filter=tree:0", "--no-checkout",
                    f"file://{repo.root}", str(clone)], check=True, env=repo.env, capture_output=True)
    trap = Trap(tmp_path)
    git_in(clone, repo.env, "config", "remote.origin.uploadpack", str(trap.program))
    trap.live_under(lambda: git_in(clone, repo.env, "status", check=False))
    code, lines = repo.run("frame", "--repo", str(clone), repo=False)
    assert not trap.ran()
    assert code == 2 and len(lines) == 1 and lines[0].startswith("handoff: error git status failed: "), lines
    assert not (clone / NOTE).exists()


def test_a_change_inside_a_submodule_counts_whatever_the_settings_say(repo, tmp_path):
    sub = add_submodule(repo, tmp_path)
    (sub / "src" / "app.py").write_text("changed\n")
    repo.git("config", "diff.ignoreSubmodules", "all")
    assert repo.git("status", "--porcelain") == "", "the control: this setting hides the change from a plain status"
    repo.framed()
    assert repo.block()["changed"] == [" M sub"]
    assert repo.run("check") == (0, ["handoff: ok"])


def test_a_change_behind_a_line_that_stays_the_same_is_not_seen(repo, tmp_path):
    """The list has the lines of `git status`: the names, and the kind of change. The text of the skill and the
    known limits say what that leaves out. This test holds each of those sentences against a run."""
    sub = add_submodule(repo, tmp_path)
    (repo.root / ".gitignore").write_text("build/\n")
    repo.commit("a rule")
    shared = Repo(tmp_path, "shared")
    subprocess.run(["git", "clone", "-q", str(shared.root), str(repo.root / "vendor")], check=True, env=repo.env,
                   capture_output=True)
    (repo.root / "build").mkdir()
    (repo.root / "build" / "out.txt").write_text("1\n")
    (repo.root / "src" / "app.py").write_text("changed once\n")
    (sub / "src" / "app.py").write_text("changed once\n")
    (repo.root / "vendor" / "new.c").write_text("work that is not committed\n")
    repo.framed()
    assert sorted(repo.block()["changed"]) == [" M src/app.py", " M sub", "?? vendor/"]
    (repo.root / "src" / "app.py").write_text("changed again\n")       # a file that was changed already
    (sub / "tests" / "test_app.py").write_text("another file\n")        # a submodule that was changed already
    (repo.root / "vendor" / "new.c").unlink()                          # a folder that is a repo of its own
    (repo.root / "vendor" / "src" / "app.py").write_text("changed\n")
    (repo.root / "build" / "out.txt").write_text("2\n")                # a file that a rule ignores
    assert repo.run("check") == (0, ["handoff: ok"])


def test_a_file_that_comes_or_goes_inside_a_folder_that_git_does_not_track_is_seen(repo):
    feature = repo.root / "src" / "feature"
    feature.mkdir()
    (feature / "a.py").write_text("1\n")
    (feature / "b.py").write_text("2\n")
    repo.framed()
    assert sorted(repo.block()["changed"]) == ["?? src/feature/a.py", "?? src/feature/b.py"]
    assert repo.run("check") == (0, ["handoff: ok"])
    (feature / "b.py").unlink()
    (feature / "c.py").write_text("3\n")
    assert repo.run("check") == (1, ["stale-changes note=2 now=2", "handoff: summary gaps=1"])


def test_the_settings_for_renames_and_new_files_do_not_change_the_list(repo):
    repo.git("mv", "tests/test_app.py", "tests/test_main.py")
    (repo.root / "new.txt").write_text("x\n")
    repo.git("config", "status.showUntrackedFiles", "no")
    repo.framed(**{"State now": "Half-done: the test file has a new name."})
    assert sorted(repo.block()["changed"]) == ["?? new.txt", "A  tests/test_main.py", "D  tests/test_app.py"]
    for name, value in (("status.renames", "copies"), ("diff.renames", "false"), ("status.showUntrackedFiles", "normal"),
                        ("status.relativePaths", "false"), ("core.quotePath", "false")):
        repo.git("config", name, value)
        assert repo.run("check", cwd=repo.root / "src") == (0, ["handoff: ok"]), name
    (repo.root / "new.txt").unlink()
    assert repo.run("check")[1][0] == "stale-changes note=3 now=2"


def test_other_settings_of_the_repo_still_shape_the_list(repo):
    """Three options of the status call take three settings out. This one stays in: a repo that is told not to
    look at file modes has no line for a file that was made executable. The known limits say so."""
    (repo.root / "src" / "app.py").chmod(0o755)
    repo.framed()
    assert repo.block()["changed"] == [" M src/app.py"]
    repo.git("config", "core.fileMode", "false")
    assert repo.run("check") == (1, ["stale-changes note=1 now=0", "handoff: summary gaps=1"])


def test_a_fact_block_that_is_edited_to_say_what_the_tree_says_now_is_an_error(repo):
    """The shortest way from a stale note to `ok` was to edit the block. The block carries a check value over its
    lines, so an edit by hand is seen."""
    repo.framed()
    (repo.root / "new.py").write_text("x\n")
    new = repo.commit("the tree moves")
    (repo.root / "later.py").write_text("x\n")
    old = repo.block()["head"]
    assert repo.run("check")[1][:2] == [f"stale-head note={old[:7]} now={new[:7]}", "stale-changes note=0 now=1"]
    text = repo.note.read_text()
    edited = text.replace(old, new).replace("dirty: 0\nchanged: []", 'dirty: 1\nchanged:\n  - "?? later.py"')
    assert edited != text
    repo.note.write_text(edited)
    assert repo.run("check") == (2, ["handoff: error the fact block is not as frame wrote it (it was changed after "
                                     "that; when the tree moved, run frame again)"])


def test_what_a_killed_frame_left_is_not_seen_by_git_and_no_file_is_deleted_by_its_name(repo):
    """Frame writes the note under a name that starts with `handoff.tmp.` and renames it. A frame that is killed
    between the two leaves that file. Git must not see it. Frame deletes nothing by the form of a name: a file
    with such a name can be the user's own."""
    code, lines = repo.run("frame")
    assert code == 0 and not [path.name for path in repo.note.parent.iterdir() if path.name != "handoff.md"]
    script = load_script()
    seen = []
    real = os.replace
    script.os.replace = lambda temp, path: seen.append(Path(temp).name)
    try:
        script.write_whole(repo.note, "half a note\n")
    finally:
        script.os.replace = real
    assert len(seen) == 1 and seen[0].startswith("handoff.tmp.") and (repo.note.parent / seen[0]).is_file()
    assert repo.git("status", "--porcelain", "--untracked-files=all") == ""
    assert repo.run("frame")[0] == 0 and (repo.note.parent / seen[0]).read_text() == "half a note\n"
    assert repo.run("check")[1][-1] == "handoff: summary gaps=7"


def test_a_name_that_the_terminal_cannot_show_is_still_a_result(repo):
    repo.git("switch", "-q", "-c", "fix-日本")
    repo.framed()
    repo.git("switch", "-q", "main")
    code, lines = repo.run("check", env=dict(repo.env, LC_ALL="en_US.ISO8859-1", LANG="en_US.ISO8859-1"))
    assert code == 1 and lines[0].startswith("stale-branch note=fix-") and lines[-1] == "handoff: summary gaps=1"


def test_a_work_tree_whose_name_ends_in_a_line_break_gets_its_own_note(tmp_path):
    first, second = Repo(tmp_path, "w"), Repo(tmp_path, "w\n")
    code, lines = second.run("frame")
    assert (code, lines[-1]) == (0, "handoff: wrote .claude/handoff.md")
    assert second.note.is_file() and not first.note.exists()
    assert "handoff" not in (first.root / ".git" / "info" / "exclude").read_text()


# ── check: what is a gap ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ("tldr",) + SECTIONS)
def test_each_part_that_is_missing_or_holds_only_space_is_a_gap(repo, name):
    for value in (None, "   \n\t"):
        repo.framed(**{name: value})
        assert repo.run("check") == (1, [f"empty-section {name}", "handoff: summary gaps=1"]), value


def test_a_heading_inside_a_code_block_starts_no_section(repo):
    repo.framed(**{"State now": "```markdown\n## Environment\n\ntext\n```", "Environment": None})
    assert repo.run("check") == (1, ["empty-section Environment", "handoff: summary gaps=1"])
    repo.framed(**{"State now": "~~~\n## Environment\n```\n~~~\nreal text", "Environment": "x"})
    assert repo.run("check") == (0, ["handoff: ok"])


def test_the_parts_can_stand_in_any_order_and_a_part_can_come_twice(repo):
    repo.framed()
    text = repo.note.read_text()
    head, body = text[:text.index("\n---\n", 4) + 5], text[text.index("\n---\n", 4) + 5:]
    parts = body.split("\n## ")
    repo.note.write_text(head + parts[0] + "".join("\n## " + part for part in reversed(parts[1:])))
    assert repo.run("check") == (0, ["handoff: ok"])
    repo.framed(Environment="   ")
    repo.note.write_text(repo.note.read_text() + "\n## Environment\n\nthe second part has the text\n")
    assert repo.run("check") == (0, ["handoff: ok"])


def test_the_summary_part_is_read_only_above_the_first_heading(repo):
    repo.framed(tldr=None, **{"Open questions": "None.\n\n> [!tldr]\n> too late\n"})
    assert repo.run("check") == (1, ["empty-section tldr", "handoff: summary gaps=1"])
    repo.framed(tldr=None)
    text = repo.note.read_text()
    cut = text.index("\n---\n", 4) + 5
    repo.note.write_text(text[:cut] + "\n> [!tldr] the goal, on the marker line\n" + text[cut:])
    assert repo.run("check") == (0, ["handoff: ok"])


@pytest.mark.parametrize("actions, gap", [
    ("Write the function and run the tests.", "no-next-action"),
    ("- Write the function, run `make test`.", "no-next-action"),
    ("- [x] Write `double` with `make`.", "no-next-action"),
    ("- [ ] Write the function and run the tests.", "no-command"),
    ("- [ ] Write the function.\n- [ ] Then run `make test`.", "no-command"),
    ("- [x] Done with `make`.\n- [ ] Write the function.", "no-command"),
    ("- [ ] Run `` ` ``.", None),
    ("- [ ] Run ` `.", "no-command"),
    ("- [ ] Run the tests:\n\n  ```sh\n  make test\n  ```\n- [ ] Next.", None),
    ("1. [ ] Run `make test`.", None),
    ("* [ ] Run `make test`.", None),
    ("- [x] Done.\n- [ ] Run `make test`.\n- [ ] Then think.", None),
    ("```\n- [ ] Run `make`\n```", "no-next-action"),
])
def test_the_first_open_action_must_name_a_command(repo, actions, gap):
    repo.framed(**{"Next actions": actions})
    assert repo.run("check") == ((1, [gap, "handoff: summary gaps=1"]) if gap else (0, ["handoff: ok"]))


def test_a_note_for_another_branch_or_other_changes_is_stale(repo):
    repo.framed()
    repo.git("switch", "-q", "-c", "claude/next")
    assert repo.run("check") == (1, ["stale-branch note=main now=claude/next", "handoff: summary gaps=1"])
    repo.git("switch", "-q", "main")
    (repo.root / "new.txt").write_text("x\n")
    assert repo.run("check") == (1, ["stale-changes note=0 now=1", "handoff: summary gaps=1"])
    (repo.root / "new.txt").unlink()
    repo.git("checkout", "-q", "--detach")
    assert repo.run("check") == (1, ["stale-branch note=main now=detached", "handoff: summary gaps=1"])


def test_the_same_count_of_other_changes_is_stale_too(repo):
    (repo.root / "one.txt").write_text("x\n")
    repo.framed()
    (repo.root / "one.txt").rename(repo.root / "two.txt")
    assert repo.run("check") == (1, ["stale-changes note=1 now=1", "handoff: summary gaps=1"])


def test_a_path_that_the_note_names_and_that_is_gone_is_a_gap(repo):
    (repo.root / "docs").mkdir()
    (repo.root / "docs" / "plan.md").write_text("plan\n")
    repo.commit("docs")
    state = ("See `src/app.py:2`, `src/app.py:1-2`, `./tests/test_app.py`, `docs/` and `docs/plan.md`. "
             "Gone: `src/gone.py:12` and `docs/old/plan.md`, and again `src/gone.py`, and `./src/gone2.py`.")
    repo.framed(tldr="The goal is in `docs/goal.md`.", **{"State now": state})
    assert repo.run("check") == (1, ["missing-path docs/goal.md", "missing-path src/gone.py", "missing-path docs/old/plan.md",
                                     "missing-path src/gone2.py", "handoff: summary gaps=4"])


def test_what_only_looks_like_a_path_is_no_gap(repo):
    state = ("Refs `origin/dev` and `claude/next`. A command `python3 -m pytest tests/gone.py -q` and its end "
             "`tests/gone.py -q`. A pattern `src/*.py` "
             "and `src/**/x.py`. A way up `src/../gone.py`. An absolute `/src/gone.py` and `~/src/gone.py`. "
             "A link `https://example.com/src/gone.py`. A flag `--out=src/gone.py`. A name alone `gone.py`.\n\n"
             "```\nsrc/gone.py\n`src/gone.py`\n```\n")
    repo.framed(**{"State now": state})
    assert repo.run("check") == (0, ["handoff: ok"])


def test_a_path_whose_first_part_is_not_in_the_tree_is_not_reported(repo):
    """The price of the rule that keeps branch names out. The text of the skill and the known limits say so."""
    repo.framed(**{"State now": "A slip `scr/app.py`, and a file at the top that is gone: `Makefile`."})
    assert repo.run("check") == (0, ["handoff: ok"])


def test_many_missing_paths_are_all_counted_and_the_first_twenty_are_printed(repo):
    repo.framed(**{"State now": " ".join(f"`src/gone{number}.py`" for number in range(25))})
    code, lines = repo.run("check")
    assert code == 1 and lines[-1] == "handoff: summary gaps=25"
    assert lines[:-1] == [f"missing-path src/gone{number}.py" for number in range(20)] + ["missing-path-more 5"]


def test_the_gaps_come_in_one_fixed_order(repo):
    repo.framed(tldr=None, **{"Next actions": "- [ ] think", "State now": "`src/gone.py`", "Open questions": None})
    (repo.root / "x").write_text("x")
    repo.commit("x")
    code, lines = repo.run("check")
    assert code == 1
    assert [line.split()[0] for line in lines] == ["stale-head", "empty-section", "empty-section", "no-command",
                                                   "missing-path", "handoff:"]
    assert lines[-1] == "handoff: summary gaps=5"


def test_a_character_that_moves_the_cursor_is_not_printed(repo):
    repo.framed(**{"State now": "`src/go\x1bcne.py`"})
    code, lines = repo.run("check")
    assert code == 1 and "\x1b" not in "\n".join(lines) and lines[0].startswith("missing-path src/go")


# ── check: what it refuses to read ───────────────────────────────────────────

def test_no_note_is_an_error_and_not_a_result(repo):
    code, lines = repo.run("check")
    assert (code, lines) == (2, ["handoff: error no note at .claude/handoff.md"])


EDITS = {
    "a line that frame does not write": lambda text: text.replace("dirty: 0\n", "dirty: 0\nnote: mine\n"),
    "a missing key": lambda text: text.replace("dirty: 0\n", ""),
    "another type": lambda text: text.replace("type: handoff", "type: report"),
    "a commit id that is no commit id": lambda text: text.replace('head: "', 'head: "zz'),
    "a branch that is no JSON text": lambda text: text.replace('branch: "main"', "branch: main"),
    "a branch that is a number": lambda text: text.replace('branch: "main"', "branch: 7"),
    "a count that does not fit the list": lambda text: text.replace("dirty: 0", "dirty: 3"),
    "a list that is longer than frame writes": lambda text: text.replace(
        "dirty: 0\nchanged: []", "dirty: 51\nchanged:\n" + "\n".join(f'  - "?? f{number}"' for number in range(51))),
    "a rest beside a list that is not full": lambda text: text.replace(
        "dirty: 0\nchanged: []", 'dirty: 5\nchanged:\n  - "?? f"\nchanged_more: 4'),
    "a time that is no time": lambda text: text.replace('written: "2', 'written: "about 2'),
    "no check value": lambda text: "\n".join(line for line in text.split("\n") if not line.startswith("seal: ")),
    "a check value of another block": lambda text: text[:text.index('seal: "') + 7] + "0" * 32
    + text[text.index('seal: "') + 39:],
    "a value with a space too many": lambda text: text.replace('branch: "main"', 'branch:  "main"'),
    "a value in another JSON spelling": lambda text: text.replace('branch: "main"', 'branch: "\\u006dain"'),
    "no end of the block": lambda text: text.replace("\n---\n", "\n", 1),
    "no block at all": lambda text: text[text.index("\n---\n", 4) + 5:],
    "a block that does not start the file": lambda text: "\n" + text,
}


@pytest.mark.parametrize("name", sorted(EDITS))
def test_a_fact_block_that_is_not_as_frame_wrote_it_is_an_error(repo, name):
    repo.framed()
    text = repo.note.read_text()
    changed = EDITS[name](text)
    assert changed != text, "the edit of this case changed nothing"
    repo.note.write_text(changed)
    code, lines = repo.run("check")
    assert code == 2 and len(lines) == 1 and lines[0].startswith("handoff: error "), lines


SAME_FACTS = {"a value with a space too many", "a value in another JSON spelling"}
NO_VALUE_TO_WRITE = {"no check value", "a check value of another block", "no block at all"}


def sealed_again(script, text):
    """The note with the check value that fits its block lines as they stand now."""
    lines = text.split("\n")
    at = next(number for number, line in enumerate(lines) if line.startswith("seal: "))
    lines[at] = f'seal: "{script.seal(lines[:at])}"'
    return "\n".join(lines)


@pytest.mark.parametrize("name", sorted(set(EDITS) - SAME_FACTS - NO_VALUE_TO_WRITE))
def test_an_edit_with_a_new_check_value_is_an_error_too(repo, name):
    """The check value shows an edit by hand. It holds no secret, so each rule on the block must hold by itself,
    also in a block whose check value fits."""
    script = load_script()
    repo.framed()
    text = repo.note.read_text()
    repo.note.write_text(sealed_again(script, EDITS[name](text)))
    code, lines = repo.run("check")
    assert code == 2 and len(lines) == 1, lines
    assert lines[0].startswith("handoff: error the fact block is not as frame wrote it (") and "changed after" not in lines[0]
    for same in sorted(SAME_FACTS):   # the control: the same facts in another spelling, sealed this way, are read
        repo.note.write_text(sealed_again(script, EDITS[same](text)))
        assert repo.run("check") == (0, ["handoff: ok"]), same


def test_a_note_that_is_no_text_too_large_or_no_plain_file_is_an_error(repo, tmp_path):
    repo.framed()
    whole = repo.note.read_bytes()
    repo.note.write_bytes(whole + b"\xff\xfe\n")
    assert repo.run("check")[0] == 2
    repo.note.write_bytes(whole + b"x" * (1 << 20))
    code, lines = repo.run("check")
    assert code == 2 and "large" in lines[-1]
    repo.note.unlink()
    (tmp_path / "elsewhere.md").write_bytes(whole)
    repo.note.symlink_to(tmp_path / "elsewhere.md")
    code, lines = repo.run("check")
    assert code == 2 and lines[-1].startswith("handoff: error ")
    repo.note.unlink()
    os.mkfifo(repo.note)
    for command in ("check", "frame"):   # neither waits for a writer, and neither reads what is no plain file
        assert repo.run(command) == (2, [f"handoff: error {NOTE} is no plain file"]), command


def test_a_note_that_git_tracks_is_not_read(repo):
    """Such a note came with the repo. It was not written by a session in this work tree."""
    repo.framed()
    assert repo.run("check") == (0, ["handoff: ok"])
    repo.git("add", "-f", NOTE)
    code, lines = repo.run("check")
    assert code == 2 and len(lines) == 1 and "git tracks .claude/handoff.md" in lines[0]
    repo.git("commit", "-q", "-m", "the note comes with the repo")
    code, lines = repo.run("check")
    assert code == 2 and len(lines) == 1 and "git tracks .claude/handoff.md" in lines[0]


def test_a_folder_that_is_no_work_tree_and_a_command_that_is_none_are_errors(repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for command in ("frame", "check"):
        done = subprocess.run([PY, str(SCRIPT), command, "--repo", str(plain)], capture_output=True, text=True, env=repo.env)
        assert done.returncode == 2 and done.stdout.splitlines()[-1].startswith("handoff: error "), done.stdout
    done = subprocess.run([PY, str(SCRIPT), "check", "--repo", str(repo.root / ".git")], capture_output=True, text=True, env=repo.env)
    assert done.returncode == 2 and done.stdout.splitlines()[-1].startswith("handoff: error ")
    # a call for help is one of them: each run ends with a result line or an error line, and a help text is neither
    for argv in (["write"], [], ["check", "--force"], ["--help"], ["frame", "--help"], ["check", "-h"]):
        code, lines = repo.run(*argv, repo=bool(argv))
        assert code == 2 and len(lines) == 1 and lines[0].startswith("handoff: error "), argv


def test_check_writes_nothing(repo):
    repo.framed()
    def snapshot():
        files = [path for path in sorted(repo.root.rglob("*")) if path.is_file()]
        return {str(path.relative_to(repo.root)): path.read_bytes() for path in files}   # the git folder too
    os.utime(repo.root / "src" / "app.py", (1, 1))   # a plain `git status` would now write the index again
    before = snapshot()
    assert repo.run("check") == (0, ["handoff: ok"])
    assert snapshot() == before


# ── the two small readers, against a second version on random text ───────────

def reference_spans(line):
    """The code spans of one line, found the slow way: for each run of backticks, the next run of the same length."""
    runs, index = [], 0
    while index < len(line):
        if line[index] == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            runs.append((index, end))
            index = end
        else:
            index += 1
    spans, position = [], 0
    while position < len(runs):
        start, end = runs[position]
        partner = next((later for later in range(position + 1, len(runs))
                        if runs[later][1] - runs[later][0] == end - start), None)
        if partner is None:
            position += 1
            continue
        spans.append(line[end:runs[partner][0]])
        position = partner + 1
    return spans


def test_the_code_spans_of_a_line_agree_with_a_second_version_on_random_text():
    script = load_script()
    rng = random.Random(20261115)
    for _ in range(4000):
        line = "".join(rng.choice("``` ab/.") for _ in range(rng.randrange(0, 24)))
        assert script.code_spans(line) == reference_spans(line), repr(line)
    assert script.code_spans("a `b` ``c`d`` `e") == ["b", "c`d"]


def test_a_megabyte_of_backtick_runs_is_read_in_the_time_of_its_length():
    """Found by an automated review: the first reader looked from each run to the end of the line for its partner,
    so a line built against it took the square of what it should. Each run is now looked at one time."""
    script = load_script()
    half = " ".join("`" * width for width in range(1, 1001))
    lonely = " ".join("`" * width for width in range(1, 1400))
    started = time.monotonic()
    assert script.code_spans(half + " x " + half) == [half[1:] + " x "]
    assert script.code_spans(lonely) == []
    assert time.monotonic() - started < 2, "the first reader needed more than thirty seconds for these two lines"


def reference_sections(lines):
    """name -> text, with fences found by a second, plainer reading of the same rules."""
    found, name, fence = {}, None, None
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        marker = stripped[:1] if stripped[:3] in ("```", "~~~") and indent <= 3 else ""
        length = len(stripped) - len(stripped.lstrip(marker)) if marker else 0
        if fence is None:
            if marker and not (marker == "`" and "`" in stripped[length:]):
                fence = (marker, length)
            elif line.startswith("## "):
                name = line[3:].strip()
                found.setdefault(name, "")
                continue
        elif marker == fence[0] and length >= fence[1] and not stripped[length:].strip():
            fence = None
        if name is not None:
            found[name] += line + "\n"
    return found


def test_the_sections_of_a_body_agree_with_a_second_version_on_random_text():
    script = load_script()
    rng = random.Random(7)
    pieces = ["## A", "## B", "##  A ", "### A", "```", "````", "~~~", "~~~~", "``` py", "```a`b", " ```", "    ```",
              "text", "", "## A#", "> ## A", "~~~ x", "```   "]
    for _ in range(4000):
        lines = [rng.choice(pieces) for _ in range(rng.randrange(0, 14))]
        _, mine = script.split_body(lines)
        assert {name: "".join(text + "\n" for text, _ in rows) for name, rows in mine.items()} == reference_sections(lines), lines


def test_the_fact_block_that_frame_writes_is_the_one_that_check_reads_on_random_names():
    script = load_script()
    rng = random.Random(3)
    alphabet = ["a", "/", " ", '"', "\\", "\n", "\t", "é", "中", "\udc80", ":", "#", "-", "'", "\x1b"]
    for _ in range(1500):
        more = rng.choice([0, 0, 4])   # frame names a rest only when the list is full
        count = script.MAX_CHANGED if more else rng.randrange(0, 5)
        changed = ["".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 9))) for _ in range(count)]
        facts = script.Facts(branch=rng.choice([None, "".join(rng.choice(alphabet) for _ in range(5))]),
                             head=rng.choice([None, "a" * 40, "0123456789" * 4, "f" * 64]),
                             dirty=len(changed) + more, changed=changed, more=more)
        text = script.fact_block(facts, "2026-11-15T00:00:00Z") + script.SKELETON
        assert yaml.safe_load(text[4:text.index("\n---\n", 4)])["dirty"] == facts.dirty
        read, _ = script.read_note(text)
        assert read == facts, text
        assert json.loads(json.dumps(facts.changed)) == facts.changed
