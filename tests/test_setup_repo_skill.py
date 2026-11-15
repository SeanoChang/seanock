"""setup-repo: the skill text says what repo-doctor.py does.

The script has its own file, tests/test_repo_doctor.py. Here: the text an
agent reads, checked against the script, and the examples of the text run
through the script on a real repo.

Run: python3 -m pytest tests/test_setup_repo_skill.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SKILLS = Path(__file__).resolve().parent.parent / "config" / "claude" / "skills"
SKILL = SKILLS / "setup-repo"
TEXT = (SKILL / "SKILL.md").read_text(encoding="utf-8")
SCRIPT = SKILL / "scripts" / "repo-doctor.py"
BRANCH_PUSH = "git push origin refs/heads/dev:refs/heads/dev"


def flat(text):
    return " ".join(text.split())


@pytest.fixture(scope="module")
def module():
    loader = importlib.machinery.SourceFileLoader("repo_doctor_for_text", str(SCRIPT))
    made = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(made)
    return made


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def doctor(repo):
    done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo)], capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout.splitlines()


def block(language, after):
    """The first fenced block of this language that follows the given text."""
    return re.search(rf"```{language}\n(.*?)```", TEXT[TEXT.index(after):], re.S).group(1)


def rows(heading):
    """The rows of the table that starts with this header cell, each as a list of cells."""
    start = TEXT.index(f"| {heading} |")
    lines = TEXT[start:].split("\n\n")[0].splitlines()[2:]
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]


# ── the user's path: the examples of the text make a repo that the script calls clean ──

def test_a_repo_that_is_set_up_as_the_text_says_is_clean(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "-b", "main")
    (repo / "README.md").write_text("demo\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    git(repo, "push", "-q", "origin", "refs/heads/main:refs/heads/main")
    code, lines = doctor(repo)
    assert code == 1 and lines[-1] == "repo-doctor: summary gaps=8 unknown=0"

    # the integration branch, with the two commands of the text; local work that is not pushed stays out of it
    (repo / "local.txt").write_text("not pushed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "local work")
    for command in block("bash", "no integration branch at all").strip().splitlines():
        done = subprocess.run(["bash", "-e", "-c", command.replace("<default>", "main")], cwd=repo,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
    git(repo, "fetch", "-q", "origin")
    assert git(repo, "rev-parse", "origin/dev") == git(repo, "rev-parse", "origin/main")
    assert git(repo, "rev-parse", "origin/dev") != git(repo, "rev-parse", "HEAD")
    # the root map of the text, with the comment rule
    (repo / "CLAUDE.md").write_text("# demo\n\n" + block("markdown", "### `root-map`"))
    assert doctor(repo)[1][3].startswith("ok root-map ") and doctor(repo)[1][5].startswith("ok comment-rule ")
    # a workflow that runs the test and lint commands of that map
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(block("yaml", "### `ci-tests` and `ci-lint`"))
    (repo / "REVIEW.md").write_text(block("markdown", "### `review-file`"))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "rules.json").write_text(block("json", "### `rule-table`"))
    verify = repo / ".claude" / "skills" / "verify-demo"
    verify.mkdir(parents=True)
    (verify / "verify").write_text("#!/bin/sh\n")
    (verify / "features.json").write_text("{}\n")

    code, lines = doctor(repo)
    assert (code, lines[-1]) == (0, "repo-doctor: clean"), lines
    assert [line.split()[0] for line in lines[:-1]] == ["ok"] * 8


def test_the_push_that_makes_the_integration_branch_does_not_move_one_that_is_there(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "-b", "main")
    (repo / "a").write_text("1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    git(repo, "push", "-q", "origin", "refs/heads/main:refs/heads/main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    git(other, "checkout", "-q", "-b", "dev")
    (other / "b").write_text("2\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "work of someone else on dev")
    git(other, "push", "-q", "origin", "refs/heads/dev:refs/heads/dev")
    theirs = git(other, "rev-parse", "HEAD")

    git(repo, "branch", "dev", "origin/main")   # this clone never fetched: it does not know of dev
    done = subprocess.run(["bash", "-e", "-c", BRANCH_PUSH], cwd=repo, capture_output=True, text=True, timeout=60)

    assert done.returncode != 0
    assert git(origin, "rev-parse", "refs/heads/dev") == theirs


# ── the text against the script ──────────────────────────────────────────────

def test_the_description_parses_and_says_when_to_use_the_skill():
    meta = yaml.safe_load(TEXT[4:TEXT.index("\n---", 4)])

    assert meta["name"] == "setup-repo" and "disable-model-invocation" not in meta
    assert "set up this repo" in meta["description"] and len(meta["description"]) <= 1024
    assert "It never merges" in meta["description"]


def test_the_three_results_in_the_text_are_the_lines_the_script_prints(module, capsys, tmp_path):
    lines = [line for line in TEXT.splitlines() if line.startswith("| ") and "`repo-doctor: " in line]

    assert [line.split("|")[1].strip() for line in lines] == ["0", "1", "2"]
    assert "`repo-doctor: clean`" in lines[0]
    assert "`repo-doctor: summary gaps=N unknown=M`" in lines[1]
    assert "`repo-doctor: error ...`" in lines[2]
    assert module.main(["--repo", str(tmp_path / "absent")]) == 2
    assert capsys.readouterr().out.startswith("repo-doctor: error ")
    (tmp_path / "empty").mkdir()
    assert module.main(["--repo", str(tmp_path / "empty")]) == 1
    assert re.fullmatch(r"repo-doctor: summary gaps=\d+ unknown=\d+", capsys.readouterr().out.splitlines()[-1])
    said = flat(TEXT)
    assert "as its own command, with no pipe" in said
    assert "Read the exit code and the last line together" in said
    assert "`unknown` is never a pass" in said
    assert re.findall(r"^python3 ~/\.claude/skills/setup-repo/scripts/repo-doctor\.py (.*)$", TEXT, re.M) == [
        "--repo <project root>"]


def test_the_text_has_a_row_and_a_section_for_each_check_of_the_script_in_its_order(module):
    assert [row[0].strip("`") for row in rows("Check")] == module.CHECKS
    headings = re.findall(r"^### (.+)$", TEXT, re.M)
    named = [name for heading in headings for name in re.findall(r"`([a-z-]+)`", heading)]
    assert sorted(named) == sorted(module.CHECKS)
    for state in ("ok", "gap", "unknown"):
        assert f"`{state}`" in TEXT


def test_the_comment_rule_of_the_text_is_the_sentence_the_script_looks_for(module):
    assert f"> {module.COMMENT_RULE}" in TEXT
    assert module.COMMENT_RULE in block("markdown", "### `root-map`")
    assert "word for word" in flat(TEXT)


def test_the_root_map_of_the_text_names_the_four_commands_as_the_script_reads_them(module):
    example = block("markdown", "### `root-map`")

    assert module.named_commands(example) == module.MAP_WORDS == ["build", "test", "lint", "install"]
    said = flat(TEXT)
    assert "a line that starts with the word and holds the command in a code span" in said
    assert "**One root map.** Do not write a `CLAUDE.md` into each folder." in said
    assert "Write only a command that you ran and saw work." in said
    assert "the script reads `CLAUDE.md`, and `AGENTS.md` only when there is no `CLAUDE.md`" in said


def test_the_workflow_of_the_text_runs_commands_that_the_script_knows(module):
    workflow = block("yaml", "### `ci-tests` and `ci-lint`")
    data = yaml.safe_load(workflow)

    runs = [step["run"] for job in data["jobs"].values() for step in job["steps"] if "run" in step]
    assert any(module.match_command(run, module.TEST_RES) for run in runs)
    assert any(module.match_command(run, module.LINT_RES) for run in runs)
    said = flat(TEXT)
    listed = said[said.index("The script knows these test commands:"):said.index("### `root-map`")]
    assert re.findall(r"`([^`]+)`", listed.split("and these lint commands:")[0]) == module.TEST_COMMANDS
    assert re.findall(r"`([^`]+)`", listed.split("and these lint commands:")[1].split(".")[0]) == module.LINT_COMMANDS


def test_the_rule_table_of_the_text_is_one_the_script_takes(module, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "rules.json").write_text(block("json", "### `rule-table`"))

    assert module.check_rules(str(tmp_path)) == ("ok", "0 rule(s)")


def test_the_text_leaves_the_merge_and_the_first_workflow_to_the_user():
    said = flat(TEXT)

    assert "This skill never merges a pull request." in said
    assert "**The first workflow of a repo is merged by the user.**" in said
    assert "Never go around the merge gate" in said
    assert "Only the user starts `/create-verify-skill`" in said and "Only the user starts `/correct`" in said
    assert "**You cannot tell which of the two it is:** do not create `dev`. Report what you see; the user decides." in said
    assert "say in the pull request body that the list is your proposal, and that the user decides." in said
    commands = "\n".join(re.findall(r"```bash\n(.*?)```", TEXT, re.S)) + "\n" + "\n".join(re.findall(r"`([^`\n]+)`", TEXT))
    for banned in ("gh pr merge", "git merge ", "git tag", "--admin", "--auto", "--delete-branch", "--force",
                   "MERGE_GATE_BASE=", "git branch -D", "git branch -d"):
        assert banned not in commands, banned
    assert [line.strip() for line in commands.splitlines() if "git push" in line] == [BRANCH_PUSH]


def test_the_tier_of_each_gap_is_in_the_text_and_ci_has_a_pull_request_of_its_own():
    table = {row[0].strip("`"): row for row in rows("Check")}

    assert table["ci-tests"][-1] == table["ci-lint"][-1] == "2"
    for check in ("root-map", "review-file", "comment-rule", "rule-table"):
        assert table[check][-1] == "0"
    said = flat(TEXT)
    assert "The workflow goes in a pull request of its own" in said
    assert "The four text gaps go in one pull request" in said


def test_scaffold_docs_left_the_repo_and_no_skill_text_points_to_it():
    assert not (SKILLS / "scaffold-docs").exists()
    for path in sorted(SKILLS.glob("*/SKILL.md")):
        assert "/scaffold-docs" not in path.read_text(encoding="utf-8"), path
    readme = (SKILLS.parent.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "`scaffold-docs`" not in readme and "| `setup-repo` " in readme
