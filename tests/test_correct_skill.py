"""correct: the skill text says what rules.py does.

The script has its own file, tests/test_rules_script.py. Here: the text an
agent reads, checked against the script, and the example row of the text run
through the script.

Run: python3 -m pytest tests/test_correct_skill.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SKILL = Path(__file__).resolve().parent.parent / "config" / "claude" / "skills" / "correct"
TEXT = (SKILL / "SKILL.md").read_text(encoding="utf-8")
SCRIPT = SKILL / "scripts" / "rules.py"


def flat(text):
    return " ".join(text.split())


@pytest.fixture(scope="module")
def module():
    loader = importlib.machinery.SourceFileLoader("rules_for_text", str(SCRIPT))
    made = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(made)
    return made


def first_cells(heading):
    """The first cell of each row of the table that starts with this header cell."""
    start = TEXT.index(f"| {heading} |")
    rows = TEXT[start:].split("\n\n")[0].splitlines()[2:]
    return [row.split("|")[1].strip().strip("`") for row in rows]


def test_only_the_user_starts_the_skill_and_its_description_says_so():
    meta = yaml.safe_load(TEXT[4:TEXT.index("\n---", 4)])

    assert meta["name"] == "correct" and meta["disable-model-invocation"] is True
    assert "/correct" in meta["description"] and len(meta["description"]) <= 1024


def test_the_three_results_in_the_text_are_the_lines_the_script_prints(module, capsys, tmp_path):
    rows = [line for line in TEXT.splitlines() if line.startswith("| ") and "`rules: " in line]

    assert [row.split("|")[1].strip() for row in rows] == ["0", "1", "2"]
    assert "`rules: clean`" in rows[0] and "`rules: summary ...`" in rows[1] and "`rules: error ...`" in rows[2]
    assert module.main(["check", "--repo", str(tmp_path / "absent")]) == 2
    assert capsys.readouterr().out.startswith("rules: error ")
    said = flat(TEXT)
    assert "as its own command, with no pipe" in said
    assert "Read the exit code and the last line together" in said


def test_the_text_names_each_command_of_the_script_as_the_script_takes_it(module):
    commands = re.findall(r"^python3 ~/\.claude/skills/correct/scripts/rules\.py (\w+) --repo <project root>$", TEXT, re.M)

    assert commands == ["mine", "check", "table"]
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'choices=("mine", "check", "table")' in source and '"--project"' in source
    assert "Add `--project <folder>`" in TEXT
    for flag in ("--no-history", "--history"):
        assert f"`{flag}" in TEXT and f'"{flag}"' in source
    assert "`table` ends with `rules: wrote .claude/rules.md` and exit 0." in flat(TEXT)
    assert 'say("rules: wrote .claude/rules.md")' in source


def test_the_layers_of_the_text_are_the_layers_of_the_script_in_the_same_order(module):
    assert tuple(first_cells("Layer")) == module.LAYERS
    assert "Take the strongest layer that works" in flat(TEXT)


def test_the_text_has_a_row_for_each_candidate_kind_and_each_gap_of_the_script(module):
    source = SCRIPT.read_text(encoding="utf-8")

    assert set(first_cells("`kind`")) == set(re.findall(r'"kind": "([a-z-]+)"', source)) == {
        "revert", "fix-cluster", "history", "held-back"}
    assert set(first_cells("Gap")) == set(re.findall(r'found\.append\(\("([a-z-]+)"', source)) == {
        "missing-enforcer", "not-in-ci", "docs-only", "table-stale"}


def test_the_text_keeps_the_agent_out_of_the_history_file_and_its_words_out_of_the_repo():
    said = flat(TEXT)

    assert "**Do not read the prompt history yourself.**" in said
    assert "and not the lines that `held-back` names" in said
    assert "Do not write those lines into a file, a commit or a pull request." in said
    assert "Write the date of a history line, never its words." in said
    assert "one sentence each, in your own words" in said and "in the user's words" not in said
    assert "how many history lines the script held back, as a number only" in said
    assert "history.jsonl" not in TEXT


def test_the_text_asks_for_an_enforcer_that_failed_on_the_old_mistake_before_the_row():
    said = flat(TEXT)

    assert said.index("## 4. Write the enforcer first") < said.index("## 5. Add the row")
    assert "**Show it fail on the old mistake.**" in said
    assert "An enforcer that never failed proves nothing." in said
    assert "Ask one time which ones to build. Build only those." in said
    assert "Such a row needs `accepted`" in said
    assert "Put the mistake back on a copy: a test fixture, or a copy of the project in a temp folder." in said
    assert "Never put the mistake back in the working tree" in said
    for command in ("git checkout", "git restore", "git stash", "git reset", "git clean"):
        assert command not in TEXT, command


def test_the_text_names_the_keys_of_a_row_as_the_script_takes_them(module):
    said = flat(TEXT)
    sentence = said[said.index("- A row has "):said.index("The script refuses a row")]

    assert set(re.findall(r"`([a-z_]+)`", sentence)) == module.ROW_KEYS | {"docs"}
    assert "`runs_in` is `ci` or `hook`. A `docs` rule, and only a `docs` rule, has `none`." in said
    assert set(module.RUNS_IN) == {"ci", "hook", "none"}


def test_the_path_forms_of_the_text_are_the_ones_that_the_script_takes(module):
    said = flat(TEXT)
    assert "written as a path: `pytest tests/`, not `pytest tests`." in said
    assert ("A name counts only as a word of its own with a slash in it: `tests/`, `./tests`, `config/hooks`, "
            "`./lint.sh`.") in said
    assert "so `pytest tests` does not count, and neither does `--ignore=tests/` or `tests/*.py`." in said
    named = lambda command, path: module.named_in([f"run: {command}\n"], path)
    assert named("pytest tests/", "tests/test_a.py") and named("pytest ./tests", "tests/test_a.py")
    assert named("pytest config/hooks", "config/hooks/test_a.py") and named("./lint.sh", "lint.sh")
    assert not named("pytest tests", "tests/test_a.py") and not named("npm test", "test/a.js")
    assert not named("pytest --ignore=tests/", "tests/test_a.py") and not named("pytest tests/*.py", "tests/test_a.py")


def test_the_example_row_of_the_text_is_a_table_that_the_script_takes(tmp_path):
    block = re.search(r"```json\n(.*?)```", TEXT, re.S).group(1)
    table = json.loads(block)
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "rules.json").write_text(block)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_attribution.py").write_text("def test_it():\n    assert True\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("run: python3 -m pytest tests/ -q\n")
    run = lambda command: subprocess.run([sys.executable, str(SCRIPT), command, "--repo", str(repo)],
                                         capture_output=True, text=True, timeout=60)

    assert table["rules"][0]["layer"] == "config"
    assert run("table").stdout == "rules: wrote .claude/rules.md\n"
    done = run("check")
    assert (done.returncode, done.stdout) == (0, "rules: clean\n")
