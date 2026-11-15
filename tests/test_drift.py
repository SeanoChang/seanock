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


def test_the_file_list_comes_from_the_project_whatever_the_caller_or_the_repo_sets(project, tmp_path):
    """A program that the repo's git settings name must not run, and a GIT_ variable of the caller must not aim git elsewhere."""
    marker = tmp_path / "ran"
    prog = tmp_path / "prog.sh"
    prog.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    prog.chmod(0o755)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    (other / "zzz.txt").write_text("x")
    git(other, "add", "-A")
    project.write_map([feature("greet", GREET["steps"][0], paths=("src/app.py", "src/gone.py"))])
    project.commit()
    with open(project.root / ".git" / "config", "a") as handle:   # after the last git call of the test itself
        handle.write(f"[core]\n\tfsmonitor = {prog}\n\tpager = {prog}\n")
    project.env.update(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE=str(other / ".git" / "index"))
    assert not marker.exists()

    code, lines = project.drift("check", "--no-run")

    assert (code, lines) == (1, ["stale-path greet src/gone.py", "drift: summary stale=1 uncovered=0 failed=0"])
    assert not marker.exists()


@pytest.mark.parametrize("no_run", [True, False], ids=["no-run", "full run"])
def test_a_module_beside_the_runner_copy_or_on_the_callers_path_is_never_loaded(project, tmp_path, no_run):
    """Found in review: the copy was started as a plain script, so Python looked for the modules it imports
    in the copy's own folder first. A tracked `tempfile.py` there ran as part of a check that says that
    nothing of the app runs, and the result was still clean."""
    marker = tmp_path / "ran"
    trap = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    for folder in (project.skill, project.root):   # the copy's own folder, and the folder the copy is started in
        for name in ("argparse.py", "tempfile.py", "json.py", "shutil.py", "sitecustomize.py", "usercustomize.py"):
            (folder / name).write_text(trap)
    project.write_map([feature("greet", step("hi"))])
    project.commit()

    code, lines = project.drift("check", *(["--no-run"] if no_run else []))

    assert not marker.exists(), marker.read_text()
    assert not list(project.skill.rglob("__pycache__")), "the copy's folder got a cache folder"
    if no_run:
        assert (code, lines) == (0, ["drift: clean"])
        assert project.runs() == []
    else:
        assert code == 1 and lines[-1] == "drift: summary stale=0 uncovered=0 failed=1", lines


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


# ── the project's runner as a source of results ──────────────────────────────

MISSING = {"kind": "run", "argv": ["no-such-tool-xyz"], "expect": {"exit": 0}}


@pytest.mark.parametrize("mode", [(), ("--no-run",)])
def test_a_gap_of_the_runners_doctor_is_a_finding_with_and_without_the_run(project, mode):
    """The doctor refuses the run, so no step runs. That is about the skill, and it hides nothing."""
    project.write_map([feature("greet", MISSING, {"kind": "run", "argv": ["bin/gone"], "expect": {"exit": 0}},
                               paths=("src/app.py", "src/moved.py"))])
    project.commit()

    assert project.drift("check", *mode) == (
        1, ["stale-path greet src/moved.py", "failed-doctor tool-missing no-such-tool-xyz", "failed-doctor tool-missing bin/gone",
            "drift: summary stale=1 uncovered=0 failed=2"])
    assert project.runs() == []


DOCTOR_GAPS = {
    "a value of env outside the temp folder": (dict(env={"HOME": "/Users/x"}), "env-outside-tmp HOME"),
    "a fixture folder that is not there": (dict(fixture="fx"), "fixture-missing fx"),
    "a tool that is not there": (dict(build=["no-such-build-tool-xyz"]), "tool-missing no-such-build-tool-xyz"),
}


@pytest.mark.parametrize("name", sorted(DOCTOR_GAPS))
def test_what_the_doctor_reads_from_the_map_is_seen_without_the_run(project, name):
    """The map has the right form, and the runner still refuses to run with it. A look without the run says so."""
    top, gap = DOCTOR_GAPS[name]
    project.write_map([GREET], **top)
    project.commit()
    assert project.verify("doctor") == (1, [gap, "verify-demo: summary gaps=1"])
    expected = (1, [f"failed-doctor {gap}", "drift: summary stale=0 uncovered=0 failed=1"])

    assert project.drift("check", "--no-run") == expected
    assert project.drift("check") == expected
    assert project.runs() == []


def test_evidence_that_git_does_not_ignore_is_a_gap_of_the_doctor(project):
    (project.root / ".gitignore").write_text("")
    project.commit()

    assert project.drift("check", "--no-run") == (
        1, ["failed-doctor evidence-not-ignored .verify/", "drift: summary stale=0 uncovered=0 failed=1"])


@pytest.mark.parametrize("mode", [(), ("--no-run",)])
def test_a_copy_that_is_not_the_current_runner_is_never_started(project, mode):
    """Only the bytes of the current runner are run. An old copy, or one that someone changed, is a finding."""
    copy = project.skill / "verify"
    copy.write_text("import pathlib\npathlib.Path(__file__).with_name('ran').write_text('x')\nprint('verify-demo: pass 1/1 abc')\n")
    project.write_map([feature("greet", MISSING, paths=("src/app.py", "src/moved.py"))])
    project.commit()

    assert project.drift("check", *mode) == (
        1, ["stale-path greet src/moved.py", "runner-old copy names no runner number, current runner 1",
            "drift: summary stale=1 uncovered=0 failed=0 runner=old"])
    assert not (project.skill / "ran").exists() and project.runs() == []


def fake_current(project, tmp_path, run="print('verify-demo: pass 1/1 abc')", code=0,
                 doctor="print('verify-demo: doctor clean')", doctor_code=0):
    """A stand-in for the current runner, beside a copy of drift.py, and the same bytes as the copy of the
    project. So drift starts it. Its doctor and its run print what the test gives."""
    text = ("import sys\nRUNNER = 1\n\n\nclass MapError(Exception):\n    pass\n\n\ndef load_map(path=None):\n    return {}\n\n\n"
            "if __name__ == '__main__':\n"
            "    if sys.argv[1] == 'doctor':\n"
            f"        {doctor}\n        sys.exit({doctor_code})\n"
            f"    {run}\n    sys.exit({code})\n")
    (project.skill / "verify").write_text(text)
    return drift_elsewhere(tmp_path, text)


FAKE_RUNNERS = {
    "exit 0 and no result line": ("print('all good')", 0),
    "exit 1 and a last line that is not its own": ("print('fail greet step 1: x'); print('done')", 1),
    "exit 1 and a summary with no failed step": ("print('verify-demo: summary pass=0 fail=1 unverified=0 abc')", 1),
    "exit 0 and a summary line": ("print('verify-demo: summary pass=1 fail=0 unverified=0 abc')", 0),
    "exit 0 and a summary with a failed step": ("print('fail greet step 1: x'); "
                                                "print('verify-demo: summary pass=0 fail=1 unverified=0 abc')", 0),
    "exit 3 and a summary with a failed step": ("print('fail greet step 1: x'); "
                                                "print('verify-demo: summary pass=0 fail=1 unverified=0 abc')", 3),
    "exit 1 and a pass line": ("print('fail greet step 1: x'); print('verify-demo: pass 1/1 abc')", 1),
    "the result line of another app": ("print('fail greet step 1: x'); "
                                       "print('verify-other: summary pass=0 fail=1 unverified=0 abc')", 1),
    "exit 3": ("print('verify-demo: pass 1/1 abc')", 3),
    "exit 2 and no output": ("pass", 2),
    "exit 2 and a pass line": ("print('verify-demo: pass 1/1 abc')", 2),
    # the runner's own words for a run that it could not finish: never a finding, never clean
    "the run was interrupted": ("print('verify-demo: error the run was interrupted')", 2),
    "the runner ended on an error of its own": ("print('verify-demo: error NotADirectoryError: x')", 2),
    "the build could not start": ("print('verify-demo: error the build could not start: x')", 2),
    "a gap that the doctor found only in the run": ("print('tool-missing x'); print('verify-demo: error the doctor found 1 gap(s)')", 2),
    "exit 0 and an error line": ("print('verify-demo: error x')", 0),
    "exit 1 and an error line": ("print('fail greet step 1: x'); print('verify-demo: error x')", 1),
    "killed by a signal": ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", None),
}


@pytest.mark.parametrize("name", sorted(FAKE_RUNNERS))
def test_runner_output_that_is_not_a_result_is_an_error(project, tmp_path, name):
    body, code = FAKE_RUNNERS[name]
    script = fake_current(project, tmp_path, run=body, code=code)

    code, lines = project.drift("check", script=script)

    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error the runner ended with "), lines
    assert project.drift("check", "--no-run", script=script) == (0, ["drift: clean"])


FAKE_DOCTORS = {
    "exit 0 and no result line": ("print('all good')", 0),
    "exit 0 and a line above the clean line": ("print('tool-missing x'); print('verify-demo: doctor clean')", 0),
    "exit 1 and a clean line": ("print('verify-demo: doctor clean')", 1),
    "exit 1 and a summary with no gap line": ("print('verify-demo: summary gaps=1')", 1),
    "exit 1 and fewer gap lines than the summary counts": ("print('tool-missing x'); print('verify-demo: summary gaps=2')", 1),
    "exit 1 and more gap lines than the summary counts": ("print('a b'); print('c d'); print('verify-demo: summary gaps=1')", 1),
    "exit 1 and a summary of no gap": ("print('verify-demo: summary gaps=0')", 1),
    "exit 0 and a summary line": ("print('tool-missing x'); print('verify-demo: summary gaps=1')", 0),
    "the summary line of another app": ("print('tool-missing x'); print('verify-other: summary gaps=1')", 1),
    "the summary line of a run": ("print('fail greet step 1: x'); print('verify-demo: summary pass=0 fail=1 unverified=0 abc')", 1),
    "exit 2 and an error line": ("print('verify-demo: error the map has no features')", 2),
    "exit 3": ("print('verify-demo: doctor clean')", 3),
    "killed by a signal": ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", None),
}


@pytest.mark.parametrize("name", sorted(FAKE_DOCTORS))
@pytest.mark.parametrize("mode", [(), ("--no-run",)])
def test_doctor_output_that_is_not_a_result_is_an_error(project, tmp_path, name, mode):
    body, code = FAKE_DOCTORS[name]
    script = fake_current(project, tmp_path, doctor=body, doctor_code=code,
                          run="import pathlib; pathlib.Path(__file__).with_name('ran').write_text('x')")

    code, lines = project.drift("check", *mode, script=script)

    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error the doctor of the runner ended with "), lines
    assert not (project.skill / "ran").exists()


def test_a_fake_runner_with_a_real_result_shape_is_read(project, tmp_path):
    script = fake_current(project, tmp_path, code=1, run=(
        "print('fail greet step 2: exit 1, expected 0'); print('not-removed /x'); print('evidence .verify/runs/x'); "
        "print('verify-demo: summary pass=0 fail=1 unverified=0 abc')"))

    assert project.drift("check", script=script) == (
        1, ["failed-step greet step 2: exit 1, expected 0", "drift: summary stale=0 uncovered=0 failed=1"])


def test_a_fake_doctor_with_a_real_result_shape_is_read_and_then_nothing_runs(project, tmp_path):
    script = fake_current(project, tmp_path, doctor_code=1,
                          doctor="print('tool-missing a b'); print('env-outside-tmp X'); print('verify-demo: summary gaps=2')",
                          run="import pathlib; pathlib.Path(__file__).with_name('ran').write_text('x')")

    assert project.drift("check", script=script) == (
        1, ["failed-doctor tool-missing a b", "failed-doctor env-outside-tmp X", "drift: summary stale=0 uncovered=0 failed=2"])
    assert not (project.skill / "ran").exists()


@pytest.mark.parametrize("stage", ["doctor", "run"])
def test_a_check_that_could_not_finish_still_shows_the_findings_it_has(project, tmp_path, stage):
    """Exit 2 is not a result, and the list is not complete. But a stale path that was found is not thrown away."""
    broken = dict(doctor="print('oops')") if stage == "doctor" else dict(run="print('verify-demo: error the run was interrupted')", code=2)
    script = fake_current(project, tmp_path, **broken)
    project.write_map([feature("greet", GREET["steps"][0], paths=("src/app.py", "src/moved.py"))])
    project.commit()

    code, lines = project.drift("check", script=script)

    assert code == 2 and lines[0] == "stale-path greet src/moved.py" and len(lines) == 2
    assert lines[1].startswith("drift: error the doctor of the runner ended with " if stage == "doctor" else "drift: error the runner ended with ")
    assert_contract(code, lines)


def test_the_doctor_reads_git_only_for_this_project(project, tmp_path):
    """The doctor asks git if the evidence folder is ignored. The GIT_ variables of the caller must not aim that
    call at another repo, where nothing is ignored."""
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    (other / "zzz.txt").write_text("x")
    git(other, "add", "-A")
    project.env.update(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE=str(other / ".git" / "index"))

    assert project.drift("check", "--no-run") == (0, ["drift: clean"])


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
    assert [code for code, _ in outcomes] == [0, 0, 0, 1, 1, 0, 1, 2, 2, 2, 2, 2]


def a_map(**change):
    """The map of the project fixture with these keys changed; None takes a key out."""
    data = {"app": "demo", "runner": 1, "features": [GREET], **change}
    return json.dumps({key: value for key, value in data.items() if value is not None})


REFUSED_MAPS = {
    "not JSON": "{",
    "a list": "[]",
    "no features key": a_map(features=None),
    "features not a list": a_map(features={}),
    "no feature": a_map(features=[]),
    "a feature that is not an object": a_map(features=["greet"]),
    "a feature with no id": a_map(features=[{"paths": ["src/app.py"], "steps": GREET["steps"]}]),
    "an id with a capital": a_map(features=[dict(GREET, id="Greet")]),
    "two features with one id": a_map(features=[GREET, GREET]),
    "paths not a list": a_map(features=[dict(GREET, paths="src/*")]),
    "no path": a_map(features=[dict(GREET, paths=[])]),
    "a path that is not text": a_map(features=[dict(GREET, paths=[1])]),
    "an empty path": a_map(features=[dict(GREET, paths=[""])]),
    "no steps": a_map(features=[dict(GREET, steps=[])]),
    "a step with no expect": a_map(features=[dict(GREET, steps=[{"kind": "run", "argv": ["true"]}])]),
    "a step of another kind": a_map(features=[dict(GREET, steps=[dict(GREET["steps"][0], kind="http")])]),
    "a key the runner does not know": a_map(retries=3),
    "a feature key the runner does not know": a_map(features=[dict(GREET, owner="ada")]),
    "the map of another runner": a_map(runner=9),
    "a runner that is not a number": a_map(runner="1"),
    "no runner": a_map(runner=None),
    "an app with a capital": a_map(app="Demo"),
    "no app": a_map(app=None),
    "a timeout of zero": a_map(timeout_s=0),
    "a bin outside the project": a_map(bin="../bin"),
}


@pytest.mark.parametrize("name", sorted(REFUSED_MAPS))
def test_a_map_that_the_runner_refuses_is_a_finding_with_the_runners_own_reason(project, name):
    """Never clean: with the run and without it, the finding is the reason that the runner itself gives."""
    (project.skill / "features.json").write_text(REFUSED_MAPS[name])
    code, said = project.verify("doctor")
    assert code == 2 and re.match(r"verify(-demo)?: error \S", said[-1]), said
    reason = said[-1].split(": error ", 1)[1]
    expected = (1, [f"failed-map {reason}", "drift: summary stale=0 uncovered=0 failed=1"])

    assert project.drift("check", "--no-run") == expected
    assert project.drift("check") == expected
    assert project.runs() == []


def test_a_refused_map_keeps_the_path_findings_and_the_runner_finding(project):
    (project.skill / "verify").write_bytes(RUNNER.read_bytes() + b"\n# an older copy\n")
    (project.skill / "features.json").write_text(a_map(retries=3, features=[dict(GREET, paths=["src/app.py", "src/moved.py"])]))
    project.commit()

    expected = (1, ["stale-path greet src/moved.py", "failed-map the map has a key this runner does not know: retries",
                    "runner-old bytes differ, both runner 1", "drift: summary stale=1 uncovered=0 failed=1 runner=old"])
    assert project.drift("check", "--no-run") == expected
    assert project.drift("check") == expected


def test_a_reason_from_the_map_cannot_add_a_line(project):
    (project.skill / "features.json").write_text(a_map(**{"x\ndrift: clean": 1}))

    code, lines = project.drift("check", "--no-run")

    assert (code, lines) == (1, ["failed-map the map has a key this runner does not know: x drift: clean",
                                 "drift: summary stale=0 uncovered=0 failed=1"])


def test_the_map_rules_are_those_of_the_current_runner_beside_the_script(project, tmp_path):
    """Another current runner, with another number: the map of the project is for the old one."""
    script = drift_elsewhere(tmp_path, RUNNER.read_text().replace("RUNNER = 1\n", "RUNNER = 2\n"))

    assert project.drift("check", "--no-run", script=script) == (
        1, ["failed-map the map is for runner 1, this is runner 2", "runner-old copy runner 1, current runner 2",
            "drift: summary stale=0 uncovered=0 failed=1 runner=old"])


def test_with_no_current_runner_nothing_is_started_and_the_result_is_never_clean(project, tmp_path):
    script = drift_elsewhere(tmp_path, None)
    (project.skill / "features.json").write_text(a_map(retries=3))
    unknown = (1, ["runner-old unknown runner", "drift: summary stale=0 uncovered=0 failed=0 runner=unknown"])

    assert project.drift("check", script=script) == unknown
    assert project.drift("check", "--no-run", script=script) == unknown
    assert project.runs() == []
    (project.skill / "features.json").write_text("{")
    assert project.drift("check", "--no-run", script=script) == (
        1, ["failed-map features.json is not JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            "runner-old unknown runner", "drift: summary stale=0 uncovered=0 failed=1 runner=unknown"])


@pytest.mark.parametrize("text", ["RUNNER = 1\n", "RUNNER = 1\nraise SystemExit(0)\n", "RUNNER = 1\ndef load_map(:\n",
                                  "RUNNER = 1\nload_map = 1\nMapError = 2\n", "RUNNER = 1\nimport no_such_module_xyz\n"])
def test_a_current_runner_with_no_map_rules_is_an_error_of_the_check(project, tmp_path, text):
    script = drift_elsewhere(tmp_path, text)

    code, lines = project.drift("check", "--no-run", script=script)

    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error the current runner"), lines


def test_reading_the_rules_of_the_current_runner_starts_none_of_its_commands_and_writes_no_file(project, tmp_path):
    script = drift_elsewhere(tmp_path, RUNNER.read_text())
    before = sorted(str(path) for path in (tmp_path / "skills").rglob("*"))

    assert project.drift("check", "--no-run", script=script) == (0, ["drift: clean"])
    assert sorted(str(path) for path in (tmp_path / "skills").rglob("*")) == before
    assert project.runs() == []


def test_bump_still_refuses_a_map_it_cannot_read(project):
    (project.skill / "verify").write_bytes(b"old\n")
    for text in ("{", "[]", a_map(features=None), a_map(features=[dict(GREET, paths=[""])])):
        (project.skill / "features.json").write_text(text)
        before = project.snapshot()
        code, lines = project.drift("bump")
        assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error "), lines
        assert project.snapshot() == before


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


def test_a_runner_line_with_a_control_character_is_cleaned(project, tmp_path):
    script = fake_current(project, tmp_path, code=1, run=(
        "print('fail greet step 1: stdout lacks \"a\\x1b[2Kb\"'); print('verify-demo: summary pass=0 fail=1 unverified=0 abc')"))

    code, lines = project.drift("check", script=script)

    assert lines[0] == 'failed-step greet step 1: stdout lacks "a [2Kb"'


def test_a_gap_line_cannot_add_a_line_or_stand_as_the_result(project, tmp_path):
    script = fake_current(project, tmp_path, doctor_code=1,
                          doctor="print('tool-missing x drift: clean'); print('verify-demo: summary gaps=1')")

    assert project.drift("check", "--no-run", script=script) == (
        1, ["failed-doctor tool-missing x drift: clean", "drift: summary stale=0 uncovered=0 failed=1"])

    # the real runner prints no line separator; a stand-in that does gives two lines for one gap, and that is no result
    script = fake_current(project, tmp_path / "second", doctor_code=1,
                          doctor="print('tool-missing x\\u2028drift: clean'); print('verify-demo: summary gaps=1')")
    code, lines = project.drift("check", "--no-run", script=script)
    assert code == 2 and len(lines) == 1 and lines[0].startswith("drift: error the doctor of the runner ended with exit 1")


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
