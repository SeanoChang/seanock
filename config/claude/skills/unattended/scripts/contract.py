#!/usr/bin/env python3
"""The contract and the log of one unattended run.

    contract.py preflight --plan FILE [--project DIR]
    contract.py start --plan FILE
    contract.py log --kind heartbeat|decision|skipped|done [--task ID] --text T [--needs N]
    contract.py status
    contract.py close [--why T]
    contract.py prune [--days N]

A run belongs to a session. Its files are in ~/.claude/unattended/<session>/<start time>/: the contract, which is
written one time, and the log, which only grows. The state of a task is read from the log, so no file is ever
changed in place. Nothing is written into a work tree.

Exit 0: done, and the last line says what. Exit 1: a result that needs action (a command that would prompt, a
close that the contract does not allow, no open run). Exit 2: `unattended: error ...`, which is not a result.
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

ROOT_ENV = "UNATTENDED_ROOT"        # tests put the runs somewhere else; a session never sets it
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
SESSION = re.compile(r"[A-Za-z0-9_-]{8,64}")
STAMP = "%Y%m%dT%H%M%SZ"
CONTRACT = "contract.json"
LOG = "log.jsonl"
KINDS = ("heartbeat", "decision", "skipped", "done")
DEFAULT_HOURS = 8.0
MAX_HOURS = 72.0
KEEP_DAYS = 14
MAX_DAYS = 3650
MAX_TEXT = 2000
LOCK_WAIT_S = 10.0
TRIES = 5                           # how often a command reads again when another command changed a folder under it
GONE = ".gone"                      # the ending of a run folder that a prune took out of its session
WORD = re.compile(r"[A-Za-z0-9_@%+:,./~-][A-Za-z0-9_@%+=:,./~-]*")


class Failed(Exception):
    pass


class Gone(Failed):
    """The run was removed while it was read."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def moment_of(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def shown(text):
    """Text for one line of output: no line break, no control character, a fixed length at most. The spaces stay
    as they are."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def one_line(text):
    """Text for one field of the contract or the log: as `shown`, and with each run of spaces made one."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return shown(" ".join(text.split()))


# ── where a run is ───────────────────────────────────────────────────────────

def root():
    given = os.environ.get(ROOT_ENV)
    if given:
        if not os.path.isabs(given):
            raise Failed(f"{ROOT_ENV} is not an absolute path")
        return Path(given)
    return Path.home() / ".claude" / "unattended"


def session_id(given=None):
    name = given if given is not None else os.environ.get(SESSION_ENV, "")
    if not SESSION.fullmatch(name):
        raise Failed(f"no session id: {SESSION_ENV} is not set, or it is not a plain name. "
                     "This script runs inside a Claude Code session")
    return name


def plain_folder(path, make):
    """The folder, with no link accepted in its place. With `make`, it is made for this user alone. Another
    command can make it, or remove it, at the same moment: so it is read again after it was made."""
    for _ in range(TRIES):
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if not make:
                return False
            with contextlib.suppress(FileExistsError):
                os.mkdir(path, 0o700)
            continue
        if not stat.S_ISDIR(mode):
            raise Failed(f"{path} is no plain folder")
        return True
    raise Failed(f"{path} is removed each time it is made")


def session_folder(session, make):
    top = root()
    if make:
        top.parent.mkdir(parents=True, exist_ok=True)
    if not plain_folder(top, make):
        return None
    folder = top / session
    return folder if plain_folder(folder, make) else None


def stamped(folder):
    """The folders of one session whose name is a start time, the oldest first, each with that time. A name that
    a prune took after the folder was listed is left out: a prune of many runs must not end a command that
    reads beside it."""
    found = []
    for name in os.listdir(folder):
        try:
            began = datetime.datetime.strptime(name, STAMP).replace(tzinfo=datetime.timezone.utc)
            mode = os.lstat(folder / name).st_mode
        except (ValueError, FileNotFoundError):
            continue
        if stat.S_ISDIR(mode):
            found.append((name, began))
    return [(folder / name, began) for name, began in sorted(found)]


def runs_of(folder):
    """The runs of one session, the oldest first. A folder that never got its contract is no run: a start that
    failed halfway leaves one."""
    return [path for path, _ in stamped(folder) if os.path.lexists(path / CONTRACT)]


def read_file(name, folder):
    """The text of one file of a folder that is held open. No link is followed."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Failed(f"{name} is no plain file")
        return handle.read().decode("utf-8")


class Run:
    """One run: the contract as it was written, and the lines of the log. Both files are read from the folder as
    it was opened, whatever name it has a moment later. A prune takes a run out of its session in one step and
    removes its files after that. So a run that still has its name after both files were read was read whole;
    one that has lost it is `Gone`."""

    def __init__(self, folder):
        self.folder = folder
        try:
            held = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.contract = json.loads(read_file(CONTRACT, held))
                try:
                    lines = read_file(LOG, held).splitlines()
                except FileNotFoundError:           # a start writes the contract first
                    lines = []
            finally:
                os.close(held)
            self.log = [json.loads(line) for line in lines if line.strip()]
            for line in self.log:
                if not isinstance(line, dict) or not isinstance(line.get("kind"), str):
                    raise ValueError("a log line is no object with a kind")
                moment_of(line["t"])
            self.tasks = [(str(task["id"]), str(task["title"]), str(task["check"])) for task in self.contract["tasks"]]
            self.ends = moment_of(self.contract["ends"])
            self.session = self.contract["session"]
        except Failed:
            raise
        except Exception as exc:      # whatever a damaged file does to the reader, the answer is the same
            if not os.path.lexists(folder / CONTRACT):
                raise Gone(f"the run in {folder} is gone")
            raise Failed(f"cannot read the run in {folder}: {type(exc).__name__}: {exc}")
        if not os.path.lexists(folder / CONTRACT):
            raise Gone(f"the run in {folder} is gone")

    def closed(self):
        return any(line["kind"] == "closed" for line in self.log)

    def state_of(self, task):
        """done, skipped or left: what the last done or skipped line of the task says."""
        state = "left"
        for line in self.log:
            if line.get("task") == task and line["kind"] in ("done", "skipped"):
                state = line["kind"]
        return state

    def left(self):
        return [task for task in self.tasks if self.state_of(task[0]) == "left"]

    def late(self):
        return now() >= self.ends

    def add(self, kind, **fields):
        line = {"t": iso(now()), "kind": kind}
        line.update({name: one_line(value) for name, value in fields.items() if value is not None})
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.folder / LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        self.log.append(line)


def last_run(session):
    """The newest run of this session, or None. A run in this folder that names another session is not this
    session's: on a disk that takes `abc` and `ABC` for one name, two ids share a folder."""
    for _ in range(TRIES):
        folder = session_folder(session, make=False)
        if folder is None:
            return None
        try:
            runs = runs_of(folder)
            if not runs:
                return None
            run = Run(runs[-1])
        except (Gone, FileNotFoundError):       # a prune took a run, or the folder, while it was read: read again
            continue
        if run.session != session:
            raise Failed(f"the run in {run.folder} belongs to another session")
        return run
    raise Failed("the runs of this session change faster than they can be read")


def open_run(session):
    """The run of this session that is not closed, or None."""
    run = last_run(session)
    return None if run is None or run.closed() else run


@contextlib.contextmanager
def the_open_run():
    """The open run of this session, read while this command alone holds its folder. A command that changes the
    run reads it under that lock: `log` and `close` at the same time then see each other, and no line is written
    after the close."""
    run = open_run(session_id())
    if run is None:
        raise Failed("this session has no open run")
    try:
        held = os.open(run.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise Failed("this session has no open run")
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Failed(f"another command holds the run for longer than {LOCK_WAIT_S:g} s")
                time.sleep(0.05)
        try:
            run = Run(run.folder)
        except Gone:
            raise Failed("this session has no open run")
        if run.closed():
            raise Failed("this session has no open run")
        yield run
    finally:
        os.close(held)


# ── the plan ─────────────────────────────────────────────────────────────────

def read_plan(path):
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise Failed(f"cannot read the plan {path}: {exc}")
    if not isinstance(plan, dict):
        raise Failed("the plan is no JSON object")
    return plan


def texts(plan, name, fields, as_written=False):
    """The list `name` of the plan, each entry with the text fields that it must have. A text is made one line,
    for it goes into the contract and into the output. `as_written` keeps it letter for letter: a command is
    judged in the form in which the session will run it, not in the form in which it is printed."""
    entries = plan.get(name)
    if not isinstance(entries, list):
        raise Failed(f"the plan has no list `{name}`. Write an empty list when there is none: that is an answer too")
    out = []
    for number, entry in enumerate(entries, 1):
        if isinstance(entry, str) and len(fields) == 1:
            entry = {fields[0]: entry}
        if not isinstance(entry, dict):
            raise Failed(f"`{name}` entry {number} is no object")
        values = []
        for field in fields:
            value = entry.get(field)
            if not isinstance(value, str) or not one_line(value):
                raise Failed(f"`{name}` entry {number} has no `{field}`")
            values.append(value if as_written else one_line(value))
        out.append(values)
    return out


def checked_plan(plan):
    tasks = texts(plan, "tasks", ("title", "check"))
    if not tasks:
        raise Failed("the plan has no task")
    approvals = texts(plan, "approvals", ("what", "answer"))
    for what, answer in approvals:
        if answer not in ("yes", "no"):
            raise Failed(f"the approval `{what}` has the answer `{answer}`: it is yes or no, as the user said it")
    hours = plan.get("hours", DEFAULT_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= MAX_HOURS:
        raise Failed(f"`hours` is a number above 0 and at most {MAX_HOURS:g}")
    return tasks, approvals, float(hours)


# ── which command would prompt ───────────────────────────────────────────────
#
# The two kinds of rule are read in opposite ways, so that a doubt never ends as `allowed`. A rule that allows is
# read narrowly: letter for letter, and a form that this script does not know for sure allows nothing. A rule
# that stops (deny, ask) is read widely: with the spaces made even, as Claude Code does it, and `prefix:*` in
# both senses that it has had.

def rule_matches(rule, command):
    """Whether a Bash rule covers this command text, by the three wildcard rules of the permissions page: a `*`
    stands for any text; a rule with no `*` is one exact command; and ` *` at the end, as the only wildcard, takes
    the bare command too. `:*` at the end is another way to write ` *`."""
    if rule.endswith(":*"):
        rule = rule[:-2] + " *"
    if "*" not in rule:
        return command == rule
    if rule.endswith(" *") and rule.count("*") == 1 and command == rule[:-2]:
        return True
    first, *middle, last = rule.split("*")
    if len(command) < len(first) + len(last) or not command.startswith(first) or not command.endswith(last):
        return False
    at, end = len(first), len(command) - len(last)
    for part in middle:             # each part at its first place: one pass, so many `*` cost no more than one
        found = command.find(part, at, end)
        if found < 0:
            return False
        at = found + len(part)
    return True


def even(text):
    """The text with each run of spaces and tabs made one space, and none at its ends."""
    return " ".join(part for part in re.split(r"[ \t]+", text) if part)


def allows(rule, command):
    """Whether an allow rule covers the command, read narrowly. Claude Code reads a `:*` beside another `*` as
    plain text; this script does not take that form for one that it knows. (A rule with a backslash needs no
    line here: it can only cover a command with a backslash, and such a command is not plain words, so it is
    `unsure` before any allow rule is read.)"""
    if rule.endswith(":*") and "*" in rule[:-2]:
        return False
    return rule_matches(rule, command)


def names_in(word):
    """Each text that one word of a command can hold as a program to run: the word, what follows an `=`
    (`FOO=1`, `--command=rm`), every tail of a word that starts with `-` (`-Srm`, the value of an option written
    onto it), and each of these without its path."""
    found = {word}
    parts = word.split("=")
    found.update("=".join(parts[at:]) for at in range(1, len(parts)))
    if word.startswith("-"):
        found.update(word[at:] for at in range(1, len(word)))
    found.update(name.rsplit("/", 1)[-1] for name in list(found))
    found.discard("")
    return found


def stops(rule, command):
    """Whether a deny or ask rule covers the command, read widely. The rule is tried on the command as written.
    Then, with the spaces made even, it is tried on each program name that a word can hold (see `names_in`),
    alone and with the rest of the command behind it. So a rule for `rm` holds for `FOO=1 rm`, `timeout 30 rm`,
    `/bin/rm` and `env -Srm`, as it does in Claude Code, and for `echo rm` too, which errs to the safe side.
    `prefix:*` also takes any text that starts with the prefix."""
    if rule_matches(rule, command):
        return True
    rule = even(rule)
    words = even(command).split(" ")
    for start, word in enumerate(words):
        rest = words[start + 1:]
        for name in names_in(word):
            for form in (" ".join([name] + rest), name):
                if rule_matches(rule, form) or (rule.endswith(":*") and form.startswith(rule[:-2])):
                    return True
    return False


def bash_rules(settings, name):
    """(all uses of Bash, the command patterns) of one list of the settings."""
    whole, patterns = False, []
    entries = settings.get("permissions", {}).get(name, []) if isinstance(settings.get("permissions"), dict) else []
    for entry in entries if isinstance(entries, list) else []:
        if entry == "Bash":
            whole = True
        elif isinstance(entry, str) and entry.startswith("Bash(") and entry.endswith(")"):
            patterns.append(entry[5:-1])
    return whole, patterns


def plain(command):
    """Whether the command is words of letters, digits and `_ @ % + = : , . / ~ -`, with spaces or tabs between
    them, and no word starts with `=`. Such a text reads in one way only. A quote, an operator, a wildcard, a
    variable, a bracket or a character outside ASCII can make Claude Code or the shell read another command
    than this script does."""
    return all(WORD.fullmatch(word) for word in re.split(r"[ \t]+", command) if word)


def judge(command, settings_list):
    """allowed, would-prompt, unsure or denied, and the rule that says so. Only `allowed` is a pass. A command
    that is not plain words is unsure: this script does not parse a shell command, and Claude Code judges each
    part of a command on its own. So is a command of more than 2,000 characters: nobody reads it in the list,
    and the time of this check has a bound."""
    if len(command) > MAX_TEXT:
        return "unsure", "it is longer than 2,000 characters"
    for name, verdict in (("deny", "denied"), ("ask", "would-prompt")):
        for settings in settings_list:
            whole, patterns = bash_rules(settings, name)
            hit = "Bash" if whole else next((p for p in patterns if stops(p, command)), None)
            if hit:
                return verdict, f"{name}: {hit}"
    if not plain(command):
        return "unsure", ("it is not plain words: a quote, an operator, a wildcard, a variable or a control "
                          "character can make Claude Code read another command than this script")
    for settings in settings_list:
        whole, patterns = bash_rules(settings, "allow")
        hit = "Bash" if whole else next((p for p in patterns if allows(p, command)), None)
        if hit:
            return "allowed", f"allow: {hit}"
    return "would-prompt", "no allow rule covers it"


def settings_files(project):
    files = [Path.home() / ".claude" / "settings.json"]
    if project is not None:
        files += [project / ".claude" / "settings.json", project / ".claude" / "settings.local.json"]
    out = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError) as exc:
            raise Failed(f"cannot read {path}: {exc}")
        if isinstance(data, dict):
            out.append(data)
    return out


def preflight(args):
    plan = read_plan(args.plan)
    commands = [values[0] for values in texts(plan, "commands", ("command",), as_written=True)]
    settings_list = settings_files(Path(args.project).resolve() if args.project else None)
    counts = dict.fromkeys(("allowed", "would-prompt", "unsure", "denied"), 0)
    lines = []
    for command in commands:
        verdict, why = judge(command, settings_list)
        counts[verdict] += 1
        room = MAX_TEXT - len(f"{verdict}  [{why}]")      # a long command is cut here, so that the reason stays
        lines.append(f"{verdict} {command if len(command) <= room else command[:room - 1] + '…'} [{why}]")
    summary = " ".join(f"{name}={count}" for name, count in counts.items())
    return (0 if counts["allowed"] == len(commands) else 1), lines + [f"unattended: preflight {summary}"]


# ── the commands ─────────────────────────────────────────────────────────────

def start(args):
    session = session_id()
    tasks, approvals, hours = checked_plan(read_plan(args.plan))
    if open_run(session) is not None:
        raise Failed("this session has an open run already: close it first")
    prune_runs(KEEP_DAYS)
    began = now()
    ends = began + datetime.timedelta(hours=hours)
    contract = {
        "version": 1, "session": session, "started": iso(began), "ends": iso(ends), "cwd": os.getcwd(),
        "tasks": [{"id": str(number), "title": title, "check": check} for number, (title, check) in enumerate(tasks, 1)],
        "approvals": [{"what": what, "answer": answer} for what, answer in approvals],
    }
    for _ in range(TRIES):
        run_folder = session_folder(session, make=True) / began.strftime(STAMP)
        try:
            os.mkdir(run_folder, 0o700)
            break
        except FileNotFoundError:       # a prune removed the session folder with the last old run in it
            continue
    else:
        raise Failed(f"{run_folder.parent} is removed each time it is made")
    # The contract gets its name when it is whole: a folder with no contract.json is no run.
    fd = os.open(run_folder / (CONTRACT + ".new"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(contract, ensure_ascii=False, indent=2) + "\n")
    os.replace(run_folder / (CONTRACT + ".new"), run_folder / CONTRACT)
    Run(run_folder).add("started")
    return 0, [f"run folder: {run_folder}", f"unattended: started tasks={len(tasks)} ends={iso(ends)}"]


def log(args):
    text, needs = one_line(args.text), one_line(args.needs or "")
    with the_open_run() as run:
        if args.kind in ("skipped", "done") or args.task is not None:
            if args.task not in [task[0] for task in run.tasks]:
                raise Failed(f"the contract has no task `{args.task}`")
        if args.kind == "skipped" and not needs:
            raise Failed("a skipped step says what it needs from the user: add --needs")
        if not text:
            raise Failed("--text is empty")
        run.add(args.kind, task=args.task, text=text, needs=needs if args.kind == "skipped" else None)
    return 0, [f"unattended: logged {args.kind}"]


def status(args):
    run = open_run(session_id())
    if run is None:
        return 1, ["unattended: none"]
    lines = [f"{run.state_of(task)} {task} {title}" for task, title, _ in run.tasks]
    late = " late" if run.late() else ""
    return 0, lines + [f"unattended: open left={len(run.left())} ends={run.contract['ends']}{late}"]


def report(run):
    title = {task: name for task, name, _ in run.tasks}
    last = {}
    for line in run.log:
        if line["kind"] in ("done", "skipped") and line.get("task") in title:
            last[line["task"]] = line
    blocked = [f"- Task {task} ({title[task]}): {line.get('text', '')} Needs: {line.get('needs', '')}"
               for task, line in last.items() if line["kind"] == "skipped"]
    changed = [f"- Task {task} ({title[task]}): {line.get('text', '')}"
               for task, line in last.items() if line["kind"] == "done"]
    found = [f"- {line.get('text', '')}" for line in run.log if line["kind"] == "decision"]
    left = [f"- Task {task} ({name}): not done." for task, name, _ in run.left()]
    count = {kind: sum(1 for line in run.log if line["kind"] == kind) for kind in ("heartbeat", "refused")}
    out = ["## Blocked on me"] + (blocked + left or ["- Nothing."])
    out += ["", "## Changed"] + (changed or ["- Nothing."])
    out += ["", "## Found"] + (found or ["- Nothing."])
    out += ["", f"Run: started {run.contract['started']}, time limit {run.contract['ends']}, "
                f"{count['heartbeat']} heartbeat lines, {count['refused']} stops refused. Log: {run.folder / LOG}"]
    return out


def close(args):
    why = one_line(args.why or "")
    with the_open_run() as run:
        left = run.left()
        if left and not run.late() and not why:
            lines = [f"left {task} {title}" for task, title, _ in left]
            return 1, lines + [f"unattended: summary left={len(left)}"]
        run.add("closed", text=why or ("the time limit is reached" if left else "every task is done or skipped"))
        states = [run.state_of(task) for task, _, _ in run.tasks]
        counts = " ".join(f"{name}={states.count(name)}" for name in ("done", "skipped", "left"))
        return 0, report(run) + [f"unattended: closed {counts}"]


def ended_long_ago(run_folder, began, days):
    """A closed run goes `days` days after its close. A run that was never closed can still have a session that
    writes to it, and a folder that never got its contract can belong to a start that is under way: both stay
    for 14 days, whatever `days` says."""
    keep_until = now() - datetime.timedelta(days=KEEP_DAYS)
    if not os.path.lexists(run_folder / CONTRACT):
        return began <= keep_until
    run = Run(run_folder)
    closes = [moment_of(line["t"]) for line in run.log if line["kind"] == "closed"]
    return closes[-1] <= now() - datetime.timedelta(days=days) if closes else run.ends <= keep_until


def remove_run(run_folder, began, days):
    """Take one run out of its session and remove it. Whether it was taken out is the answer. The run is held
    as `log` and `close` hold it, and read again: a command that writes to it now keeps it. Then the folder gets
    another name, in one step, so a command that reads at the same time sees the whole run or no run. A run of
    which no file can be removed gets its name back."""
    held = os.open(run_folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if not ended_long_ago(run_folder, began, days):
            return False
        left = run_folder.with_name(run_folder.name + GONE)
        os.rename(run_folder, left)
    finally:
        os.close(held)
    before = None
    try:
        before = sorted(os.listdir(left))
        shutil.rmtree(left)
    except FileNotFoundError:
        return True             # another prune removed it under its new name
    except OSError:
        try:
            after = sorted(os.listdir(left))
        except OSError:
            after = None
        if after != before:
            return True         # a part is gone, so this is no run any more: the next prune removes the rest
        os.rename(left, run_folder)
        raise
    return True


def remove_left(folder):
    """Remove what an earlier prune left: a real folder whose name is a start time and the ending of a prune.
    Whether one was removed is the answer."""
    removed = False
    for name in os.listdir(folder):
        try:
            if not name.endswith(GONE):
                continue
            datetime.datetime.strptime(name[:-len(GONE)], STAMP)
            if stat.S_ISDIR(os.lstat(folder / name).st_mode):
                shutil.rmtree(folder / name)
                removed = True
        except (ValueError, OSError):
            continue
    return removed


def prune_session(folder, days):
    """Remove the runs of one session that ended long ago (see `ended_long_ago`), and return how many. A run that
    cannot be read, or cannot be removed, is left alone."""
    removed = 0
    emptied = remove_left(folder)
    for run_folder, began in stamped(folder):
        try:
            if ended_long_ago(run_folder, began, days) and remove_run(run_folder, began, days):
                removed += 1
        except (Failed, OSError):
            continue
    if removed or emptied:
        with contextlib.suppress(OSError):
            os.rmdir(folder)    # only a folder that this call emptied, and only when a start has put no run in it
    return removed


def prune_runs(days):
    """Prune every session folder of the root. A fault in one of them stops at that folder."""
    top = root()
    if not plain_folder(top, make=False):
        return 0
    removed = 0
    for name in os.listdir(top):
        try:
            folder = top / name
            if SESSION.fullmatch(name) and stat.S_ISDIR(os.lstat(folder).st_mode):
                removed += prune_session(folder, days)
        except (Failed, OSError):
            continue
    return removed


def prune(args):
    if not 0 <= args.days <= MAX_DAYS:
        raise Failed(f"--days is 0 to {MAX_DAYS}")
    return 0, [f"unattended: pruned {prune_runs(args.days)}"]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failed(f"{message}. The first lines of the script have the commands and their options")


def main(argv=None):
    parser = Parser(prog="contract.py", add_help=False, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", parser_class=Parser)
    for name in ("preflight", "start"):
        one = sub.add_parser(name, add_help=False, allow_abbrev=False)
        one.add_argument("--plan", required=True)
        if name == "preflight":
            one.add_argument("--project")
    one = sub.add_parser("log", add_help=False, allow_abbrev=False)
    one.add_argument("--kind", required=True, choices=KINDS)
    one.add_argument("--task")
    one.add_argument("--text", required=True)
    one.add_argument("--needs")
    sub.add_parser("status", add_help=False, allow_abbrev=False)
    one = sub.add_parser("close", add_help=False, allow_abbrev=False)
    one.add_argument("--why")
    one = sub.add_parser("prune", add_help=False, allow_abbrev=False)
    one.add_argument("--days", type=int, default=KEEP_DAYS)
    commands = {"preflight": preflight, "start": start, "log": log, "status": status, "close": close, "prune": prune}
    try:
        args = parser.parse_args(argv)
        if args.command not in commands:
            raise Failed("no command: preflight, start, log, status, close or prune")
        code, lines = commands[args.command](args)
    except Failed as exc:
        code, lines = 2, [f"unattended: error {one_line(exc)}"]
    except OSError as exc:
        code, lines = 2, [f"unattended: error {one_line(exc.strerror or exc)}"]
    except Exception as exc:   # a fault of this script is an error line too, never a traceback in place of a result
        code, lines = 2, [f"unattended: error {type(exc).__name__}: {one_line(exc)}"]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")   # a terminal that cannot show a letter gets it escaped
    for line in lines:
        print(shown(line), flush=True)   # a log or a plan cannot put a second line, or a result line, into the output
    return code


if __name__ == "__main__":
    sys.exit(main())
