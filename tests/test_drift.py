"""drift.py: where a project's verify skill no longer fits the project.

Each test builds a small project in a temp folder: a stand-in command-line
app under src/, a git repo, and the verify skill written by the real
scaffold script of create-verify-skill. Then it runs drift.py as a program
and reads what a caller reads: the exit code and the lines, the last line
above all.

DRIFT_UNDER_TEST names another copy of drift.py to run instead; the mutation
check in the report runs these tests against broken copies that way.

Run: python3 -m pytest tests/test_drift.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "config" / "claude" / "skills"
SCAFFOLD = SKILLS / "create-verify-skill" / "scripts" / "scaffold.py"
RUNNER = SKILLS / "create-verify-skill" / "scripts" / "verify"
DRIFT = Path(os.environ.get("DRIFT_UNDER_TEST") or SKILLS / "maintain-verify-skill" / "scripts" / "drift.py")
PY = sys.executable
LAST = re.compile(r"drift: (clean|bumped .+|summary stale=\d+ uncovered=\d+ failed=\d+( runner=(old|unknown))?|error .+)\Z")

APP = r'''
import sys
cmd, rest = sys.argv[1], sys.argv[2:]
if cmd == "greet":
    print("hello", rest[0])
elif cmd == "boom":
    sys.stderr.write("it broke\n")
    sys.exit(3)
else:
    sys.stderr.write("unknown command\n")
    sys.exit(2)
'''


def step(*args, expect=None):
    return {"kind": "run", "argv": [PY, "{root}/src/app.py", *args], "expect": expect or {"exit": 0}}


def feature(fid, *steps, paths=("src/app.py",)):
    return {"id": fid, "paths": list(paths), "steps": list(steps)}


GREET = feature("greet", step("greet", "Ada", expect={"exit": 0, "stdout_has": ["hello Ada"]}))


def sh(*argv, cwd, env=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300, env=env)


def git(folder, *args):
    done = sh("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
              *args, cwd=folder)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class Project:
    def __init__(self, root, app="demo"):
        self.root = root
        self.app = app
        self.skill = root / ".claude" / "skills" / f"verify-{app}"
        tmp = root.parent / "t"
        tmp.mkdir(exist_ok=True)
        # every temp folder of drift.py and of the runner it starts goes under pytest's folder
        self.env = dict(os.environ, TMPDIR=str(tmp), GIT_CEILING_DIRECTORIES=str(root.parent))

    def write_map(self, features, app=None, **top):
        data = {"app": app or self.app, "runner": 1, "features": features, **top}
        (self.skill / "features.json").write_text(json.dumps(data), encoding="utf-8")

    def commit(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "change")

    def drift(self, *args, script=DRIFT, project=True):
        extra = ["--project", str(self.root)] if project else []
        done = sh(PY, str(script), *args, *extra, cwd=self.root.parent, env=self.env)
        assert done.stderr == "", done.stderr
        return done.returncode, done.stdout.splitlines()

    def verify(self, *args):
        done = sh(PY, str(self.skill / "verify"), *args, cwd=self.root, env=self.env)
        return done.returncode, done.stdout.splitlines()

    def runs(self):
        base = self.root / ".verify" / "runs"
        return sorted(base.iterdir()) if base.is_dir() else []

    def snapshot(self):
        """Every file outside .git and .verify, with its bytes and mode, and what each link points to."""
        seen = {}
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if rel.parts[0] in (".git", ".verify"):
                continue
            if path.is_symlink():
                seen[str(rel)] = ("link", os.readlink(path))
            elif path.is_file():
                seen[str(rel)] = ("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        return seen


def make_project(root, app="demo"):
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP, encoding="utf-8")
    git(root, "init", "-q")
    done = sh(PY, str(SCAFFOLD), "--project", str(root), "--app", app, cwd=root)
    assert done.returncode == 0, done.stdout + done.stderr
    made = Project(root, app)
    made.write_map([GREET])
    made.commit()
    return made


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path / "proj")


def assert_contract(code, lines):
    assert lines and LAST.match(lines[-1]), lines
    kind = lines[-1].split()[1]
    assert code == {"clean": 0, "bumped": 0, "summary": 1, "error": 2}[kind], (code, lines)
    assert all(not LAST.match(line) for line in lines[:-1]), lines


# ── the user's path, end to end ──────────────────────────────────────────────

def test_a_stale_command_in_the_map_is_found_and_after_the_fix_the_skill_is_clean(project):
    project.write_map([feature("greet", step("hi", "Ada", expect={"exit": 0, "stdout_has": ["hello Ada"]}))])
    project.commit()

    code, lines = project.drift("check")

    assert code == 1
    assert lines == ["failed-step greet step 1: exit 2, expected 0",
                     'failed-step greet step 1: stdout lacks "hello Ada"',
                     "drift: summary stale=0 uncovered=0 failed=2"]

    project.write_map([GREET])
    project.commit()

    assert project.drift("check") == (0, ["drift: clean"])
    code, lines = project.verify("run")
    assert code == 0 and lines[-1].startswith("verify-demo: pass 1/1 "), lines


# ── each finding ─────────────────────────────────────────────────────────────

def test_a_pattern_that_matches_no_tracked_file_is_a_stale_path(project):
    (project.root / "src" / "loose.py").write_text("x = 1\n")  # on disk but not tracked: it does not count
    project.write_map([feature("greet", *GREET["steps"], paths=["src/app.py", "src/gone/*.py", "src/loose.py"]),
                       feature("other", *GREET["steps"], paths=["lib/**", "Src/App.py"])])
    project.commit()
    git(project.root, "rm", "-q", "--cached", "src/loose.py")

    code, lines = project.drift("check", "--no-run")

    # a pattern matches with its case, as git lists the path, whatever the disk does
    assert (code, lines) == (1, ["stale-path greet src/gone/*.py", "stale-path greet src/loose.py",
                                 "stale-path other lib/**", "stale-path other Src/App.py",
                                 "drift: summary stale=4 uncovered=0 failed=0"])


def test_tracked_files_that_no_pattern_matches_are_counted_per_folder(project):
    for name in ("src/extra.py", "src/sub/a.py", "src/sub/b.py", "src/sub/deep/c.py", "docs/guide.md", "top.txt"):
        (project.root / name).parent.mkdir(parents=True, exist_ok=True)
        (project.root / name).write_text("x\n")
    project.commit()
    (project.root / "src" / "untracked.py").write_text("x\n")

    code, lines = project.drift("check", "--no-run")

    # docs/ and top.txt: no pattern reaches into them, so they are not the map's to cover
    assert (code, lines) == (1, ["uncovered-path src/ 1", "uncovered-path src/sub/ 2", "uncovered-path src/sub/deep/ 1",
                                 "drift: summary stale=0 uncovered=3 failed=0"])

    project.write_map([feature("greet", *GREET["steps"], paths=["src/app.py", "src/sub/**"])])
    code, lines = project.drift("check", "--no-run")
    assert (code, lines) == (1, ["uncovered-path src/ 1", "drift: summary stale=0 uncovered=1 failed=0"])

    project.write_map([feature("greet", *GREET["steps"], paths=["src/*"])])
    assert project.drift("check", "--no-run") == (0, ["drift: clean"])


def test_a_pattern_of_any_feature_covers_a_file(project):
    (project.root / "src" / "more.py").write_text("x\n")
    project.write_map([GREET, feature("more", *GREET["steps"], paths=["src/m?re.py"])])
    project.commit()

    assert project.drift("check", "--no-run") == (0, ["drift: clean"])


def test_failed_and_unstarted_steps_are_passed_on(project):
    project.write_map([GREET, feature("boom", step("boom", expect={"exit": 3, "stderr_has": ["fine"]})),
                       feature("later", {"kind": "run", "argv": ["{bin}/not-built"], "expect": {"exit": 0}})])
    project.commit()

    code, lines = project.drift("check")

    assert code == 1
    assert lines[0] == 'failed-step boom step 1: stderr lacks "fine"'
    assert lines[1].startswith("failed-step later step 1: could not start: ")
    assert lines[2:] == ["drift: summary stale=0 uncovered=0 failed=2"]


def test_a_failed_build_is_a_failed_step_finding(project):
    project.write_map([GREET], build=[PY, "{root}/src/app.py", "boom"])
    project.commit()

    assert project.drift("check") == (1, ["failed-build exit 3", "drift: summary stale=0 uncovered=0 failed=1"])


def test_no_run_does_not_start_the_runner(project):
    project.write_map([feature("greet", step("hi"))])
    project.commit()

    assert project.drift("check", "--no-run") == (0, ["drift: clean"])
    assert project.runs() == []
    assert project.drift("check")[0] == 1 and len(project.runs()) == 1


def test_a_runner_copy_that_differs_from_the_current_runner_is_old(project):
    copy = project.skill / "verify"
    copy.write_bytes(RUNNER.read_bytes() + b"\n# edited here\n")
    project.commit()

    code, lines = project.drift("check")

    assert (code, lines) == (1, ["runner-old bytes differ, both runner 1",
                                 "drift: summary stale=0 uncovered=0 failed=0 runner=old"])

    copy.write_bytes(RUNNER.read_bytes().replace(b"\nRUNNER = 1\n", b"\nRUNNER = 0\n"))
    assert project.drift("check", "--no-run")[1][0] == "runner-old copy runner 0, current runner 1"
    copy.write_bytes(RUNNER.read_bytes().replace(b"\nRUNNER = 1\n", b"\n"))
    assert project.drift("check", "--no-run")[1][0] == "runner-old copy names no runner number, current runner 1"
    copy.unlink()
    assert project.drift("check", "--no-run")[1][0] == "runner-old no copy, current runner 1"
    copy.symlink_to(RUNNER)
    assert project.drift("check", "--no-run")[1][0] == "runner-old copy is a link, current runner 1"


def drift_elsewhere(tmp_path, runner_text):
    """A copy of drift.py in a skills folder of its own, with this runner text beside it, or none."""
    skills = tmp_path / "skills"
    script = skills / "maintain-verify-skill" / "scripts" / "drift.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(DRIFT, script)
    if runner_text is not None:
        (skills / "create-verify-skill" / "scripts").mkdir(parents=True)
        (skills / "create-verify-skill" / "scripts" / "verify").write_text(runner_text)
    return script


def test_with_no_current_runner_beside_it_the_runner_is_unknown_and_that_is_a_finding(project, tmp_path):
    script = drift_elsewhere(tmp_path, None)

    assert project.drift("check", "--no-run", script=script) == (
        1, ["runner-old unknown runner", "drift: summary stale=0 uncovered=0 failed=0 runner=unknown"])


def test_the_current_runner_is_found_beside_the_script_and_not_in_the_project(project, tmp_path):
    script = drift_elsewhere(tmp_path, RUNNER.read_text().replace("RUNNER = 1\n", "RUNNER = 2\n"))

    code, lines = project.drift("check", "--no-run", script=script)

    assert (code, lines[0]) == (1, "runner-old copy runner 1, current runner 2")


# ── the project's runner as a source of results ──────────────────────────────

def test_a_runner_that_could_not_finish_is_an_error_not_a_clean_result(project):
    project.write_map([feature("greet", {"kind": "run", "argv": ["no-such-tool-xyz"], "expect": {"exit": 0}})])
    project.commit()

    code, lines = project.drift("check")

    assert code == 2 and len(lines) == 1
    assert lines[0] == "drift: error the runner ended with exit 2: verify-demo: error the doctor found 1 gap(s)"


FAKE_RUNNERS = {
    "exit 0 and no result line": ("print('all good')", 0),
    "exit 1 and a last line that is not its own": ("print('fail greet step 1: x'); print('done')", 1),
    "exit 1 and a summary with no failed step": ("print('verify-demo: summary pass=0 fail=1 unverified=0 abc')", 1),
    "exit 0 and a summary line": ("print('verify-demo: summary pass=1 fail=0 unverified=0 abc')", 0),
    "exit 1 and a pass line": ("print('fail greet step 1: x'); print('verify-demo: pass 1/1 abc')", 1),
    "the result line of another app": ("print('fail greet step 1: x'); "
                                       "print('verify-other: summary pass=0 fail=1 unverified=0 abc')", 1),
    "exit 3": ("print('verify-demo: pass 1/1 abc')", 3),
    "killed by a signal": ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", None),
}


@pytest.mark.parametrize("name", sorted(FAKE_RUNNERS))
def test_runner_output_that_is_not_a_result_is_an_error(project, name):
    body, code = FAKE_RUNNERS[name]
    (project.skill / "verify").write_text(f"import sys\n{body}\nsys.exit({code})\n")

    code, lines = project.drift("check")

    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error "), lines


def test_a_fake_runner_with_a_real_result_shape_is_read(project):
    (project.skill / "verify").write_text("print('fail greet step 2: exit 1, expected 0')\n"
                                          "print('not-removed /x')\nprint('evidence .verify/runs/x')\n"
                                          "print('verify-demo: summary pass=0 fail=1 unverified=0 abc')\n"
                                          "raise SystemExit(1)\n")

    code, lines = project.drift("check")

    assert lines == ["failed-step greet step 2: exit 1, expected 0", "runner-old copy names no runner number, current runner 1",
                     "drift: summary stale=0 uncovered=0 failed=1 runner=old"]


# ── which skill ──────────────────────────────────────────────────────────────

def test_with_two_skills_the_app_must_be_named(project):
    done = sh(PY, str(SCAFFOLD), "--project", str(project.root), "--app", "other", cwd=project.root)
    assert done.returncode == 0, done.stdout
    other = Project(project.root, "other")
    other.write_map([feature("bad", step("boom"))])
    project.commit()

    code, lines = project.drift("check")
    assert code == 2 and lines == ["drift: error two or more verify skills (verify-demo, verify-other): name one with --app"]

    assert project.drift("check", "--app", "demo") == (0, ["drift: clean"])
    assert project.drift("check", "--app", "other")[1][-1] == "drift: summary stale=0 uncovered=0 failed=1"
    assert project.drift("check", "--app", "nope") == (2, ["drift: error no skill folder verify-nope"])
    assert project.drift("check", "--app", "../demo")[0] == 2
    # a name that would still reach a skill folder through '..' is refused for its form
    assert project.drift("check", "--app", "demo/../verify-demo") == (
        2, ["drift: error the app name must be lower-case letters, digits and '-'"])


def test_with_one_skill_it_is_found_and_with_none_it_is_an_error(project, tmp_path):
    assert project.drift("check", "--no-run") == (0, ["drift: clean"])
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    done = sh(PY, str(DRIFT), "check", "--project", str(empty), cwd=tmp_path)
    assert done.returncode == 2 and done.stdout.splitlines() == ["drift: error no verify skill in .claude/skills/"]


def test_a_skill_folder_that_is_a_link_is_not_used(project, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    project.skill.rename(elsewhere)
    project.skill.symlink_to(elsewhere)

    assert project.drift("check", "--no-run") == (2, ["drift: error no verify skill in .claude/skills/"])
    assert project.drift("bump", "--app", "demo") == (2, ["drift: error no skill folder verify-demo"])


# ── bump ─────────────────────────────────────────────────────────────────────

def test_bump_replaces_an_old_copy_keeps_its_mode_and_changes_nothing_else(project):
    copy = project.skill / "verify"
    copy.write_bytes(b"#!/usr/bin/env python3\nRUNNER = 1\n# an old copy\n")
    copy.chmod(0o750)
    before = project.snapshot()

    code, lines = project.drift("bump")

    assert (code, lines) == (0, [f"drift: bumped {copy}"])
    assert copy.read_bytes() == RUNNER.read_bytes() and not copy.is_symlink()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o750
    after = project.snapshot()
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    assert changed == [".claude/skills/verify-demo/verify"]
    assert project.drift("bump") == (0, ["drift: clean"])
    assert project.snapshot() == after


def test_bump_makes_a_copy_that_lost_its_execute_bits_executable(project):
    copy = project.skill / "verify"
    copy.write_bytes(b"old\n")
    copy.chmod(0o644)

    assert project.drift("bump")[0] == 0
    assert stat.S_IMODE(copy.stat().st_mode) == 0o755


@pytest.mark.parametrize("runner", [2, 0, "1", True, None])
def test_bump_refuses_a_map_for_another_runner_number(project, runner):
    copy = project.skill / "verify"
    copy.write_bytes(b"old\n")
    data = json.loads((project.skill / "features.json").read_text())
    if runner is None:
        data.pop("runner")
    else:
        data["runner"] = runner
    (project.skill / "features.json").write_text(json.dumps(data))
    before = project.snapshot()

    code, lines = project.drift("bump")

    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error "), lines
    assert project.snapshot() == before


def test_bump_refuses_when_the_current_runner_names_no_clear_number(project, tmp_path):
    for text in ("print('no number')\n", "RUNNER = 1\nRUNNER = 2\n", "RUNNER = 1  # one\n", "RUNNER=1\n"):
        script = drift_elsewhere(tmp_path / str(abs(hash(text))), text)
        before = project.snapshot()
        code, lines = project.drift("bump", script=script)
        assert code == 2 and lines[0].startswith("drift: error "), (text, lines)
        assert project.snapshot() == before
    assert project.drift("bump", script=drift_elsewhere(tmp_path / "none", None))[0] == 2


def test_bump_never_imports_or_runs_the_current_runner(project, tmp_path):
    marker = tmp_path / "ran"
    script = drift_elsewhere(tmp_path / "x", f"RUNNER = 1\nopen({str(marker)!r}, 'w').write('ran')\n")

    assert project.drift("bump", script=script)[0] == 0
    assert not marker.exists()


def test_bump_does_not_write_through_a_link_at_the_target(project, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("mine")
    copy = project.skill / "verify"
    copy.unlink()
    copy.symlink_to(victim)

    assert project.drift("bump")[0] == 0
    assert victim.read_text() == "mine"
    assert not copy.is_symlink() and copy.read_bytes() == RUNNER.read_bytes()


def test_bump_does_not_take_a_link_to_the_current_runner_as_current(project):
    copy = project.skill / "verify"
    copy.unlink()
    copy.symlink_to(RUNNER)

    assert project.drift("bump")[0] == 0
    assert not copy.is_symlink() and RUNNER.read_bytes() == copy.read_bytes()


def test_bump_does_not_write_through_a_link_at_the_temp_path(project, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("mine")
    (project.skill / "verify").write_bytes(b"old\n")
    (project.skill / "verify.drift-tmp").symlink_to(victim)

    assert project.drift("bump")[0] == 0
    assert victim.read_text() == "mine"
    assert not os.path.lexists(project.skill / "verify.drift-tmp")
    assert (project.skill / "verify").read_bytes() == RUNNER.read_bytes()


# ── the contract ─────────────────────────────────────────────────────────────

def test_every_outcome_keeps_the_contract(project, tmp_path):
    outcomes = [project.drift("check", "--no-run"), project.drift("check"), project.drift("bump")]
    project.write_map([feature("greet", step("hi"), paths=["nope/*"])])
    outcomes += [project.drift("check"), project.drift("check", "--no-run")]
    (project.skill / "verify").write_bytes(b"old\n")
    outcomes += [project.drift("bump")]
    (project.skill / "features.json").write_text("{")
    outcomes += [project.drift("check"), project.drift("bump")]
    outcomes += [project.drift("frob"), project.drift("check", "--bogus"), project.drift("check", project=False)]
    outcomes += [project.drift("check", "--app", "x\ndrift: clean")]

    for code, lines in outcomes:
        assert_contract(code, lines)
    assert [code for code, _ in outcomes] == [0, 0, 0, 1, 1, 0, 2, 2, 2, 2, 2, 2]


BAD_MAPS = {
    "not JSON": "{",
    "a list": "[]",
    "no features": json.dumps({"app": "demo", "runner": 1}),
    "features not a list": json.dumps({"app": "demo", "runner": 1, "features": {}}),
    "a feature with no id": json.dumps({"app": "demo", "runner": 1, "features": [{"paths": ["x"], "steps": []}]}),
    "paths not a list": json.dumps({"app": "demo", "runner": 1, "features": [{"id": "a", "paths": "src/*"}]}),
    "a path that is not text": json.dumps({"app": "demo", "runner": 1, "features": [{"id": "a", "paths": [1]}]}),
    "an empty path": json.dumps({"app": "demo", "runner": 1, "features": [{"id": "a", "paths": [""]}]}),
}


@pytest.mark.parametrize("name", sorted(BAD_MAPS))
def test_a_map_it_cannot_read_is_an_error(project, name):
    (project.skill / "features.json").write_text(BAD_MAPS[name])

    code, lines = project.drift("check", "--no-run")

    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error "), lines


def test_a_folder_outside_git_is_an_error(tmp_path):
    root = tmp_path / "plain"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP)
    assert sh(PY, str(SCAFFOLD), "--project", str(root), "--app", "demo", cwd=root).returncode == 0
    plain = Project(root)
    plain.write_map([GREET])

    code, lines = plain.drift("check", "--no-run")

    assert code == 2 and lines == ["drift: error git cannot list the tracked files of the project"]


def test_names_with_a_line_break_cannot_add_a_line(project):
    forged = "src/x\ndrift: clean"
    odd = project.root / "src" / "new\nline" / "f.py"
    odd.parent.mkdir()
    odd.write_text("x\n")
    project.write_map([feature("greet", *GREET["steps"], paths=["src/app.py", forged])])
    project.commit()

    code, lines = project.drift("check", "--no-run")

    assert code == 1
    assert lines == ["stale-path greet src/x drift: clean", "uncovered-path src/new line/ 1",
                     "drift: summary stale=1 uncovered=1 failed=0"]


def test_a_runner_line_with_a_control_character_is_cleaned(project):
    (project.skill / "verify").write_text("print('fail greet step 1: stdout lacks \"a\\x1b[2Kb\"')\n"
                                          "print('verify-demo: summary pass=0 fail=1 unverified=0 abc')\n"
                                          "raise SystemExit(1)\n")

    code, lines = project.drift("check")

    assert lines[0] == 'failed-step greet step 1: stdout lacks "a [2Kb"'


# ── the path rules, against a plain second version on random input ───────────

@pytest.fixture(scope="module")
def drift_module():
    loader = importlib.machinery.SourceFileLoader("drift_under_test", str(DRIFT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def plain_match(pattern, path):
    """'*' takes any run of characters, '/' too; '?' takes one; every other character is itself."""
    if not pattern:
        return not path
    if pattern[0] == "*":
        return any(plain_match(pattern[1:], path[cut:]) for cut in range(len(path) + 1))
    return bool(path) and pattern[0] in ("?", path[0]) and plain_match(pattern[1:], path[1:])


def plain_findings(features, files):
    stale = [(fid, pattern) for fid, patterns in features for pattern in patterns
             if not any(plain_match(pattern, path) for path in files)]
    heads = [pattern.split("/")[0] for _, patterns in features for pattern in patterns if "/" in pattern]
    counts = {}
    for path in files:
        if "/" not in path or not any(plain_match(head, path.split("/")[0]) for head in heads):
            continue
        if any(plain_match(pattern, path) for _, patterns in features for pattern in patterns):
            continue
        folder = path.rsplit("/", 1)[0] + "/"
        counts[folder] = counts.get(folder, 0) + 1
    return stale, sorted(counts.items())


def test_the_path_findings_match_a_plain_rule_on_random_input(drift_module):
    rng = random.Random(20261114)
    parts = ["src", "lib", "a", "b", "x.py", "y.md", "s"]
    for _ in range(1500):
        files = sorted({"/".join(rng.choice(parts) for _ in range(rng.randint(1, 4))) for _ in range(rng.randint(0, 8))})
        features = []
        for number in range(rng.randint(1, 3)):
            patterns = []
            for _ in range(rng.randint(1, 3)):
                pieces = [rng.choice(parts + ["*", "**", "?", "s?c", "*.py"]) for _ in range(rng.randint(1, 3))]
                patterns.append("/".join(pieces))
            features.append((f"f{number}", patterns))
        got = drift_module.path_findings(features, files)
        assert got == plain_findings(features, files), (features, files)
