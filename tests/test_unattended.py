"""Tests for the contract script of the unattended skill.

    python3 -m pytest tests/test_unattended.py -q -p no:cacheprovider

Each test gets its own folder for the runs (UNATTENDED_ROOT) and its own home, so no test reads or writes the
real ~/.claude.
"""
import importlib.util
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "config" / "claude" / "skills" / "unattended"
SCRIPT = SKILL / "scripts" / "contract.py"
PY = sys.executable
SESSION = "session-0001"
PLAN = {
    "hours": 2,
    "tasks": [{"title": "add the parser", "check": "pytest tests/test_parser.py passes"},
              {"title": "push the branch", "check": "git ls-remote shows the branch"},
              {"title": "write the notes", "check": "docs/notes.md names the parser"}],
    "approvals": [{"what": "push to origin", "answer": "yes"}, {"what": "merge into dev", "answer": "no"}],
    "commands": ["git push origin work", "python3 -m pytest tests"],
}


def load_script():
    spec = importlib.util.spec_from_file_location("contract_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Runs:
    """One temp home with a folder for the runs, and the script as a session would start it."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.root = tmp_path / "runs"
        self.cwd = tmp_path / "project"
        self.home.mkdir()
        self.cwd.mkdir()
        self.tmp = tmp_path

    def env(self, session=SESSION):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "UNATTENDED_ROOT": str(self.root)}
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return env

    def run(self, *args, session=SESSION):
        done = subprocess.run([PY, "-I", str(SCRIPT), *args], cwd=self.cwd, env=self.env(session), capture_output=True,
                              text=True, timeout=60)
        assert done.stderr == "", done.stderr
        return done.returncode, done.stdout.splitlines()

    def plan(self, **changes):
        plan = {name: value for name, value in {**PLAN, **changes}.items() if value is not ...}
        path = self.tmp / f"plan-{len(list(self.tmp.glob('plan-*')))}.json"
        path.write_text(json.dumps(plan))
        return str(path)

    def started(self, session=SESSION, **changes):
        code, lines = self.run("start", "--plan", self.plan(**changes), session=session)
        assert code == 0 and lines[-1].startswith("unattended: started "), lines
        return Path(lines[0].removeprefix("run folder: "))

    def log_lines(self, folder):
        return [json.loads(line) for line in (folder / "log.jsonl").read_text().splitlines()]


@pytest.fixture
def runs(tmp_path):
    return Runs(tmp_path)


# ── start ────────────────────────────────────────────────────────────────────

def test_start_writes_the_contract_for_this_session_and_nothing_into_the_project(runs):
    folder = runs.started()
    assert folder.parent == runs.root / SESSION
    contract = json.loads((folder / "contract.json").read_text())
    assert contract["session"] == SESSION and contract["cwd"] == str(runs.cwd.resolve())
    assert [task["id"] for task in contract["tasks"]] == ["1", "2", "3"]
    assert contract["tasks"][1] == {"id": "2", "title": "push the branch", "check": "git ls-remote shows the branch"}
    assert contract["approvals"] == PLAN["approvals"]
    assert [line["kind"] for line in runs.log_lines(folder)] == ["started"]
    assert list(runs.cwd.iterdir()) == [], "a run writes nothing into the folder that it works in"


def test_the_run_is_for_its_user_alone(runs):
    folder = runs.started()
    for path, mode in ((runs.root, 0o700), (folder.parent, 0o700), (folder, 0o700), (folder / "contract.json", 0o600),
                       (folder / "log.jsonl", 0o600)):
        assert stat.S_IMODE(path.stat().st_mode) == mode, path


def test_the_time_limit_is_the_start_plus_the_hours_and_eight_hours_with_none_named(runs):
    script = load_script()
    for hours, expected in ((2, 2.0), (..., 8.0), (0.5, 0.5)):
        folder = runs.started(session=f"session-h{expected}".replace(".", "-"), hours=hours)
        contract = json.loads((folder / "contract.json").read_text())
        span = script.moment_of(contract["ends"]) - script.moment_of(contract["started"])
        assert span.total_seconds() == expected * 3600


@pytest.mark.parametrize("changes, message", [
    ({"tasks": []}, "the plan has no task"),
    ({"tasks": ...}, "the plan has no list `tasks`"),
    ({"tasks": [{"title": "t"}]}, "`tasks` entry 1 has no `check`"),
    ({"tasks": [{"title": "  ", "check": "c"}]}, "`tasks` entry 1 has no `title`"),
    ({"tasks": ["a title alone"]}, "`tasks` entry 1 is no object"),
    ({"approvals": ...}, "the plan has no list `approvals`"),
    ({"approvals": [{"what": "push", "answer": "maybe"}]}, "it is yes or no"),
    ({"approvals": [{"what": "push"}]}, "`approvals` entry 1 has no `answer`"),
    ({"hours": 0}, "`hours` is a number above 0"),
    ({"hours": 100}, "`hours` is a number above 0"),
    ({"hours": True}, "`hours` is a number above 0"),
    ({"hours": "8"}, "`hours` is a number above 0"),
])
def test_a_plan_that_is_not_whole_starts_no_run(runs, changes, message):
    code, lines = runs.run("start", "--plan", runs.plan(**changes))
    assert code == 2 and len(lines) == 1 and lines[0].startswith("unattended: error ") and message in lines[0], lines
    assert not runs.root.exists()


def test_an_empty_list_of_approvals_is_an_answer(runs):
    folder = runs.started(approvals=[])
    assert json.loads((folder / "contract.json").read_text())["approvals"] == []


@pytest.mark.parametrize("session", [None, "", "short", "../../outside", "a/b-session-1", "session 0001", "x" * 65])
def test_without_a_plain_session_id_nothing_runs(runs, session):
    for args in (("start", "--plan", runs.plan()), ("status",), ("close",), ("log", "--kind", "decision", "--text", "x")):
        code, lines = runs.run(*args, session=session)
        assert code == 2 and len(lines) == 1 and "no session id" in lines[0], (args, lines)
    assert not runs.root.exists() and not (runs.tmp / "outside").exists()


def test_a_second_start_in_an_open_run_is_refused_and_another_session_has_its_own_run(runs):
    first = runs.started()
    code, lines = runs.run("start", "--plan", runs.plan())
    assert code == 2 and "open run already" in lines[0]
    other = runs.started(session="session-0002")
    assert other.parent != first.parent
    assert runs.run("status", session="session-0003") == (1, ["unattended: none"])


# ── log and status ───────────────────────────────────────────────────────────

def test_the_state_of_a_task_is_what_its_last_line_says(runs):
    folder = runs.started()
    assert runs.run("status")[1][:3] == ["left 1 add the parser", "left 2 push the branch", "left 3 write the notes"]
    assert runs.run("log", "--kind", "heartbeat", "--task", "1", "--text", "start") == (0, ["unattended: logged heartbeat"])
    assert runs.run("log", "--kind", "skipped", "--task", "2", "--text", "the push was denied", "--needs",
                    "an allow rule for git push") == (0, ["unattended: logged skipped"])
    assert runs.run("log", "--kind", "done", "--task", "1", "--text", "12 passed") == (0, ["unattended: logged done"])
    code, lines = runs.run("status")
    assert code == 0 and lines[:3] == ["done 1 add the parser", "skipped 2 push the branch", "left 3 write the notes"]
    assert lines[3].startswith("unattended: open left=1 ends=")
    assert runs.run("log", "--kind", "done", "--task", "2", "--text", "pushed after all")[0] == 0
    assert runs.run("status")[1][1] == "done 2 push the branch", "a step that was skipped can be done later"
    kinds = [(line["kind"], line.get("task")) for line in runs.log_lines(folder)]
    assert kinds == [("started", None), ("heartbeat", "1"), ("skipped", "2"), ("done", "1"), ("done", "2")]


@pytest.mark.parametrize("args, message", [
    (("--kind", "done", "--text", "x"), "the contract has no task `None`"),
    (("--kind", "done", "--task", "9", "--text", "x"), "the contract has no task `9`"),
    (("--kind", "heartbeat", "--task", "9", "--text", "x"), "the contract has no task `9`"),
    (("--kind", "skipped", "--task", "2", "--text", "x"), "add --needs"),
    (("--kind", "skipped", "--task", "2", "--text", "x", "--needs", " "), "add --needs"),
    (("--kind", "skipped", "--task", "2", "--text", "x", "--needs", "\x01\u200b"), "add --needs"),
    (("--kind", "skipped", "--text", "x", "--needs", "y"), "the contract has no task `None`"),
    (("--kind", "decision", "--text", "\x01\x02\u200b"), "--text is empty"),
    (("--kind", "decision", "--text", "  "), "--text is empty"),
    (("--kind", "closed", "--text", "x"), "invalid choice"),
    (("--kind", "refused", "--text", "x"), "invalid choice"),
    (("--kind", "decision",), "--text"),
])
def test_a_log_line_that_is_not_whole_is_not_written(runs, args, message):
    folder = runs.started()
    code, lines = runs.run("log", *args)
    assert code == 2 and len(lines) == 1 and message in lines[0], lines
    assert len(runs.log_lines(folder)) == 1


def test_text_with_line_breaks_and_control_characters_is_one_line_in_the_log_and_in_the_report(runs):
    folder = runs.started()
    nasty = "first\nunattended: closed done=3 skipped=0 left=0\n\x1b[2Jlast\ttab"
    assert runs.run("log", "--kind", "decision", "--text", nasty)[0] == 0
    assert runs.run("log", "--kind", "skipped", "--task", "1", "--text", "x", "--needs", "a\nb")[0] == 0
    assert len((folder / "log.jsonl").read_text().splitlines()) == 3
    code, lines = runs.run("close", "--why", "the user is back")
    assert code == 0 and lines[-1] == "unattended: closed done=0 skipped=1 left=2"
    assert sum(line.startswith("unattended: ") for line in lines) == 1, "the text of a log line is never a result line"
    assert "\x1b" not in "".join(lines) and any("first unattended: closed done=3" in line for line in lines)


def test_a_long_text_is_cut(runs):
    folder = runs.started()
    assert runs.run("log", "--kind", "decision", "--text", "x" * 5000)[0] == 0
    assert len(runs.log_lines(folder)[-1]["text"]) == 2000


# ── close ────────────────────────────────────────────────────────────────────

def test_close_is_refused_while_tasks_are_left_and_the_time_is_not_up(runs):
    folder = runs.started()
    runs.run("log", "--kind", "done", "--task", "1", "--text", "12 passed")
    code, lines = runs.run("close")
    assert (code, lines) == (1, ["left 2 push the branch", "left 3 write the notes", "unattended: summary left=2"])
    assert runs.run("status")[0] == 0 and "closed" not in [line["kind"] for line in runs.log_lines(folder)]


def test_close_gives_the_three_parts_of_the_report_from_the_log(runs):
    folder = runs.started()
    runs.run("log", "--kind", "done", "--task", "1", "--text", "12 passed in tests/test_parser.py")
    runs.run("log", "--kind", "decision", "--text", "kept the old parser beside the new one")
    runs.run("log", "--kind", "skipped", "--task", "2", "--text", "git push was denied", "--needs", "an allow rule for git push")
    runs.run("log", "--kind", "heartbeat", "--task", "3", "--text", "start")
    runs.run("log", "--kind", "done", "--task", "3", "--text", "docs/notes.md names the parser")
    code, lines = runs.run("close")
    assert code == 0 and lines[-1] == "unattended: closed done=2 skipped=1 left=0"
    text = "\n".join(lines)
    blocked, rest = text.split("## Changed")
    changed, found = rest.split("## Found")
    assert blocked.startswith("## Blocked on me\n")
    assert "- Task 2 (push the branch): git push was denied Needs: an allow rule for git push" in blocked
    assert "Task 1" not in blocked and "Task 3" not in blocked
    assert "- Task 1 (add the parser): 12 passed in tests/test_parser.py" in changed
    assert "- Task 3 (write the notes): docs/notes.md names the parser" in changed and "Task 2" not in changed
    assert "- kept the old parser beside the new one" in found
    assert f"1 heartbeat lines, 0 stops refused. Log: {folder / 'log.jsonl'}" in found
    assert runs.log_lines(folder)[-1]["kind"] == "closed"
    assert runs.run("status") == (1, ["unattended: none"])
    assert runs.run("close")[0] == 2 and runs.run("log", "--kind", "decision", "--text", "x")[0] == 2


def test_a_close_with_a_reason_lists_the_tasks_that_are_left_under_blocked_on_me(runs):
    folder = runs.started()
    runs.run("log", "--kind", "done", "--task", "1", "--text", "12 passed")
    code, lines = runs.run("close", "--why", "the user is back")
    assert code == 0 and lines[-1] == "unattended: closed done=1 skipped=0 left=2"
    blocked = "\n".join(lines).split("## Changed")[0]
    assert "- Task 2 (push the branch): not done." in blocked and "- Task 3 (write the notes): not done." in blocked
    assert runs.log_lines(folder)[-1]["text"] == "the user is back"


@pytest.mark.parametrize("why", ["  ", "\u200b", "\x01"])
def test_a_reason_that_holds_no_letter_is_no_reason(runs, why):
    runs.started()
    code, lines = runs.run("close", "--why", why)
    assert code == 1 and lines[-1] == "unattended: summary left=3"


def test_the_report_takes_the_last_line_of_each_task_and_no_heartbeat(runs):
    runs.started()
    runs.run("log", "--kind", "skipped", "--task", "1", "--text", "first try failed", "--needs", "a key")
    runs.run("log", "--kind", "heartbeat", "--task", "1", "--text", "a heartbeat text")
    runs.run("log", "--kind", "done", "--task", "1", "--text", "second try passed")
    runs.run("log", "--kind", "done", "--task", "2", "--text", "pushed")
    runs.run("log", "--kind", "skipped", "--task", "2", "--text", "the push was rejected after all", "--needs", "a rebase")
    code, lines = runs.run("close", "--why", "x")
    text = "\n".join(lines)
    assert code == 0 and "first try failed" not in text and "a heartbeat text" not in text and "- Task 2 (push the branch): pushed" not in text
    assert text.count("Task 1 (") == 1 and text.count("Task 2 (") == 1 and text.count("Task 3 (") == 1
    assert "- Task 1 (add the parser): second try passed" in text.split("## Changed")[1]
    assert "the push was rejected after all Needs: a rebase" in text.split("## Changed")[0]


def test_a_terminal_that_cannot_show_a_letter_gets_it_in_escaped_form(runs):
    runs.started()
    runs.run("log", "--kind", "decision", "--text", "修 the parser")
    env = dict(runs.env(), LC_ALL="en_US.US-ASCII")
    done = subprocess.run([PY, "-I", str(SCRIPT), "close", "--why", "x"], cwd=runs.cwd, env=env, capture_output=True)
    assert done.returncode == 0 and done.stderr == b"", done.stderr
    out = done.stdout.decode("ascii")
    assert "- \\u4fee the parser" in out and out.splitlines()[-1] == "unattended: closed done=0 skipped=0 left=3"


def test_after_the_time_limit_close_needs_no_reason(runs):
    folder = runs.started(hours=0.0000001)
    code, lines = runs.run("status")
    assert code == 0 and lines[-1].endswith(" late")
    code, lines = runs.run("close")
    assert code == 0 and lines[-1] == "unattended: closed done=0 skipped=0 left=3"
    assert runs.log_lines(folder)[-1]["text"] == "the time limit is reached"


def test_a_line_is_never_written_after_the_close(runs):
    """Found by the adversary: `log` read the state, `close` closed the run, and `log` then wrote its line. The
    line was in no report. With the lock, a `log` that comes second sees the close and is refused."""
    script = load_script()
    for attempt in range(12):
        session = f"session-race{attempt:02d}"
        folder = runs.started(session=session)
        both = [subprocess.Popen([PY, "-I", str(SCRIPT), *args], cwd=runs.cwd, env=runs.env(session), stdout=subprocess.PIPE,
                                 text=True)
                for args in (("close", "--why", "back"), ("log", "--kind", "done", "--task", "2", "--text", "t2"))]
        (closed, _), (logged, _) = [one.communicate(timeout=60) for one in both]
        kinds = [line["kind"] for line in runs.log_lines(folder)]
        assert kinds[-1] == "closed", (attempt, kinds)
        if "done" in kinds:
            assert "Task 2 (push the branch): t2" in closed and both[1].returncode == 0
        else:
            assert both[1].returncode == 2 and "no open run" in logged


def test_a_command_waits_for_the_lock_of_the_run_and_gives_up_with_an_error(runs, monkeypatch):
    import fcntl
    script = load_script()
    folder = runs.started()
    monkeypatch.setenv("UNATTENDED_ROOT", str(runs.root))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setattr(script, "LOCK_WAIT_S", 0.3)
    held = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert script.main(["log", "--kind", "decision", "--text", "x"]) == 2
    finally:
        os.close(held)
    assert script.main(["log", "--kind", "decision", "--text", "x"]) == 0
    assert [line["kind"] for line in runs.log_lines(folder)] == ["started", "decision"]


def test_a_new_run_can_start_after_a_close(runs, monkeypatch):
    first = runs.started()
    assert runs.run("close", "--why", "done for now")[0] == 0
    script = load_script()
    later = script.now() + script.datetime.timedelta(seconds=5)
    while script.now() < later - script.datetime.timedelta(seconds=4):   # the name of a run folder is its second
        pass
    second = runs.started()
    assert second != first and second.parent == first.parent
    assert runs.run("status")[1][-1].startswith("unattended: open left=3")
    assert [line["kind"] for line in runs.log_lines(first)] == ["started", "closed"]


# ── files that are not what they should be ───────────────────────────────────

def test_a_link_in_the_place_of_a_folder_or_of_a_file_is_not_followed(runs, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    runs.root.symlink_to(target)
    code, lines = runs.run("start", "--plan", runs.plan())
    assert code == 2 and "is no plain folder" in lines[0] and list(target.iterdir()) == []
    runs.root.unlink()
    runs.root.mkdir()
    (runs.root / SESSION).symlink_to(target)                        # the same for the folder of the session
    for args in (("start", "--plan", runs.plan()), ("status",)):
        code, lines = runs.run(*args)
        assert code == 2 and "is no plain folder" in lines[0] and list(target.iterdir()) == [], (args, lines)
    (runs.root / SESSION).unlink()
    folder = runs.started()
    victim = tmp_path / "victim.txt"
    victim.write_text("a file of the user\n")
    (folder / "log.jsonl").unlink()
    (folder / "log.jsonl").symlink_to(victim)
    for args in (("log", "--kind", "decision", "--text", "x"), ("status",), ("close", "--why", "x")):
        code, lines = runs.run(*args)
        assert code == 2 and lines[0].startswith("unattended: error "), (args, lines)
    assert victim.read_text() == "a file of the user\n"


def test_a_link_to_a_file_that_reads_well_is_refused_too(runs, tmp_path):
    """The link above ends on text that is no log, so a read through it fails by itself. These links end on files
    that read well: only the refusal of the link stops them."""
    folder = runs.started()
    for name in ("contract.json", "log.jsonl"):
        real = tmp_path / f"real-{name}"
        (folder / name).rename(real)
        (folder / name).symlink_to(real)
        before = real.read_bytes()
        for args in (("status",), ("log", "--kind", "decision", "--text", "x"), ("close", "--why", "x")):
            code, lines = runs.run(*args)
            assert code == 2 and lines[0].startswith("unattended: error "), (name, args, lines)
        assert real.read_bytes() == before
        (folder / name).unlink()
        real.rename(folder / name)
    assert runs.run("status")[0] == 0


def test_a_log_that_becomes_a_link_after_it_was_read_is_not_written_through(runs, tmp_path, monkeypatch):
    script = load_script()
    folder = runs.started()
    monkeypatch.setenv("UNATTENDED_ROOT", str(runs.root))
    run = script.Run(folder)
    victim = tmp_path / "victim.jsonl"
    victim.write_text("")
    (folder / "log.jsonl").unlink()
    (folder / "log.jsonl").symlink_to(victim)
    with pytest.raises(OSError):
        run.add("decision", text="x")
    assert victim.read_text() == ""


def test_a_file_with_the_name_of_a_run_is_no_run(runs):
    folder = runs.started()
    (folder.parent / "20990101T000000Z").write_text("a file, and its name sorts last\n")
    code, lines = runs.run("status")
    assert code == 0 and lines[-1].startswith("unattended: open left=3"), lines
    assert runs.run("prune", "--days", "0") == (0, ["unattended: pruned 0"])


def test_a_root_that_is_no_absolute_path_is_an_error(runs):
    done = subprocess.run([PY, "-I", str(SCRIPT), "start", "--plan", runs.plan()], cwd=runs.cwd, capture_output=True,
                          text=True, env=dict(runs.env(), UNATTENDED_ROOT="runs-here"))
    assert done.returncode == 2 and "UNATTENDED_ROOT is not an absolute path" in done.stdout
    assert list(runs.cwd.iterdir()) == []


def test_a_run_that_cannot_be_read_is_an_error_and_not_a_state(runs):
    folder = runs.started()
    (folder / "contract.json").write_text("{not json")
    for args in (("status",), ("close",), ("log", "--kind", "decision", "--text", "x")):
        code, lines = runs.run(*args)
        assert code == 2 and "cannot read the run" in lines[0], (args, lines)


# ── prune ────────────────────────────────────────────────────────────────────

def test_prune_removes_the_runs_that_ended_long_ago_and_keeps_the_rest(runs):
    old = runs.started(session="session-old1")
    runs.run("close", "--why", "x", session="session-old1")
    open_run = runs.started(session="session-open")
    late = runs.started(session="session-late", hours=0.0000001)
    stray = runs.root / "session-old1" / "notes.txt"
    stray.write_text("not a run\n")
    assert runs.run("prune") == (0, ["unattended: pruned 0"]), "nothing is older than 14 days"
    assert runs.run("prune", "--days", "0") == (0, ["unattended: pruned 1"])
    assert not old.exists() and open_run.exists()
    assert late.exists(), "a run that was never closed may still have a session that writes to it"
    assert stray.exists(), "a file that is no run stays, and so does its folder"
    assert runs.run("prune", "--days", "-1")[0] == 2
    for days in ("3651", "1000000", "99999999999999999999"):
        code, lines = runs.run("prune", "--days", days)
        assert code == 2 and "--days is 0 to 3650" in lines[0], lines
    assert runs.run("prune", "--days", "3650") == (0, ["unattended: pruned 0"])


def test_a_run_that_was_never_closed_is_removed_fourteen_days_after_its_time_limit(runs):
    script = load_script()
    late = runs.started(session="session-late")
    contract = json.loads((late / "contract.json").read_text())
    for days, gone in ((13, False), (15, True)):
        contract["ends"] = script.iso(script.now() - script.datetime.timedelta(days=days))
        (late / "contract.json").write_text(json.dumps(contract))
        for wanted in ("0", "30"):
            assert runs.run("prune", "--days", wanted, session="session-0009") == (0, [f"unattended: pruned {int(gone)}"]) \
                or not late.exists()
        assert late.exists() is not gone
    assert not (runs.root / "session-late").exists(), "a session folder with nothing left is removed"


def test_a_long_keep_time_does_not_keep_a_run_that_was_never_closed(runs):
    """Found by the verifier: no test told 14 days from `--days` when `--days` is the longer one."""
    script = load_script()
    late = runs.started(session="session-late")
    contract = json.loads((late / "contract.json").read_text())
    contract["ends"] = script.iso(script.now() - script.datetime.timedelta(days=20))
    (late / "contract.json").write_text(json.dumps(contract))
    assert runs.run("prune", "--days", "30", session="session-0009") == (0, ["unattended: pruned 1"])
    assert not late.exists()


def test_a_folder_of_a_start_under_way_is_not_removed_by_a_keep_time_of_zero(runs):
    script = load_script()
    half = runs.root / "session-half" / script.now().strftime(script.STAMP)
    half.mkdir(parents=True)
    assert runs.run("prune", "--days", "0") == (0, ["unattended: pruned 0"])
    assert half.exists()


def test_a_task_whose_title_is_invisible_text_is_refused(runs):
    code, lines = runs.run("start", "--plan", runs.plan(tasks=[{"title": "\u200b", "check": "x"}]))
    assert code == 2 and "`tasks` entry 1 has no `title`" in lines[0], lines


def test_a_log_that_is_a_link_to_nothing_is_a_run_that_cannot_be_read(runs):
    folder = runs.started()
    (folder / "log.jsonl").unlink(missing_ok=True)
    (folder / "log.jsonl").symlink_to(runs.tmp / "nowhere")
    code, lines = runs.run("status")
    assert code == 2 and "cannot read the run" in lines[0], lines
    code, lines = runs.run("log", "--kind", "decision", "--text", "x")
    assert code == 2 and not (runs.tmp / "nowhere").exists(), lines


@pytest.mark.parametrize("damage", ["a log line with no time", "a log line with no kind", "a log line that is no object",
                                    "a contract that is no object",
                                    "a log that is no text", "a contract with a time that is none"])
def test_a_run_that_cannot_be_read_stops_no_other_session(runs, damage):
    """Found in review: `start` prunes, and one damaged file of any session ended it with a traceback."""
    bad = runs.started(session="session-bad1")
    runs.run("close", "--why", "x", session="session-bad1")
    if damage == "a log line with no time":
        (bad / "log.jsonl").write_text('{"kind": "started"}\n{"kind": "closed"}\n')
    elif damage == "a log line with no kind":
        (bad / "log.jsonl").write_text('{"t": "2026-11-17T00:00:00Z"}\n{"t": "2026-11-17T00:00:01Z", "kind": 7}\n')
    elif damage == "a log line that is no object":
        (bad / "log.jsonl").write_text('7\n"closed"\n[1]\n')
    elif damage == "a contract that is no object":
        (bad / "contract.json").write_text("[]")
    elif damage == "a log that is no text":
        (bad / "log.jsonl").write_bytes(b"\xff\xfe\x00")
    else:
        (bad / "contract.json").write_text(json.dumps(dict(json.loads((bad / "contract.json").read_text()), ends="soon")))
    assert runs.run("prune", "--days", "0") == (0, ["unattended: pruned 0"])
    runs.started()
    assert bad.exists(), "what cannot be read is left alone"
    code, lines = runs.run("status", session="session-bad1")
    assert code in (1, 2) and len(lines) == 1 and lines[0].startswith("unattended: "), lines


def old_closed_run(runs, session):
    """A run of another session that was closed 20 days ago."""
    script = load_script()
    folder = runs.started(session=session)
    runs.run("close", "--why", "x", session=session)
    lines = runs.log_lines(folder)
    lines[-1]["t"] = script.iso(script.now() - script.datetime.timedelta(days=20))
    (folder / "log.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    return folder


@pytest.mark.parametrize("fault", ["a session folder that cannot be read", "an old run that cannot be removed",
                                   "a contract that is nested very deep"])
def test_a_folder_that_cannot_be_read_or_removed_stops_no_other_session(runs, fault):
    """Found by the adversary: each of these ended `start` and `prune` of every session with exit 2."""
    bad = old_closed_run(runs, "session-bad1")
    locked = bad.parent if fault == "a session folder that cannot be read" else bad
    try:
        if fault == "a contract that is nested very deep":
            (bad / "contract.json").write_text("[" * 200000 + "]" * 200000)
        else:
            locked.chmod(0o000 if locked == bad.parent else 0o500)
        good = old_closed_run(runs, "session-good")
        assert runs.run("prune") == (0, ["unattended: pruned 1"]), "the run beside the fault is still pruned"
        assert not good.exists()
        runs.started()
        assert runs.run("status")[0] == 0
    finally:
        locked.chmod(0o700)
    assert bad.exists()


def test_prune_follows_no_link_and_removes_no_folder_that_is_not_a_run(runs, tmp_path):
    outside = old_closed_run(runs, "session-out1")
    moved = tmp_path / "outside-session"
    outside.parent.rename(moved)
    (runs.root / "session-out1").symlink_to(moved)                  # a link in the place of a session folder
    inside = old_closed_run(runs, "session-in01")
    target = tmp_path / "outside-run"
    inside.rename(target)
    inside.symlink_to(target)                                       # a link in the place of a run folder
    (runs.root / "session-empty").mkdir()
    (runs.root / "keepThisFolder").mkdir()
    assert runs.run("prune", "--days", "0") == (0, ["unattended: pruned 0"])
    assert (moved / outside.name / "contract.json").exists() and (target / "contract.json").exists()
    assert (runs.root / "session-empty").is_dir() and (runs.root / "keepThisFolder").is_dir(), \
        "an empty folder is removed only when this prune took the last run out of it"


def test_a_start_that_fails_halfway_does_not_block_its_session(runs):
    """Found by both reviewers: the run folder was made before the contract, and a folder with no contract made
    every later command of the session end with exit 2."""
    gone = runs.tmp / "gone"
    gone.mkdir()
    plan = runs.plan()
    done = subprocess.run([PY, "-I", str(SCRIPT), "start", "--plan", plan], cwd=gone, env=runs.env(), capture_output=True,
                          text=True, preexec_fn=lambda: os.rmdir(os.getcwd()))
    assert done.returncode == 2 and done.stdout.startswith("unattended: error "), done.stdout
    assert runs.run("status") == (1, ["unattended: none"])
    half = runs.root / SESSION / "20200101T000000Z"                 # a run folder that never got its contract
    half.mkdir(parents=True)
    assert runs.run("status") == (1, ["unattended: none"])
    folder = runs.started()
    assert not half.exists(), "the next start removes a folder that is older than 14 days and never got a contract"
    fresh = folder.parent / "20990101T000000Z"                      # one that a start under way could have made
    fresh.mkdir()
    assert runs.run("prune", "--days", "0", session="session-0009") == (0, ["unattended: pruned 0"])
    assert fresh.is_dir() and runs.run("status")[0] == 0


def test_a_link_in_the_place_of_a_run_folder_is_no_run(runs, tmp_path):
    """The files of a run are opened with no link followed, but that holds for the last part of a path only."""
    folder = runs.started()
    elsewhere = tmp_path / "elsewhere-run"
    folder.rename(elsewhere)
    folder.symlink_to(elsewhere)
    assert runs.run("status") == (1, ["unattended: none"])
    code, lines = runs.run("log", "--kind", "decision", "--text", "x")
    assert code == 2 and "no open run" in lines[0]
    assert [line["kind"] for line in runs.log_lines(elsewhere)] == ["started"]


def test_a_run_that_cannot_be_removed_does_not_keep_the_next_run_of_its_session(runs):
    stuck = old_closed_run(runs, "session-two1")
    second = stuck.parent / "20200202T020202Z"
    shutil.copytree(stuck, second)
    stuck.chmod(0o500)
    try:
        assert runs.run("prune") == (0, ["unattended: pruned 1"])
    finally:
        stuck.chmod(0o700)
    assert stuck.exists() and not second.exists()


def test_a_run_belongs_to_the_session_that_its_contract_names(runs):
    """On a disk that takes `abc` and `ABC` for one name, two session ids could share a folder."""
    folder = runs.started()
    other = runs.root / "session-0002"
    other.mkdir()
    (folder).rename(other / folder.name)
    for args in (("status",), ("close", "--why", "mine"), ("log", "--kind", "decision", "--text", "x")):
        code, lines = runs.run(*args, session="session-0002")
        assert code == 2 and "belongs to another session" in lines[0], (args, lines)


def test_start_prunes_what_is_older_than_fourteen_days(runs):
    script = load_script()
    old = runs.started(session="session-old1")
    runs.run("close", "--why", "x", session="session-old1")
    lines = runs.log_lines(old)
    lines[-1]["t"] = script.iso(script.now() - script.datetime.timedelta(days=15))
    (old / "log.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    recent = runs.started(session="session-new1")
    runs.run("close", "--why", "x", session="session-new1")
    runs.started(session="session-new2")
    assert not old.exists() and recent.exists()


# ── which command would prompt ───────────────────────────────────────────────

# The rows of the table under "Wildcard patterns" on the permissions page of the Claude Code docs, read 2026-11-17.
DOCS_ROWS = [
    ("npm run build", ["npm run build"], ["npm run build --watch"]),
    ("npm run *", ["npm run build", "npm run test --watch", "npm run"], ["npm install"]),
    ("git log * main", ["git log --oneline main", "git log -5 main", "git log --output=<file> main"],
     ["git log main", "git push origin main"]),
    ("git * main", ["git merge main", "git push origin main", "git -c core.fsmonitor=<script> diff main"], ["git log"]),
    ("* --version", ["node --version", "bash -c 'echo hi' --version"], ["node -v"]),
    ("ls *", ["ls -la", "ls"], ["lsof"]),
    ("ls*", ["ls -la", "lsof"], []),
    ("* --help *", ["npm --help x"], ["npm --help"]),
    ("ls:*", ["ls -la", "ls"], ["lsof"]),
    ("git:* push", [], ["git push", "git x push"]),
]


@pytest.mark.parametrize("rule, matches, misses", DOCS_ROWS)
def test_a_rule_matches_as_the_table_of_the_docs_says(rule, matches, misses):
    script = load_script()
    for command in matches:
        assert script.rule_matches(rule, command), (rule, command)
    for command in misses:
        assert not script.rule_matches(rule, command), (rule, command)


def reference_match(rule, command):
    """A second version, with no regular expression: the three rules of the docs, letter by letter."""
    if rule.endswith(":*"):
        rule = rule[:-2] + " *"
    if "*" not in rule:
        return rule == command
    if rule.count("*") == 1 and rule.endswith(" *") and command == rule[:-2]:
        return True

    def walk(r, c):
        if not r:
            return not c
        if r[0] == "*":
            return any(walk(r[1:], c[at:]) for at in range(len(c) + 1))
        return bool(c) and r[0] == c[0] and walk(r[1:], c[1:])
    return walk(rule, command)


def test_the_matcher_agrees_with_a_second_version_on_random_rules_and_commands():
    script = load_script()
    rng = random.Random(20261117)
    matched = 0
    for _ in range(40000):
        rule = "".join(rng.choice("ab *:.\n") for _ in range(rng.randint(0, 7)))
        command = "".join(rng.choice("ab *:.\n") for _ in range(rng.randint(0, 7)))
        expected = reference_match(rule, command)
        matched += expected
        assert script.rule_matches(rule, command) == expected, (rule, command)
    assert 1000 < matched < 39000, "the random cases must hold both answers"


def settings(runs, where, **permissions):
    path = {"user": runs.home / ".claude" / "settings.json", "project": runs.cwd / ".claude" / "settings.json",
            "local": runs.cwd / ".claude" / "settings.local.json"}[where]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": permissions, "env": {"TOKEN": "never printed"}}))


def test_preflight_names_each_command_that_would_prompt(runs):
    settings(runs, "user", allow=["Bash(python3 -m pytest *)", "Bash(git status)"], deny=["Bash(git push --force *)"])
    settings(runs, "project", allow=["Bash(git push *)"], ask=["Bash(gh pr merge *)"])
    settings(runs, "local", allow=["Bash(make:*)"])
    commands = ["python3 -m pytest tests", "git push origin work", "git push --force origin work", "gh pr merge 7 --squash",
                "make test", "npm test", "git status && git push origin work", "git status"]
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=commands), "--project", str(runs.cwd))
    assert code == 1 and lines[-1] == "unattended: preflight allowed=4 would-prompt=2 unsure=1 denied=1", lines
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["allowed", "allowed", "denied", "would-prompt", "allowed",
                                                             "would-prompt", "unsure", "allowed"]
    assert lines[1] == "allowed git push origin work [allow: git push *]"
    assert lines[2] == "denied git push --force origin work [deny: git push --force *]", "a deny rule wins over an allow rule"
    assert lines[3] == "would-prompt gh pr merge 7 --squash [ask: gh pr merge *]"
    assert "never printed" not in "\n".join(lines)


def test_a_deny_or_ask_rule_with_a_colon_is_read_in_its_wider_sense(runs):
    """The docs say that `rm:*` is `rm *`. An older reading takes it as a plain prefix. For a rule that stops a
    command, the script takes whichever reading stops more; for a rule that allows one, the reading of the docs."""
    settings(runs, "user", allow=["Bash(ls:*)", "Bash(rmdir build)", "Bash(ghost run)"], deny=["Bash(rm:*)"], ask=["Bash(gh:*)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["ls -la", "lsof", "rm -rf x", "rmdir build", "ghost run", "gh pr list"]))
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["allowed", "would-prompt", "denied", "denied", "would-prompt",
                                                             "would-prompt"], lines
    assert lines[3] == "denied rmdir build [deny: rm:*]"


@pytest.mark.parametrize("deny, ask, command, verdict", [
    (["Bash( rm -rf *)"], [], "rm -rf build", "denied"),            # a stray space in the rule
    (["Bash(git  push *)"], [], "git push origin main", "denied"),
    (["Bash(git push *)"], [], "git  push origin main", "denied"),  # two spaces in the command
    (["Bash(git push *)"], [], " git push origin main", "denied"),
    (["Bash(git push *)"], [], "git\tpush origin main", "denied"),
    ([], ["Bash(git push *)"], "git  push origin main", "would-prompt"),
    ([], ["Bash(  gh pr merge * )"], "gh pr merge 7", "would-prompt"),
    (["Bash(rm *)"], [], "FOO=1 rm -rf build", "denied"),            # behind a variable: the docs say a deny rule holds
    (["Bash(rm *)"], [], "FOO=1 BAR=2 rm -rf build", "denied"),
    (["Bash(rm *)"], [], "timeout 30 rm -rf build", "denied"),      # behind a wrapper that Claude Code strips
    (["Bash(git push *)"], [], "nice -n 5 git push origin", "denied"),
    (["Bash(rm *)"], [], "/bin/rm -rf build", "denied"),            # the program written with its path
    (["Bash(rm:*)"], [], "xargs rm", "denied"),
    ([], ["Bash(git push *)"], "GIT_TRACE=1 git push origin", "would-prompt"),
    (["Bash(rm:*)"], [], "env -Srm -rf build", "denied"),            # a command as the value of an option: Claude Code
    (["Bash(rm *)"], [], "env -iSrm -rf build", "denied"),           # takes it out of `env -S`, `flock -c`, `script -c`
    (["Bash(reboot:*)"], [], "env --split-string=reboot", "denied"),
    (["Bash(reboot:*)"], [], "flock --command=reboot /tmp/l", "denied"),
    (["Bash(reboot:*)"], [], "flock -creboot /tmp/l", "denied"),
    (["Bash(reboot:*)"], [], "script --command=/sbin/reboot /dev/null", "denied"),
    (["Bash(reboot)"], [], "flock -c reboot /tmp/l", "denied"),      # a rule for one exact command, and words after it
    (["Bash(k=v *)"], [], "run WITH=k=v now", "denied"),             # a name that holds an equals sign itself
    ([], ["Bash(reboot)"], "flock -c reboot /tmp/l", "would-prompt"),
    ([], ["Bash(reboot)"], "flock --command=reboot /tmp/l", "would-prompt"),
    (["Bash"], [], "git status", "denied"),                         # the whole tool
    ([], ["Bash"], "git status", "would-prompt"),
])
def test_a_rule_that_stops_a_command_is_read_with_its_spaces_made_even(runs, deny, ask, command, verdict):
    """Found by the adversary: Claude Code makes each run of spaces one space, in the rule and in the command,
    before it matches. Read letter for letter, a deny rule missed the command and a wide allow rule passed it."""
    settings(runs, "user", allow=["Bash", "Bash(*)", "Bash(git *)"], deny=deny, ask=ask)
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[command]))
    assert code == 1 and lines[0].split(" ", 1)[0] == verdict, lines


def test_a_rule_that_stops_counts_from_each_of_the_three_settings_files(runs):
    settings(runs, "user", allow=["Bash"])
    settings(runs, "project", allow=["Bash"], ask=["Bash(make deploy)"])
    settings(runs, "local", allow=["Bash"], deny=["Bash(git push *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin x", "make deploy", "make test"]),
                           "--project", str(runs.cwd))
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["denied", "would-prompt", "allowed"], lines


@pytest.mark.parametrize("allow, command", [
    ("Bash(git * push:*)", "git -C repo push origin main"),   # Claude Code reads a `:*` beside another `*` as plain text
    ("Bash(echo \\*)", "echo \\hello"),                       # `\*` is a star as a letter there
    ("Bash(git push *)", "git  push origin main"),            # allow rules stay letter for letter
    ("Bash( git push *)", "git push origin main"),
    ("Bash(rm *)", "\\rm -rf x"),
])
def test_an_allow_rule_in_a_form_that_the_script_does_not_know_for_sure_allows_nothing(runs, allow, command):
    settings(runs, "user", allow=[allow])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[command]))
    assert code == 1 and lines[0].split(" ", 1)[0] in ("would-prompt", "unsure"), lines


def test_preflight_prints_the_command_with_the_spaces_that_were_judged(runs):
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git  push   origin"]))
    assert lines[0].startswith("would-prompt git  push   origin [")


def test_preflight_passes_only_when_every_command_is_allowed(runs):
    settings(runs, "user", allow=["Bash"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin work", "anything at all"]))
    assert (code, lines[-1]) == (0, "unattended: preflight allowed=2 would-prompt=0 unsure=0 denied=0")
    assert runs.run("preflight", "--plan", runs.plan(commands=[])) == (0, ["unattended: preflight allowed=0 would-prompt=0 unsure=0 denied=0"])


def test_without_the_project_only_the_settings_of_the_user_count(runs):
    settings(runs, "project", allow=["Bash(git push *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin work"]))
    assert code == 1 and lines[0].startswith("would-prompt git push origin work")


@pytest.mark.parametrize("command", ["a && b", "a || b", "a; b", "a | b", "a & b", "a\nb", "echo $(b)", "echo `b`", "a > f", "a < f",
                                     "a\x00b", "a\x01b", "a\x1fb", "a\x7fb", "a\x85b", "a\u2028b", "a\u200bb", "a\rb", "a\\b"])
def test_a_command_with_a_shell_operator_is_never_called_allowed(runs, command):
    script = load_script()
    everything = [{"permissions": {"allow": ["Bash(*)", "Bash"]}}]
    assert script.judge("a", everything)[0] == "allowed"
    assert script.judge(command, everything)[0] == "unsure"


@pytest.mark.parametrize("command", [
    "'rm' -rf build", '"rm" -rf build', "r''m -rf build",       # a quote hides the name of the program
    "(rm -rf build)", "{rm,-rf,build}", "=rm -rf build",        # a subshell, a brace list, the `=name` of zsh
    "echo $HOME", "echo ${IFS}rm", "rm build/*", "ls ?", "ls [ab]", "echo hi #x", "!rm", "^a^b",
    "git commit -m 'a message'", "echo caf\u00e9", "echo a\u00a0b", "echo a\u3000b",
])
def test_a_command_that_is_not_plain_words_is_never_called_allowed(runs, command):
    """The script does not parse a shell command, so it reads only what cannot be read in two ways: words of
    letters, digits and `_ @ % + = : , . / ~ -`. A list of the marks that are not read was open at one end."""
    settings(runs, "user", allow=["Bash", "Bash(*)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[command]))
    assert code == 1 and lines[0].split(" ", 1)[0] == "unsure", lines
    assert "not plain words" in lines[0]


OUTSIDE = list("'\"$*(){}[];&|<>\\`#!?^") + ["\n", "\r", "\x00", "\x0b", "\x7f", "\x85", "\u00a0", "\u2028", "\u3000", "\u00e9", "\uff52"]


@pytest.mark.parametrize("mark", OUTSIDE)
def test_each_mark_outside_plain_words_makes_a_command_unsure_wherever_it_stands(mark):
    script = load_script()
    everything = [{"permissions": {"allow": ["Bash(*)", "Bash"]}}]
    for command in (f"{mark}a b", f"a{mark}b c", f"a b{mark}", f"a {mark} b", f"a {mark}", mark):
        assert script.judge(command, everything)[0] == "unsure", command
    assert script.judge("=a b", everything)[0] == "unsure" and script.judge("a =b", everything)[0] == "unsure"
    assert script.judge("a=b c=d", everything)[0] == "allowed", "an equals sign inside a word is plain"


def plain_by_hand(command):
    """A second version of `plain`, with no regular expression: one pass, letter by letter."""
    inside = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./~-"
    at_start = True
    for ch in command:
        if ch in " \t":
            at_start = True
            continue
        if ch not in inside or (at_start and ch == "="):
            return False
        at_start = False
    return True


def test_plain_words_against_a_second_version_on_random_commands():
    script = load_script()
    rng = random.Random(20261117)
    letters = list("ab0_@%+=:,./~- \t") * 6 + OUTSIDE
    yes = 0
    for _ in range(40000):
        command = "".join(rng.choice(letters) for _ in range(rng.randint(0, 9)))
        expected = plain_by_hand(command)
        yes += expected
        assert bool(script.plain(command)) == expected, repr(command)
    assert 4000 < yes < 36000, "the random cases must hold both answers"


def test_no_random_command_that_runs_a_denied_program_is_called_allowed():
    """A model of what Claude Code stops: a deny rule for `rm` holds when any word of the command, with its
    quotes, brackets and path taken away, is `rm`. Whatever the script calls `allowed` must be free of that."""
    script = load_script()
    rules = [{"permissions": {"allow": ["Bash", "Bash(*)"], "deny": ["Bash(rm *)"]}}]
    rng = random.Random(20261117)
    parts = ["rm", "/bin/rm", "./rm", "'rm'", '"rm"', "(rm", "rm)", "{rm,", "=rm", "r''m", "\\rm", "FOO=1", "timeout", "30", "echo", "-rf",
             "build", "rmdir", "arm", "rm.txt", ";", "&&", "|", "$(rm", "`rm`", "-Srm", "-iSrm", "--command=rm", "--run=/bin/rm", "X=rm",
             "env", "-S", "-c", "--", "-arm"]
    gaps = [" ", "  ", "\t", "\n", ""]
    seen = dict.fromkeys(("allowed", "denied", "unsure"), 0)
    for _ in range(20000):
        command = "".join(rng.choice(parts) + rng.choice(gaps) for _ in range(rng.randint(1, 5)))
        verdict = script.judge(command, rules)[0]
        seen[verdict] += 1
        if verdict == "allowed":
            for word in command.split():
                word = re.sub(r"""['"(){},`$\\]""", "", word)
                tails = [word] + [word.split("=", cut)[-1] for cut in range(1, word.count("=") + 1)]
                names = {tail.rsplit("/", 1)[-1] for tail in tails}
                assert "rm" not in names and not (word.startswith("-") and word.endswith("rm")), repr(command)
    assert all(count > 500 for count in seen.values()), seen


@pytest.mark.parametrize("command", [
    "git push origin work", "python3 -m pytest tests/test_a.py -q -p no:cacheprovider", "FOO=1 make test",
    "gh pr merge 7 --squash --match-head-commit 0a1b2c3", "python3 ~/.claude/skills/x/run.py --at=1,2 a@b 50%+1",
    "make\ttest", "make  test ",
])
def test_a_command_of_plain_words_can_be_allowed(runs, command):
    settings(runs, "user", allow=["Bash"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[command]))
    assert code == 0 and lines[0].split(" ", 1)[0] == "allowed", lines


def test_a_rule_that_stops_is_read_before_the_command_is_called_unsure(runs):
    settings(runs, "user", allow=["Bash"], deny=["Bash(git push *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin 'work'", "git status && git push origin work"]))
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["denied", "denied"], lines


@pytest.mark.parametrize("command", [
    "git status\nrm -rf build",                  # a line break is a command separator
    "git status\r\nrm -rf build",
    "git status\x0brm -rf build",                # a control character
    "git status " + "x" * 3000 + " && rm -rf build",   # an operator behind the point where a text is cut for printing
    "git status " + "x" * 1990 + "; rm -rf build",
])
def test_preflight_judges_the_command_as_it_is_written_and_not_as_it_is_printed(runs, command):
    """Found in review: the command was made one line and cut to 2,000 characters before it was judged, so a rule
    for the first command of several covered them all."""
    settings(runs, "user", allow=["Bash(git status *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[command]))
    assert code == 1 and lines[-1] == "unattended: preflight allowed=0 would-prompt=0 unsure=1 denied=0", lines[-1]
    assert len(lines) == 2 and lines[0].startswith("unsure git status ")


def test_a_command_is_matched_letter_for_letter(runs):
    settings(runs, "user", allow=["Bash(git push *)", "Bash(echo *)"])
    commands = ["git  push origin work", " git push origin work", "git push origin work", "echo " + "x" * 1995,
                "echo " + "x" * 1996, "echo " + "x" * 3000]
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=commands))
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["would-prompt", "would-prompt", "allowed", "allowed", "unsure",
                                                             "unsure"], lines
    assert lines[4].endswith("… [it is longer than 2,000 characters]") and lines[5].endswith("… [it is longer than 2,000 characters]")
    assert lines[3].endswith("x… [allow: echo *]"), "a command of 2,000 characters is still judged, and its reason is printed"
    assert all(len(line) <= 2000 for line in lines), "a long command is cut when it is printed"


def test_a_rule_with_many_wildcards_does_not_make_preflight_slow(runs):
    """Found by the adversary: a long command against a rule with several `*` took 22 seconds. The matcher now
    goes over the command one time for each part of the rule."""
    stars = "Bash(a" + " *" * 14 + " b)"
    settings(runs, "user", allow=[stars], deny=[stars], ask=[stars])
    commands = [("a " * 999) + "aa", ("-a" * 999) + " c", "a " + "-a " * 600 + "c"]
    began = time.monotonic()
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=commands))
    assert time.monotonic() - began < 20
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["would-prompt"] * 3, [line[:40] for line in lines]


def test_settings_that_cannot_be_read_and_a_plan_with_no_commands_are_errors(runs):
    (runs.home / ".claude").mkdir()
    (runs.home / ".claude" / "settings.json").write_text("{broken")
    code, lines = runs.run("preflight", "--plan", runs.plan())
    assert code == 2 and "cannot read" in lines[0] and "settings.json" in lines[0]
    (runs.home / ".claude" / "settings.json").unlink()
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=...))
    assert code == 2 and "the plan has no list `commands`" in lines[0]


def test_a_fault_of_the_script_itself_is_an_error_line_and_no_traceback(runs, monkeypatch, capsys):
    script = load_script()

    def broken(args):
        raise RuntimeError("a fault\nwith two lines")
    monkeypatch.setattr(script, "status", broken)
    assert script.main(["status"]) == 2
    assert capsys.readouterr().out == "unattended: error RuntimeError: a fault with two lines\n"


# ── the command line ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [(), ("bogus",), ("--help",), ("start",), ("start", "--pla", "x"), ("status", "extra"),
                                  ("start", "--plan", "/nonexistent/plan.json")])
def test_a_wrong_command_line_is_one_error_line(runs, args):
    code, lines = runs.run(*args)
    assert code == 2 and len(lines) == 1 and lines[0].startswith("unattended: error "), lines
