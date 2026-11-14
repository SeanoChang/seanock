"""The verify runner: the file that create-verify-skill copies into a project.

Each test builds a small project in a temp folder: a stand-in command-line
app (APP below), a git repo, and the verify skill written by the real
scaffold script. Then it runs the copied runner as a program and reads what a
caller reads: the exit code, the last line, and the run folder.

Run: python3 -m pytest tests/test_verify_runner.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent / "config" / "claude" / "skills" / "create-verify-skill"
SCAFFOLD = SKILL / "scripts" / "scaffold.py"
RUNNER = SKILL / "scripts" / "verify"
PY = sys.executable

APP = r'''
import os, pathlib, subprocess, sys, time
cmd, rest = sys.argv[1], sys.argv[2:]
if cmd == "greet":
    print("hello", rest[0])
elif cmd == "seed":
    print(pathlib.Path(os.environ["APP_DATA"], "seed.txt").read_text().strip())
elif cmd == "append":
    with open(pathlib.Path(os.environ["APP_DATA"], "seed.txt"), "a") as handle:
        handle.write(rest[0] + "\n")
    print(pathlib.Path(os.environ["APP_DATA"], "seed.txt").read_text().strip())
elif cmd == "boom":
    print("partial output")
    sys.stderr.write("it broke\n")
    sys.exit(3)
elif cmd == "stdin":
    print("got", sys.stdin.read().strip())
elif cmd == "cwd":
    print(os.getcwd())
elif cmd in ("hang", "daemon"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    pathlib.Path(rest[0]).write_text(f"{os.getpid()} {child.pid}")
    if cmd == "hang":
        time.sleep(600)
    print("started")
'''


def step(*args, expect=None, **more):
    return {"kind": "run", "argv": [PY, "{root}/app.py", *args], "expect": expect or {"exit": 0}, **more}


def feature(fid, *steps):
    return {"id": fid, "paths": ["app.py"], "steps": list(steps)}


GREET = feature("greet", step("greet", "Ada", expect={"exit": 0, "stdout_has": ["hello Ada"]}))
BOOM = feature("boom", step("boom", expect={"exit": 0}))


def sh(*argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)


def git(project, *args):
    done = sh("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
              *args, cwd=project)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class Project:
    def __init__(self, root):
        self.root = root
        self.skill = root / ".claude" / "skills" / "verify-demo"

    def write_map(self, features, **top):
        data = {"app": "demo", "runner": 1, "features": features, **top}
        (self.skill / "features.json").write_text(json.dumps(data), encoding="utf-8")

    def commit(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def verify(self, *args):
        done = sh(PY, str(self.skill / "verify"), *args, cwd=self.root)
        lines = done.stdout.splitlines()
        return done.returncode, lines

    def start(self, *args):
        return subprocess.Popen([PY, str(self.skill / "verify"), *args], cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def runs(self):
        base = self.root / ".verify" / "runs"
        return sorted(base.iterdir()) if base.is_dir() else []

    def record(self, index=-1):
        return json.loads((self.runs()[index] / "run.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(APP, encoding="utf-8")
    git(root, "init", "-q")
    done = sh(PY, str(SCAFFOLD), "--project", str(root), "--app", "demo", cwd=root)
    assert done.returncode == 0, done.stdout + done.stderr
    made = Project(root)
    made.write_map([GREET])
    made.commit()
    return made


def alive(pid):
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def gone(pid, within=8.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def wait_for(path, within=20.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip().count(" ") == 1:
            return [int(part) for part in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear")


def stop(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ── the user's path, end to end ──────────────────────────────────────────────

def test_a_run_passes_names_the_commit_and_leaves_evidence_after_its_cleanup(project):
    project.write_map([GREET, feature("seed", step("seed", expect={"exit": 0, "stdout_has": ["planted"]}))],
                      fixture="data", env={"APP_DATA": "{tmp}/data"})
    (project.skill / "data").mkdir()
    (project.skill / "data" / "seed.txt").write_text("planted\n")
    head = project.commit()

    code, lines = project.verify("run")

    assert code == 0, lines
    assert lines[-1] == f"verify-demo: pass 2/2 {head}"
    run_dir = project.runs()[0]
    assert lines[-2] == f"evidence .verify/runs/{run_dir.name}"
    record = project.record()
    assert record["commit"] == head and record["dirty"] is False and record["result"] == "pass"
    assert record["cleaned"] is True and not Path(record["tmp"]).exists()
    assert (run_dir / "greet-1.out").read_text() == "hello Ada\n"
    assert (run_dir / "seed-1.out").read_text() == "planted\n"
    assert git(project.root, "status", "--porcelain") == ""


def test_a_failed_step_gives_exit_1_a_summary_and_the_reason(project):
    project.write_map([GREET, BOOM, feature("text", step("greet", "Ada", expect={"exit": 0, "stdout_has": ["hello Bob"]}))])
    head = project.commit()

    code, lines = project.verify("run")

    assert code == 1
    assert lines[-1] == f"verify-demo: summary pass=1 fail=2 unverified=0 {head}"
    assert "fail boom step 1: exit 3, expected 0" in lines
    assert 'fail text step 1: stdout lacks "hello Bob"' in lines
    assert (project.runs()[0] / "boom-1.err").read_text() == "it broke\n"


def test_each_text_check_can_fail_the_step(project):
    project.write_map([
        feature("a", step("boom", expect={"exit": 3, "stdout_lacks": ["partial"]})),
        feature("b", step("boom", expect={"exit": 3, "stderr_has": ["no such text"]})),
        feature("c", step("boom", expect={"exit": 3, "stderr_lacks": ["broke"]})),
        feature("d", step("boom", expect={"exit": 3, "stdout_has": ["partial"], "stderr_has": ["broke"],
                                          "stdout_lacks": ["broke"], "stderr_lacks": ["partial"]})),
    ])
    project.commit()

    code, lines = project.verify("run")

    assert code == 1 and "pass=1 fail=3" in lines[-1]
    assert 'fail a step 1: stdout has "partial"' in lines
    assert 'fail b step 1: stderr lacks "no such text"' in lines
    assert 'fail c step 1: stderr has "broke"' in lines


def test_a_feature_stops_at_its_first_failed_step(project):
    project.write_map([feature("two", step("boom"), step("greet", "Ada"))])
    project.commit()

    code, _ = project.verify("run")

    assert code == 1 and len(project.record()["features"][0]["steps"]) == 1


def test_stdin_cwd_and_the_placeholders_reach_the_step(project):
    project.write_map([feature("io",
        step("stdin", stdin="from the map\n", expect={"exit": 0, "stdout_has": ["got from the map"]}),
        step("cwd", cwd="{tmp}", expect={"exit": 0, "stdout_has": ["verify-demo-"]}),
        {"kind": "run", "argv": [PY, "-c", "import sys; print(sys.argv[1:])", "{bin}", "{other}"],
         "expect": {"exit": 0, "stdout_has": ["/built'", "'{other}'"]}})], bin="built")
    project.commit()

    code, lines = project.verify("run")

    assert code == 0, lines


def test_each_feature_gets_its_own_copy_of_the_fixture_alone_and_in_a_full_run(project):
    writes = feature("writes", step("append", "added by writes", expect={"exit": 0, "stdout_has": ["planted\nadded by writes"]}))
    reads = feature("reads", step("seed", expect={"exit": 0, "stdout_has": ["planted"], "stdout_lacks": ["added"]}))
    project.write_map([writes, reads, feature("again", *writes["steps"])], fixture="data", env={"APP_DATA": "{tmp}/data"})
    (project.skill / "data").mkdir()
    (project.skill / "data" / "seed.txt").write_text("planted\n")
    head = project.commit()

    assert project.verify("run")[1][-1] == f"verify-demo: pass 3/3 {head}"
    assert project.verify("run", "--only", "reads")[0] == 0
    assert (project.skill / "data" / "seed.txt").read_text() == "planted\n"


# ── a pass that does not cover the commit says so ────────────────────────────

def test_a_pass_on_a_tree_with_uncommitted_changes_says_dirty(project):
    (project.root / "app.py").write_text(APP + "\n# edited\n")

    code, lines = project.verify("run")

    assert code == 0 and lines[-1].endswith(" dirty") and project.record()["dirty"] is True


def test_a_failed_run_carries_the_same_marks_on_its_summary_line(project):
    project.write_map([GREET, BOOM])
    head = project.commit()
    (project.root / "app.py").write_text(APP + "\n# edited\n")

    code, lines = project.verify("run", "--only", "boom")

    assert code == 1 and lines[-1] == f"verify-demo: summary pass=0 fail=1 unverified=0 {head} dirty partial"


def test_a_pass_of_some_features_says_partial(project):
    project.write_map([GREET, feature("other", step("greet", "Bo"))])
    head = project.commit()

    code, lines = project.verify("run", "--only", "greet")

    assert code == 0 and lines[-1] == f"verify-demo: pass 1/1 {head} partial"
    code, lines = project.verify("run", "--only", "greet", "--only", "other")
    assert lines[-1] == f"verify-demo: pass 2/2 {head}"


def test_a_folder_with_no_commit_gives_nogit(project, tmp_path):
    bare = tmp_path / "plain"
    bare.mkdir()
    (bare / "app.py").write_text(APP)
    assert sh(PY, str(SCAFFOLD), "--project", str(bare), "--app", "demo", cwd=bare).returncode == 0
    plain = Project(bare)
    plain.write_map([GREET])

    code, lines = plain.verify("run")

    assert code == 0 and lines[-1] == "verify-demo: pass 1/1 nogit"
    assert plain.runs()[0].name.endswith("-nogit")


# ── not a result: exit 2 ─────────────────────────────────────────────────────

BAD_MAPS = {
    "unknown top key": lambda m: m.update(features_=1),
    "unknown step key": lambda m: m["features"][0]["steps"][0].update(expects={"exit": 0}),
    "unknown expect key": lambda m: m["features"][0]["steps"][0]["expect"].update(stdout_contains=["x"]),
    "expect with no exit code": lambda m: m["features"][0]["steps"][0].update(expect={"stdout_has": ["hello"]}),
    "empty expect": lambda m: m["features"][0]["steps"][0].update(expect={}),
    "exit code as true": lambda m: m["features"][0]["steps"][0].update(expect={"exit": True}),
    "empty text list": lambda m: m["features"][0]["steps"][0]["expect"].update(stdout_has=[]),
    "empty text": lambda m: m["features"][0]["steps"][0]["expect"].update(stdout_has=[""]),
    "a later kind": lambda m: m["features"][0]["steps"][0].update(kind="http"),
    "empty argv": lambda m: m["features"][0]["steps"][0].update(argv=[]),
    "no steps": lambda m: m["features"][0].update(steps=[]),
    "no paths": lambda m: m["features"][0].pop("paths"),
    "no features": lambda m: m.update(features=[]),
    "two features with one id": lambda m: m["features"].append(dict(m["features"][0])),
    "an id with a space": lambda m: m["features"][0].update(id="a b"),
    "a newer runner": lambda m: m.update(runner=2),
    "runner as text": lambda m: m.update(runner="1"),
    "an app name with a slash": lambda m: m.update(app="a/b"),
    "a zero timeout": lambda m: m.update(timeout_s=0),
    "a fixture outside the skill": lambda m: m.update(fixture="../../.."),
    "an absolute bin": lambda m: m.update(bin="/usr/bin"),
    "env that is not text": lambda m: m.update(env={"A": 1}),
    "a list": None,
    "not JSON": None,
}


@pytest.mark.parametrize("name", sorted(BAD_MAPS))
def test_a_map_that_the_runner_cannot_trust_is_an_error_not_a_result(project, name):
    data = json.loads(json.dumps({"app": "demo", "runner": 1, "features": [GREET]}))
    change = BAD_MAPS[name]
    if change:
        change(data)
    text = {"a list": "[]", "not JSON": "{"}.get(name) or json.dumps(data)
    (project.skill / "features.json").write_text(text)

    for command in ("run", "doctor"):
        code, lines = project.verify(command)
        assert code == 2, (command, lines)
        assert lines[-1].startswith(("verify: error ", "verify-demo: error ")), lines
    assert project.runs() == []


def test_a_name_from_the_map_cannot_write_a_last_line(project):
    forged = "x\nverify-demo: pass 1/1 0000000 verify-demo: pass 1/1 1111111"
    (project.skill / "features.json").write_text(json.dumps({"app": "demo", "runner": 1, "features": [GREET], forged: 1}))

    code, lines = project.verify("run")

    assert code == 2 and len(lines) == 1 and lines[0].startswith("verify: error ")


def test_an_unknown_feature_id_is_an_error(project):
    code, lines = project.verify("run", "--only", "nope")

    assert code == 2 and lines[-1] == 'verify-demo: error no feature has the id "nope"'
    assert project.runs() == []


def test_the_runner_outside_a_skill_folder_is_an_error(project, tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "verify").write_bytes((project.skill / "verify").read_bytes())
    (lone / "features.json").write_text(json.dumps({"app": "demo", "runner": 1, "features": [GREET]}))

    done = sh(PY, str(lone / "verify"), "run", cwd=lone)

    assert done.returncode == 2 and done.stdout.splitlines()[-1].startswith("verify-demo: error ")


# ── the doctor ───────────────────────────────────────────────────────────────

def test_the_doctor_is_clean_on_a_good_map(project):
    assert project.verify("doctor") == (0, ["verify-demo: doctor clean"])


def test_the_doctor_lists_each_gap_and_a_run_then_refuses(project):
    project.write_map([feature("a", {"kind": "run", "argv": ["no-such-tool-xyz", "x"], "expect": {"exit": 0}},
                               {"kind": "run", "argv": ["scripts/absent.sh"], "expect": {"exit": 0}},
                               {"kind": "run", "argv": ["{bin}/built-later"], "expect": {"exit": 0}})],
                      build=["no-such-builder-xyz"], fixture="data",
                      env={"REAL": "/Users/someone/vault", "HOMEISH": "~/data", "FINE": "{tmp}/x", "WORD": "plain"})
    (project.root / ".gitignore").write_text("")

    code, lines = project.verify("doctor")

    assert code == 1
    assert lines == ["tool-missing no-such-builder-xyz", "tool-missing no-such-tool-xyz",
                     "tool-missing scripts/absent.sh", "fixture-missing data",
                     "env-outside-tmp HOMEISH", "env-outside-tmp REAL",
                     "evidence-not-ignored .verify/", "verify-demo: summary gaps=7"]
    code, lines = project.verify("run")
    assert code == 2 and lines[-1] == "verify-demo: error the doctor found 7 gap(s)"
    assert project.runs() == []


# ── steps that do not end well ───────────────────────────────────────────────

def test_a_step_that_cannot_start_is_unverified_and_not_a_pass(project):
    project.write_map([GREET, feature("later", {"kind": "run", "argv": ["{bin}/not-built"], "expect": {"exit": 0}})])
    head = project.commit()

    code, lines = project.verify("run")

    assert code == 1 and lines[-1] == f"verify-demo: summary pass=1 fail=0 unverified=1 {head}"
    assert any(line.startswith("unverified later step 1: could not start: ") for line in lines)
    assert project.record()["result"] == "unverified"


def test_a_step_past_its_timeout_fails_and_nothing_of_it_stays_running(project):
    pid_file = project.root / ".verify" / "hang.pid"
    project.write_map([feature("hang", step("hang", str(pid_file), timeout_s=3))])
    project.commit()

    code, lines = project.verify("run")

    pids = [int(part) for part in pid_file.read_text().split()]
    try:
        assert code == 1 and "fail hang step 1: timeout" in lines
        assert all(gone(pid) for pid in pids)
    finally:
        stop(pids)


def test_what_a_step_leaves_in_the_background_is_stopped(project):
    pid_file = project.root / ".verify" / "daemon.pid"
    project.write_map([feature("daemon", step("daemon", str(pid_file), expect={"exit": 0, "stdout_has": ["started"]}))])
    project.commit()

    code, lines = project.verify("run")

    pids = [int(part) for part in pid_file.read_text().split()]
    try:
        assert code == 0, lines
        assert gone(pids[1])
    finally:
        stop(pids)


def test_a_failed_build_fails_the_run_and_keeps_the_build_output(project):
    project.write_map([GREET], build=[PY, "{root}/app.py", "boom"])
    head = project.commit()

    code, lines = project.verify("run")

    assert code == 1 and "fail build: exit 3" in lines
    assert lines[-1] == f"verify-demo: summary pass=0 fail=0 unverified=1 build=fail {head}"
    assert (project.runs()[0] / "build.err").read_text() == "it broke\n"
    assert project.record()["result"] == "fail" and project.record()["features"][0]["steps"] == []


def test_a_build_that_works_runs_before_the_steps(project):
    project.write_map([feature("built", step("seed", expect={"exit": 0, "stdout_has": ["from the build"]}))],
                      build=[PY, "-c", "import os, pathlib; pathlib.Path(os.environ['APP_DATA']).mkdir(); "
                                       "pathlib.Path(os.environ['APP_DATA'], 'seed.txt').write_text('from the build'); "
                                       "print(os.environ['SCRATCH'])"],
                      env={"APP_DATA": "{root}/.verify/made", "SCRATCH": "{tmp}"})
    project.commit()

    code, lines = project.verify("run")

    assert code == 0, lines
    assert project.record()["build"]["exit"] == 0
    scratch = Path((project.runs()[0] / "build.out").read_text().strip())
    assert scratch.name == "_build" and scratch.parent == Path(project.record()["tmp"])


# ── cut-off runs, live runs, and clean ───────────────────────────────────────

def hang_project(project, timeout_s=300):
    pid_file = project.root / ".verify" / "hang.pid"
    project.write_map([feature("hang", step("hang", str(pid_file), timeout_s=timeout_s))])
    project.commit()
    return pid_file


def test_clean_stops_what_a_killed_run_left_removes_its_temp_folder_and_keeps_the_evidence(project):
    pid_file = hang_project(project)
    runner = project.start("run")
    pids = wait_for(pid_file)
    try:
        runner.kill()
        runner.wait()
        tmp = Path(project.record()["tmp"])
        assert tmp.is_dir() and alive(pids[0])
        code, lines = project.verify("runs")
        assert lines[0].split()[1:] == ["cut-off", "unverified"]

        code, lines = project.verify("clean")

        assert code == 0 and lines[-1] == "verify-demo: cleaned 1"
        assert all(gone(pid) for pid in pids)
        assert not tmp.exists()
        assert project.record()["cleaned"] is True
        assert (project.runs()[0] / "hang-1.out").exists()
        assert project.verify("clean") == (0, ["verify-demo: cleaned 0"])
    finally:
        runner.kill()
        stop(pids)


def test_clean_leaves_a_live_run_alone_and_a_stop_signal_ends_that_run_cleanly(project):
    pid_file = hang_project(project)
    runner = project.start("run")
    pids = wait_for(pid_file)
    try:
        tmp = Path(project.record()["tmp"])

        code, lines = project.verify("clean")

        assert code == 0 and lines[-1] == "verify-demo: cleaned 0 live=1"
        assert alive(pids[0]) and tmp.is_dir()

        runner.send_signal(signal.SIGTERM)
        out, _ = runner.communicate(timeout=30)

        assert runner.returncode == 2
        assert out.splitlines()[-1] == "verify-demo: error the run was interrupted"
        assert all(gone(pid) for pid in pids)
        assert not tmp.exists()
        record = project.record()
        assert record["result"] == "unverified" and record["cleaned"] is True
    finally:
        runner.kill()
        stop(pids)


@pytest.mark.parametrize("marker", [None, "another-run"])
def test_clean_never_removes_a_folder_that_does_not_carry_the_marker_of_the_run(project, tmp_path, marker):
    pid_file = hang_project(project)
    runner = project.start("run")
    pids = wait_for(pid_file)
    victim = tmp_path / "verify-demo-victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("mine")
    if marker:
        (victim / ".verify-run").write_text(marker)
    try:
        runner.kill()
        runner.wait()
        run_json = project.runs()[0] / "run.json"
        record = json.loads(run_json.read_text())
        real_tmp = Path(record["tmp"])
        record["tmp"] = str(victim)
        run_json.write_text(json.dumps(record))

        code, lines = project.verify("clean")

        assert code == 1 and lines[-1] == "verify-demo: summary kept=1 cleaned=0 live=0"
        assert (victim / "keep.txt").read_text() == "mine"
        assert project.record()["cleaned"] is False
    finally:
        runner.kill()
        stop(pids)
        import shutil
        shutil.rmtree(real_tmp, ignore_errors=True)


def test_clean_does_not_stop_a_process_that_only_shares_a_recorded_pid(project):
    pid_file = hang_project(project)
    runner = project.start("run")
    pids = wait_for(pid_file)
    bystander = subprocess.Popen([PY, "-c", "import time; time.sleep(600)"], start_new_session=True)
    try:
        runner.kill()
        runner.wait()
        pids_json = project.runs()[0] / "started.pids"
        pids_json.write_text(json.dumps([{"pid": bystander.pid, "start": "Thu Jan  1 00:00:00 1970"}]))

        code, lines = project.verify("clean")

        assert code == 0
        time.sleep(0.5)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()
        runner.kill()
        stop(pids)


def cut_off_run(project):
    """A run folder whose runner is gone: start a run that hangs, and kill the runner and the step."""
    pid_file = hang_project(project)
    runner = project.start("run")
    pids = wait_for(pid_file)
    runner.kill()
    runner.wait()
    stop(pids)
    return project.runs()[0]


def test_clean_does_not_write_through_a_link_that_sits_in_the_run_folder(project, tmp_path):
    run_dir = cut_off_run(project)
    victim = tmp_path / "victim.txt"
    victim.write_text("mine")
    (run_dir / "run.json.part").symlink_to(victim)

    code, lines = project.verify("clean")

    assert code == 0 and lines[-1] == "verify-demo: cleaned 1"
    assert victim.read_text() == "mine"
    assert not (run_dir / "run.json").is_symlink() and project.record()["cleaned"] is True


def test_a_run_folder_that_is_a_link_is_not_a_run(project, tmp_path):
    run_dir = cut_off_run(project)
    elsewhere = tmp_path / "elsewhere"
    run_dir.rename(elsewhere)
    run_dir.symlink_to(elsewhere)

    assert project.verify("runs") == (0, ["verify-demo: runs 0"])
    assert project.verify("clean") == (0, ["verify-demo: cleaned 0"])
    assert json.loads((elsewhere / "run.json").read_text())["cleaned"] is False
    import shutil
    shutil.rmtree(json.loads((elsewhere / "run.json").read_text())["tmp"], ignore_errors=True)


@pytest.mark.parametrize("pid", [0, 1, -1, -42])
def test_clean_never_signals_group_0_or_1_whatever_the_file_says(runner_module, tmp_path, monkeypatch, pid):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"cleaned": False, "runner_pid": None, "tmp": None}))
    (run_dir / "started.pids").write_text(json.dumps([{"pid": pid, "start": "same"}, {"pid": 4242, "start": "same"}]))
    signalled = []
    monkeypatch.setattr(runner_module, "proc_start", lambda _pid: "same")
    monkeypatch.setattr(runner_module, "kill_group", lambda target, sig: signalled.append(target))

    assert runner_module.clean_run(run_dir) == ("cleaned", "")
    assert signalled == [4242]


def test_clean_of_a_named_run_that_does_not_exist_is_an_error(project):
    code, lines = project.verify("clean", "../../..")

    assert code == 2 and lines[-1].startswith("verify-demo: error no run folder named")


def test_two_runs_at_the_same_time_keep_their_own_evidence(project):
    project.write_map([feature("slow", {"kind": "run", "argv": [PY, "-c", "import time; time.sleep(1); print('ok')"],
                                        "expect": {"exit": 0, "stdout_has": ["ok"]}})])
    project.commit()

    first, second = project.start("run"), project.start("run")
    outs = [proc.communicate(timeout=60)[0] for proc in (first, second)]

    assert first.returncode == 0 and second.returncode == 0, outs
    assert len(project.runs()) == 2
    tmps = {project.record(0)["tmp"], project.record(1)["tmp"]}
    assert len(tmps) == 2 and not any(Path(tmp).exists() for tmp in tmps)


# ── the pure parts, against a second version on random input ─────────────────

@pytest.fixture(scope="module")
def runner_module():
    loader = importlib.machinery.SourceFileLoader("verify_runner", str(RUNNER))
    spec = importlib.util.spec_from_loader("verify_runner", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_run_result_matches_a_plain_rule_on_random_feature_results(runner_module):
    rng = random.Random(20261114)
    for _ in range(3000):
        results = [rng.choice(["pass", "fail", "unverified"]) for _ in range(rng.randint(0, 5))]
        build_failed = rng.random() < 0.2
        got = runner_module.overall(results, build_failed)
        every_pass = len(results) > 0 and all(result == "pass" for result in results) and not build_failed
        any_fail = build_failed or any(result == "fail" for result in results)
        assert got == ("pass" if every_pass else "fail" if any_fail else "unverified"), (results, build_failed)


def test_the_step_check_matches_a_plain_rule_on_random_output(runner_module, tmp_path):
    rng = random.Random(7)
    words = ["ok", "error", "panic", "2 notes", "done"]
    out_path, err_path = tmp_path / "o", tmp_path / "e"
    for _ in range(1500):
        out = " ".join(rng.sample(words, rng.randint(0, 4)))
        err = " ".join(rng.sample(words, rng.randint(0, 4)))
        out_path.write_text(out)
        err_path.write_text(err)
        expect = {"exit": rng.choice([0, 1])}
        for key in runner_module.TEXT_CHECKS:
            if rng.random() < 0.5:
                expect[key] = rng.sample(words, rng.randint(1, 2))
        result = {"exit": rng.choice([0, 1]), "timeout": rng.random() < 0.1}
        holds = (not result["timeout"] and result["exit"] == expect["exit"]
                 and all(word in out for word in expect.get("stdout_has", []))
                 and not any(word in out for word in expect.get("stdout_lacks", []))
                 and all(word in err for word in expect.get("stderr_has", []))
                 and not any(word in err for word in expect.get("stderr_lacks", [])))
        assert (runner_module.check_step(expect, result, out_path, err_path) == []) == holds, (expect, result, out, err)
