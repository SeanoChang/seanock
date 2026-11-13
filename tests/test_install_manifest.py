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
import subprocess
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
