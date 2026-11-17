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
import datetime
import json
import os
import re
import shutil
import stat
import sys
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
MAX_TEXT = 2000
SEPARATORS = ("&&", "||", ";", "|", "&", "\n", "$(", "`", ">", "<")


class Failed(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def moment_of(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def one_line(text):
    """Text for one log field or one output line: no line break, no control character, a fixed length at most."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


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
    """The folder, with no link accepted in its place. With `make`, it is made for this user alone."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        if not make:
            return False
        os.mkdir(path, 0o700)
        return True
    if not stat.S_ISDIR(mode):
        raise Failed(f"{path} is no plain folder")
    return True


def session_folder(session, make):
    top = root()
    if make:
        top.parent.mkdir(parents=True, exist_ok=True)
    if not plain_folder(top, make):
        return None
    folder = top / session
    return folder if plain_folder(folder, make) else None


def runs_of(folder):
    """The run folders of one session, the oldest first. A name that is no start time is not a run."""
    names = []
    for name in os.listdir(folder):
        try:
            datetime.datetime.strptime(name, STAMP)
        except ValueError:
            continue
        if stat.S_ISDIR(os.lstat(folder / name).st_mode):
            names.append(name)
    return [folder / name for name in sorted(names)]


def read_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Failed(f"{path} is no plain file")
        return handle.read().decode("utf-8")


class Run:
    """One run: the contract as it was written, and the lines of the log."""

    def __init__(self, folder):
        self.folder = folder
        try:
            self.contract = json.loads(read_file(folder / CONTRACT))
            lines = read_file(folder / LOG).splitlines() if (folder / LOG).exists() else []
            self.log = [json.loads(line) for line in lines if line.strip()]
            for line in self.log:
                if not isinstance(line, dict) or not isinstance(line.get("kind"), str):
                    raise ValueError("a log line is no object with a kind")
                moment_of(line["t"])
            self.tasks = [(str(task["id"]), str(task["title"]), str(task["check"])) for task in self.contract["tasks"]]
            self.ends = moment_of(self.contract["ends"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Failed(f"cannot read the run in {folder}: {exc}")

    def closed(self):
        return any(line.get("kind") == "closed" for line in self.log)

    def state_of(self, task):
        """done, skipped or left: what the last done or skipped line of the task says."""
        state = "left"
        for line in self.log:
            if line.get("task") == task and line.get("kind") in ("done", "skipped"):
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


def open_run(session):
    """The run of this session that is not closed, or None."""
    folder = session_folder(session, make=False)
    if folder is None:
        return None
    runs = runs_of(folder)
    if not runs:
        return None
    run = Run(runs[-1])
    return None if run.closed() else run


# ── the plan ─────────────────────────────────────────────────────────────────

def read_plan(path):
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
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
            if not isinstance(value, str) or not value.strip():
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
    pattern = ".*".join(re.escape(part) for part in rule.split("*"))
    return re.fullmatch(pattern, command, re.DOTALL) is not None


def stops(rule, command):
    """Whether a deny or ask rule covers the command. `prefix:*` is read in both senses that it has had, as
    `prefix *` and as any command that starts with the prefix: a rule that stops a command must not be read too
    narrowly."""
    return rule_matches(rule, command) or (rule.endswith(":*") and command.startswith(rule[:-2]))


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


def judge(command, settings_list):
    """allowed, would-prompt, unsure or denied, and the rule that says so. Only `allowed` is a pass. A command
    with a shell operator is unsure: Claude Code judges each part of it on its own, and this script does not
    split commands."""
    for name, verdict in (("deny", "denied"), ("ask", "would-prompt")):
        for settings in settings_list:
            whole, patterns = bash_rules(settings, name)
            hit = "Bash" if whole else next((p for p in patterns if stops(p, command)), None)
            if hit:
                return verdict, f"{name}: {hit}"
    if any(mark in command for mark in SEPARATORS) or not command.isprintable():
        return "unsure", "it has a shell operator, a line break or a control character; write each part as its own command"
    for settings in settings_list:
        whole, patterns = bash_rules(settings, "allow")
        hit = "Bash" if whole else next((p for p in patterns if rule_matches(p, command)), None)
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
        except (OSError, ValueError) as exc:
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
        lines.append(f"{verdict} {command}  [{why}]")
    summary = " ".join(f"{name}={count}" for name, count in counts.items())
    return (0 if counts["allowed"] == len(commands) else 1), lines + [f"unattended: preflight {summary}"]


# ── the commands ─────────────────────────────────────────────────────────────

def start(args):
    session = session_id()
    tasks, approvals, hours = checked_plan(read_plan(args.plan))
    if open_run(session) is not None:
        raise Failed("this session has an open run already: close it first")
    prune_runs(KEEP_DAYS)
    folder = session_folder(session, make=True)
    began = now()
    run_folder = folder / began.strftime(STAMP)
    os.mkdir(run_folder, 0o700)
    ends = began + datetime.timedelta(hours=hours)
    contract = {
        "version": 1, "session": session, "started": iso(began), "ends": iso(ends), "cwd": os.getcwd(),
        "tasks": [{"id": str(number), "title": title, "check": check} for number, (title, check) in enumerate(tasks, 1)],
        "approvals": [{"what": what, "answer": answer} for what, answer in approvals],
    }
    fd = os.open(run_folder / CONTRACT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(contract, ensure_ascii=False, indent=2) + "\n")
    Run(run_folder).add("started")
    return 0, [f"run folder: {run_folder}", f"unattended: started tasks={len(tasks)} ends={iso(ends)}"]


def the_open_run():
    run = open_run(session_id())
    if run is None:
        raise Failed("this session has no open run")
    return run


def log(args):
    run = the_open_run()
    if args.kind in ("skipped", "done") or args.task is not None:
        if args.task not in [task[0] for task in run.tasks]:
            raise Failed(f"the contract has no task `{args.task}`")
    if args.kind == "skipped" and not (args.needs or "").strip():
        raise Failed("a skipped step says what it needs from the user: add --needs")
    if not args.text.strip():
        raise Failed("--text is empty")
    run.add(args.kind, task=args.task, text=args.text, needs=args.needs if args.kind == "skipped" else None)
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
        if line.get("kind") in ("done", "skipped") and line.get("task") in title:
            last[line["task"]] = line
    blocked = [f"- Task {task} ({title[task]}): {line.get('text', '')} Needs: {line.get('needs', '')}"
               for task, line in last.items() if line["kind"] == "skipped"]
    changed = [f"- Task {task} ({title[task]}): {line.get('text', '')}"
               for task, line in last.items() if line["kind"] == "done"]
    found = [f"- {line.get('text', '')}" for line in run.log if line.get("kind") == "decision"]
    left = [f"- Task {task} ({name}): not done." for task, name, _ in run.left()]
    count = {kind: sum(1 for line in run.log if line.get("kind") == kind) for kind in ("heartbeat", "refused")}
    out = ["## Blocked on me"] + (blocked + left or ["- Nothing."])
    out += ["", "## Changed"] + (changed or ["- Nothing."])
    out += ["", "## Found"] + (found or ["- Nothing."])
    out += ["", f"Run: started {run.contract['started']}, time limit {run.contract['ends']}, "
                f"{count['heartbeat']} heartbeat lines, {count['refused']} stops refused. Log: {run.folder / LOG}"]
    return out


def close(args):
    run = the_open_run()
    left = run.left()
    why = (args.why or "").strip()
    if left and not run.late() and not why:
        lines = [f"left {task} {title}" for task, title, _ in left]
        return 1, lines + [f"unattended: summary left={len(left)}"]
    run.add("closed", text=why or ("the time limit is reached" if left else "every task is done or skipped"))
    states = [run.state_of(task) for task, _, _ in run.tasks]
    counts = " ".join(f"{name}={states.count(name)}" for name in ("done", "skipped", "left"))
    return 0, report(run) + [f"unattended: closed {counts}"]


def prune_runs(days):
    """Remove the runs that ended long ago, and return how many. A closed run goes `days` days after its close.
    A run that was never closed can still have a session that writes to it, so it stays for the full 14 days
    after its time limit, whatever `days` says. A run that cannot be read is left alone."""
    top = root()
    if not plain_folder(top, make=False):
        return 0
    limit = now() - datetime.timedelta(days=days)
    removed = 0
    for name in os.listdir(top):
        folder = top / name
        if not SESSION.fullmatch(name) or not stat.S_ISDIR(os.lstat(folder).st_mode):
            continue
        for run_folder in runs_of(folder):
            try:
                run = Run(run_folder)
            except Failed:
                continue
            closes = [moment_of(line["t"]) for line in run.log if line["kind"] == "closed"]
            if closes[-1] <= limit if closes else run.ends <= now() - datetime.timedelta(days=max(days, KEEP_DAYS)):
                shutil.rmtree(run_folder)
                removed += 1
        if not os.listdir(folder):
            os.rmdir(folder)
    return removed


def prune(args):
    if args.days < 0:
        raise Failed("--days is 0 or more")
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
    for line in lines:
        print(one_line(line), flush=True)   # a log or a plan cannot put a second line, or a result line, into the output
    return code


if __name__ == "__main__":
    sys.exit(main())
