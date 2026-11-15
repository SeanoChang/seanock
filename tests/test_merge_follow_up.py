"""merge-follow-up: the skill text says what pr-state.py does, and its repair recipe works.

The script has its own file, tests/test_pr_state.py. Here: the text an agent
reads, checked against the script, and the commands for a squashed release,
run as written on a real repo.

Run: python3 -m pytest tests/test_merge_follow_up.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

SKILL = Path(__file__).resolve().parent.parent / "config" / "claude" / "skills" / "merge-follow-up"
TEXT = (SKILL / "SKILL.md").read_text(encoding="utf-8")
SCRIPT = SKILL / "scripts" / "pr-state.py"


def flat(text):
    return " ".join(text.split())


@pytest.fixture(scope="module")
def module():
    loader = importlib.machinery.SourceFileLoader("pr_state_for_text", str(SCRIPT))
    made = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(made)
    return made


def test_the_model_may_start_the_skill_and_its_description_names_the_users_word():
    meta = yaml.safe_load(TEXT[4:TEXT.index("\n---", 4)])

    assert meta["name"] == "merge-follow-up" and "disable-model-invocation" not in meta
    assert '"merged"' in meta["description"] and len(meta["description"]) <= 1024


def test_the_text_holds_no_command_that_merges_tags_or_deletes():
    commands = "\n".join(re.findall(r"```bash\n(.*?)```", TEXT, re.S)) + "\n" + "\n".join(re.findall(r"`([^`\n]+)`", TEXT))

    for banned in ("gh pr merge", "git merge ", "git tag", "git branch -D", "git branch -d", "--delete-branch",
                   "git worktree remove", "--admin", "--auto"):
        assert banned not in commands, banned
    pushes = [line for line in commands.splitlines() if "git push" in line]
    assert pushes == ["git push --force-with-lease", "git push origin sync/<release>-into-<integration>-<version>:<integration>"]
    said = flat(TEXT)
    assert "Give the user this command, and do not run it yourself" in said
    assert "This skill never merges a pull request." in said


def test_the_three_results_in_the_text_are_the_lines_the_script_prints(module, capsys, tmp_path):
    rows = [line for line in TEXT.splitlines() if line.startswith("| ") and "pr-state:" in line]

    assert [row.split("|")[1].strip() for row in rows] == ["0", "1", "2"]
    assert "`pr-state: clean`" in rows[0] and "`pr-state: summary events=N`" in rows[1] and "`pr-state: error ...`" in rows[2]
    assert module.main(["--repo", str(tmp_path / "absent"), "--session", "s"]) == 2
    assert capsys.readouterr().out.startswith("pr-state: error ")
    said = flat(TEXT)
    assert "as its own command, with no pipe" in said
    assert "Read the exit code and the last line together" in said
    assert 'Never read a failed look as "nothing changed".' in said


def test_the_text_has_a_section_for_each_event_kind_of_the_script(module):
    source = SCRIPT.read_text(encoding="utf-8")
    kinds = set(re.findall(r'"kind": "([a-z-]+)"', source))

    assert kinds == {"merged", "closed", "checks-failed", "behind-base"}
    for kind in kinds - {"behind-base"}:
        assert re.search(rf"^### `{kind}`", TEXT, re.M), kind
    assert "`behind-base`" in TEXT
    for field in ("squashed_release", "head_sha", "release"):
        assert f"`{field}`" in TEXT and f'"{field}"' in source


def test_the_text_keeps_a_session_off_the_branches_of_another():
    said = flat(TEXT)

    assert "Rebase the ones that **this session created**" in said
    assert "**Leave every other branch alone.**" in said
    assert "run `git rebase --abort` and report the branch" in said
    assert "a reviewer that passed the branch re-checks it before it is merged" in said
    assert "Delete nothing" in said
    assert "Never wait with `sleep` or a loop in the shell." in said
    assert "--claim install:<pr number>" in TEXT


# ── the repair of a squashed release, run as the text gives it ───────────────

def git(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit(repo, name):
    (repo / name).write_text(name)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def recipe():
    section = TEXT[TEXT.index("### `merged`, and `release` is true"):TEXT.index("### `closed`")]
    blocks = re.findall(r"```bash\n(.*?)```", section, re.S)
    assert len(blocks) == 2
    fill = lambda text, head: (text.replace("<head_sha>", head).replace("<release>", "main")
                               .replace("<integration>", "dev").replace("<version>", "1.2.0"))
    return [lambda head, block=block: "\n".join(line.strip() for line in fill(block, head).splitlines()) for block in blocks]


@pytest.fixture
def squashed(tmp_path):
    """A clone where the release went into main as one commit with one parent."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "main")
    commit(work, "first")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "dev")
    commit(work, "feature-a")
    head = commit(work, "feature-b")
    git(work, "push", "-q", "origin", "dev")
    squash = git(work, "commit-tree", "dev^{tree}", "-p", "main", "-m", "Release (squashed)")
    git(work, "push", "-q", "origin", f"{squash}:main")
    git(work, "fetch", "-q", "origin")
    return work, head


def test_the_checks_pass_and_the_repair_joins_the_histories_without_changing_a_file(squashed):
    work, head = squashed
    check, build = recipe()
    assert subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "origin/dev"], cwd=work).returncode == 1

    checked = subprocess.run(["bash", "-e", "-c", check(head)], cwd=work, capture_output=True, text=True)
    trees = checked.stdout.split()
    assert checked.returncode == 0 and len(trees) == 2 and trees[0] == trees[1]

    built = subprocess.run(["bash", "-e", "-c", build(head)], cwd=work, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    branch = "sync/main-into-dev-1.2.0"
    assert git(work, "rev-parse", f"{branch}^{{tree}}") == git(work, "rev-parse", "origin/dev^{tree}")
    assert git(work, "rev-list", "--parents", "-n", "1", branch).split()[1:] == [
        git(work, "rev-parse", "origin/dev"), git(work, "rev-parse", "origin/main")]
    assert git(work, "push", "--dry-run", "origin", f"{branch}:dev") == ""
    git(work, "push", "-q", "origin", f"{branch}:dev")
    git(work, "fetch", "-q", "origin")
    assert subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "origin/dev"], cwd=work).returncode == 0
    assert git(work, "rev-parse", "origin/dev") == git(work, "rev-parse", branch)


def test_the_repair_keeps_what_dev_took_after_the_release(squashed):
    work, head = squashed
    check, build = recipe()
    git(work, "checkout", "-q", "dev")
    commit(work, "merged-into-dev-after-the-release")
    git(work, "push", "-q", "origin", "dev")
    git(work, "fetch", "-q", "origin")

    assert subprocess.run(["bash", "-e", "-c", check(head)], cwd=work, capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-e", "-c", build(head)], cwd=work, capture_output=True, text=True).returncode == 0

    branch = "sync/main-into-dev-1.2.0"
    assert git(work, "rev-parse", f"{branch}^{{tree}}") == git(work, "rev-parse", "origin/dev^{tree}")
    assert "merged-into-dev-after-the-release" in git(work, "ls-tree", "--name-only", branch).split()
    assert git(work, "push", "--dry-run", "origin", f"{branch}:dev") == ""


def test_the_checks_fail_when_main_holds_something_that_dev_does_not(squashed):
    work, head = squashed
    check, _ = recipe()
    git(work, "checkout", "-q", "-B", "hotfix", "origin/main")
    commit(work, "hotfix-only-on-main")
    git(work, "push", "-q", "origin", "hotfix:main")
    git(work, "fetch", "-q", "origin")

    checked = subprocess.run(["bash", "-e", "-c", check(head)], cwd=work, capture_output=True, text=True)

    trees = checked.stdout.split()
    assert len(trees) == 2 and trees[0] != trees[1]


def test_the_checks_fail_when_the_release_head_is_not_in_dev(squashed):
    work, head = squashed
    check, _ = recipe()
    git(work, "checkout", "-q", "-B", "side", "origin/main")
    other = commit(work, "not-in-dev")

    checked = subprocess.run(["bash", "-e", "-c", check(other)], cwd=work, capture_output=True, text=True)

    assert checked.returncode == 1
