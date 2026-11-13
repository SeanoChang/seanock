"""The harness version, its changelog and its architecture document stay in step.

The harness has one version, in VERSION at the repo root. Three things can
drift from it with no error:

- the changelog, when a release changes the number and adds no entry;
- the architecture document, when a major release keeps the old description;
- the decision records, when a file is added out of order or without a number.

The status line shows the version of the tree that is live. That part is
tested against a real git repo in a temp folder, in two ways: with the
script's own override for the folder it reads (STATUSLINE_HARNESS_DIR), and
through a symlink to a copy of the script, which is how the installed script
runs.

This file does not check that a git tag exists for the version: CI checks out
no tags, and the tag is made after the release merge.

Run: python3 -m pytest tests/test_harness_version.py -q -p no:cacheprovider
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERSION = REPO / "VERSION"
CHANGELOG = REPO / "CHANGELOG.md"
ARCHITECTURE = REPO / "docs" / "architecture.md"
DECISIONS = REPO / "docs" / "decisions"
STATUSLINE = REPO / "config" / "claude" / "statusline-command.sh"

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
ENTRY = re.compile(r"^## (\S+) \((unreleased|\d{4}-\d{2}-\d{2})\)$", re.M)


def version():
    return VERSION.read_text(encoding="utf-8")


def entries(text):
    """Each release heading of the changelog as (version, date or 'unreleased')."""
    return ENTRY.findall(text)


def as_numbers(v):
    return tuple(int(part) for part in v.split("."))


def test_the_version_is_one_plain_semver_line():
    assert SEMVER.fullmatch(version().removesuffix("\n")), (
        f"VERSION holds {version()!r}; it must be X.Y.Z and one line end"
    )
    assert version().endswith("\n")


def test_the_top_changelog_entry_is_the_version():
    found = entries(CHANGELOG.read_text(encoding="utf-8"))
    assert found, "CHANGELOG.md has no '## X.Y.Z (date)' heading"
    assert found[0][0] == version().strip(), (
        f"VERSION is {version().strip()} and the top changelog entry is {found[0][0]}"
    )


def test_every_changelog_heading_is_a_release_entry():
    text = CHANGELOG.read_text(encoding="utf-8")
    headings = re.findall(r"^## .*$", text, flags=re.M)
    assert len(headings) == len(entries(text)), (
        "a '## ' heading in the changelog is not of the form '## X.Y.Z (date)': "
        f"{headings}"
    )
    for v, _ in entries(text):
        assert SEMVER.fullmatch(v), f"changelog entry {v!r} is not X.Y.Z"


def test_changelog_entries_go_from_new_to_old_and_only_the_top_one_is_unreleased():
    found = entries(CHANGELOG.read_text(encoding="utf-8"))
    numbers = [as_numbers(v) for v, _ in found]
    assert numbers == sorted(set(numbers), reverse=True), (
        f"changelog entries are out of order or repeated: {[v for v, _ in found]}"
    )
    assert all(date != "unreleased" for _, date in found[1:]), (
        "only the top changelog entry may be unreleased"
    )


def test_the_architecture_document_names_the_current_major():
    major = version().split(".")[0]
    text = " ".join(ARCHITECTURE.read_text(encoding="utf-8").split())
    named = re.findall(r"describes major version (\d+)", text)
    assert named == [major], (
        f"architecture.md says major version {named}, and VERSION is major {major}"
    )


def test_decision_records_are_numbered_from_one_with_no_gap():
    names = sorted(p.name for p in DECISIONS.iterdir())
    assert names, "docs/decisions is empty"
    for position, name in enumerate(names, start=1):
        match = re.fullmatch(r"(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md", name)
        assert match, f"{name} is not named NNNN-short-name.md"
        assert int(match.group(1)) == position, (
            f"{name} should have the number {position:04d}: records are numbered "
            "from 0001 with no gap"
        )


@pytest.mark.parametrize("path", sorted(DECISIONS.glob("*.md")), ids=lambda p: p.name)
def test_a_decision_record_says_what_was_rejected(path):
    text = path.read_text(encoding="utf-8")
    number = path.name[:4]
    assert re.search(rf"^# {number} \S", text, flags=re.M), (
        f"{path.name} has no '# {number} title' heading"
    )
    assert "Rejected:" in text, f"{path.name} does not say what was rejected"


# ── the status line ──────────────────────────────────────────────────────────

ANSI = re.compile(r"\x1b\[[0-9;]*m")
needs_tools = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("git") is None,
    reason="the status line script needs jq and git",
)


def git(folder, *args):
    env = dict(os.environ)
    for name in [n for n in env if n.startswith("GIT_")]:
        del env[name]
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    subprocess.run(["git", "-C", str(folder), *args], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def commit(folder, name):
    (folder / name).write_text(name, encoding="utf-8")
    git(folder, "add", name)
    git(folder, "commit", "-q", "-m", name)


def run_status_line(script, tmp_path, harness_dir=None, cwd=None):
    """Line 1 of the status line, colours removed, for a session in tmp_path.

    With harness_dir the script reads that folder. Without it the script must
    find the folder it really lives in.
    """
    env = dict(os.environ)
    env.pop("SSH_CONNECTION", None)
    env.pop("STATUSLINE_HARNESS_DIR", None)
    if harness_dir is not None:
        env["STATUSLINE_HARNESS_DIR"] = str(harness_dir)
    out = subprocess.run(
        ["sh", str(script)],
        input=json.dumps({"cwd": str(tmp_path / "elsewhere")}),
        capture_output=True, text=True, env=env, check=True, cwd=cwd,
    ).stdout
    return ANSI.sub("", out.splitlines()[0]) if out.strip() else ""


def first_line(harness_dir, tmp_path):
    return run_status_line(STATUSLINE, tmp_path, harness_dir=harness_dir)


@pytest.fixture
def harness(tmp_path):
    """A git repo on main with one commit that has the tag v1.2.3."""
    folder = tmp_path / "harness"
    folder.mkdir()
    (tmp_path / "elsewhere").mkdir()
    git(folder, "init", "-q", "-b", "main")
    commit(folder, "a")
    git(folder, "tag", "v1.2.3")
    return folder


@needs_tools
def test_the_status_line_shows_the_tag_of_a_clean_tree_on_main(harness, tmp_path):
    assert re.search(r"(^|\s)v1\.2\.3(\s|$)", first_line(harness, tmp_path))


@needs_tools
def test_the_status_line_counts_commits_past_the_tag(harness, tmp_path):
    commit(harness, "b")
    commit(harness, "c")
    assert re.search(r"(^|\s)v1\.2\.3\+2(\s|$)", first_line(harness, tmp_path))


@needs_tools
def test_the_status_line_marks_a_changed_tree(harness, tmp_path):
    (harness / "a").write_text("changed", encoding="utf-8")
    assert re.search(r"(^|\s)v1\.2\.3\*(\s|$)", first_line(harness, tmp_path))


@needs_tools
def test_the_status_line_names_a_branch_that_is_not_main(harness, tmp_path):
    git(harness, "checkout", "-q", "-b", "work/x")
    commit(harness, "b")
    assert re.search(r"(^|\s)v1\.2\.3\+1@work/x(\s|$)", first_line(harness, tmp_path))


@needs_tools
def test_the_status_line_shows_every_mark_at_once(harness, tmp_path):
    git(harness, "checkout", "-q", "-b", "work/x")
    commit(harness, "b")
    (harness / "a").write_text("changed", encoding="utf-8")
    assert re.search(r"(^|\s)v1\.2\.3\+1\*@work/x(\s|$)", first_line(harness, tmp_path))


@needs_tools
def test_the_status_line_names_the_commit_of_a_detached_tree(harness, tmp_path):
    commit(harness, "b")
    git(harness, "checkout", "-q", "--detach")
    assert re.search(r"(^|\s)v1\.2\.3\+1@[0-9a-f]{7}(\s|$)", first_line(harness, tmp_path))


@needs_tools
def test_the_status_line_takes_the_newest_release_tag(harness, tmp_path):
    commit(harness, "b")
    git(harness, "tag", "v1.10.0")
    commit(harness, "c")
    assert re.search(r"(^|\s)v1\.10\.0\+1(\s|$)", first_line(harness, tmp_path))


@needs_tools
@pytest.mark.parametrize("tag", ["skill-v9.9.9", "harness-v9.9.9", "v9", "vault"])
def test_the_status_line_ignores_tags_that_are_not_releases(harness, tmp_path, tag):
    commit(harness, "b")
    git(harness, "tag", tag)
    line = first_line(harness, tmp_path)
    assert "v9" not in line and "vault" not in line
    assert re.search(r"(^|\s)v1\.2\.3\+1(\s|$)", line)


@needs_tools
def test_the_status_line_shows_no_version_when_there_is_no_release_tag(tmp_path):
    folder = tmp_path / "untagged"
    folder.mkdir()
    (tmp_path / "elsewhere").mkdir()
    git(folder, "init", "-q", "-b", "main")
    commit(folder, "a")
    assert not re.search(r"\bv\d+\.\d+\.\d+", first_line(folder, tmp_path))


@needs_tools
def test_the_status_line_shows_no_version_outside_a_git_repo(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not re.search(r"\bv\d+\.\d+\.\d+", first_line(plain, tmp_path))


@needs_tools
@pytest.mark.parametrize("kind", ["absolute", "relative", "chain"])
def test_the_installed_script_finds_its_tree_through_a_symlink(harness, tmp_path, kind):
    """~/.claude/statusline-command.sh is a symlink into the harness tree."""
    inside = harness / "config" / "claude"
    inside.mkdir(parents=True)
    real = inside / "statusline-command.sh"
    shutil.copy(STATUSLINE, real)          # untracked, so the tree stays clean
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    link = home / "statusline-command.sh"
    if kind == "absolute":
        link.symlink_to(real)
    elif kind == "relative":
        link.symlink_to(os.path.relpath(real, home))
    else:
        middle = tmp_path / "home" / "middle.sh"
        middle.symlink_to(os.path.relpath(real, middle.parent))
        link.symlink_to("../middle.sh")
    line = run_status_line(link, tmp_path, cwd=tmp_path / "elsewhere")
    assert re.search(r"(^|\s)v1\.2\.3(\s|$)", line)
