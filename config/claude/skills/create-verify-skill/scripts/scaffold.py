#!/usr/bin/env python3
"""Write the frame of a verify skill into a project.

  scaffold.py --project <folder> --app <name>

It makes <project>/.claude/skills/verify-<app>/ with three files:

  verify          the runner, a copy of the file beside this one
  features.json   a feature map with no features yet; you write them
  SKILL.md        how to run it, from templates/SKILL.md.tmpl

In a git repo it also adds `.verify/` to .gitignore, because the runner keeps
its evidence there and refuses to run when git does not ignore it.

It never changes a skill folder that exists.

  exit 0   "create-verify-skill: wrote <folder>"
  exit 2   "create-verify-skill: error ..."     nothing was written
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "verify"
TEMPLATE = HERE.parent / "templates" / "SKILL.md.tmpl"
APP = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


class Refused(Exception):
    pass


def runner_version():
    match = re.search(r"^RUNNER = (\d+)$", RUNNER.read_text(encoding="utf-8"), re.M)
    if not match:
        raise Refused("the runner beside this file names no version")
    return int(match.group(1))


def ignores_evidence(project):
    """True, False, or None when the folder is not in a git repo."""
    try:
        out = subprocess.run(["git", "-C", str(project), "check-ignore", "-q", ".verify/probe"],
                             capture_output=True, env=dict(os.environ, LC_ALL="C"), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(out.returncode)


def scaffold(project, app):
    if not APP.match(app):
        raise Refused("the app name must be lower-case letters, digits and '-'")
    if not project.is_dir():
        raise Refused(f"no such folder: {project}")
    dest = project / ".claude" / "skills" / f"verify-{app}"
    version = runner_version()
    text = TEMPLATE.read_text(encoding="utf-8").replace("__APP__", app)
    try:
        dest.mkdir(parents=True)
    except FileExistsError:
        raise Refused(f"{dest} exists; this command does not change it")
    try:
        shutil.copyfile(RUNNER, dest / "verify")
        (dest / "verify").chmod(0o755)
        (dest / "features.json").write_text(
            json.dumps({"app": app, "runner": version, "features": []}, indent=2) + "\n", encoding="utf-8")
        (dest / "SKILL.md").write_text(text, encoding="utf-8")
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)  # the folder is new: this command made it a moment ago
        raise
    notes = []
    if ignores_evidence(project) is False:
        ignore = project / ".gitignore"
        old = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        ignore.write_text(old + ("" if old.endswith("\n") or not old else "\n") + ".verify/\n", encoding="utf-8")
        notes.append("added .verify/ to .gitignore")
    return dest, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--app", required=True)
    args = parser.parse_args(argv)
    try:
        dest, notes = scaffold(args.project.resolve(), args.app)
    except Refused as exc:
        print(f"create-verify-skill: error {exc}".replace("\n", " "))
        return 2
    except OSError as exc:
        print(f"create-verify-skill: error {exc.strerror or exc}".replace("\n", " "))
        return 2
    for note in notes:
        print(note)
    print(f"create-verify-skill: wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
