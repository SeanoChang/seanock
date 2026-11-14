"""Claude Code adds no attribution to commits and pull request descriptions.

By default Claude Code tells each session to end a commit message with a
`Co-Authored-By` trailer and a pull request description with a "Generated
with" line. The rule "No AI attribution" in CLAUDE.md forbids both, but a rule
is text that a session can miss: the same correction was typed in 16 prompts
on 12 days. The settings key `attribution` removes the instruction at the
source, so there is nothing to miss.

The static tests pin the key in the settings template. The live test proves
what the key does, in a real session: with the template's value, a commit and
a pull request description carry no attribution line.

The live test starts three short sessions and costs model calls, so it runs
only on request:

    ATTRIBUTION_LIVE=1 python3 -m pytest tests/test_attribution.py -q -p no:cacheprovider -k live

ATTRIBUTION_LIVE_MODEL picks the model (default: haiku).

What the live test does not cover: a session in the cloud or under Remote
Control, where Claude Code also adds a session link (`sessionUrl`), and a
commit made by Codex, which reads the rule in AGENTS.md and not this key.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "config" / "claude" / "settings.json.tmpl"
RULES = ROOT / "config" / "claude" / "CLAUDE.md"

# Each pattern is one form of attribution that Claude Code is known to add.
ATTRIBUTION = [
    re.compile(r"^\s*Co-Authored-By:", re.I | re.M),
    re.compile(r"Generated with", re.I),
    re.compile(r"^\s*Claude-Session:", re.I | re.M),
    re.compile(r"claude\.ai/code", re.I),
    re.compile(r"claude\.com/claude-code", re.I),
    re.compile(r"noreply@anthropic\.com", re.I),
]


def attribution_lines(text):
    """The lines of `text` that hold a known form of attribution."""
    return [line for line in text.splitlines() if any(p.search(line) for p in ATTRIBUTION)]


def strict_json(text):
    """Parse as Claude Code does. Python also accepts NaN and Infinity, and
    JavaScript does not: a settings file with one of them is skipped whole."""
    def refuse(name):
        raise ValueError(f"{name} is not JSON for JavaScript")
    return json.loads(text, parse_constant=refuse)


def template():
    return strict_json(TEMPLATE.read_text(encoding="utf-8").replace("__HOME__", "/home/someone"))


# ------------------------------------------------------------------ the template

def test_the_template_turns_attribution_off():
    attribution = template().get("attribution")
    assert attribution == {"commit": "", "pr": "", "sessionUrl": False}
    # 0 == False in Python, and the docs give this key the type Boolean.
    assert attribution["sessionUrl"] is False


@pytest.mark.parametrize("bad", ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'])
def test_the_parser_refuses_what_javascript_refuses(bad):
    with pytest.raises(ValueError):
        strict_json(bad)


def test_the_template_uses_the_form_that_every_version_reads():
    """`"attribution": false` also hides everything, but a Claude Code older
    than 2.1.281 rejects it and then skips the whole settings file, and with it
    the deny rules and the hooks. The object form is read by every version that
    knows the key."""
    assert isinstance(template().get("attribution"), dict)


def test_the_deprecated_key_is_not_set():
    """Claude Code ignores `includeCoAuthoredBy` once `attribution` is set. A
    second key would only suggest that it still decides something."""
    assert "includeCoAuthoredBy" not in template()


def test_the_rule_stays_in_the_rules_file():
    """The key reaches Claude Code only. Codex and any other agent still learn
    the rule from the rules file."""
    assert "No AI attribution" in RULES.read_text(encoding="utf-8")


# ------------------------------------------------------------------ the detector

DEFAULT_COMMIT = "Add hello.txt\n\nCo-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>\n"
DEFAULT_PR = "## Summary\n\nAdds a file.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
CLOUD_COMMIT = "Add hello.txt\n\nClaude-Session: https://claude.ai/code/session_0123\n"


@pytest.mark.parametrize("text, found", [
    (DEFAULT_COMMIT, 1),
    (DEFAULT_PR, 1),
    (CLOUD_COMMIT, 1),
    ("Add hello.txt\n\nco-authored-by: Someone <a@b.c>\n", 1),
    ("Add hello.txt\n\nThe file holds one line.\n", 0),
    ("## Summary\n\nAdds `hello.txt`.\n\n## Testing\n\nNone needed.\n", 0),
    ("", 0),
])
def test_the_detector_finds_each_known_form(text, found):
    assert len(attribution_lines(text)) == found


# ------------------------------------------------------------------ end to end

LIVE = pytest.mark.skipif(not os.environ.get("ATTRIBUTION_LIVE"), reason="set ATTRIBUTION_LIVE=1 to run real sessions")

# The session gets the rules of no CLAUDE.md file and no user settings, so the
# attribution text is decided by `--settings` alone. Without that, the user's
# own "No AI attribution" rule would hide what the key does.
ENV_KEPT = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
)
# Git runs with this identity and with no config of the user or the system, for
# the test's own commands and for the session's. So a global hook, a signing
# program or a commit template of the caller's machine takes no part. No GIT_*
# variable of the caller goes along either: with GIT_DIR set (inside a git
# hook, say), the setup commit would land in the caller's repo.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Probe", "GIT_AUTHOR_EMAIL": "probe@example.com",
    "GIT_COMMITTER_NAME": "Probe", "GIT_COMMITTER_EMAIL": "probe@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
# The session has a shell, because the attribution instruction is part of the
# shell tool's git guidance: a session with no shell is never told to add it.
# The shell is its only tool, and it is allowed two commands, in a repo made
# for this test. The session has no tool that writes a file: with a bare Write
# rule it could write to any path on the disk. So it prints the pull request
# description in its answer, between two marker lines.
TOOLS = "Bash"
ALLOWED = ("Bash(git add:*)", "Bash(git commit:*)")
BODY_START, BODY_END = "PR-BODY-START", "PR-BODY-END"
PROMPT = (
    "The file hello.txt in this folder is new. Do two things, then stop. "
    "1. Commit it: run git add hello.txt, then one git commit with a message. "
    "2. Print the description of a pull request for this commit, exactly as you would pass it to gh pr create. "
    f"Print a line that says only {BODY_START} before it and a line that says only {BODY_END} after it. Do not run gh."
)
MARKER_COMMIT = "Probe-Trailer: commit-7f3a"
MARKER_PR = "Probe line pr-7f3a"


def session_argv(settings, model):
    argv = [
        "claude", "-p", PROMPT,
        "--strict-mcp-config", "--setting-sources", "project",
        "--tools", TOOLS,
        "--allowedTools", *ALLOWED,
        "--model", model,
        "--output-format", "text",
    ]
    if settings is not None:
        argv += ["--settings", json.dumps(settings)]
    return argv


def session_env():
    env = {name: os.environ[name] for name in ENV_KEPT if name in os.environ}
    env.update(GIT_ENV)
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    return env


def git(repo, *args):
    env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if name in os.environ}
    return subprocess.run(["git", *args], cwd=repo, env={**env, **GIT_ENV}, check=True, capture_output=True, text=True).stdout


def new_repo(folder):
    """A repo with one commit and one new, uncommitted file."""
    repo = Path(folder)
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("a repo for one test\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    (repo / "hello.txt").write_text("hello\n")
    return repo


def described(answer):
    """The pull request description in a session's answer: the text between
    the two marker lines."""
    found = re.search(rf"^\s*{BODY_START}\s*$(.*?)^\s*{BODY_END}\s*$", answer, flags=re.M | re.S)
    assert found, f"the answer holds no description between {BODY_START} and {BODY_END}:\n{answer[-2000:]}"
    return found.group(1).strip() + "\n"


def commit_and_describe(folder, settings):
    """Run one session in a new repo. Return the message of the commit it made
    and the pull request description it printed."""
    repo = new_repo(folder)
    done = subprocess.run(
        session_argv(settings, os.environ.get("ATTRIBUTION_LIVE_MODEL", "haiku")),
        cwd=repo, env=session_env(), capture_output=True, text=True, timeout=600,
    )
    assert done.returncode == 0, f"the session failed:\n{done.stderr[-2000:]}"
    count = int(git(repo, "rev-list", "--count", "HEAD"))
    assert count == 2, f"the session made {count - 1} commits, not one:\n{done.stdout[-2000:]}"
    return git(repo, "log", "-1", "--format=%B"), described(done.stdout)


@LIVE
def test_live_the_default_adds_attribution(tmp_path):
    """The control. If this fails, Claude Code no longer adds attribution by
    default (or the session did not get the git guidance), and the test of the
    template below proves nothing: look at both again."""
    assert shutil.which("claude"), "the claude CLI is not on PATH"
    message, body = commit_and_describe(tmp_path / "repo", None)
    assert attribution_lines(message), f"no attribution in the commit message:\n{message}"
    assert attribution_lines(body), f"no attribution in the pull request description:\n{body}"


@LIVE
def test_live_the_key_decides_the_text(tmp_path):
    """The session ends its commit and its description with the text of the
    key, so the key is what the session reads."""
    message, body = commit_and_describe(tmp_path / "repo", {"attribution": {"commit": MARKER_COMMIT, "pr": MARKER_PR}})
    assert MARKER_COMMIT in message, message
    assert MARKER_PR in body, body
    assert not attribution_lines(message) and not attribution_lines(body)


@LIVE
def test_live_the_template_leaves_no_attribution(tmp_path):
    message, body = commit_and_describe(tmp_path / "repo", {"attribution": template()["attribution"]})
    assert not attribution_lines(message), f"attribution in the commit message:\n{message}"
    assert not attribution_lines(body), f"attribution in the pull request description:\n{body}"


def test_the_live_session_gets_two_git_commands_and_no_user_rules(monkeypatch):
    """The shape of the live run, checked without a session: the shell is the
    only tool and is limited to `git add` and `git commit`, the user's
    settings, CLAUDE.md files and git config are left out, and no secret and
    no GIT_* variable of the caller's shell goes along."""
    argv = session_argv({"attribution": {"commit": "", "pr": "", "sessionUrl": False}}, "MODEL")
    assert argv[argv.index("--tools") + 1] == "Bash"
    start = argv.index("--allowedTools") + 1
    assert argv[start:argv.index("--model")] == ["Bash(git add:*)", "Bash(git commit:*)"]
    assert not any(word.startswith(("Write", "Edit")) for word in argv[3:]), "the session must have no tool that writes a file"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"attribution": {"commit": "", "pr": "", "sessionUrl": False}}
    assert "--settings" not in session_argv(None, "MODEL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-key")
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_INDEX_FILE", "/somewhere/else/index")
    env = session_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert set(env) <= set(ENV_KEPT) | set(GIT_ENV) | {"CLAUDE_CODE_DISABLE_CLAUDE_MDS"}


def test_the_setup_commit_stays_in_the_test_repo(tmp_path, monkeypatch):
    """With GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE set by the caller (a git
    hook sets them), the setup must still make its repo in the test folder and
    leave the caller's repo alone. A global config that signs commits or runs
    hooks must take no part."""
    victim = tmp_path / "victim"
    victim.mkdir()
    git(victim, "init", "-q", "-b", "main")
    (victim / "v.txt").write_text("the caller's file\n")
    git(victim, "add", ".")
    git(victim, "commit", "-q", "-m", "the caller's commit")
    head = git(victim, "rev-parse", "HEAD")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch '{tmp_path}/hook-ran'\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /usr/bin/false\n")
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(victim))
    monkeypatch.setenv("GIT_INDEX_FILE", str(victim / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    repo = new_repo(tmp_path / "repo")

    assert (repo / ".git").is_dir()
    assert git(repo, "log", "--format=%s").split() == ["start"]
    assert git(victim, "rev-parse", "HEAD") == head and git(victim, "status", "--porcelain") == ""
    assert not (tmp_path / "hook-ran").exists(), "a hook from the caller's global config ran"


@pytest.mark.parametrize("answer, body", [
    (f"Done.\n{BODY_START}\n## Summary\n\nAdds a file.\n{BODY_END}\nBye.", "## Summary\n\nAdds a file.\n"),
    (f"  {BODY_START}  \nOne line.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n{BODY_END}", "One line.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"),
])
def test_the_description_is_read_from_between_the_markers(answer, body):
    assert described(answer) == body


@pytest.mark.parametrize("answer", ["No markers at all.", f"{BODY_START}\nnever closed", f"text {BODY_START} inline {BODY_END} text"])
def test_an_answer_without_both_marker_lines_fails(answer):
    with pytest.raises(AssertionError):
        described(answer)
