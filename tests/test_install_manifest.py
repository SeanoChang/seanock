"""install.sh puts every part of the harness into a home folder, and loses nothing.

The installer reads manifest.txt. Two things can go wrong with no error:

- a part under config/ has no row in the manifest, so a new machine never gets
  it (the six workers had no row until 0.1.0; their links were made by hand);
- the installer changes how it treats a file that is already there.

So this file checks the manifest against the tree, and then runs the real
install.sh against a home folder in a temp dir: an empty home, a second run, a
dry run, a file in the way, and a rendered file that was changed in place.
Nothing outside the temp dir is written: every destination, the render folder
and the backup folder are under $HOME.

Run: python3 -m pytest tests/test_install_manifest.py -q -p no:cacheprovider
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "manifest.txt"
INSTALL = REPO / "install.sh"

# Parts under config/ that are not installed, each with its reason.
NOT_INSTALLED = {
    "config/codex/AGENTS.src.md": "an input of the AGENTS.md build",
    "config/codex/build_agents_md.py": "the AGENTS.md build itself",
}

WORKERS = ["adversary", "builder", "planner", "scout", "scribe", "verifier"]


def rows():
    """The manifest as (mode, source, destination), comments and blanks left out."""
    found = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        mode, source, destination = line.split()
        found.append((mode, source, destination))
    return found


def visible(folder):
    """The entries a shell `*` gives: no dot files."""
    return sorted(p for p in folder.iterdir() if not p.name.startswith("."))


def install(home, *args):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["bash", str(INSTALL), *args], capture_output=True, text=True, env=env,
    )


def in_home(home, destination):
    assert destination.startswith("~/"), destination
    return home / destination[2:]


def snapshot(folder):
    """Every path under folder with what it is, to compare two states."""
    state = {}
    for path in sorted(folder.rglob("*")):
        key = path.relative_to(folder).as_posix()
        if path.is_symlink():
            state[key] = ("link", os.readlink(path))
        elif path.is_dir():
            state[key] = ("dir", "")
        else:
            state[key] = ("file", path.read_bytes())
    return state


# ── the manifest against the tree ────────────────────────────────────────────

def test_every_part_under_config_has_a_row_or_a_reason():
    sources = {source for _, source, _ in rows()}
    missing = []
    for group in visible(REPO / "config"):
        for part in visible(group):
            name = part.relative_to(REPO).as_posix()
            if name not in sources and name not in NOT_INSTALLED:
                missing.append(name)
    assert not missing, (
        f"{missing} would not be installed: add a row to manifest.txt, or a "
        "reason to NOT_INSTALLED in this file"
    )


def readme_test_folders():
    """The folder of each pytest command in the README's list of tests."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    return re.findall(r"^python3 -m pytest (\S+) -q -p no:cacheprovider$", text, flags=re.MULTILINE)


def test_no_test_run_writes_bytecode_into_the_repo(tmp_path):
    """Some test files load a source file of config/ as a module. With
    bytecode on, Python writes a __pycache__ folder beside that file: a part
    with no manifest row under config/codex (the test above fails on it), and
    a folder the installer links under config/claude/hooks and the skills.
    conftest.py at the repo root turns bytecode off for every run, and
    pytest.ini makes pytest read it.

    This takes each folder the README gives a pytest command for and collects
    its tests in a copy of the repo, with the caller's own switch taken away.
    Collecting reads every test file, so a load at the top of a file is seen.
    One folder is also collected by a run that starts inside it. The merge
    gate is loaded inside a test, so that one test is run by name."""
    folders = readme_test_folders()
    assert len(folders) >= 5 and "tests" in folders and "config/claude/hooks/tests" in folders, (
        f"the README no longer lists its pytest commands in the form this test reads: {folders}"
    )
    copy = tmp_path / "repo"
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=REPO, capture_output=True, check=True).stdout
    for name in filter(None, listed.decode().split("\0")):
        source = REPO / name
        if source.is_file():                 # a tracked file that is deleted in the work tree is not copied
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}

    def pytest_in_the_copy(*args, start_in="."):
        return subprocess.run([sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
                              cwd=copy / start_in, env=env, capture_output=True, text=True)

    for folder in folders:
        done = pytest_in_the_copy(folder, "--collect-only")
        assert done.returncode == 0 and re.search(r"^\d+ tests? collected", done.stdout, flags=re.MULTILINE), (
            f"could not collect the tests of {folder}:\n" + done.stdout + done.stderr
        )
    # A run that starts inside a folder of the repo. Without pytest.ini, pytest
    # takes that folder as its root and does not read the conftest.py above it.
    inside = "config/claude/skills/brief-artifact"
    done = pytest_in_the_copy("tests", "--collect-only", start_in=inside)
    assert done.returncode == 0 and re.search(r"^\d+ tests? collected", done.stdout, flags=re.MULTILINE), (
        f"could not collect the tests from inside {inside}:\n" + done.stdout + done.stderr
    )
    one_test = "test_an_internal_error_after_the_merge_is_seen_is_a_deny"
    done = pytest_in_the_copy("config/claude/hooks/tests/test_merge_gate.py", "-k", one_test)
    assert done.returncode == 0 and re.search(r"^1 passed\b", done.stdout, flags=re.MULTILINE), (
        f"the run of {one_test} did not pass with exactly one test:\n" + done.stdout + done.stderr
    )
    # pytest writes one __pycache__ at the root, for conftest.py itself, before that file can turn bytecode off.
    written = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("__pycache__") if p.parent != copy)
    assert not written, f"a test run wrote bytecode into the repo: {written}"


def test_every_row_and_every_reason_names_a_real_path():
    for _, source, _ in rows():
        assert (REPO / source).exists(), f"manifest.txt names {source}, which is not there"
    for name in NOT_INSTALLED:
        assert (REPO / name).exists(), f"NOT_INSTALLED names {name}, which is not there"
    both = {source for _, source, _ in rows()} & set(NOT_INSTALLED)
    assert not both, f"{both} has a manifest row and a reason not to install it"


def test_every_destination_is_under_the_home_folder_and_used_once():
    destinations = [destination for _, _, destination in rows()]
    for destination in destinations:
        assert destination.startswith("~/."), destination
        assert ".." not in destination.split("/"), destination
    assert len(destinations) == len(set(destinations)), "a destination has two rows"


def test_the_manifest_installs_the_workers():
    assert ("merge", "config/claude/agents", "~/.claude/agents") in rows()
    names = sorted(p.stem for p in (REPO / "config/claude/agents").glob("*.md"))
    assert names == WORKERS


# ── the real installer in a temp home ────────────────────────────────────────

@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


@pytest.fixture
def installed(home):
    result = install(home)
    assert result.returncode == 0, result.stdout + result.stderr
    return home


def test_an_empty_home_gets_every_part(installed):
    for mode, source, destination in rows():
        target = in_home(installed, destination)
        if mode == "merge":
            assert target.is_dir() and not target.is_symlink(), destination
            for part in visible(REPO / source):
                link = target / part.name
                assert link.is_symlink(), f"{link} is not a link"
                assert Path(os.readlink(link)) == part
        elif source.endswith(".tmpl"):
            rendered = installed / ".dotfiles-rendered" / source[len("config/"):-len(".tmpl")]
            assert Path(os.readlink(target)) == rendered
            text = rendered.read_text(encoding="utf-8")
            assert "__HOME__" not in text
            assert str(installed) in text
        else:
            assert Path(os.readlink(target)) == REPO / source
    for name in WORKERS:
        worker = installed / ".claude" / "agents" / f"{name}.md"
        assert worker.resolve() == (REPO / "config/claude/agents" / f"{name}.md").resolve()
    assert not (installed / ".dotfiles-backup").exists(), "an empty home needs no backup"


def test_a_second_run_changes_nothing(installed):
    before = snapshot(installed)
    result = install(installed)
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(installed) == before
    for word in ("linked", "created", "backed up", "drifted"):
        assert word not in result.stdout, result.stdout


def test_a_dry_run_writes_nothing_and_names_what_it_would_link(home):
    result = install(home, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(home.iterdir()) == []
    assert "~/.claude/agents/verifier.md" in result.stdout
    assert "~/.codex/AGENTS.md" in result.stdout


def test_a_dry_run_on_an_installed_home_changes_nothing(installed):
    before = snapshot(installed)
    result = install(installed, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(installed) == before


def test_one_group_installs_alone(home):
    result = install(home, "--only", "codex")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / ".codex" / "AGENTS.md").is_symlink()
    assert not (home / ".claude").exists()


def test_a_file_in_the_way_is_moved_to_the_backup_and_not_lost(home):
    (home / ".claude" / "agents").mkdir(parents=True)
    rules = home / ".claude" / "CLAUDE.md"
    rules.write_text("my own rules", encoding="utf-8")
    old_worker = home / ".claude" / "agents" / "verifier.md"
    old_worker.symlink_to("/somewhere/else/verifier.md")
    own_worker = home / ".claude" / "agents" / "mine.md"
    own_worker.write_text("a worker of this machine", encoding="utf-8")

    result = install(home)
    assert result.returncode == 0, result.stdout + result.stderr

    assert Path(os.readlink(rules)) == REPO / "config/claude/CLAUDE.md"
    kept = list((home / ".dotfiles-backup").glob("*/.claude/CLAUDE.md"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "my own rules"
    moved = list((home / ".dotfiles-backup").glob("*/.claude/agents/verifier.md"))
    assert len(moved) == 1 and os.readlink(moved[0]) == "/somewhere/else/verifier.md"
    assert Path(os.readlink(old_worker)) == REPO / "config/claude/agents/verifier.md"
    assert own_worker.read_text(encoding="utf-8") == "a worker of this machine"


def test_a_rendered_file_changed_in_place_stops_the_run(installed):
    live = installed / ".dotfiles-rendered" / "claude" / "settings.json"
    live.write_text(live.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    before = snapshot(installed)

    result = install(installed)
    assert result.returncode == 1
    assert "drifted" in result.stdout
    assert snapshot(installed) == before

    forced = install(installed, "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    kept = list((installed / ".dotfiles-backup").glob("*/.dotfiles-rendered/claude/settings.json"))
    assert len(kept) == 1, "the changed file must be kept in the backup"
    assert install(installed).returncode == 0


def test_a_folder_that_was_linked_whole_is_moved_and_its_files_stay(home, tmp_path):
    """An older install linked ~/.claude/agents as one folder. The link is
    moved to the backup; what it pointed at is not touched."""
    theirs = tmp_path / "their-agents"
    theirs.mkdir()
    (theirs / "mine.md").write_text("a worker kept elsewhere", encoding="utf-8")
    (home / ".claude").mkdir()
    (home / ".claude" / "agents").symlink_to(theirs)

    result = install(home)
    assert result.returncode == 0, result.stdout + result.stderr

    agents = home / ".claude" / "agents"
    assert agents.is_dir() and not agents.is_symlink()
    assert (agents / "verifier.md").is_symlink()
    moved = list((home / ".dotfiles-backup").glob("*/.claude/agents"))
    assert len(moved) == 1 and moved[0].is_symlink()
    assert Path(os.readlink(moved[0])) == theirs
    assert sorted(p.name for p in theirs.iterdir()) == ["mine.md"]
    assert (theirs / "mine.md").read_text(encoding="utf-8") == "a worker kept elsewhere"


def test_a_real_folder_in_the_way_is_moved_with_everything_in_it(home):
    own_skill = home / ".claude" / "skills" / "shape"
    (own_skill / "notes").mkdir(parents=True)
    (own_skill / "SKILL.md").write_text("my own version", encoding="utf-8")
    (own_skill / "notes" / "a.txt").write_text("keep me", encoding="utf-8")

    result = install(home)
    assert result.returncode == 0, result.stdout + result.stderr

    assert Path(os.readlink(own_skill)) == REPO / "config/claude/skills/shape"
    kept = list((home / ".dotfiles-backup").glob("*/.claude/skills/shape"))
    assert len(kept) == 1 and kept[0].is_dir() and not kept[0].is_symlink()
    assert (kept[0] / "SKILL.md").read_text(encoding="utf-8") == "my own version"
    assert (kept[0] / "notes" / "a.txt").read_text(encoding="utf-8") == "keep me"


def test_a_changed_rendered_file_also_stops_a_run_for_its_own_group(installed):
    """--only must not skip the check that protects the live settings file."""
    live = installed / ".dotfiles-rendered" / "claude" / "settings.json"
    live.write_text(live.read_text(encoding="utf-8") + '"my edit"\n', encoding="utf-8")
    before = snapshot(installed)

    result = install(installed, "--only", "claude")
    assert result.returncode == 1
    assert "drifted" in result.stdout
    assert snapshot(installed) == before

    other = install(installed, "--only", "codex")
    assert other.returncode == 0, other.stdout + other.stderr
    assert snapshot(installed) == before, "another group's run must leave the file alone"
