#!/usr/bin/env python3
"""drift: tell where a project's verify skill no longer fits the project.

  drift.py check --project <dir> [--app <name>] [--no-run]
  drift.py bump  --project <dir> [--app <name>]

The verify skill is the folder <project>/.claude/skills/verify-<app>/ with
`verify` (a copy of the runner), `features.json` (the feature map) and
`SKILL.md`. With one such folder it is found; with more, --app names it.

check prints one line per finding, then a last line:

  stale-path <feature> <pattern>             a pattern of `paths` that matches
                                             no file that git tracks
  uncovered-path <folder>/ <n>               n tracked files in that folder that
                                             no pattern matches, in a top folder
                                             that some pattern reaches into
  failed-step <feature> step <n>: <reason>   a step of the runner's run that
                                             failed or could not start
  failed-build <reason>                      the runner's build failed
  runner-old <what differs>                  the runner copy is not the current
                                             runner byte for byte; "unknown
                                             runner" when there is no current
                                             runner beside this script

--no-run leaves out the runner's run and so the failed-* findings.

A pattern matches a path as fnmatch does, case-sensitive: "*" takes any run
of characters, "/" too, so "src/**" and "src/*" both take every file under
src/. The current runner is ../../create-verify-skill/scripts/verify from
this file, after links are resolved.

bump replaces the runner copy with the current runner. It refuses when the
map's `runner` number is not the RUNNER number of the current runner, which
it reads from that file's text and never by running it. It writes a new file
beside the copy and renames it over the copy, so it never writes through a
link, and it keeps the copy's mode, executable.

Read the exit code and the last line together:

  exit 0   "drift: clean"  or  "drift: bumped <path>"
  exit 1   "drift: summary stale=N uncovered=N failed=N"   (+ " runner=old" or
           " runner=unknown")
  exit 2   "drift: error ..."   it could not finish; this is not a result
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CURRENT = HERE.parent.parent / "create-verify-skill" / "scripts" / "verify"
APP = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
RUNNER_LINE = re.compile(r"^RUNNER = (0|[1-9][0-9]*)$", re.M)
STEP_LINE = re.compile(r"(fail|unverified) (\S+) step ([1-9][0-9]*): (.+)\Z")
BUILD_LINE = re.compile(r"fail build: (.+)\Z")
TMP_NAME = "verify.drift-tmp"
GIT_TIMEOUT_S = 60
RUN_TIMEOUT_S = 3 * 3600  # the runner bounds each step and the build; this only stops a runner that hangs


class Failed(Exception):
    """drift could not finish."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failed(message)


def clean_text(text):
    """A name from a file or from git cannot start a new line, or move the cursor."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text))


def say(text):
    print(clean_text(text), flush=True)


# ── the skill and its map ────────────────────────────────────────────────────

def find_skill(project, app):
    skills = project / ".claude" / "skills"
    if app is not None:
        if not APP.match(app):
            raise Failed("the app name must be lower-case letters, digits and '-'")
        folder = skills / f"verify-{app}"
        if folder.is_symlink() or not folder.is_dir():
            raise Failed(f"no skill folder verify-{app}")
        return folder
    try:
        found = sorted(entry for entry in skills.iterdir()
                       if entry.name.startswith("verify-") and entry.is_dir() and not entry.is_symlink())
    except OSError:
        found = []
    if not found:
        raise Failed("no verify skill in .claude/skills/")
    if len(found) > 1:
        raise Failed(f"two or more verify skills ({', '.join(entry.name for entry in found)}): name one with --app")
    return found[0]


def load_map(skill):
    try:
        raw = json.loads((skill / "features.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise Failed(f"cannot read features.json: {exc.strerror}")
    except ValueError as exc:
        raise Failed(f"features.json is not JSON: {exc}")
    if not isinstance(raw, dict):
        raise Failed("the map is not an object")
    features = raw.get("features")
    if not isinstance(features, list):
        raise Failed("the map has no list of features")
    out = []
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("id"), str) or not feature["id"]:
            raise Failed("a feature has no 'id'")
        paths = feature.get("paths")
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise Failed(f"'paths' of {feature['id']} must be a list of non-empty strings")
        out.append((feature["id"], paths))
    return raw, out


def runner_number(text):
    """The RUNNER number in a runner's text, or None when there is not exactly one."""
    found = RUNNER_LINE.findall(text)
    return int(found[0]) if len(found) == 1 else None


# ── the paths ────────────────────────────────────────────────────────────────

def tracked_files(project):
    """The files that git tracks in the project. The call only reads: the GIT_ variables of the caller
    are dropped, so they cannot aim git at another repo, and a program that the repo's settings name
    for watching files is not started."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        done = subprocess.run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(project), "ls-files", "-z"],
                              capture_output=True, env=env, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode != 0:
        raise Failed("git cannot list the tracked files of the project")
    return [os.fsdecode(name) for name in done.stdout.split(b"\0") if name]


def matches(pattern, path):
    return fnmatch.fnmatchcase(path, pattern)


def path_findings(features, files):
    """(stale, uncovered): stale is [(feature, pattern)] in map order; uncovered is
    [(folder + "/", count)] sorted by folder."""
    stale = [(fid, pattern) for fid, patterns in features for pattern in patterns
             if not any(matches(pattern, path) for path in files)]
    every = [pattern for _, patterns in features for pattern in patterns]
    heads = {pattern.split("/", 1)[0] for pattern in every if "/" in pattern}
    counts = {}
    for path in files:
        if "/" not in path:
            continue
        if not any(matches(head, path.split("/", 1)[0]) for head in heads):
            continue
        if any(matches(pattern, path) for pattern in every):
            continue
        folder = path.rsplit("/", 1)[0] + "/"
        counts[folder] = counts.get(folder, 0) + 1
    return stale, sorted(counts.items())


# ── the runner's run ─────────────────────────────────────────────────────────

def run_findings(project, skill, app):
    try:
        done = subprocess.run([sys.executable, str(skill / "verify"), "run"], cwd=str(project),
                              capture_output=True, timeout=RUN_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Failed(f"the runner did not end within {RUN_TIMEOUT_S} s")
    except OSError as exc:
        raise Failed(f"the runner could not start: {exc.strerror or exc}")
    lines = done.stdout.decode("utf-8", errors="replace").splitlines()
    last = lines[-1] if lines else ""
    tag = re.escape(f"verify-{app}") if isinstance(app, str) else None
    is_pass = bool(tag) and re.match(tag + r": pass \S", last) is not None
    is_summary = bool(tag) and re.match(tag + r": summary \S", last) is not None
    if done.returncode == 0 and is_pass:
        return []
    if done.returncode != 1 or not is_summary:
        raise Failed(f"the runner ended with exit {done.returncode}: {last or '(no output)'}")
    found = []
    for line in lines[:-1]:
        step = STEP_LINE.match(line)
        build = BUILD_LINE.match(line)
        if step:
            found.append(f"failed-step {step.group(2)} step {step.group(3)}: {step.group(4)}")
        elif build:
            found.append(f"failed-build {build.group(1)}")
    if not found:
        raise Failed(f"the runner gave a summary with no failed step: {last}")
    return found


# ── the runner copy ──────────────────────────────────────────────────────────

def read_current():
    """The current runner's bytes, or None when it is not there."""
    try:
        return CURRENT.read_bytes() if CURRENT.is_file() else None
    except OSError:
        return None


def runner_finding(skill):
    """(line, state): state is None when the copy is current, else "old" or "unknown"."""
    current = read_current()
    if current is None:
        return "runner-old unknown runner", "unknown"
    copy_path = skill / "verify"
    now = runner_number(current.decode("utf-8", errors="replace"))
    now_text = f"current runner {now}" if now is not None else "current runner names no runner number"
    if copy_path.is_symlink():
        return f"runner-old copy is a link, {now_text}", "old"
    try:
        copy = copy_path.read_bytes()
    except OSError:
        copy = None
    if copy == current:
        return None, None
    if copy is None:
        return f"runner-old no copy, {now_text}", "old"
    was = runner_number(copy.decode("utf-8", errors="replace"))
    if was is None:
        return f"runner-old copy names no runner number, {now_text}", "old"
    if was == now:
        return f"runner-old bytes differ, both runner {now}", "old"
    return f"runner-old copy runner {was}, {now_text}", "old"


# ── the commands ─────────────────────────────────────────────────────────────

def check(project, app, no_run):
    skill = find_skill(project, app)
    raw, features = load_map(skill)
    stale, uncovered = path_findings(features, tracked_files(project))
    failed = [] if no_run else run_findings(project, skill, raw.get("app"))
    runner_line, runner_state = runner_finding(skill)
    lines = [f"stale-path {fid} {pattern}" for fid, pattern in stale]
    lines += [f"uncovered-path {folder} {count}" for folder, count in uncovered]
    lines += failed
    if runner_line:
        lines.append(runner_line)
    if not lines:
        return 0, ["drift: clean"]
    summary = f"drift: summary stale={len(stale)} uncovered={len(uncovered)} failed={len(failed)}"
    if runner_state:
        summary += f" runner={runner_state}"
    return 1, lines + [summary]


def write_executable(target, data):
    """Replace target with data: a new file beside it, then a rename. Never through a link."""
    try:
        info = os.lstat(target)
        mode = info.st_mode & 0o777 if not os.path.islink(target) and os.path.isfile(target) else 0o755
    except FileNotFoundError:
        mode = 0o755
    mode |= (mode & 0o444) >> 2  # execute wherever it may be read
    mode |= 0o100
    partial = target.with_name(TMP_NAME)
    if os.path.lexists(partial):
        os.unlink(partial)  # a link or a file left there: removed, never opened
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(partial, target)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise


def bump(project, app):
    skill = find_skill(project, app)
    raw, _ = load_map(skill)
    current = read_current()
    if current is None:
        raise Failed(f"no current runner at {CURRENT}")
    now = runner_number(current.decode("utf-8", errors="replace"))
    if now is None:
        raise Failed("the current runner does not name exactly one RUNNER number")
    wanted = raw.get("runner")
    if not isinstance(wanted, int) or isinstance(wanted, bool):
        raise Failed("the map has no whole-number 'runner'")
    if wanted != now:
        raise Failed(f"the map is for runner {wanted}, the current runner is {now}; the map must change first")
    target = skill / "verify"
    try:
        same = not target.is_symlink() and target.is_file() and target.read_bytes() == current
    except OSError:
        same = False
    if same:
        return 0, ["drift: clean"]
    try:
        write_executable(target, current)
    except OSError as exc:
        raise Failed(f"cannot write {target}: {exc.strerror or exc}")
    return 0, [f"drift: bumped {target}"]


def main(argv=None):
    parser = Parser(prog="drift.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    check_parser = commands.add_parser("check")
    bump_parser = commands.add_parser("bump")
    for sub in (check_parser, bump_parser):
        sub.add_argument("--project", required=True, type=Path)
        sub.add_argument("--app")
    check_parser.add_argument("--no-run", action="store_true")
    try:
        args = parser.parse_args(argv)
        project = args.project.resolve()
        if not project.is_dir():
            raise Failed(f"no such folder: {project}")
        if args.command == "check":
            code, lines = check(project, args.app, args.no_run)
        else:
            code, lines = bump(project, args.app)
    except Failed as exc:
        code, lines = 2, [f"drift: error {exc}"]
    except Exception as exc:  # anything else: drift did not finish, and that must not read as a result
        code, lines = 2, [f"drift: error {type(exc).__name__}: {exc}"]
    for line in lines:
        say(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
