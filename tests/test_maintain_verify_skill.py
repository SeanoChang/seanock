"""maintain-verify-skill: the skill text says what drift.py does.

The script has its own file, tests/test_drift.py. Here: the text an agent
reads, checked against the script, and the commands of the text run as
written on a project with a verify skill.

Run: python3 -m pytest tests/test_maintain_verify_skill.py -q -p no:cacheprovider
"""
import re
import subprocess

import pytest
import yaml

from test_drift import DRIFT, GREET, PY, RUNNER, SKILLS, feature, make_project

SKILL = SKILLS / "maintain-verify-skill"
TEXT = (SKILL / "SKILL.md").read_text(encoding="utf-8")
SOURCE = (SKILL / "scripts" / "drift.py").read_text(encoding="utf-8")
HOME_PATH = "~/.claude/skills/maintain-verify-skill/scripts/drift.py"


def flat(text):
    return " ".join(text.split())


def rows(heading):
    """The rows of the table that starts with this header cell, each as a list of cells."""
    start = TEXT.index(f"| {heading} |")
    lines = TEXT[start:].split("\n\n")[0].splitlines()[2:]
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]


def commands():
    """Each drift.py command line of the text, as the words after the script path."""
    return re.findall(r"^python3 " + re.escape(HOME_PATH) + r" (.*)$", TEXT, re.M)


def as_written(project, line):
    """Run one command line of the text on the project, with the script of this repo."""
    argv = line.replace("<project root>", str(project.root)).split()
    done = subprocess.run([PY, str(DRIFT), *argv], cwd=project.root.parent, capture_output=True, text=True,
                          timeout=300, env=project.env)
    return done.returncode, done.stdout.splitlines()


# ── the user's path: the commands of the text, run as written ────────────────

def test_the_commands_of_the_text_find_a_runner_that_is_behind_and_bring_it_up(tmp_path):
    project = make_project(tmp_path / "proj")
    check, quick, bump = commands()
    assert (check, quick, bump) == ("check --project <project root>", "check --project <project root> --no-run",
                                    "bump --project <project root>")
    assert as_written(project, check) == (0, ["drift: clean"])

    copy = project.skill / "verify"
    copy.write_bytes(RUNNER.read_bytes() + b"\n# an older copy\n")
    project.write_map([feature("greet", GREET["steps"][0], paths=("src/app.py", "src/moved.py"))])
    project.commit()

    code, lines = as_written(project, quick)
    assert (code, lines[-1]) == (1, "drift: summary stale=1 uncovered=0 failed=0 runner=old")
    assert lines[0] == "stale-path greet src/moved.py" and lines[1].startswith("runner-old ")

    code, lines = as_written(project, bump)
    assert code == 0 and lines == [f"drift: bumped {copy}"]
    assert copy.read_bytes() == RUNNER.read_bytes()
    project.write_map([GREET])
    project.commit()
    assert as_written(project, check) == (0, ["drift: clean"])


# ── the text against the script ──────────────────────────────────────────────

def test_the_description_parses_and_says_when_to_use_the_skill():
    meta = yaml.safe_load(TEXT[4:TEXT.index("\n---", 4)])

    assert meta["name"] == "maintain-verify-skill" and "disable-model-invocation" not in meta
    assert "check the verify skill" in meta["description"] and len(meta["description"]) <= 1024
    assert "never weakens a step" in meta["description"]


def test_the_results_in_the_text_are_the_lines_the_script_prints(tmp_path):
    lines = [line for line in TEXT.splitlines() if line.startswith("| ") and "`drift: " in line]

    assert [line.split("|")[1].strip() for line in lines] == ["0", "1", "2"]
    assert "`drift: clean`" in lines[0]
    assert "`drift: summary stale=N uncovered=N failed=N`" in lines[1]
    assert "`drift: error ...`" in lines[2]
    for last in ("drift: clean", "drift: summary stale=", " uncovered=", " failed=", " runner=", "drift: error ", "drift: bumped "):
        assert last in SOURCE, last
    said = flat(TEXT)
    assert "` runner=old` or ` runner=unknown`" in said
    assert "as its own command, with no pipe" in said
    assert "Read the exit code and the last line together" in said
    assert "`bump` ends with `drift: bumped <path>` or `drift: clean`, and exit 0." in said
    done = subprocess.run([PY, str(DRIFT), "check", "--project", str(tmp_path / "absent")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and done.stdout.startswith("drift: error ")


def test_the_text_has_a_row_and_a_section_for_each_finding_of_the_script():
    kinds = ["stale-path", "uncovered-path", "failed-step", "failed-build", "runner-old"]

    assert [row[0].strip("`").split()[0] for row in rows("Finding")] == kinds
    assert set(re.findall(r'f?"(stale-path|uncovered-path|failed-step|failed-build|runner-old) ', SOURCE)) == set(kinds)
    headings = [name for heading in re.findall(r"^### (.+)$", TEXT, re.M) for name in re.findall(r"`([a-z-]+)`", heading)]
    assert sorted(headings) == sorted(kinds)
    for flag in ("--no-run", "--app"):
        assert f"`{flag}" in TEXT and f'"{flag}"' in SOURCE


def test_a_failed_step_is_about_the_app_first_and_no_step_is_weakened():
    said = flat(TEXT)
    failed = said[said.index("### `failed-step` and `failed-build`"):said.index("### `runner-old`")]

    assert "**A failed step is first a finding about the app.**" in failed
    assert "Change a step only when you can show that the step is wrong" in failed
    assert "If you cannot show that, the app is broken: report it, and leave the step as it is." in failed
    assert "**Never weaken a step to get a pass.**" in failed
    assert "Do not take an expected text out, do not make it shorter, and do not remove the step." in failed
    assert "show that it fails on a broken app" in said


def test_a_path_is_not_added_only_to_silence_a_line_and_a_feature_is_not_dropped_in_silence():
    said = flat(TEXT)

    assert "Do not add a pattern only to make the line go away." in said
    assert "say in the report which folders you left, and why" in said
    assert "If the feature itself is gone from the app, take its entry out, and say so in the report." in said


def test_the_text_says_when_bump_refuses_and_what_follows_a_bump():
    said = flat(TEXT)
    old = said[said.index("### `runner-old`"):said.index("## 3. Prove the map again")]

    assert "It refuses, with exit 2, when the `runner` number of the map is not the number of the current runner" in old
    assert "the map is for runner" in SOURCE
    assert "`runner-old unknown runner`" in old and '"runner-old unknown runner"' in SOURCE
    assert "Never copy a runner by hand" in old
    proof = said[said.index("## 3. Prove the map again"):said.index("## Report")]
    assert "python3 .claude/skills/verify-<app>/verify doctor" in TEXT and "python3 .claude/skills/verify-<app>/verify run" in TEXT
    assert "a last line that starts `verify-<app>: pass`" in proof


def test_the_text_holds_no_command_that_merges_pushes_or_deletes():
    spans = "\n".join(re.findall(r"```bash\n(.*?)```", TEXT, re.S)) + "\n" + "\n".join(re.findall(r"`([^`\n]+)`", TEXT))

    for banned in ("gh pr merge", "git merge", "git push", "git tag", "git branch -D", "git checkout", "git reset",
                   "git stash", "git clean", "rm ", "--admin", "--force"):
        assert banned not in spans, banned
