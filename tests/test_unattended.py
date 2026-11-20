"""Tests for the contract script of the unattended skill.

    python3 -m pytest tests/test_unattended.py -q -p no:cacheprovider

Each test gets its own folder for the runs (UNATTENDED_ROOT) and its own home, so no test reads or writes the
real ~/.claude.
"""
import contextlib
import importlib.util
import io
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

    def hook_text(self, event, text, session=None):
        """What the hook prints for this text on its input. A hook gets no session in its environment here: the
        id comes with the event. It never fails and never writes an error."""
        done = subprocess.run([PY, "-I", str(SCRIPT), "hook", "--event", event], cwd=self.cwd, env=self.env(session),
                              input=text, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0 and done.stderr == "", (done.returncode, done.stderr)
        return done.stdout

    def hook(self, event, session=SESSION, **fields):
        """The answer of the hook to one event, or None when it leaves the matter to Claude Code."""
        name = {"stop": "Stop", "permission": "PermissionRequest"}[event]
        out = self.hook_text(event, json.dumps({"session_id": session, "hook_event_name": name, **fields}))
        if not out:
            return None
        assert out.endswith("\n") and out.count("\n") == 1 and out.isascii(), out
        return json.loads(out)

    def stop(self, session=SESSION, **fields):
        """The reason with which the Stop hook refuses the stop, or None when the session may stop."""
        answer = self.hook("stop", session=session, **{"stop_hook_active": False, "background_tasks": [],
                                                       "session_crons": [], **fields})
        if answer is None:
            return None
        assert set(answer) == {"decision", "reason"} and answer["decision"] == "block", answer
        return answer["reason"]

    def asked(self, tool="Bash", session=SESSION, **tool_input):
        """The message with which the PermissionRequest hook denies the prompt, or None when the prompt stays."""
        answer = self.hook("permission", session=session, tool_name=tool, tool_input=tool_input)
        if answer is None:
            return None
        assert answer == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
            "behavior": "deny", "message": answer["hookSpecificOutput"]["decision"]["message"]}}}, answer
        return answer["hookSpecificOutput"]["decision"]["message"]

    def kinds(self, folder):
        return [line["kind"] for line in self.log_lines(folder)]

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
    assert f"1 heartbeat lines, 0 stops refused, 0 prompts denied. Log: {folder / 'log.jsonl'}" in found
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


# ── two commands at one time ─────────────────────────────────────────────────
#
# Command X runs in this process. Before its call number k to the disk, command Y runs to its end, as a second
# session does between two steps of X. Each case is run one time for each k. The result must be one that the two
# commands can give one after the other: no error, no line that is lost, no run that is seen half.
#
# `shutil.rmtree` is one call for X, but not one step on the disk. So X gets a version of it that removes a folder
# of plain files one file at a time, the log first, with Y possible before each file. What this does not show:
# two commands that each stop in the middle (Y always runs to its end), and three commands.

class Watch:
    """Counts the calls of X to the disk, and runs `between` one time before call number `at`. With `each`, that
    is run before every call, with the name of the call and what it was given."""
    NAMES = {"os": {"lstat", "mkdir", "listdir", "open", "replace", "rename", "rmdir", "write"},
             "path": {"lexists"}, "shutil": {"rmtree"}, "fcntl": {"flock"}}

    def __init__(self, at, between, each=None):
        self.at, self.between, self.each, self.calls, self.armed = at, between, each, 0, False

    def step(self, name=None, args=()):
        if self.armed:
            self.calls += 1
            self.armed = False
            try:
                if self.each is not None:
                    self.each(name, args)
                if self.calls == self.at:
                    self.between()
            finally:
                self.armed = True

    def in_steps(self, folder):
        names = sorted(os.listdir(folder), reverse=True)
        if os.path.islink(folder) or any(os.path.isdir(os.path.join(folder, name)) for name in names):
            self.step()
            return shutil.rmtree(folder)
        for name in names:
            self.step()
            os.unlink(os.path.join(folder, name))
        self.step()
        os.rmdir(folder)

    def module(self, real, kind):
        watch = self

        class Stepped:
            def __getattr__(self, name):
                value = getattr(real, name)
                if kind == "os" and name == "path":
                    return watch.module(value, "path")
                if name not in watch.NAMES[kind]:
                    return value
                if name == "rmtree":
                    return watch.in_steps

                def call(*args, **kwargs):
                    watch.step(name, args)
                    return value(*args, **kwargs)
                return call
        return Stepped()


class Twice:
    """The runs of one test folder, with each command run inside this process."""
    A, B, C, D = "session-aaaa", "session-bbbb", "session-cccc", "session-dddd"

    def __init__(self, where, monkeypatch):
        where.mkdir()
        self.root = where / "runs"
        (where / "home").mkdir()
        monkeypatch.setenv("HOME", str(where / "home"))
        monkeypatch.setenv("UNATTENDED_ROOT", str(self.root))
        self.monkeypatch = monkeypatch
        self.plan = where / "plan.json"
        self.plan.write_text(json.dumps({"tasks": [{"title": "read", "check": "it reads"}], "approvals": []}))
        self.script = load_script()
        self.script.LOCK_WAIT_S = 0.2       # X stands still while Y runs, so Y can wait for a lock of X in vain

    def call(self, session, *args, script=None):
        """The command for this session. A hook gets its event on the input: a stop with no wake-up, or the
        prompt for `rm -rf build`."""
        args = [str(self.plan) if arg == "PLAN" else arg for arg in args]
        event = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
        out = io.StringIO()
        with self.monkeypatch.context() as patch, contextlib.redirect_stdout(out):
            patch.setenv("CLAUDE_CODE_SESSION_ID", session)
            patch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(event).encode())))
            code = (script or self.script).main(args)
        return code, out.getvalue().splitlines()

    def must(self, session, *args):
        code, lines = self.call(session, *args)
        assert code == 0, lines
        return lines

    def open_run(self, session):
        return Path(self.must(session, "start", "--plan", "PLAN")[0].removeprefix("run folder: "))

    def closed_run(self, session, name="20200101T000000Z"):
        """A closed run of the session, under the name of a start time long ago: the session can start again."""
        folder = self.open_run(session)
        self.must(session, "close", "--why", "x")
        return folder.rename(folder.with_name(name))

    def watched(self, session, *args, each):
        """The command, with `each` run before every call that it makes to the disk."""
        watch = Watch(None, None, each)
        script = load_script()
        script.os, script.shutil, script.fcntl = (watch.module(real, kind) for real, kind in
                                                  ((os, "os"), (shutil, "shutil"), (script.fcntl, "fcntl")))
        watch.armed = True
        try:
            return self.call(session, *args, script=script)
        finally:
            watch.armed = False

    def kinds(self, folder):
        return [json.loads(line)["kind"] for line in (folder / "log.jsonl").read_text().splitlines()]

    def names(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_dir())


START, BEAT, PRUNE = ("start", "--plan", "PLAN"), ("log", "--kind", "heartbeat", "--text", "at work"), ("prune", "--days", "0")
CLOSE = ("close", "--why", "x")
STOP, ASKED = ("hook", "--event", "stop"), ("hook", "--event", "permission")


def no_run_yet(world):
    return {}


def an_old_run_and_an_open_run(world):
    return {"old": world.closed_run(world.A), "open": world.open_run(world.A)}


def an_old_run_only(world):
    return {"old": world.closed_run(world.A)}


def an_open_run_only(world):
    return {"open": world.open_run(world.A)}


def a_late_run_that_was_never_closed(world):
    folder = world.open_run(world.A)
    contract = json.loads((folder / "contract.json").read_text())
    contract["ends"] = world.script.iso(world.script.now() - world.script.datetime.timedelta(days=20))
    (folder / "contract.json").write_text(json.dumps(contract))
    return {"open": folder}


def both_start(world, made, x, y):
    assert x[0] == 0 and y[0] == 0, (x, y)
    for session in (world.A, world.B):
        assert world.call(session, "status")[1][-1].startswith("unattended: open left=1"), session


def the_heartbeat_is_written_and_the_old_run_is_gone(world, made, beat, prune):
    assert beat == (0, ["unattended: logged heartbeat"]) and prune == (0, ["unattended: pruned 1"]), (beat, prune)
    assert world.kinds(made["open"]) == ["started", "heartbeat"] and not made["old"].exists()


def the_stop_is_refused_and_the_old_run_is_gone(world, made, stop, prune):
    assert stop[0] == 0 and json.loads(stop[1][0])["decision"] == "block", stop
    assert prune == (0, ["unattended: pruned 1"]) and not made["old"].exists()
    assert world.kinds(made["open"]) == ["started", "refused"]


def the_open_run_is_seen(world, made, status, prune):
    assert status[0] == 0 and status[1][-1].startswith("unattended: open left=1"), status
    assert prune == (0, ["unattended: pruned 1"]) and not made["old"].exists()


def no_run_is_seen(world, made, status, prune):
    assert status == (1, ["unattended: none"]) and prune == (0, ["unattended: pruned 1"]), (status, prune)
    assert world.names() == [], "the run is gone whole, and so is the folder that it left empty"


def the_new_run_is_open(world, made, start, prune):
    assert start[0] == 0 and prune == (0, ["unattended: pruned 1"]), (start, prune)
    assert world.call(world.A, "status")[1][-1].startswith("unattended: open left=1") and not made["old"].exists()


def the_close_is_written(world, made, close, prune):
    assert close[0] == 0 and close[1][-1] == "unattended: closed done=0 skipped=0 left=1", close
    assert prune == (0, ["unattended: pruned 0"]), "the run was open, or its close held it"
    assert world.kinds(made["open"]) == ["started", "closed"]


def the_run_is_closed_and_kept_or_closed_and_removed(world, made, prune, close):
    assert close[0] == 0 and close[1][-1] == "unattended: closed done=0 skipped=0 left=1", close
    assert prune in ((0, ["unattended: pruned 0"]), (0, ["unattended: pruned 1"])), prune
    if made["open"].exists():
        assert prune[1] == ["unattended: pruned 0"] and world.kinds(made["open"]) == ["started", "closed"]
    else:
        assert prune[1] == ["unattended: pruned 1"] and world.names() == []


def the_late_run_is_closed_and_kept_or_removed_before_its_close(world, made, prune, close):
    if made["open"].exists():
        assert prune == (0, ["unattended: pruned 0"]) and close[0] == 0, (prune, close)
        assert world.kinds(made["open"]) == ["started", "closed"], "a run that is closed now is kept for its report"
    else:
        assert prune == (0, ["unattended: pruned 1"]) and world.names() == [], prune
        assert close[0] == 2 and close[1][0].endswith(("this session has no open run",       # the prune was first
                                                       "holds the run for longer than 0.2 s")), close


def the_hook_writes_before_the_close_or_not_at_all(kind):
    def check(world, made, hook, close):
        assert hook[0] == 0, hook
        kinds = world.kinds(made["open"])
        if close[0] == 2:           # X is the hook and stands still with the run in its hand: the close gave up
            assert close[1][0].endswith("holds the run for longer than 0.2 s") and kinds == ["started", kind], (close, kinds)
        else:
            assert close[1][-1] == "unattended: closed done=0 skipped=0 left=1", close
            assert kinds in (["started", kind, "closed"], ["started", "closed"]), "no line of a hook comes after the close"
        if kind == "refused":
            assert bool(hook[1]) == (kind in kinds), "a stop is refused only with its line in the log"
        elif kind in kinds:
            assert hook[1], "a prompt whose line is in the log was denied"
        for line in hook[1]:
            json.loads(line)
    return check


def one_of_the_two_removes_it(world, made, first, second):
    assert first[0] == 0 and second[0] == 0, (first, second)
    assert sorted(first[1] + second[1]) == ["unattended: pruned 0", "unattended: pruned 1"], (first, second)
    assert world.names() == []


def flip(check):
    return lambda world, made, x, y: check(world, made, y, x)


AT_ONE_TIME = {
    "two sessions start, and no folder for the runs is there yet":
        (no_run_yet, ("A", *START), ("B", *START), both_start),
    "a heartbeat, and another session prunes an older run of this session":
        (an_old_run_and_an_open_run, ("A", *BEAT), ("C", *PRUNE), the_heartbeat_is_written_and_the_old_run_is_gone),
    "a prune, and a session with an older run writes a heartbeat":
        (an_old_run_and_an_open_run, ("C", *PRUNE), ("A", *BEAT), flip(the_heartbeat_is_written_and_the_old_run_is_gone)),
    "status, and another session prunes an older run of this session":
        (an_old_run_and_an_open_run, ("A", "status"), ("C", *PRUNE), the_open_run_is_seen),
    "status, and another session prunes the only run of this session":
        (an_old_run_only, ("A", "status"), ("C", *PRUNE), no_run_is_seen),
    "a prune of the only run of a session, and that session asks its status":
        (an_old_run_only, ("C", *PRUNE), ("A", "status"), flip(no_run_is_seen)),
    "a start, and another session prunes the last run of this session":
        (an_old_run_only, ("A", *START), ("C", *PRUNE), the_new_run_is_open),
    "a prune of the last run of a session, and that session starts":
        (an_old_run_only, ("C", *PRUNE), ("A", *START), flip(the_new_run_is_open)),
    "a close, and another session prunes":
        (an_open_run_only, ("A", *CLOSE), ("C", *PRUNE), the_close_is_written),
    "a prune, and a session closes its run":
        (an_open_run_only, ("C", *PRUNE), ("A", *CLOSE), the_run_is_closed_and_kept_or_closed_and_removed),
    "a stop of a session, and the close of its run":
        (an_open_run_only, ("A", *STOP), ("A", *CLOSE), the_hook_writes_before_the_close_or_not_at_all("refused")),
    "the close of a run, and a stop of its session":
        (an_open_run_only, ("A", *CLOSE), ("A", *STOP), flip(the_hook_writes_before_the_close_or_not_at_all("refused"))),
    "a prompt of a session, and the close of its run":
        (an_open_run_only, ("A", *ASKED), ("A", *CLOSE), the_hook_writes_before_the_close_or_not_at_all("denied")),
    "the close of a run, and a prompt of its session":
        (an_open_run_only, ("A", *CLOSE), ("A", *ASKED), flip(the_hook_writes_before_the_close_or_not_at_all("denied"))),
    "a stop of a session, and a prune by another session":
        (an_old_run_and_an_open_run, ("A", *STOP), ("C", *PRUNE), the_stop_is_refused_and_the_old_run_is_gone),
    "two sessions prune":
        (an_old_run_only, ("C", *PRUNE), ("D", *PRUNE), one_of_the_two_removes_it),
    "a prune, and the close of a run that is 20 days late":
        (a_late_run_that_was_never_closed, ("C", "prune"), ("A", *CLOSE),
         the_late_run_is_closed_and_kept_or_removed_before_its_close),
    "the close of a run that is 20 days late, and a prune":
        (a_late_run_that_was_never_closed, ("A", *CLOSE), ("C", "prune"),
         flip(the_late_run_is_closed_and_kept_or_removed_before_its_close)),
}


@pytest.mark.parametrize("case", AT_ONE_TIME)
def test_two_commands_at_one_time_give_what_they_give_one_after_the_other(case, tmp_path, monkeypatch):
    """Found by a second adversary (GPT-6.2 Astra): two first starts ended with `File exists`, and a prune of
    another run of the session made `status` and `log` end with exit 2, so a heartbeat was lost."""
    setup, x, y, check = AT_ONE_TIME[case]

    def one(at, where):
        world = Twice(where, monkeypatch)
        made, got = setup(world), []
        watch = Watch(at, lambda: got.append(world.call(getattr(world, y[0]), *y[1:])))
        script = load_script()
        script.os, script.shutil, script.fcntl = (watch.module(real, kind) for real, kind in
                                                  ((os, "os"), (shutil, "shutil"), (script.fcntl, "fcntl")))
        watch.armed = True
        try:
            result = world.call(getattr(world, x[0]), *x[1:], script=script)
        finally:
            watch.armed = False
        return world, made, watch.calls, result, got

    calls = one(None, tmp_path / "alone")[2]
    assert calls >= 3, "X made no call that the watch counts"
    for at in range(1, calls + 1):
        world, made, _, result, got = one(at, tmp_path / f"at{at}")
        assert len(got) == 1, f"Y did not run before call {at}"
        try:
            check(world, made, result, got[0])
        except AssertionError as fault:
            raise AssertionError(f"with Y before call {at} of {calls} of X: {fault}") from None


@pytest.mark.parametrize("command", [("status",), BEAT])
def test_a_prune_that_takes_run_after_run_of_a_session_ends_no_command_of_that_session(command, tmp_path, monkeypatch):
    """Found by the adversary: one Y that runs to its end is not the worst case. A prune of 100 old runs took one
    more run between each two looks of a `status`, and after five tries the `status` gave up (2 of 96 calls)."""
    world = Twice(tmp_path / "many", monkeypatch)
    old = [world.closed_run(world.A, f"2020010{number}T000000Z") for number in range(1, 9)]
    newest = world.open_run(world.A)

    def take_the_run_that_is_looked_at(name, args):
        folder = Path(args[0]) if name == "lstat" else None
        if folder in old and folder.exists():
            shutil.rmtree(folder.rename(folder.with_name(folder.name + ".gone")))
    code, lines = world.watched(world.A, *command, each=take_the_run_that_is_looked_at)
    assert code == 0, lines
    assert not any(folder.exists() for folder in old), "each old run was taken at the moment when it was looked at"
    assert world.kinds(newest)[-1] == ("heartbeat" if command == BEAT else "started")


def test_a_run_that_loses_its_name_and_gets_it_back_while_it_is_read_is_read_whole(tmp_path, monkeypatch):
    """Found by the adversary, by reading: a prune that can remove no file of a run gives the folder its name
    back. A reader that took the log for missing in between must not take the closed run for an open one."""
    world = Twice(tmp_path / "back", monkeypatch)
    folder = world.closed_run(world.A)
    away = folder.with_name(folder.name + ".gone")
    moves = []

    def lose_the_name_after_the_contract_and_get_it_back_after_the_log(name, args):
        target = str(args[0]) if name in ("open", "lexists") else ""
        if name == "open" and target.endswith("contract.json") and not moves:
            moves.append("the contract is opened")
        elif moves == ["the contract is opened"]:             # the next call to the disk, whatever it is
            moves.append(folder.rename(away))
        elif name == "lexists" and target.endswith("contract.json") and away.exists():
            moves.append(away.rename(folder))
    assert world.watched(world.A, "status", each=lose_the_name_after_the_contract_and_get_it_back_after_the_log) == \
        (1, ["unattended: none"])
    assert len(moves) == 3 and folder.exists(), "the run had another name while its log was read"


def test_six_sessions_that_start_at_one_time_each_get_their_run(runs):
    """The same as the first case above, with real processes and no chosen moment: it shows that the commands
    run so, not that no moment is wrong."""
    plan = runs.plan()
    for turn in range(4):
        shutil.rmtree(runs.root, ignore_errors=True)
        sessions = [f"session-{turn}{number}00" for number in range(6)]
        started = [subprocess.Popen([PY, "-I", str(SCRIPT), "start", "--plan", plan], cwd=runs.cwd, env=runs.env(session),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for session in sessions]
        for process in started:
            out, err = process.communicate(timeout=60)
            assert process.returncode == 0 and err == "", (out, err)
        for session in sessions:
            assert runs.run("status", session=session)[0] == 0


def test_a_link_that_is_put_in_the_place_of_the_root_while_a_start_makes_it_is_not_followed(tmp_path, monkeypatch):
    """A folder that this command made, or that another command made at the same moment, is read again."""
    def one(at, where):
        world = Twice(where, monkeypatch)
        elsewhere = where / "elsewhere"
        elsewhere.mkdir()
        watch = Watch(at, lambda: os.path.lexists(world.root) or world.root.symlink_to(elsewhere))
        script = load_script()
        script.os = watch.module(os, "os")
        watch.armed = True
        try:
            result = world.call(world.A, *START, script=script)
        finally:
            watch.armed = False
        return watch.calls, result, world.root.is_symlink(), list(elsewhere.iterdir())

    calls, result, _, _ = one(None, tmp_path / "alone")
    assert result[0] == 0 and calls >= 3
    put = 0
    for at in range(1, calls + 1):       # the link comes as long as the root is not there: also right before it is made
        _, result, is_link, written = one(at, tmp_path / f"at{at}")
        assert written == [], (at, result, written)
        if is_link:
            put += 1
            assert result[0] == 2 and "is no plain folder" in result[1][0], (at, result)
        else:
            assert result[0] == 0, (at, result)
    assert put >= 3, "the link was put before each look at the root, and between the last look and the making"


def test_a_run_of_which_a_part_was_removed_does_not_get_its_name_back(runs, monkeypatch):
    """A prune that cannot remove every file of a run: with no file gone, the run stays a run (tested above with
    a folder that cannot be written). With a file gone, it is no run any more."""
    folder = runs.started()
    runs.run("close", "--why", "x")
    script = load_script()
    monkeypatch.setenv("UNATTENDED_ROOT", str(runs.root))

    class Stuck:
        def rmtree(self, path):
            os.unlink(os.path.join(path, "log.jsonl"))
            raise PermissionError(1, "cannot remove", str(path))
    monkeypatch.setattr(script, "shutil", Stuck())
    assert script.main(["prune", "--days", "0"]) == 0
    left = folder.with_name(folder.name + ".gone")
    assert not folder.exists() and [path.name for path in left.iterdir()] == ["contract.json"]
    assert runs.run("status") == (1, ["unattended: none"])
    assert runs.run("prune", session="session-0009") == (0, ["unattended: pruned 0"])
    assert not left.exists() and not left.parent.exists()


def test_a_run_that_a_prune_left_half_removed_is_no_run_and_the_next_prune_removes_it(runs):
    """A prune that stops between its two steps leaves the run under a name that is no start time."""
    folder = runs.started()
    runs.run("close", "--why", "x")
    left = folder.rename(folder.with_name(folder.name + ".gone"))
    (left / "log.jsonl").unlink()
    assert runs.run("status") == (1, ["unattended: none"])
    assert runs.run("prune", session="session-0009") == (0, ["unattended: pruned 0"]), "it was counted when it was taken"
    assert not left.exists() and not left.parent.exists()


def test_a_folder_with_the_ending_of_a_prune_that_is_no_run_of_a_prune_stays(runs, tmp_path):
    folder = runs.started()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep\n")
    (folder.parent / "20200101T000000Z.gone").symlink_to(elsewhere)      # a link with the name of a left run
    (folder.parent / "notes.gone").mkdir()                              # no start time in front of the ending
    assert runs.run("prune", "--days", "0", session="session-0009") == (0, ["unattended: pruned 0"])
    assert (elsewhere / "keep.txt").exists() and (folder.parent / "notes.gone").is_dir()
    stuck = folder.parent / "20200202T020202Z.gone"                     # one that a prune left, and cannot remove
    stuck.mkdir()
    (stuck / "log.jsonl").write_text("")
    stuck.chmod(0o500)
    try:
        runs.run("close", "--why", "x")
        assert runs.run("prune", "--days", "0", session="session-0009") == (0, ["unattended: pruned 1"]), \
            "the run beside it is still pruned"
    finally:
        stuck.chmod(0o700)
    assert not folder.exists() and stuck.exists()


# ── the two hooks ────────────────────────────────────────────────────────────
#
# Claude Code starts `hook --event stop` when a session wants to end its turn, and `hook --event permission` when
# it is about to ask the user. The event is JSON on the input. The hook prints one line of JSON, or nothing.

def test_a_session_with_no_open_run_stops_and_is_asked_as_it_is_today(runs):
    assert runs.stop() is None and runs.asked(command="rm -rf build") is None
    folder = runs.started()
    assert runs.stop(session="session-0002") is None and runs.asked(session="session-0002", command="rm x") is None
    assert runs.run("close", "--why", "x")[0] == 0
    assert runs.stop() is None and runs.asked(command="rm -rf build") is None
    assert runs.kinds(folder) == ["started", "closed"], "and the hooks wrote nothing"


def test_a_stop_is_refused_while_tasks_are_left_and_the_reason_names_the_next_one(runs):
    folder = runs.started()
    reason = runs.stop()
    assert "3 tasks are left" in reason and "task 1 (add the parser)" in reason
    assert "pytest tests/test_parser.py passes" in reason, "the check of the task is in the reason"
    assert "do not wait" in reason and 'close --why "the user is back"' in reason
    runs.run("log", "--kind", "done", "--task", "1", "--text", "x")
    runs.run("log", "--kind", "skipped", "--task", "2", "--text", "x", "--needs", "y")
    reason = runs.stop()
    assert "1 task is left" in reason and "task 3 (write the notes)" in reason
    refused = [line for line in runs.log_lines(folder) if line["kind"] == "refused"]
    assert [(line["why"], line["task"]) for line in refused] == [("tasks", "1"), ("tasks", "3")]


def test_a_stop_is_never_refused_when_a_wake_up_is_scheduled(runs):
    folder = runs.started()
    wake = {"id": "cron-001", "schedule": "30 4 21 11 *", "recurring": False, "prompt": "check the build"}
    assert runs.stop(session_crons=[wake]) is None
    assert runs.kinds(folder) == ["started"], "nothing is logged for a stop that is a planned wait"
    assert runs.stop(session_crons=[]) is not None
    assert runs.hook("stop") is not None, "an event with no list of wake-ups has none"
    for odd in ("cron-001", {"id": "x"}, 1, None, [None], [{"id": "x"}, "y"]):
        runs.run("log", "--kind", "heartbeat", "--text", "x")       # so that the three refusals in a row never come
        assert runs.hook("stop", session_crons=odd) is not None, f"{odd!r} is no list of wake-ups"


def test_a_wait_for_a_background_task_with_no_wake_up_is_refused(runs):
    runs.started()
    task = {"id": "task-001", "type": "shell", "status": "running", "description": "tests", "command": "pytest"}
    reason = runs.stop(background_tasks=[task])
    assert "1 background task is running" in reason and "set a wake-up" in reason
    assert "2 background tasks are running" in runs.stop(background_tasks=[task, task])
    runs.run("log", "--kind", "heartbeat", "--text", "x")
    for none in ([], None, "task-001", 3):
        assert "running" not in runs.hook("stop", background_tasks=none)["reason"], none
        runs.run("log", "--kind", "heartbeat", "--text", "x")


def test_after_three_refusals_with_no_line_between_them_the_session_may_stop(runs):
    folder = runs.started()
    assert [runs.stop() is not None for _ in range(5)] == [True, True, True, False, False]
    assert runs.kinds(folder) == ["started", "refused", "refused", "refused", "released"], \
        "the hook lets go one time and says so; after that it is silent"
    runs.run("log", "--kind", "heartbeat", "--text", "at work again")
    assert [runs.stop() is not None for _ in range(4)] == [True, True, True, False], "a line of the session starts the count again"
    assert runs.kinds(folder)[-5:] == ["heartbeat", "refused", "refused", "refused", "released"]


def test_a_prompt_that_was_denied_is_no_line_between_two_refusals(runs):
    """A session that only meets denied prompts makes no progress: the count goes on."""
    folder = runs.started()
    assert runs.stop() and runs.asked(command="rm x") and runs.stop() and runs.asked(command="rm y") and runs.stop()
    assert runs.stop() is None
    assert runs.kinds(folder) == ["started", "refused", "denied", "refused", "denied", "refused", "released"]


def test_with_no_task_left_the_stop_is_refused_one_time_for_the_close(runs):
    folder = runs.started()
    for task in ("1", "2", "3"):
        runs.run("log", "--kind", "done", "--task", task, "--text", "x")
    reason = runs.stop()
    assert "No task is left" in reason and "close" in reason and "Blocked on me" in reason
    assert runs.stop() is None, "one time only"
    runs.run("log", "--kind", "heartbeat", "--text", "x")
    assert runs.stop() is None, "also after a new line of the session"
    assert [(line["kind"], line.get("why")) for line in runs.log_lines(folder)][-2:] == [("refused", "close"), ("heartbeat", None)]


def test_at_the_time_limit_the_stop_is_refused_one_time_for_the_close(runs):
    folder = runs.started(hours=0.0000001)
    time.sleep(1.1)
    reason = runs.stop()
    assert "The time limit is reached" in reason and "3 tasks are left" in reason and "close" in reason
    assert "task 1" not in reason, "no new task is named at the limit"
    assert runs.stop() is None
    assert [(line["kind"], line.get("why")) for line in runs.log_lines(folder)] == [("started", None), ("refused", "close")]


def test_the_refusals_before_the_limit_do_not_use_up_the_one_at_the_limit(runs):
    script = load_script()
    folder = runs.started()
    assert runs.stop() is not None
    contract = json.loads((folder / "contract.json").read_text())
    contract["ends"] = script.iso(script.now() - script.datetime.timedelta(minutes=1))
    (folder / "contract.json").write_text(json.dumps(contract))
    assert "The time limit is reached" in runs.stop()
    assert runs.stop() is None


def test_a_reason_is_one_line_of_bounded_length_whatever_the_plan_holds(runs):
    long = "a title with é and ☃ " + "x" * 1990
    runs.started(tasks=[{"title": long, "check": "c " * 900}])
    reason = runs.stop()
    assert len(reason) < 1500 and "\n" not in reason and "a title with é and ☃ xxx" in reason
    assert "…" in reason, "a long title and a long check are cut, and the cut is shown"


def test_a_prompt_during_an_open_run_is_denied_and_the_session_is_told_what_to_do(runs):
    folder = runs.started()
    message = runs.asked(command="git push origin work", description="push the branch")
    assert "it is denied" in message and "--kind skipped" in message and "--needs" in message
    assert 'close --why "the user is back"' in message
    assert "as its own command, in the form of the skill text" in message, "a close in another form would be denied too"
    assert len(message) < 1000
    assert runs.log_lines(folder)[-1] | {"t": ""} == {"t": "", "kind": "denied", "tool": "Bash", "text": "git push origin work"}


@pytest.mark.parametrize("tool, tool_input, text", [
    ("Edit", {"file_path": "/etc/hosts", "old_string": "a", "new_string": "b"}, "/etc/hosts"),
    ("Write", {"file_path": "/tmp/x.txt", "content": "secret text"}, "/tmp/x.txt"),
    ("AskUserQuestion", {"questions": [{"question": "Which one?"}]}, '{"questions": [{"question": "Which one?"}]}'),
    ("mcp__github__create_issue", {"title": "t"}, '{"title": "t"}'),
    ("WebFetch", {"url": "https://example.com/a", "prompt": "p"}, "https://example.com/a"),
    ("Bash", {}, "{}"),
    ("Agent", None, "null"),
])
def test_the_prompt_of_every_tool_is_denied(runs, tool, tool_input, text):
    folder = runs.started()
    answer = runs.hook("permission", tool_name=tool, tool_input=tool_input)
    assert answer["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert runs.log_lines(folder)[-1] | {"t": ""} == {"t": "", "kind": "denied", "tool": tool, "text": text}


def test_a_prompt_is_denied_after_the_time_limit_too_until_the_run_is_closed(runs):
    runs.started(hours=0.0000001)
    time.sleep(1.1)
    assert runs.asked(command="rm x") is not None
    assert runs.run("close")[0] == 0
    assert runs.asked(command="rm x") is None


def test_the_same_prompt_again_is_denied_again_and_logged_one_time(runs):
    folder = runs.started()
    for command in ("rm x", "rm x", "rm x", "rm y", "rm x"):
        assert runs.asked(command=command) is not None
    assert [line["text"] for line in runs.log_lines(folder)[1:]] == ["rm x", "rm y", "rm x"]
    assert runs.asked(tool="Read", file_path="rm x") is not None
    assert [(line["tool"], line["text"]) for line in runs.log_lines(folder)[-2:]] == [("Bash", "rm x"), ("Read", "rm x")], \
        "the same text for another tool is another prompt"


def test_a_long_command_is_cut_in_the_log(runs):
    folder = runs.started()
    assert runs.asked(command="echo " + "x" * 5000) is not None
    text = runs.log_lines(folder)[-1]["text"]
    assert len(text) == 300 and text.endswith("…")


def own(runs, *args):
    """A command of the script as the skill writes it, with the home folder of the test."""
    return " ".join(["python3", "-I", "~/.claude/skills/unattended/scripts/contract.py", *args])


def test_a_command_that_starts_the_script_is_not_denied(runs):
    """A run that denied its own `close` could not be ended by its session. For these commands the hook is
    silent, so Claude Code decides as it does today."""
    folder = runs.started()
    (runs.home / ".claude" / "skills").mkdir(parents=True)
    (runs.home / ".claude" / "skills" / "unattended").symlink_to(SKILL)       # as the installer links it
    silent = [own(runs, "close", "--why", '"the user is back"'), own(runs, "log", "--kind", "heartbeat", "--text", "x"),
              f"python3 -I {SCRIPT} status", f"{PY} {SCRIPT} status", f"  python3   -I\t{SCRIPT}  status",
              f"/usr/bin/python3 -I {SCRIPT} close && rm -rf x", f"python3.12 -I {SCRIPT} status", f"python {SCRIPT} close"]
    for command in silent:
        assert runs.asked(command=command) is None, command
    denied = [f"echo python3 -I {SCRIPT} close", f"python3 -I {SCRIPT}; rm -rf x", "python3 -I /tmp/contract.py close",
              f"python3 -I {SCRIPT.parent} close", f"sh -c 'python3 -I {SCRIPT} close'", f"rm -rf x; python3 -I {SCRIPT} close",
              f"python3 -c 'import os' {SCRIPT}", f"python3 -I -I {SCRIPT} close", "   ", "python3 -I", "", "python3",
              f"cat {SCRIPT}", f"rm {SCRIPT}", f"sh {SCRIPT} close", f"python3 -B {SCRIPT} close", f"python3 -u {SCRIPT} status",
              # found by the adversary: the first command of each is not this script, started by python
              f"python3;rm {SCRIPT}", f"python3|rm {SCRIPT}", f"python3-config {SCRIPT} status", f"/tmp/python-evil {SCRIPT} close",
              f"pythonista {SCRIPT}", f"python3\n{SCRIPT} close\nrm -rf ~", f"python3=1 {SCRIPT} close",
              f"python3 -I {SCRIPT} close\nrm -rf ~", f"python3 -I {SCRIPT}\x00 close", f"python3 -I '{SCRIPT}' close",
              f"python3\u00a0-I {SCRIPT} close", f"python3 -I $HOME{SCRIPT} close"]
    for command in denied:
        assert runs.asked(command=command) is not None, command
    assert runs.asked(tool="Write", file_path=str(SCRIPT), content="x") is not None, "only as a shell command"
    assert runs.asked(tool="Monitor", command=f"python3 -I {SCRIPT} status") is not None, "and only of the shell tool"
    assert runs.kinds(folder).count("denied") == len(denied) + 2


def test_a_start_of_the_script_is_made_of_plain_words(runs):
    """The script is known by its file, so a link to it counts. A link whose name the shell reads as two
    commands does not: `python3 /x/a;b.py close` starts another file, and then a command `b.py`."""
    folder = runs.started()
    for name in ("link.py", "a;b.py", "a&&b.py", "a$(id).py", "a|b.py"):
        (runs.home / name).symlink_to(SCRIPT)
    for command in (f"python3 -I {runs.home}/link.py close", f"python3 {runs.home}/link.py status"):
        assert runs.asked(command=command) is None, command
    denied = [f"python3 -I {runs.home}/a;b.py close", f"python3 {runs.home}/a;b.py close", f"python3 {runs.home}/a&&b.py close",
              f"python3 -I {runs.home}/a$(id).py close", f"python3 {runs.home}/a|b.py close",
              f"true&&/usr/bin/python3 -I {SCRIPT} close", f"$(id)/python3 {SCRIPT} close", f"`id`/python3 -I {SCRIPT} close",
              f"x;/usr/bin/python3 {SCRIPT} close"]
    for command in denied:
        assert runs.asked(command=command) is not None, command
    assert runs.kinds(folder).count("denied") == len(denied)


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '"session-0001"', "{}", '{"session_id": 7}',
                                  '{"session_id": "../../outside"}', '{"session_id": "short"}', "\x00\x01",
                                  '{"session_id": "session-0001"', "[" * 200000])
def test_an_event_that_cannot_be_read_gets_no_answer_and_no_error(runs, text):
    folder = runs.started()
    for event in ("stop", "permission"):
        assert runs.hook_text(event, text) == ""
    assert runs.kinds(folder) == ["started"]


def test_a_prompt_event_that_names_no_tool_gets_no_answer(runs):
    folder = runs.started()
    for fields in ({}, {"tool_name": 7}, {"tool_name": ""}, {"tool_name": None, "tool_input": {"command": "rm x"}}):
        assert runs.hook("permission", **fields) is None, fields
    assert runs.kinds(folder) == ["started"]


def test_an_event_that_is_too_long_gets_no_answer(runs):
    runs.started()
    event = {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "rm x"}}
    assert runs.hook_text("permission", json.dumps(event)) != ""
    assert runs.hook_text("permission", json.dumps({**event, "last_assistant_message": "x" * 9_000_000})) == ""
    padded = json.dumps(event) + " " * (8 * 1024 * 1024)
    assert runs.hook_text("permission", padded) == "", "also when the part that is read is whole JSON"
    assert runs.hook_text("permission", json.dumps(event) + " " * 1000) != ""


def test_a_session_id_that_leads_out_of_the_folder_of_the_runs_reads_no_run_there(runs):
    """The id of an event is a plain name before it is a part of a path."""
    folder = runs.started()
    outside = runs.tmp / "elsewhere"
    shutil.copytree(folder.parent, outside)
    contract = json.loads((outside / folder.name / "contract.json").read_text())
    contract["session"] = "../elsewhere"
    (outside / folder.name / "contract.json").write_text(json.dumps(contract))
    event = {"session_id": "../elsewhere", "tool_name": "Bash", "tool_input": {"command": "rm x"}}
    for name in ("stop", "permission"):
        assert runs.hook_text(name, json.dumps(event)) == ""
    assert [json.loads(line)["kind"] for line in (outside / folder.name / "log.jsonl").read_text().splitlines()] == ["started"]


@pytest.mark.parametrize("args", [("hook",), ("hook", "--event"), ("hook", "--event", "bogus"), ("hook", "--help"),
                                  ("hook", "--event", "stop", "extra"), ("hook", "--event=stop"), ("hook", "stop")])
def test_a_hook_that_is_started_wrongly_gets_no_answer_and_no_error(runs, args):
    """Claude Code shows a hook that fails as an error at each stop and each prompt of every session."""
    runs.started()
    done = subprocess.run([PY, "-I", str(SCRIPT), *args], cwd=runs.cwd, env=runs.env(None), capture_output=True, text=True,
                          input=json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": {}}), timeout=60)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


@pytest.mark.parametrize("damage", ["a log line that is no object", "a contract that is cut", "a link at the log"])
def test_a_run_that_cannot_be_read_gets_no_answer(runs, damage, tmp_path):
    folder = runs.started()
    if damage == "a log line that is no object":
        (folder / "log.jsonl").write_text("[]\n")
    elif damage == "a contract that is cut":
        (folder / "contract.json").write_text('{"version": 1')
    else:
        (folder / "log.jsonl").unlink()
        (folder / "log.jsonl").symlink_to(tmp_path / "nothing")
    assert runs.hook_text("stop", json.dumps({"session_id": SESSION})) == ""
    assert runs.hook_text("permission", json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": {}})) == ""


def test_the_stop_hook_agrees_with_a_second_version_on_random_runs(runs, monkeypatch):
    """The second version keeps three numbers and never reads the log. Each run is a random row of stops (with
    and with no wake-up), lines of the session, prompts, and at some point the time limit."""
    script = load_script()
    monkeypatch.setenv("UNATTENDED_ROOT", str(runs.root))
    rng = random.Random(20261120)
    plan = runs.plan()
    for number in range(120):
        session = f"session-r{number:04d}"
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        with contextlib.redirect_stdout(io.StringIO()):
            assert script.main(["start", "--plan", plan]) == 0
        (folder,) = (runs.root / session).iterdir()
        state = {"1": "left", "2": "left", "3": "left"}
        in_a_row, let_go, asked_to_close, late, expected = 0, False, False, False, ["started"]
        for _ in range(rng.randrange(5, 40)):
            move = rng.choice(["stop"] * 6 + ["wake", "beat", "decision", "done", "skipped", "prompt", "late"])
            if move in ("stop", "wake"):
                wake = [{"id": "c", "schedule": "* * * * *", "recurring": False, "prompt": "p"}] if move == "wake" else []
                answer = in_process_hook(script, monkeypatch, "stop", session=session, session_crons=wake)
                left = [task for task, value in state.items() if value == "left"]
                if move == "wake" or let_go:
                    want = None
                elif in_a_row >= 3:
                    want, let_go = None, True
                    expected.append("released")
                elif left and not late:
                    want, in_a_row = f"Take task {left[0]} (", in_a_row + 1
                    expected.append("refused")
                elif asked_to_close:
                    want = None
                else:
                    want, in_a_row, asked_to_close = "is still open", in_a_row + 1, True
                    expected.append("refused")
                assert (answer is None) == (want is None) and (want is None or want in answer["reason"]), \
                    (number, expected, answer)
            elif move == "prompt":
                command = f"rm {rng.randrange(3)}"
                answer = in_process_hook(script, monkeypatch, "permission", session=session, tool_name="Bash",
                                         tool_input={"command": command})
                assert answer is not None
                last = runs.log_lines(folder)[-1]
                if not (last["kind"] == "denied" and expected[-1] == "denied" and len(runs.log_lines(folder)) == len(expected)):
                    expected.append("denied")
            elif move == "late":
                late = True
                contract = json.loads((folder / "contract.json").read_text())
                contract["ends"] = script.iso(script.now() - script.datetime.timedelta(minutes=1))
                (folder / "contract.json").write_text(json.dumps(contract))
            else:
                kind = {"beat": "heartbeat"}.get(move, move)
                task = rng.choice("123")
                args = ["log", "--kind", kind, "--text", "x"] + (["--task", task] if kind in ("done", "skipped") else [])
                args += ["--needs", "y"] if kind == "skipped" else []
                with contextlib.redirect_stdout(io.StringIO()):
                    assert script.main(args) == 0
                if kind in ("done", "skipped"):
                    state[task] = kind
                in_a_row, let_go = 0, False
                expected.append(kind)
            assert runs.kinds(folder) == expected, number


def in_process_hook(script, monkeypatch, event, session=SESSION, **fields):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"session_id": session, **fields}).encode())))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert script.main(["hook", "--event", event]) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_while_another_command_holds_the_run_a_stop_is_let_go_and_a_prompt_is_still_denied(runs, monkeypatch):
    """The stop hook decides from the log, so it must hold the run; with no lock it does not guess. A prompt is
    denied from what was read, also when the line for it cannot be written."""
    import fcntl
    script = load_script()
    folder = runs.started()
    monkeypatch.setenv("UNATTENDED_ROOT", str(runs.root))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(script, "LOCK_WAIT_S", 0.3)
    held = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert in_process_hook(script, monkeypatch, "stop") is None
        answer = in_process_hook(script, monkeypatch, "permission", tool_name="Bash", tool_input={"command": "rm x"})
        assert answer["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    finally:
        os.close(held)
    assert runs.kinds(folder) == ["started"]
    assert in_process_hook(script, monkeypatch, "stop")["decision"] == "block"


def test_an_answer_that_nobody_reads_is_no_error(runs):
    """Found by the adversary: with the reading end of its output closed, the hook ended with a traceback and
    exit 120."""
    runs.started()
    event = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "rm x"}})
    for name in ("stop", "permission"):
        hook = subprocess.Popen([PY, "-I", str(SCRIPT), "hook", "--event", name], cwd=runs.cwd, env=runs.env(None),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        hook.stdout.close()
        hook.stdin.write(event.encode())
        hook.stdin.close()
        assert hook.wait(timeout=60) == 0 and hook.stderr.read() == b"", name
        hook.stderr.close()


def test_a_fault_inside_a_hook_is_no_answer_and_no_traceback(runs, monkeypatch, capsys):
    script = load_script()
    runs.started()
    monkeypatch.setenv("UNATTENDED_ROOT", str(runs.root))

    def broken(*args):
        raise RuntimeError("a fault")
    for name in ("stop_answer", "permission_answer"):
        monkeypatch.setattr(script, name, broken)
    assert in_process_hook(script, monkeypatch, "stop") is None
    assert in_process_hook(script, monkeypatch, "permission", tool_name="Bash", tool_input={}) is None
    assert capsys.readouterr() == ("", "")


def test_log_does_not_take_the_kinds_of_the_hooks(runs):
    runs.started()
    for kind in ("refused", "released", "denied", "started", "closed"):
        code, lines = runs.run("log", "--kind", kind, "--text", "x")
        assert code == 2 and "invalid choice" in lines[0], (kind, lines)


def test_a_prompt_that_comes_while_a_log_line_is_half_written_is_still_denied(tmp_path, monkeypatch):
    """Found by the verifier, by reading: the hook reads the run without holding it. A line that is on the disk
    in part at that moment made the run unreadable, the hook was silent, and the prompt waited for the user."""
    world = Twice(tmp_path / "half", monkeypatch)
    folder = world.open_run(world.A)
    log, whole = folder / "log.jsonl", (folder / "log.jsonl").read_bytes()
    state = []

    def half_a_line_at_the_first_look(name, args):
        if name == "open" and str(args[0]).endswith("log.jsonl") and not state:
            state.append("half")
            log.write_bytes(whole + b'{"t": "2026-11-20T00:00:00Z", "kind": "hear')
        elif state == ["half"] and name == "open" and str(args[0]).endswith("contract.json"):
            state.append("whole")                # the next read of the run comes after the writer is done
            log.write_bytes(whole + b'{"t": "2026-11-20T00:00:00Z", "kind": "heartbeat", "text": "x"}\n')
    code, lines = world.watched(world.A, *ASKED, each=half_a_line_at_the_first_look)
    assert code == 0 and json.loads(lines[0])["hookSpecificOutput"]["decision"]["behavior"] == "deny", lines
    assert state == ["half", "whole"] and world.kinds(folder) == ["started", "heartbeat", "denied"]
    log.write_bytes(whole + b'{"t": "2026-11-20T00:00:00Z", "kind": "hear')
    assert world.call(world.A, *ASKED) == (0, []), "a log that stays damaged gets no answer"


def test_the_report_counts_the_refusals_and_names_what_was_denied(runs):
    runs.started()
    assert runs.asked(command="git push origin work") and runs.asked(tool="Edit", file_path="/etc/hosts")
    assert runs.asked(command="git push origin work")
    assert [runs.stop() is not None for _ in range(4)] == [True, True, True, False]
    code, lines = runs.run("close", "--why", "x")
    assert code == 0
    text = "\n".join(lines[lines.index("## Found"):])
    assert "- A prompt was denied, for nobody could answer it: Bash: git push origin work (2 times)" in text
    assert "- A prompt was denied, for nobody could answer it: Edit: /etc/hosts" in text
    assert "- The session was let go after 3 stops were refused and it logged no line between them." in text
    assert "3 stops refused, 3 prompts denied." in text


def test_a_task_state_is_not_changed_by_a_line_of_a_hook(runs):
    runs.started()
    runs.run("log", "--kind", "done", "--task", "1", "--text", "x")
    assert runs.stop() and runs.asked(command="rm x")
    code, lines = runs.run("status")
    assert lines == ["done 1 add the parser", "left 2 push the branch", "left 3 write the notes",
                     lines[-1]] and lines[-1].startswith("unattended: open left=2 ")


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


OWN = "python3 -I ~/.claude/skills/unattended/scripts/contract.py"
OWN_RULE = f"Bash({OWN} *)"


FOR_THE_RUN = "; the run itself needs this]"


def own_lines(lines, start=OWN):
    """The verdicts of the five lines of `preflight` for what a session types of the script from `start` on,
    which come before its last line: every `start`, `status`, every `log`, a bare `close` and every `close` with
    words. Each line is checked."""
    own, verdicts = lines[-6:-1], []
    assert len(own) == 5, lines
    for line, form in zip(own, ("start *", "status", "log *", "close", "close *")):
        verdict, rest = line.split(" ", 1)
        assert rest.startswith(f"{start} {form} [") and rest.endswith(FOR_THE_RUN), (line, form)
        verdicts.append(verdict)
    return verdicts


def of_the_plan(lines):
    """The lines of `preflight` for the commands of the plan, and its last line. The five lines for the script
    itself are checked and left out."""
    own_lines(lines)
    return lines[:-6] + lines[-1:]


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
    lines = of_the_plan(lines)
    assert code == 1 and lines[-1] == "unattended: preflight allowed=4 would-prompt=7 unsure=1 denied=1", lines
    assert [line.split(" ", 1)[0] for line in lines[:-1]] == ["allowed", "allowed", "denied", "would-prompt", "allowed",
                                                             "would-prompt", "unsure", "allowed"]
    assert lines[1] == "allowed git push origin work [allow: git push *]"
    assert lines[2] == "denied git push --force origin work [deny: git push --force *]", "a deny rule wins over an allow rule"
    assert lines[3] == "would-prompt gh pr merge 7 --squash [ask: gh pr merge *]"
    assert "never printed" not in "\n".join(lines)


def test_the_commands_of_the_script_are_judged_as_the_plan_starts_the_script(runs):
    """Found by a live session: the script ran from another folder than the one of the skill text. `preflight`
    judged a form that the session would never type, called it `would-prompt`, and the session started no run."""
    mine = f"/usr/bin/python3 -I {SCRIPT}"
    settings(runs, "user", allow=[f"Bash({mine} *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[f"{mine} status"]))
    assert (code, lines[-1]) == (0, "unattended: preflight allowed=6 would-prompt=0 unsure=0 denied=0"), lines
    assert own_lines(lines, start=mine) == ["allowed"] * 5 and lines[0] == f"allowed {mine} status [allow: {mine} *]"
    assert lines[3] == f"allowed {mine} log * [allow: {mine} *; the run itself needs this]"
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git status", f"{mine}   log --kind done --text x"]))
    assert code == 1 and own_lines(lines, start=mine) == ["allowed"] * 5, \
        "any command of the script shows its start, and the start is written with one space between its words"
    assert [line.split(" ", 1)[0] for line in lines[:2]] == ["would-prompt", "allowed"]
    for none in ([], ["git status"], [f"echo {mine} log"], [f"cat {SCRIPT}"], [f"{mine}x status"]):
        code, lines = runs.run("preflight", "--plan", runs.plan(commands=none))
        assert code == 1 and own_lines(lines) == ["would-prompt"] * 5, f"{none}: the start of the skill text"
    other = f"python3 {SCRIPT}"
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[f"{other} status", f"{mine} status", f"{other} close"]))
    assert code == 1 and len(lines) == 3 + 5 + 5 + 1, "each start of the plan is judged, one time"
    assert own_lines(lines[:8] + lines[-1:], start=other) == ["would-prompt"] * 5 and own_lines(lines, start=mine) == ["allowed"] * 5


PER_LINE = ["status", "log --kind heartbeat --task * --text *", "log --kind decision --text *",
            "log --kind skipped --task * --text * --needs *", "log --kind done --task * --text *", "close", "close --why *"]


def test_a_rule_for_each_line_that_a_run_was_seen_to_type_is_not_enough(runs):
    """Found by the adversary, two times. First `log` was judged with no word behind it, and an allow rule for
    that one text gave exit 0. Then seven lines were judged, and a rule for each of them gave exit 0, while a
    heartbeat that names no task had no rule. A list of lines is never the lines of the next run: the first
    `log` outside the list would have waited for the user all night."""
    yes, no = "allowed", "would-prompt"
    cases = [(["log", "status", "close", "start"], [no, yes, no, yes, no]),
             (PER_LINE, [no, yes, no, yes, no]),
             (PER_LINE + ["start --plan *"], [no, yes, no, yes, no]),
             (["start *", "status", "log --kind *", "close *"], [yes, yes, no, yes, yes]),
             (["status", "log * --text *", "close"], [no, yes, no, yes, no]),
             (["status", "log *x", "close*"], [no, yes, no, yes, yes]),
             (["log *", "close --why *"], [no, no, yes, no, no])]
    for rules, expected in cases:
        settings(runs, "user", allow=[f"Bash({OWN} {rule})" for rule in rules])
        code, lines = runs.run("preflight", "--plan", runs.plan(commands=[]))
        assert code == 1 and own_lines(lines) == expected, rules
    assert lines[-3] == f"would-prompt {OWN} close [no allow rule covers it; the run itself needs this]"
    assert lines[-2] == (f"would-prompt {OWN} close * [no allow rule covers every such command; this one does: "
                         f"Bash({OWN} close *); the run itself needs this]")
    enough = [[f"Bash({OWN} *)"], [f"Bash({OWN}:*)"], ["Bash(python3 *)"], ["Bash"], ["Bash(*)"], [f"Bash({OWN} **)"],
              [f"Bash({OWN} {rule})" for rule in ("start *", "status", "log *", "close *")],
              [f"Bash({OWN} {rule})" for rule in ("start:*", "status", "log:*", "close:*")],
              [f"Bash({OWN} {rule})" for rule in ("s*", "lo*", "close", "close *")]]
    for rules in enough:
        settings(runs, "user", allow=rules)
        code, lines = runs.run("preflight", "--plan", runs.plan(commands=[]))
        assert code == 0 and own_lines(lines) == ["allowed"] * 5, rules
    settings(runs, "user", allow=[f"Bash({OWN} *:*)"])
    assert own_lines(runs.run("preflight", "--plan", runs.plan(commands=[]))[1]) == ["would-prompt"] * 5, \
        "a `:*` beside another `*` is a form that the script does not take for one that it knows"


def test_a_rule_that_stops_some_lines_of_the_run_is_found_by_its_beginning(runs):
    """A deny or ask rule for one kind of line of the run stops that line, also when an allow rule covers every
    command of the script. A rule for a command that a run does not type (`prune`) stops nothing of the run."""
    base = "contract.py"
    cases = [(f"{OWN} log --kind heartbeat --text *", [0, 0, 1, 0, 0]), (f"{OWN} log --kind heartbeat --text waited", [0, 0, 1, 0, 0]),
             (f"{OWN} log:*", [0, 0, 1, 0, 0]), (f"{OWN} lo:*", [0, 0, 1, 0, 0]), (f"{OWN} log --kind:*", [0, 0, 1, 0, 0]),
             (f"{OWN} clos:*", [0, 0, 0, 1, 1]), (f"{OWN} lo*", [0, 0, 1, 0, 0]), (f"{OWN} close", [0, 0, 0, 1, 0]),
             (f"{OWN} close --why x", [0, 0, 0, 0, 1]), (f"{OWN} close *", [0, 0, 0, 1, 1]), (f"{OWN} close --why*", [0, 0, 0, 0, 1]),
             (f"{OWN} status", [0, 1, 0, 0, 0]), (f"{OWN} start --plan *", [1, 0, 0, 0, 0]), (f"{OWN} s*", [1, 1, 0, 0, 0]), (f"{OWN} *", [1, 1, 1, 1, 1]), ("python3 *", [1, 1, 1, 1, 1]),
             ("python3", [1, 1, 1, 1, 1]), ("p*", [1, 1, 1, 1, 1]), (f"*{base}*", [1, 1, 1, 1, 1]), (f"{base} log --kind done*", [0, 0, 1, 0, 0]),
             (f"*{base} log *", [0, 0, 1, 0, 0]), (f"{base} close*", [0, 0, 0, 1, 1]), (f"python3  -I   {OWN.split()[-1]} log *", [0, 0, 1, 0, 0]),
             (f"{OWN} prune *", [0, 0, 0, 0, 0]), (f"{OWN} hook *", [0, 0, 0, 0, 0]), (f"{OWN} logs*", [0, 0, 0, 0, 0]),
             ("git push *", [0, 0, 0, 0, 0]), ("rm *", [0, 0, 0, 0, 0]), (f"{OWN} closed", [0, 0, 0, 0, 0]), ("python3 -c *", [0, 0, 0, 0, 0])]
    for kind, verdict in (("ask", "would-prompt"), ("deny", "denied")):
        for rule, stopped in cases:
            settings(runs, "user", allow=[OWN_RULE], **{kind: [f"Bash({rule})"]})
            code, lines = runs.run("preflight", "--plan", runs.plan(commands=[]))
            assert own_lines(lines) == [verdict if hit else "allowed" for hit in stopped], (kind, rule)
            assert code == (1 if any(stopped) else 0), (kind, rule)
    settings(runs, "user", allow=[OWN_RULE], ask=[f"Bash({OWN} log --kind skipped*)"])
    lines = runs.run("preflight", "--plan", runs.plan(commands=[]))[1]
    assert lines[-4] == f"would-prompt {OWN} log * [ask: {OWN} log --kind skipped*; the run itself needs this]"
    settings(runs, "user", allow=[OWN_RULE], ask=["Bash"])
    assert own_lines(runs.run("preflight", "--plan", runs.plan(commands=[]))[1]) == ["would-prompt"] * 5
    settings(runs, "user")
    settings(runs, "project", ask=[f"Bash({OWN} log --kind done *)"])
    settings(runs, "local", allow=[OWN_RULE], deny=[f"Bash({OWN} close --why *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[]), "--project", str(runs.cwd))
    assert own_lines(lines) == ["allowed", "allowed", "would-prompt", "allowed", "denied"], "the rules of each settings file count"
    assert own_lines(runs.run("preflight", "--plan", runs.plan(commands=[]))[1]) == ["would-prompt"] * 5, "with no project, none of them"


def test_a_line_for_every_command_agrees_with_the_verdict_of_each_command():
    """Against the judge of one command, on random rules: when the line `start *`, `log *` or `close *` is `allowed`, each
    command that starts so is `allowed` too. The words of a command are options of the script and words that
    stand in no rule. The rules that stop are made from the start of a real command: one that starts with `*`
    and goes on into the text, or one for a word of the text, is the limit that the known limits state."""
    script, rng = load_script(), random.Random(20261120)
    options = ["--kind", "heartbeat", "decision", "skipped", "done", "--task", "--text", "--needs", "--why"]

    def tail():
        return " ".join(rng.choice(options + ["w%04d" % rng.randrange(10000)]) for _ in range(rng.randint(1, 6)))

    def cut(text):
        return text[:rng.randint(0, len(text))]

    seen = {"allowed": 0, "other": 0, "stopped by a rule": 0}
    for _ in range(4000):
        name = rng.choice(["start", "log", "close"])
        head = f"{OWN} {name}"
        full = f"{head} {' '.join(rng.choice(options) for _ in range(rng.randint(1, 4)))}"
        allow = [rng.choice([f"{OWN} *", f"{head} *", f"{head}:*", f"{head}*", cut(full) + "*", cut(full), cut(full) + ":*",
                             cut(full) + "*" + rng.choice(options) + "*", f"{head} * *", "*", "python3 *", f"{OWN} {name[:2]}*"])
                 for _ in range(rng.randint(1, 3))]
        begin = rng.choice([full, "contract.py" + full[len(OWN):], full.replace(" ", "  ", 1)])
        stop = [rng.choice([cut(begin) + "*", cut(begin), cut(begin) + ":*", cut(begin) + " *", "*" + cut(OWN[rng.randint(0, 40):]) + "*",
                            cut(begin) + "*" + rng.choice(options) + "*", "git push *", f"{OWN} prune *"])
                for _ in range(rng.choice([0, 0, 1, 2]))]
        stop = [rule for rule in stop if rule.split("*")[0].strip() or rule.strip("* ") in OWN]
        rules = [{"permissions": {"allow": [f"Bash({rule})" for rule in allow], rng.choice(["ask", "deny"]): [f"Bash({rule})" for rule in stop]}}]
        verdict = script.judge_every(head, rules)[0]
        each = [script.judge(f"{head} {tail()}", rules)[0] for _ in range(12)]
        if verdict == "allowed":
            assert each == ["allowed"] * 12, (allow, stop, head)
        seen["allowed" if verdict == "allowed" else "other"] += 1
        seen["stopped by a rule"] += verdict in ("denied", "would-prompt") and bool(stop) and script.judge_every(
            head, [{"permissions": {"allow": [f"Bash({rule})" for rule in allow]}}])[0] == "allowed"
    assert min(seen.values()) > 300, seen


def test_a_deny_or_ask_rule_with_a_colon_is_read_in_its_wider_sense(runs):
    """The docs say that `rm:*` is `rm *`. An older reading takes it as a plain prefix. For a rule that stops a
    command, the script takes whichever reading stops more; for a rule that allows one, the reading of the docs."""
    settings(runs, "user", allow=["Bash(ls:*)", "Bash(rmdir build)", "Bash(ghost run)"], deny=["Bash(rm:*)"], ask=["Bash(gh:*)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["ls -la", "lsof", "rm -rf x", "rmdir build", "ghost run", "gh pr list"]))
    lines = of_the_plan(lines)
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
    lines = of_the_plan(lines)
    assert code == 1 and lines[0].split(" ", 1)[0] == verdict, lines


def test_a_rule_that_stops_counts_from_each_of_the_three_settings_files(runs):
    settings(runs, "user", allow=["Bash"])
    settings(runs, "project", allow=["Bash"], ask=["Bash(make deploy)"])
    settings(runs, "local", allow=["Bash"], deny=["Bash(git push *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin x", "make deploy", "make test"]),
                           "--project", str(runs.cwd))
    lines = of_the_plan(lines)
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
    lines = of_the_plan(lines)
    assert code == 1 and lines[0].split(" ", 1)[0] in ("would-prompt", "unsure"), lines


def test_preflight_prints_the_command_with_the_spaces_that_were_judged(runs):
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git  push   origin"]))
    lines = of_the_plan(lines)
    assert lines[0].startswith("would-prompt git  push   origin [")


def test_preflight_passes_only_when_every_command_is_allowed(runs):
    settings(runs, "user", allow=["Bash"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin work", "anything at all"]))
    lines = of_the_plan(lines)
    assert (code, lines[-1]) == (0, "unattended: preflight allowed=7 would-prompt=0 unsure=0 denied=0")
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[]))
    assert code == 0 and of_the_plan(lines) == ["unattended: preflight allowed=5 would-prompt=0 unsure=0 denied=0"]
    settings(runs, "user", allow=["Bash(git push *)", "Bash(anything at all)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin work", "anything at all"]))
    assert (code, lines[-1]) == (1, "unattended: preflight allowed=2 would-prompt=5 unsure=0 denied=0"), \
        "every command of the plan is allowed, and the commands of the script itself are not"
    assert own_lines(lines) == ["would-prompt"] * 5
    settings(runs, "user", allow=["Bash(git push *)", "Bash(anything at all)", OWN_RULE])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin work", "anything at all"]))
    assert (code, lines[-1]) == (0, "unattended: preflight allowed=7 would-prompt=0 unsure=0 denied=0")
    assert lines[-3:-1] == [f"allowed {OWN} close [allow: {OWN} *; the run itself needs this]",
                            f"allowed {OWN} close * [allow: {OWN} *; the run itself needs this]"]
    settings(runs, "user", allow=["Bash"], deny=["Bash(python3 *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=[]))
    assert (code, lines[-1]) == (1, "unattended: preflight allowed=0 would-prompt=0 unsure=0 denied=5")
    settings(runs, "user", allow=["Bash"], deny=["Bash(python3 *)"], ask=["Bash(python3 *)"])
    assert own_lines(runs.run("preflight", "--plan", runs.plan(commands=[]))[1]) == ["denied"] * 5, "a deny rule wins"


def test_without_the_project_only_the_settings_of_the_user_count(runs):
    settings(runs, "project", allow=["Bash(git push *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin work"]))
    lines = of_the_plan(lines)
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
    lines = of_the_plan(lines)
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
    lines = of_the_plan(lines)
    assert code == 0 and lines[0].split(" ", 1)[0] == "allowed", lines


def test_a_rule_that_stops_is_read_before_the_command_is_called_unsure(runs):
    settings(runs, "user", allow=["Bash"], deny=["Bash(git push *)"])
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=["git push origin 'work'", "git status && git push origin work"]))
    lines = of_the_plan(lines)
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
    lines = of_the_plan(lines)
    assert code == 1 and lines[-1] == "unattended: preflight allowed=0 would-prompt=5 unsure=1 denied=0", lines[-1]
    assert len(lines) == 2 and lines[0].startswith("unsure git status ")


def test_a_command_is_matched_letter_for_letter(runs):
    settings(runs, "user", allow=["Bash(git push *)", "Bash(echo *)"])
    commands = ["git  push origin work", " git push origin work", "git push origin work", "echo " + "x" * 1995,
                "echo " + "x" * 1996, "echo " + "x" * 3000]
    code, lines = runs.run("preflight", "--plan", runs.plan(commands=commands))
    lines = of_the_plan(lines)
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
    lines = of_the_plan(lines)
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
