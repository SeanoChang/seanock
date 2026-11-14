"""create-verify-skill: the scaffold script, and the two skill texts.

The runner has its own file, tests/test_verify_runner.py. Here: what the
scaffold writes and refuses, and that the texts an agent reads say what the
runner does.

Run: python3 -m pytest tests/test_create_verify_skill.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SKILL = Path(__file__).resolve().parent.parent / "config" / "claude" / "skills" / "create-verify-skill"
SCAFFOLD = SKILL / "scripts" / "scaffold.py"
RUNNER = SKILL / "scripts" / "verify"
TEMPLATE = SKILL / "templates" / "SKILL.md.tmpl"
GUIDE = SKILL / "SKILL.md"


def scaffold(project, app="demo"):
    done = subprocess.run([sys.executable, str(SCAFFOLD), "--project", str(project), "--app", app],
                          capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout.splitlines()


def git_init(folder):
    subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True, timeout=60)


def frontmatter(text):
    assert text.startswith("---\n")
    return yaml.safe_load(text[4:text.index("\n---", 4)])


def flat(text):
    return " ".join(text.split())


@pytest.fixture(scope="module")
def runner():
    loader = importlib.machinery.SourceFileLoader("verify_runner_for_text", str(RUNNER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


# ── the scaffold ─────────────────────────────────────────────────────────────

def test_it_writes_the_runner_an_empty_map_and_the_skill_file(tmp_path, runner):
    code, lines = scaffold(tmp_path)

    dest = tmp_path / ".claude" / "skills" / "verify-demo"
    assert code == 0 and lines[-1] == f"create-verify-skill: wrote {dest.resolve()}"
    assert sorted(path.name for path in dest.iterdir()) == ["SKILL.md", "features.json", "verify"]
    assert (dest / "verify").read_bytes() == RUNNER.read_bytes()
    assert os.access(dest / "verify", os.X_OK)
    assert json.loads((dest / "features.json").read_text()) == {"app": "demo", "runner": runner.RUNNER, "features": []}
    text = (dest / "SKILL.md").read_text()
    assert frontmatter(text)["name"] == "verify-demo" and "__APP__" not in text


def test_a_new_frame_is_not_a_result_until_the_map_has_features(tmp_path):
    scaffold(tmp_path)

    done = subprocess.run([sys.executable, str(tmp_path / ".claude/skills/verify-demo/verify"), "run"],
                          capture_output=True, text=True, cwd=tmp_path, timeout=60)

    assert done.returncode == 2
    assert done.stdout.splitlines()[-1] == "verify: error the map has no features"


def test_it_never_changes_a_skill_folder_that_exists(tmp_path):
    scaffold(tmp_path)
    mine = tmp_path / ".claude" / "skills" / "verify-demo" / "features.json"
    mine.write_text('{"mine": true}')

    code, lines = scaffold(tmp_path)

    assert code == 2 and lines[-1].startswith("create-verify-skill: error ") and "exists" in lines[-1]
    assert mine.read_text() == '{"mine": true}'


@pytest.mark.parametrize("app", ["Demo", "a b", "../x", "", "x_y", "a/b"])
def test_it_refuses_an_app_name_that_is_not_a_plain_word(tmp_path, app):
    code, lines = scaffold(tmp_path, app)

    assert code == 2 and lines[-1].startswith("create-verify-skill: error ")
    assert not (tmp_path / ".claude").exists()


def test_it_refuses_a_project_folder_that_is_not_there(tmp_path):
    code, lines = scaffold(tmp_path / "absent")

    assert code == 2 and "no such folder" in lines[-1]


def test_in_a_git_repo_it_adds_the_evidence_folder_to_gitignore_once(tmp_path):
    git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("target/")

    code, lines = scaffold(tmp_path)

    assert code == 0 and "added .verify/ to .gitignore" in lines
    assert (tmp_path / ".gitignore").read_text() == "target/\n.verify/\n"


def test_it_leaves_gitignore_alone_when_git_ignores_the_evidence_already(tmp_path):
    git_init(tmp_path)
    (tmp_path / ".gitignore").write_text(".verify\n")

    code, lines = scaffold(tmp_path)

    assert code == 0 and (tmp_path / ".gitignore").read_text() == ".verify\n"


def test_outside_git_it_writes_no_gitignore(tmp_path):
    # tmp_path can sit inside a repo on some machines; an explicit ceiling makes this folder "not in git"
    done = subprocess.run([sys.executable, str(SCAFFOLD), "--project", str(tmp_path), "--app", "demo"],
                          capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path.parent)))

    assert done.returncode == 0 and not (tmp_path / ".gitignore").exists()


# ── the texts ────────────────────────────────────────────────────────────────

def test_only_the_user_starts_the_generator():
    meta = frontmatter(GUIDE.read_text())

    assert meta["name"] == "create-verify-skill"
    assert meta["disable-model-invocation"] is True


def test_the_project_skill_can_be_called_by_the_model():
    meta = frontmatter(TEMPLATE.read_text().replace("__APP__", "demo"))

    assert "disable-model-invocation" not in meta
    assert len(meta["description"]) <= 1024 and "demo" in meta["description"]


def test_the_example_map_in_the_guide_is_a_map_that_the_runner_accepts(tmp_path, runner):
    block = re.search(r"```json\n(.*?)```", GUIDE.read_text(), re.S).group(1)
    example = tmp_path / "features.json"
    example.write_text("\n".join(line[3:] if line.startswith("   ") else line for line in block.splitlines()))

    loaded = runner.load_map(example)

    assert loaded["runner"] == runner.RUNNER and loaded["features"][0]["steps"][0]["kind"] == "run"


def test_the_guide_names_every_key_that_the_runner_knows(runner):
    text = GUIDE.read_text()

    for key in sorted(runner.TOP_KEYS | runner.FEATURE_KEYS | runner.STEP_KEYS | runner.EXPECT_KEYS):
        assert f"`{key}`" in text or f'"{key}"' in text, key


@pytest.mark.parametrize("path", [GUIDE, TEMPLATE], ids=["guide", "project skill"])
def test_both_texts_say_how_to_run_and_how_to_read_a_result(path):
    text = flat(path.read_text())

    assert "as its own command, with no pipe" in text
    assert "exit code and the last line together" in text
    for word in ("dirty", "partial"):
        assert f"`{word}`" in text


def test_the_project_skill_states_the_three_results_and_that_anything_else_is_no_result(runner):
    text = TEMPLATE.read_text()
    rows = [line for line in text.splitlines() if line.startswith("| ")]

    assert any(row.startswith("| 0 | `verify-__APP__: pass`") for row in rows)
    assert any(row.startswith("| 1 | `verify-__APP__: summary`") for row in rows)
    assert any(row.startswith("| 2 | `verify-__APP__: error`") for row in rows)
    said = flat(text)
    assert "Report that as unverified, never as a pass." in said
    assert "the commit on the pass line is the output of `git rev-parse HEAD`" in said
    assert "Run it again after each new commit." in said


def test_the_last_lines_in_the_project_skill_are_the_ones_the_runner_prints(runner):
    record = {"app": "demo", "commit": "abc", "dirty": True, "partial": True, "build": None,
              "features": [{"result": "pass"}], "result": "pass"}

    assert runner.last_line(record) == "verify-demo: pass 1/1 abc dirty partial"
    record.update(result="fail", features=[{"result": "fail"}])
    assert runner.last_line(record).startswith("verify-demo: summary ")
