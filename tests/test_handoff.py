"""handoff.py: the frame of a resume note, and the check of a note.

The script runs as a program on real git repos in temp folders. The first test
is the user's path: pause (frame, fill, check), then resume in a tree that
moved. The rest holds each part: the facts that frame reads from git, where the
note lives and that git does not see it, what check reads as a gap, and what it
refuses to read.

Run: python3 -m pytest tests/test_handoff.py -q -p no:cacheprovider
"""
import importlib.util
import json
import os
import random
import subprocess
import sys
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
    assert block["dirty"] == 3
    assert sorted(block["changed"]) == [" M src/app.py", "?? new.txt", "R  tests/test_main.py"]
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
    assert code == 0 and "added 2 lines to .git/info/exclude" in lines
    exclude = (repo.root / ".git" / "info" / "exclude").read_text()
    assert exclude.endswith("/.claude/handoff.md\n/.claude/handoff.prev.md\n")
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
    assert exclude.read_text() == "*.log\n/.claude/handoff.md\n/.claude/handoff.prev.md\n"


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


def test_a_file_at_the_note_path_that_is_no_note_is_kept_too(repo):
    repo.note.parent.mkdir()
    repo.note.write_text("my own notes, not a frame\n")
    assert repo.run("frame")[0] == 0
    assert (repo.root / PREV).read_text() == "my own notes, not a frame\n"
    assert repo.block()["dirty"] == 0, "the facts must be read after the note is kept out of git"


def test_a_link_at_the_note_path_is_replaced_and_its_target_is_not_written(repo, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("do not write here\n")
    repo.note.parent.mkdir()
    repo.note.symlink_to(target)
    assert repo.run("frame")[0] == 0
    assert target.read_text() == "do not write here\n"
    assert repo.note.is_file() and not repo.note.is_symlink()
    assert not (repo.root / PREV).exists()


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
    code, lines = other.run("frame")
    assert code == 2 and "git check-ignore failed" in lines[-1]
    assert list(elsewhere.iterdir()) == []


def test_a_folder_at_the_note_path_or_a_file_at_the_claude_path_is_an_error(repo, tmp_path):
    (repo.root / NOTE).mkdir(parents=True)
    code, lines = repo.run("frame")
    assert code == 2 and lines[-1].startswith("handoff: error ")
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
    "a count that does not fit the list": lambda text: text.replace("dirty: 0", "dirty: 3"),
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
    code, lines = repo.run("check")   # must not wait for a writer
    assert code == 2 and lines[-1].startswith("handoff: error ")


def test_a_folder_that_is_no_work_tree_and_a_command_that_is_none_are_errors(repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for command in ("frame", "check"):
        done = subprocess.run([PY, str(SCRIPT), command, "--repo", str(plain)], capture_output=True, text=True, env=repo.env)
        assert done.returncode == 2 and done.stdout.splitlines()[-1].startswith("handoff: error "), done.stdout
    done = subprocess.run([PY, str(SCRIPT), "check", "--repo", str(repo.root / ".git")], capture_output=True, text=True, env=repo.env)
    assert done.returncode == 2 and done.stdout.splitlines()[-1].startswith("handoff: error ")
    for argv in (["write"], [], ["check", "--force"]):
        code, lines = repo.run(*argv, repo=bool(argv))
        assert code == 2 and lines[-1].startswith("handoff: error "), argv


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
