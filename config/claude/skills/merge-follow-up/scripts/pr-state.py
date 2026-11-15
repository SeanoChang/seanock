#!/usr/bin/env python3
"""pr-state: what changed on your pull requests since this session last looked.

  pr-state.py --repo <dir> [--session <id>]                 print the new events
  pr-state.py --repo <dir> [--session <id>] --claim <name>  take a step that must run once

It only reads: `gh repo view`, `gh pr list` and a GET through `gh api`. It
runs no merge, no push and no git command, and it refuses to call `gh` with
anything else.

Each session has its own cursor in the state file, so two sessions that watch
one repo both get each event, once. The first look of a session records what
is there and prints no event. A step that must run once for all sessions,
such as a reinstall, is taken with --claim: the first session gets it.

Read the exit code and the last line together:

  exit 0   "pr-state: clean"              nothing new        (--claim: "pr-state: claimed <name>")
  exit 1   "pr-state: summary events=N"   N events, one JSON object per line above
                                          (--claim: "pr-state: summary taken <name>": another session has it)
  exit 2   "pr-state: error ..."          it could not finish; this says nothing about the pull requests

Events:

  merged        a pull request was merged. `sha` is the merge commit and
                `head_sha` the last commit of the branch. `release` is true when
                the base is a release branch; `parents` is the number of parents
                of the merge commit; `squashed_release` is true for a release
                merged with one parent, which leaves the two branches with no
                shared history.
  closed        a pull request was closed without a merge.
  checks-failed a check failed on the head commit of an open pull request.
  behind-base   an open pull request whose base branch just took a merge.

`checks-failed` and `behind-base` carry `shared_head`: true when the head of
the pull request is a branch that takes pull requests itself: a release
branch or an integration branch by name, the branch that $MERGE_GATE_BASE
names, or the base of a listed pull request. Names are compared without
regard to case. Such a branch is never rebased and never pushed to by an
agent. This script reads $MERGE_GATE_BASE and never sets it.

Branch names and commit ids come from GitHub, and an agent puts them into
commands. One that is not of the plain form (a name of letters, digits and
. _ / - ; a commit id of 40 hex digits) is given as null.

State: ~/.claude/state/pr-follow/<owner>__<repo>.json, changed under a file lock.
A cursor or a claim that is older than 30 days is dropped.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

RELEASE = {"main", "master", "trunk"}                         # the merge gate has the same names
INTEGRATION = {"dev", "develop", "development", "integration"}
FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
FIELDS = "number,state,baseRefName,headRefName,headRefOid,mergeCommit,statusCheckRollup,url"
STATE_DIR = Path.home() / ".claude" / "state" / "pr-follow"
KEEP_DAYS = 30
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
NWO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


class Failed(Exception):
    """The script could not finish."""


# ── gh, read-only ────────────────────────────────────────────────────────────

def allowed(args):
    """True for the three read calls this script makes, and for nothing else."""
    if args[:2] == ["repo", "view"] or args[:2] == ["pr", "list"]:
        return True
    return (len(args) == 4 and args[0] == "api" and args[2] == "--jq"
            and re.fullmatch(r"repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/commits/[0-9a-f]{40}", args[1]) is not None)


def gh(args, repo):
    if not allowed(args):
        raise Failed(f"refused to run gh {' '.join(args[:2])}: this script only reads")
    try:
        done = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        raise Failed("gh is not installed")
    except subprocess.TimeoutExpired:
        raise Failed(f"gh {' '.join(args[:2])} gave no answer in 90 seconds")
    if done.returncode != 0:
        reason = (done.stderr.strip().splitlines() or ["no message"])[-1]
        raise Failed(f"gh {' '.join(args[:2])} failed: {reason}")
    return done.stdout


def read_repo(repo):
    try:
        nwo = json.loads(gh(["repo", "view", "--json", "nameWithOwner"], repo))["nameWithOwner"]
    except (ValueError, KeyError, TypeError):
        raise Failed("gh repo view gave no repo name")
    if not isinstance(nwo, str) or not NWO.match(nwo):
        raise Failed("gh repo view gave a repo name this script cannot use")
    return nwo


def read_prs(repo):
    """The pull requests of the signed-in user in this repo, newest first, at most 100."""
    out = gh(["pr", "list", "--author", "@me", "--state", "all", "--limit", "100", "--json", FIELDS], repo)
    try:
        prs = json.loads(out)
    except ValueError:
        raise Failed("gh pr list gave no JSON")
    if not isinstance(prs, list) or not all(isinstance(pr, dict) and isinstance(pr.get("number"), int) for pr in prs):
        raise Failed("gh pr list gave a shape this script does not know")
    return prs


def parents_of(nwo, sha, repo):
    out = gh(["api", f"repos/{nwo}/commits/{sha}", "--jq", ".parents | length"], repo).strip()
    if not out.isdigit():
        raise Failed("gh api gave no parent count")
    return int(out)


# ── events ───────────────────────────────────────────────────────────────────

def failed_checks(pr):
    """The names of the checks that failed on the head commit, sorted."""
    names = set()
    for check in pr.get("statusCheckRollup") or []:
        if isinstance(check, dict) and (check.get("conclusion") or check.get("state") or "").upper() in FAILED:
            names.add(str(check.get("name") or check.get("context") or "check"))
    return sorted(names)


def is_release(name):
    """True for a release branch, compared as the merge gate compares it."""
    return isinstance(name, str) and name.casefold() in RELEASE


def ref(name):
    """A branch name that is safe to put into a command, else None."""
    return name if isinstance(name, str) and REF.match(name) and ".." not in name else None


def commit(sha):
    """A full commit id, else None."""
    return sha if isinstance(sha, str) and SHA.match(sha) else None


def snapshot(pr):
    """What the cursor keeps for one pull request."""
    state = pr.get("state")
    failed = pr.get("headRefOid") if state == "OPEN" and failed_checks(pr) else None
    return {"state": state, "failed_sha": failed}


def events(prs, seen, parents, also_shared=()):
    """The events between a cursor and the pull requests as they are now.

    seen: {number as text: snapshot} from this session's last look.
    parents: a function from a merge commit to its number of parents; it is
    called once for each newly merged pull request.
    also_shared: more names of branches that take pull requests.
    A pull request that the cursor does not hold was opened after that look,
    so its merge or its close is news too. (The first look of a session has
    no cursor and never comes here: it records what is there.)
    """
    found = []
    merged_bases = {}
    takes = {name.casefold() for name in [*RELEASE, *INTEGRATION, *also_shared, *(pr.get("baseRefName") for pr in prs)]
             if isinstance(name, str)}

    def shared(head):
        """True when the head takes pull requests itself, so no agent rebases it or pushes to it.

        A head with no name counts as shared: what is not known is not an agent's to push.
        """
        return not (isinstance(head, str) and head) or head.casefold() in takes

    for pr in sorted(prs, key=lambda pr: pr["number"]):
        number, state = pr["number"], pr.get("state")
        base, head = pr.get("baseRefName"), pr.get("headRefName")
        before = seen.get(str(number)) or {}
        if state == "MERGED" and before.get("state") != "MERGED":
            sha = commit((pr.get("mergeCommit") or {}).get("oid"))
            count = parents(sha) if sha else None
            release = is_release(base)
            found.append({"kind": "merged", "pr": number, "base": ref(base), "head": ref(head),
                          "sha": sha, "head_sha": commit(pr.get("headRefOid")), "release": release, "parents": count,
                          "squashed_release": release and count == 1, "url": pr.get("url")})
            merged_bases.setdefault(base, number)
        elif state == "CLOSED" and before.get("state") != "CLOSED":
            found.append({"kind": "closed", "pr": number, "base": ref(base), "head": ref(head), "url": pr.get("url")})
        elif state == "OPEN":
            names = failed_checks(pr)
            if names and before.get("failed_sha") != pr.get("headRefOid"):
                found.append({"kind": "checks-failed", "pr": number, "head": ref(head), "shared_head": shared(head),
                              "sha": commit(pr.get("headRefOid")), "checks": names, "url": pr.get("url")})
    for pr in sorted(prs, key=lambda pr: pr["number"]):
        base, head = pr.get("baseRefName"), pr.get("headRefName")
        if pr.get("state") == "OPEN" and base in merged_bases:
            found.append({"kind": "behind-base", "pr": pr["number"], "base": ref(base), "head": ref(head),
                          "shared_head": shared(head), "because": merged_bases[base], "url": pr.get("url")})
    return found


# ── the state file ───────────────────────────────────────────────────────────

def now():
    return datetime.datetime.now(datetime.timezone.utc)


class State:
    """The state file of one repo, held under a file lock from enter to exit."""

    def __init__(self, state_dir, nwo):
        self.path = Path(state_dir) / (nwo.replace("/", "__") + ".json")
        self.data = None
        self.lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:  # never through a link, and never emptied: the lock file is only a thing to hold
            self.lock = os.open(self.path.with_name(self.path.name + ".lock"),
                                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise Failed(f"the lock file cannot be opened: {self.path.name}.lock")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            raise Failed(f"the state file cannot be read: {self.path}")
        if not isinstance(data, dict) or not isinstance(data.get("sessions", {}), dict) \
                or not isinstance(data.get("claims", {}), dict):
            raise Failed(f"the state file has a shape this script does not know: {self.path}")
        self.data = {"version": 1, "sessions": data.get("sessions", {}), "claims": data.get("claims", {})}
        return self

    def save(self):
        partial = self.path.with_name(self.path.name + ".part")
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
        with open(partial, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(self.data, indent=2) + "\n")
        os.replace(partial, self.path)

    def __exit__(self, *exc):
        os.close(self.lock)  # closing the file gives the lock back
        return False


def forget_old(entries, key, today):
    """Drop each entry (a session's cursor, a claim) whose time under `key` is older than KEEP_DAYS days."""
    limit = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    for name in [name for name, entry in entries.items()
                 if not isinstance(entry, dict) or str(entry.get(key, "")) < limit]:
        del entries[name]


# ── the command line ─────────────────────────────────────────────────────────

def say(text):
    print("".join(ch if ch.isprintable() else " " for ch in str(text)), flush=True)


def look(repo, session, state_dir):
    nwo = read_repo(repo)
    prs = read_prs(repo)
    with State(state_dir, nwo) as state:
        sessions = state.data["sessions"]
        cursor = sessions.get(session)
        first = not isinstance(cursor, dict) or not isinstance(cursor.get("prs"), dict)
        found = [] if first else events(prs, cursor["prs"], lambda sha: parents_of(nwo, sha, repo),
                                        also_shared=[os.environ.get("MERGE_GATE_BASE", "")])
        forget_old(sessions, "looked", now())
        forget_old(state.data["claims"], "at", now())
        sessions[session] = {"looked": now().isoformat(timespec="seconds"),
                             "prs": {str(pr["number"]): snapshot(pr) for pr in prs}}
        state.save()
    for event in found:
        say(json.dumps(event, sort_keys=True))
    say(f"pr-state: summary events={len(found)}" if found else "pr-state: clean")
    return 1 if found else 0


def claim(repo, session, state_dir, name):
    if not NAME.match(name):
        raise Failed("a claim name is letters, digits and . _ : - , at most 100")
    nwo = read_repo(repo)
    with State(state_dir, nwo) as state:
        holder = state.data["claims"].get(name)
        if isinstance(holder, dict) and holder.get("by") != session:
            say(f"pr-state: summary taken {name} by {holder.get('by')}")
            return 1
        if not isinstance(holder, dict):
            state.data["claims"][name] = {"by": session, "at": now().isoformat(timespec="seconds")}
            state.save()
    say(f"pr-state: claimed {name}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    parser.add_argument("--claim", metavar="NAME")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not NAME.match(args.session):
            raise Failed("no session name: pass --session <id> (letters, digits and . _ : -)")
        if not args.repo.is_dir():
            raise Failed(f"no such folder: {args.repo}")
        if args.claim is not None:
            return claim(args.repo, args.session, args.state_dir, args.claim)
        return look(args.repo, args.session, args.state_dir)
    except Failed as exc:
        say(f"pr-state: error {exc}")
        return 2
    except Exception as exc:  # anything else: it did not finish, and that must not read as "nothing new"
        say(f"pr-state: error {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
