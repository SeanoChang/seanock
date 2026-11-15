"""rules.py: the script behind the correct skill.

The tests run the script as a program on a real git repo in a temp folder, a
history file written by the test, and a rule table on disk. Every secret in
this file is made up, and is built at run time so that no scanner takes this
file for a leak.

Run: python3 -m pytest tests/test_rules_script.py -q -p no:cacheprovider
"""
import importlib.machinery
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "config" / "claude" / "skills" / "correct" / "scripts" / "rules.py"
PY = sys.executable
DAY = 86_400_000
T0 = 1_793_102_400_000  # 2026-10-27 12:00 UTC, in milliseconds


def made_up_secrets():
    """One made-up secret of each form that the script must hold back."""
    return {
        "vendor key": "sk-" + "a1B2c3D4" * 4,
        "vendor key with a prefix": "sk-ant-" + "Zz9" * 9,
        "github token": "ghp_" + "A1b2C3d4E5" * 3,
        "github fine token": "github_pat_" + "11ABCDEFG0_" * 3,
        "aws key id": "AKIA" + "Q7Z2M4K9P1X8W3V6",
        "slack token": "xoxb-" + "123456789012-abcdefABCDEF",
        "private key": "-----BEGIN OPENSSH PRIVATE KEY-----",
        "web token": "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4",
        "bearer": "Bearer " + "abcdefghij0123456789",
        "password assignment": "password=" + "hunter2hunter2",
        "password with a colon": "api_key: " + "'zzTop99zzTop99'",
        "url with a password": "https://sean:" + "pw12345" + "@example.invalid/x",
        "long hex": "0123456789abcdef" * 2,
        "long mixed run": "Qw3rTy" * 5,
        "long mixed run with signs": "Qw3r/Ty9+" * 3,
        # the same forms as letters only, so that only the rule for that form can catch them
        "vendor key, its own rule": "sk-" + "abcd-efgh-ijkl-mnop",
        "github token, its own rule": "ghp_" + "abcdefghijklmnopqrstuv",
        "github fine token, its own rule": "github_pat_" + "abcdefghi_" * 3,
        "aws key id, its own rule": "AKIA" + "QZMKPXWVBNHTRDLS",
        "slack token, its own rule": "xoxb-" + "abcdefghij-klmnop",
        "web token, its own rule": "eyJ" + "abcdefgh" + "." + "abcdefgh" + "." + "abcd",
        "bearer, its own rule": "Bearer " + "abcdefgh.ijklmnop",
        "uuid": "123e4567-e89b-42d3-a456-" + "426614174000",
        "long run of small letters and digits": "q7w8r9t0z1" * 2,
    }


def hostile_secrets():
    """Made-up secrets in the forms that slipped past a first version: each touches a character that
    hides a word boundary, or has a part left out, or is split by a break or an invisible character."""
    hex64 = "0123456789abcdef" * 4
    token = made_up_secrets()["github token"]
    mixed = "wJalrXUtnFEMIK7MDENG" + "bPxRfiCYEXAMPLEKEY12"
    return {
        "hex after 0x": ("0x" + hex64, [hex64]),
        "hex after a name and _": ("key_" + hex64, [hex64]),
        "hex before _ and a name": (hex64 + "_prod", [hex64]),
        "hex after a vendor prefix": ("dop_v1_" + hex64, [hex64]),
        "hex inside letters": ("zz" + hex64[:32] + "zz", [hex64[:32]]),
        "url with a password and no user": ("redis://:" + "hunter2x" + "@db.invalid:6379", ["hunter2x"]),
        "url with a token and no user": ("https://:" + "tok3nvalue" + "@git.example.invalid/x", ["tok3nvalue"]),
        "url with a slash in the password": ("https://sean:" + "ab/cd12" + "@host.invalid", ["ab/cd12"]),
        "password in escaped json": ('{\\"password\\": \\"' + "hunter2xyz" + '\\"}', ["hunter2xyz"]),
        "password in quotes with a space": ('password: "my ' + "hunter2 pass" + '"', ["hunter2 pass"]),
        "passphrase": ("passphrase: " + "hunter2xyz", ["hunter2xyz"]),
        "password as a flag": ("mysql --password " + "hunter2xyz", ["hunter2xyz"]),
        "token as a short flag": ("tool -token '" + "hunter2xyz" + "'", ["hunter2xyz"]),
        "password after a wide colon": ("password\uff1a" + "hunter2xyz", ["hunter2xyz"]),
        "token split by a line break": (token[:20] + "\n" + token[20:], [token[:20], token[20:]]),
        "token split by a wrapped line": (token[:20] + "\r\n    " + token[20:], [token[:20], token[20:]]),
        "token split by a zero-width space": (token[:20] + "\u200b" + token[20:], [token[:20], token[20:]]),
        "token split by a control character": (token[:20] + "\x01" + token[20:], [token[:20], token[20:]]),
        "token split by a bare carriage return": (token[:20] + "\r" + token[20:], [token[:20], token[20:]]),
        "token split by a unit separator": (token[:20] + "\x1f" + token[20:], [token[:20], token[20:]]),
        "mixed run split in two halves": (mixed[:20] + "\n" + mixed[20:], [mixed[:20], mixed[20:]]),
        # the printed line has a space where a character does not print: that form is checked too
        "flag and value joined by a control character": ("mysql --password" + "\x01" + "hunter2xyz", ["hunter2xyz"]),
        "bearer and token joined by a control character": ("Bearer" + "\x01" + "abcdefgh.ijklmnop", ["abcdefgh.ijklmnop"]),
        "many spaces before the equals sign": ("password" + " " * 12 + "= " + "hunter2xyz", ["hunter2xyz"]),
        "url with a very long password": ("https://sean:" + "pw-" * 100 + "@host.invalid", ["pw-pw-pw-"]),
        # a key word that touches other characters: the rule asks for no neighbour
        "key word inside a longer name": ("SECRET_" + "KEY=" + "hunter2xyz", ["hunter2xyz"]),
        "key word in the middle of a name": ("JWT_SECRET_" + "KEY: " + "hunter2xyz", ["hunter2xyz"]),
        "key word as an index": ('config["password"] = "' + "hunter2xyz" + '"', ["hunter2xyz"]),
        "key word in a call": ("os.environ['API_KEY'] = '" + "hunter2xyz" + "'", ["hunter2xyz"]),
        "key word in a code span": ("`token`: " + "hunter2xyz", ["hunter2xyz"]),
        "key word before a digit": ("PASSWORD1=" + "hunter2xyz", ["hunter2xyz"]),
        "key word before a bracket": ("password[0]=" + "hunter2xyz", ["hunter2xyz"]),
        "key word before a name": ("API_KEY_PROD=" + "hunter2xyz", ["hunter2xyz"]),
        "key word before a version": ("TOKEN_V2=" + "hunter2xyz", ["hunter2xyz"]),
        "key word, then words, then the value": ("the password for the db is: " + "hunter2xyz", ["hunter2xyz"]),
        "flag with a name before the key word": ("tool --db-password " + "hunter2xyz", ["hunter2xyz"]),
        "flag with two key words": ("tool --auth-token " + "hunter2xyz", ["hunter2xyz"]),
        "flag with a name after the key word": ("tool --password-file-value " + "hunter2xyz", ["hunter2xyz"]),
        "flag glued to a word": ("tool x--password " + "hunter2xyz", ["hunter2xyz"]),
        "flag in a list of arguments": ('["--password", "' + "hunter2xyz" + '"]', ["hunter2xyz"]),
        "flag and a value that starts with a dash": ("mysql --password " + "-hunter2xyz", ["hunter2xyz"]),
        "flag with a long dash": ("mysql \u2014password " + "hunter2xyz", ["hunter2xyz"]),
        "bearer in quotes": ('"Bearer", "' + "abcdefghijklmnopqrstuv" + '"', ["abcdefghijklmnopqrstuv"]),
        "bearer with an equals sign": ("bearer=" + "abcdefghijklmnopqrstuv", ["abcdefghijklmnopqrstuv"]),
    }


class World:
    def __init__(self, tmp_path):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.history = tmp_path / "history.jsonl"
        self.rows = []
        self.git("init", "-q", "-b", "main", ".")

    def git(self, *args):
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false", *args], cwd=self.repo, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def commit(self, subject, **files):
        for name, text in (files or {"notes.txt": subject}).items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((path.read_text() if path.exists() else "") + text + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def typed(self, text, project=None, day=0, **more):
        self.rows.append(json.dumps({"display": text, "pastedContents": {}, "timestamp": T0 + day * DAY,
                                     "project": str(project or self.repo.resolve()), **more}))
        self.history.write_text("\n".join(self.rows) + "\n")
        return len(self.rows)

    def run(self, command, *args, history=True):
        argv = [PY, str(SCRIPT), command, "--repo", str(self.repo), *args]
        if command == "mine":  # never the real history of the person who runs the tests
            argv += ["--history", str(self.history)] if history else ["--no-history"]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        lines = done.stdout.splitlines()
        return done.returncode, lines[:-1], lines[-1] if lines else "", done.stdout

    def mine(self, *args, history=True):
        code, lines, last, out = self.run("mine", *args, history=history)
        return code, [json.loads(line) for line in lines], last, out

    def table(self, *rows):
        (self.repo / ".claude").mkdir(exist_ok=True)
        (self.repo / ".claude" / "rules.json").write_text(json.dumps({"rules": list(rows)}))

    def file(self, rel, text="x\n"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    made.typed("build the parser", day=0)
    return made


@pytest.fixture(scope="module")
def module():
    loader = importlib.machinery.SourceFileLoader("rules_script", str(SCRIPT))
    made = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(made)
    return made


def row(rule_id="no-emojis", layer="test", path="tests/test_no_emojis.py", runs_in="ci", **more):
    return {"id": rule_id, "rule": "Output holds no emojis.", "layer": layer,
            "enforcer": {"path": path, "runs_in": runs_in}, "evidence": ["history 2026-10-01"], **more}


# ── the user's path: two repeats, a rule, an enforcer ────────────────────────

def test_mine_lists_a_revert_a_file_fixed_twice_and_a_correction_typed_twice(world):
    world.commit("first", **{"dates.py": "v1"})
    world.commit("fix: the date parser", **{"dates.py": "v2"})
    bad = world.commit("Add a faster path", **{"fast.py": "v1"})
    world.commit("Fix the date parser again", **{"dates.py": "v3", "other.py": "v1"})
    world.git("revert", "--no-edit", bad)
    world.typed("don't add emojis", day=1)
    world.typed("Don't add emojis!", day=9)
    world.typed("never push to main", day=3)

    code, found, last, _ = world.mine()

    assert code == 1 and last == "rules: summary candidates=4"
    assert found[0]["kind"] == "revert" and found[0]["subject"] == 'Revert "Add a faster path"'
    assert found[1]["kind"] == "fix-cluster" and (found[1]["path"], found[1]["count"]) == ("dates.py", 2)
    assert len(found[1]["commits"]) == 2
    assert found[2] == {"kind": "history", "text": "don't add emojis", "count": 2,
                        "first": "2026-10-28", "last": "2026-11-05"}
    assert found[3]["text"] == "never push to main" and found[3]["count"] == 1


def test_a_rule_is_a_gap_until_its_enforcer_is_there_ci_names_it_and_the_table_is_written(world):
    world.table(row())

    assert world.run("check")[:3] == (1, ["missing-enforcer no-emojis tests/test_no_emojis.py",
                                          "table-stale - .claude/rules.md"], "rules: summary gaps=2")

    world.file("tests/test_no_emojis.py", "def test_no_emojis():\n    assert True\n")
    assert world.run("check")[1][0] == "not-in-ci no-emojis tests/test_no_emojis.py"

    world.file(".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - run: python3 -m pytest tests/ -q\n")
    assert world.run("check")[:3] == (1, ["table-stale - .claude/rules.md"], "rules: summary gaps=1")

    assert world.run("table")[::2] == (0, "rules: wrote .claude/rules.md")
    assert world.run("check")[:3] == (0, [], "rules: clean")
    text = (world.repo / ".claude" / "rules.md").read_text()
    assert "| **no-emojis**: Output holds no emojis. | test | `tests/test_no_emojis.py` | ci | history 2026-10-01 |" in text


# ── secrets: a line that may hold one is never printed ───────────────────────

@pytest.mark.parametrize("name", sorted(made_up_secrets()))
def test_a_typed_line_with_a_secret_is_held_back_whole(world, name):
    secret = made_up_secrets()[name]
    number = world.typed(f"don't use this again: {secret} ok?")
    world.typed("don't add emojis")

    code, found, last, out = world.mine()

    assert secret not in out and "use this again" not in out
    assert code == 1 and last == "rules: summary candidates=1 held_back=1"
    assert found == [{"kind": "history", "text": "don't add emojis", "count": 1, "first": "2026-10-27", "last": "2026-10-27"},
                     {"kind": "held-back", "count": 1, "lines": [number]}]


@pytest.mark.parametrize("name", sorted(hostile_secrets()))
def test_a_secret_in_a_hostile_form_is_held_back_whole(world, name):
    secret, parts = hostile_secrets()[name]
    number = world.typed(f"don't use {secret} again")
    world.typed("don't add emojis")

    code, found, last, out = world.mine()

    for part in parts:
        assert part not in out, part
    assert "again" not in out
    assert code == 1 and last == "rules: summary candidates=1 held_back=1"
    assert found[-1] == {"kind": "held-back", "count": 1, "lines": [number]}


def test_a_secret_with_any_neighbour_and_any_split_is_held_back(module):
    """The secret is planted, so what must be held back is known without asking the script.

    Each made-up secret gets random characters right before and after it, and in some cases a line
    break or an invisible character inside it."""
    rng = random.Random(20261116)
    secrets = list(made_up_secrets().values()) + [secret for secret, _ in hostile_secrets().values()]
    near = list("_azGZ09fFxX\"'\\/:=.,-+()[]{}<>|@#%&*!? \t") + ["0x", "key_", "_id", "\u200b", "\n", ""]
    breaks = ["\n", "\r\n", "\n  ", "\r", "\u200b", "\u2060", "\ufeff", "\x00", "\x7f", "\u2028", "\x1f", "\x0b", "\x85"]
    words = ["don't", "never", "use", "again", "the", "v2", "path/to/file.py", "ok?"]
    for _ in range(20000):
        secret = rng.choice(secrets)
        if rng.random() < 0.4 and len(secret) > 8:
            at = rng.randint(3, len(secret) - 3)
            secret = secret[:at] + rng.choice(breaks) + secret[at:]
        line = " ".join([rng.choice(words), rng.choice(near) + secret + rng.choice(near), rng.choice(words)])
        assert module.looks_secret(line) is True, repr(line)
        assert module.shown(line) is None, repr(line)


def test_each_of_the_five_forms_of_a_line_is_checked(module):
    """A key header with a tab in it and a break inside a word: only the closed text with its spaces
    joined has the header in one piece."""
    line = "use -----BEGIN\tOPENSSH PRIV" + "\nATE KEY----- never"
    assert module.looks_secret(line) is True and module.shown(line) is None


def test_a_line_with_a_character_that_does_not_show_is_held_back_and_a_space_of_another_script_is_not(module):
    """A control or zero-width character can split a secret or join it to a word, so such a line is never
    printed. The wide space of Chinese text and a no-break space are spaces: they hold no line back."""
    for odd in ("\x00", "\x01", "\x1b", "\x7f", "\x0b", "\x0c", "\x1f", "\x85", "\u200b", "\u200d", "\u2060", "\ufeff",
                "\u2028", "\u2029", "\u202e"):
        line = f"never use{odd}mocks here"
        assert module.looks_secret(line) is True and module.shown(line) is None, repr(line)
    for space in ("\t", "\n", "\r\n", " ", "\u00a0", "\u3000", "\u2009"):
        line = f"不要用{space}mock，never use{space}mocks here"
        assert module.looks_secret(line) is False, repr(line)
        assert module.shown(line) == "不要用 mock，never use mocks here", repr(line)
    for space in ("\u00a0", "\u3000", "\u2009"):
        assert module.looks_secret(f"password:{space}hunter2xyz") is True
        assert module.looks_secret(f"mysql --password{space}hunter2xyz") is True


def test_a_secret_is_held_back_also_in_a_line_that_is_no_correction_or_is_too_long(world):
    secret = made_up_secrets()["github token"]
    first = world.typed(f"here is the value {secret}")
    second = world.typed(f"never {'x ' * 300}{secret}")

    code, found, last, out = world.mine()

    assert secret not in out
    assert (code, last) == (1, "rules: summary candidates=0 held_back=2")
    assert found == [{"kind": "held-back", "count": 2, "lines": [first, second]}]


def test_the_pasted_content_of_a_prompt_is_never_read(world):
    secret = made_up_secrets()["vendor key"]
    world.typed("don't log the body [Pasted text #1 +3 lines]", pastedContents={"1": {"content": f"never {secret}"}})

    code, found, last, out = world.mine()

    assert secret not in out and last == "rules: summary candidates=1"
    assert found[0]["text"] == "don't log the body [Pasted text #1 +3 lines]"


def test_a_commit_subject_or_a_path_with_a_secret_is_not_printed(world):
    secret = made_up_secrets()["long mixed run"]
    world.commit("first")
    bad = world.commit(f"Add {secret}")
    world.git("revert", "--no-edit", bad)
    world.commit("fix one", **{f"{secret}.py": "a"})
    world.commit("fix two", **{f"{secret}.py": "b"})

    code, found, last, out = world.mine(history=False)

    assert secret not in out and last == "rules: summary candidates=2"
    assert [(item["kind"], item.get("subject"), item.get("path")) for item in found] == [
        ("revert", None, None), ("fix-cluster", None, None)]


def test_on_random_lines_no_planted_secret_reaches_the_output(world, module):
    """Against a second version: a line is shown only when it holds no made-up secret at all."""
    rng = random.Random(20261115)
    words = ["don't", "never", "always", "stop", "again", "push", "to", "main", "the", "tests", "please", "emojis",
             "I", "said", "use", "wrong", "instead", "of", "v2", "x9", "path/to/file.py", "A1", "ok?", "(really)"]
    secrets = list(made_up_secrets().values())
    planted, clean = [], 0
    for _ in range(300):
        parts = [rng.choice(words) for _ in range(rng.randint(2, 12))]
        if rng.random() < 0.5:
            secret = rng.choice(secrets)
            glue = rng.choice([" ", "", "=", ":", "'", '"', "(", ","])
            at = rng.randint(0, len(parts))
            parts[at:at] = [glue + secret + rng.choice([" ", "", ".", ")", "'", ","])]
            planted.append(secret)
            assert module.shown(" ".join(parts)) is None, " ".join(parts)
        else:
            clean += 1
        world.rows.append(json.dumps({"display": " ".join(parts), "pastedContents": {}, "timestamp": T0,
                                      "project": str(world.repo.resolve())}))
    world.history.write_text("\n".join(world.rows) + "\n")

    code, found, last, out = world.mine()

    assert code == 1 and clean > 100 and len(planted) > 100
    for secret in set(planted):
        assert secret not in out
    held = [item for item in found if item["kind"] == "held-back"][0]
    assert held["count"] >= len(planted)
    for item in found:
        if item["kind"] == "history":
            assert not module.looks_secret(item["text"]) and len(item["text"]) <= module.SHOWN


@pytest.mark.parametrize("text", ["don't add emojis", "never push to main, use a PR", "fix tests/test_a.py again",
                                  "why did you skip step 3?", "stop asking, continue with S5", "no — I said v0.2.0",
                                  "always run python3 -m pytest tests -q", "wrong file: config/claude/CLAUDE.md",
                                  "don't edit docs/KnownLimits/VerifyRunner/README by hand",
                                  "again: fix tests/test_verify_runner_part2_more.py first",
                                  "never use Python 3 again in this repo, we are on 3.12",
                                  "don't touch config/claude/settings.json.tmpl or docs/known-limits.md",
                                  "stop: the token count is wrong again", "the password field is wrong again",
                                  "the price of a token is wrong again", "use the limit of 4096 tokens, don't guess",
                                  "wrong branch, use claude/merge-follow-fixes-2 instead of dev",
                                  "不對，不要再改 CLAUDE.md 了", "again: commit 1a2b3c4 broke it",
                                  "I said 2026-11-14, not 2026-11-15", "why did you use http://localhost:8080/docs?"])
def test_an_ordinary_correction_is_not_taken_for_a_secret(module, text):
    assert module.looks_secret(text) is False and module.shown(text) == text


@pytest.mark.parametrize("text", ["the per-token price is wrong again", "use the max-token limit, don't guess",
                                  "stop using the token: it is the wrong one", "the author said: no emojis",
                                  "never bypass the gate = always ask", "why --no-auth again"])
def test_the_cost_of_a_rule_with_no_neighbour_is_a_line_that_is_counted_and_not_shown(module, text):
    """A key word counts inside a longer word, with `:` or `=` anywhere after it, or with a dash before it
    in one run of letters. Such a line is held back also when it holds no secret."""
    assert module.looks_secret(text) is True and module.shown(text) is None


def key_word_form_second_version(module, text):
    """The two key word rules as two patterns that back up, written apart from the script's staged scan."""
    dash = "[\\-\u2010-\u2015\u2212]"
    return bool(re.search("(?is)" + module.WORD + ".*[:=]", text)
                or re.search("(?i)" + dash + "(?:\\w|" + dash + ")*" + module.WORD, text))


def test_on_random_lines_the_key_word_rules_agree_with_a_second_version(module):
    rng = random.Random(20261117)
    pieces = ["password", "PASS", "Token", "api_key", "api-key", "secret", "auth", "pwd", "credentials", "bearer",
              "pas", "tok", "key", "the", "v2", "x", "9", "_", "-", "--", "\u2014", ":", "=", " ", " ", "\t", "\n", ".",
              "/", "[", "]", '"', "'", "(", "é", "字"]
    seen = {True: 0, False: 0}
    for _ in range(30000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 9)))
        want = key_word_form_second_version(module, text)
        assert module.has_key_word_form(text) is want, repr(text)
        seen[want] += 1
    assert min(seen.values()) > 3000, seen


@pytest.mark.parametrize("text", ["-" * 1_000_000, "pas-" * 250_000, "a-" * 500_000 + "x", "pass" * 250_000,
                                  "pass " * 200_000, "-pas" * 250_000 + " x"])
def test_the_key_word_rules_read_a_long_line_in_linear_time(module, text):
    import time
    start = time.monotonic()
    module.has_key_word_form(text)
    assert time.monotonic() - start < 5


# ── which lines of the history count ─────────────────────────────────────────

def test_only_lines_of_this_project_count_and_a_folder_can_be_added(world, tmp_path):
    sibling = Path(str(world.repo.resolve()) + ".worktree")
    world.typed("never rebase dev", project=world.repo.resolve() / "sub" / "dir")
    world.typed("don't touch the sibling", project=sibling)
    world.typed("stop using tabs", project=tmp_path / "elsewhere")
    world.typed("always ask first", project=None)
    world.rows.append(json.dumps({"display": "never mind the project", "timestamp": T0, "project": 7}))
    world.history.write_text("\n".join(world.rows) + "\n")

    texts = lambda *args: sorted(item["text"] for item in world.mine(*args)[1])

    assert texts() == ["always ask first", "never rebase dev"]
    assert texts("--project", str(sibling)) == ["always ask first", "don't touch the sibling", "never rebase dev"]


def test_long_lines_commands_and_broken_lines_are_left_out_and_counted_where_it_matters(world):
    world.typed("never " + "word " * 100)
    world.typed("/clear never")
    world.typed("stop")
    world.rows.append("{not json")
    world.rows.append(json.dumps(["never", "a list"]))
    world.typed("don't add emojis", timestamp="soon")

    code, found, last, _ = world.mine()

    assert last == "rules: summary candidates=1 too_long=1"
    assert found == [{"kind": "history", "text": "don't add emojis", "count": 1, "first": None, "last": None}]


def test_lines_that_differ_in_case_and_marks_are_one_candidate_and_the_first_text_is_shown(world):
    for day, text in enumerate(["No emojis, again", "no emojis again!!", "NO EMOJIS — AGAIN"]):
        world.typed(text, day=day)

    _, found, _, _ = world.mine()

    assert found == [{"kind": "history", "text": "No emojis, again", "count": 3, "first": "2026-10-27", "last": "2026-10-29"}]


def test_lines_in_another_script_are_one_candidate_only_when_they_differ_in_marks(world):
    world.typed("這個不對，again", day=1)
    world.typed("那個錯了，別再改 again", day=2)
    world.typed("這個不對,again!", day=3)
    world.typed("Ne jamais écrire ça, never", day=4)
    world.typed("ne jamais ecrire ca, never", day=5)

    code, found, last, out = world.mine()

    assert last == "rules: summary candidates=4"
    assert [(item["text"], item["count"]) for item in found] == [
        ("這個不對，again", 2), ("Ne jamais écrire ça, never", 1), ("ne jamais ecrire ca, never", 1),
        ("那個錯了，別再改 again", 1)]


def test_more_candidates_than_the_limit_are_counted_not_dropped_in_silence(world, module, monkeypatch, capsys):
    for number in range(8):
        world.typed(f"never do thing {number}")
    monkeypatch.setattr(module, "MAX_HISTORY", 5)

    code = module.main(["mine", "--repo", str(world.repo), "--history", str(world.history)])

    lines = capsys.readouterr().out.splitlines()
    assert code == 1 and len(lines) == 6 and lines[-1] == "rules: summary candidates=5 not_shown=3"


def test_the_line_typed_most_often_comes_first_whatever_its_first_letter(world):
    world.typed("always ask before a push")
    for _ in range(3):
        world.typed("zip it, never explain twice")
    world.typed("never mind")

    _, found, _, _ = world.mine()

    assert [(item["count"], item["text"]) for item in found] == [
        (3, "zip it, never explain twice"), (1, "always ask before a push"), (1, "never mind")]


def test_a_line_longer_than_what_is_shown_is_cut(world, module):
    world.typed("never " + "ab " * 100)

    _, found, _, _ = world.mine()

    assert len(found[0]["text"]) == module.SHOWN and found[0]["text"].startswith("never ab ab")


# ── mine: the repo side and the failures ─────────────────────────────────────

def test_a_repo_with_nothing_to_report_is_clean(world):
    world.commit("first")
    world.commit("Add the parser", **{"parser.py": "v1"})
    world.commit("fix: one thing", **{"parser.py": "v2"})

    assert world.mine()[::2] == (0, "rules: clean")
    assert world.mine(history=False)[::2] == (0, "rules: clean")


def test_a_repo_with_no_commit_is_clean_and_a_folder_that_is_no_repo_is_an_error(world, tmp_path):
    assert world.mine(history=False)[::2] == (0, "rules: clean")
    plain = tmp_path / "plain"
    plain.mkdir()

    done = subprocess.run([PY, str(SCRIPT), "mine", "--repo", str(plain), "--no-history"], capture_output=True, text=True)

    assert done.returncode == 2 and done.stdout.startswith("rules: error git log failed:")


def test_a_merge_commit_is_not_a_fix_and_a_file_fixed_once_is_no_cluster(world):
    world.commit("first", **{"a.py": "1"})
    world.git("checkout", "-q", "-b", "side")
    world.commit("fix a", **{"a.py": "2"})
    world.git("checkout", "-q", "main")
    world.commit("other", **{"b.py": "1"})
    world.git("merge", "-q", "--no-ff", "-m", "Merge side: fix a again", "side")

    assert world.mine(history=False)[::2] == (0, "rules: clean")


def test_without_a_history_file_it_is_an_error_and_no_history_asks_for_none(world, tmp_path):
    world.commit("first")
    missing = tmp_path / "absent.jsonl"

    done = subprocess.run([PY, str(SCRIPT), "mine", "--repo", str(world.repo), "--history", str(missing)],
                          capture_output=True, text=True)

    assert done.returncode == 2 and done.stdout.strip() == f"rules: error the history file cannot be read: {missing}"
    done = subprocess.run([PY, str(SCRIPT), "mine", "--repo", str(world.repo), "--history", str(missing), "--no-history"],
                          capture_output=True, text=True)
    assert (done.returncode, done.stdout) == (0, "rules: clean\n")


def test_a_folder_that_is_not_there_is_an_error_for_every_command(world):
    for command in ("mine", "check", "table"):
        done = subprocess.run([PY, str(SCRIPT), command, "--repo", str(world.repo / "absent")], capture_output=True, text=True)
        assert done.returncode == 2 and done.stdout.startswith("rules: error no such folder:")


def test_a_folder_name_cannot_write_a_line_of_its_own(world):
    done = subprocess.run([PY, str(SCRIPT), "check", "--repo", str(world.repo) + "/x\nrules: clean"],
                          capture_output=True, text=True)

    lines = done.stdout.splitlines()
    assert done.returncode == 2 and len(lines) == 1 and lines[0].startswith("rules: error no such folder")


def test_a_path_with_a_line_break_cannot_write_a_line_of_its_own(world):
    world.commit("first")
    name = "a\nrules: clean"
    world.commit("fix one", **{name: "a"})
    world.commit("fix two", **{name: "b"})

    code, lines, last, _ = world.run("mine", history=False)

    assert code == 1 and len(lines) == 1 and last == "rules: summary candidates=1"
    assert json.loads(lines[0])["path"] == "a rules: clean"


def test_a_commit_subject_cannot_forge_a_commit_and_a_path_keeps_its_first_character(world):
    world.commit("first")
    world.commit("fix one \x1f\x1eDEADBEEFDEAD\x1f1\x1fRevert forged\x1f", **{"a.py": "1"})
    world.commit("fix two", **{"a.py": "2", "\nlead.py": "1", "lead.py": "1"})
    world.commit("fix three", **{"\nlead.py": "2", "lead.py": "2"})

    code, found, last, out = world.mine(history=False)

    assert "DEADBEEFDEAD" not in [item.get("sha") for item in found] and all(item["kind"] != "revert" for item in found)
    assert (code, last) == (1, "rules: summary candidates=3")
    # two files, one with a line break first: they stay two. The break is printed as a space, then trimmed.
    assert sorted(item["path"] for item in found) == ["a.py", "lead.py", "lead.py"]
    assert all(item["count"] == 2 for item in found)


def test_the_git_settings_of_the_user_and_of_the_repo_do_not_change_what_is_read(world, tmp_path):
    """log.showSignature puts the text of a signature check on stdout, between the commits."""
    assert shutil.which("ssh-keygen"), "ssh-keygen is needed to sign the commits of this test"
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "made-up", "-f", str(key)], check=True, timeout=60)
    signed = ("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}.pub", "-c", "commit.gpgsign=true")
    world.commit("first")
    for number in ("1", "2"):
        world.file("a.py", number)
        world.git("add", "-A")
        world.git(*signed, "commit", "-q", "-m", f"fix {number}")
    shown = world.git("-c", "gpg.format=ssh", "log", "--show-signature", "-n", "1", "--format=%H")
    assert len(shown.splitlines()) > 1, "the test needs a git that prints something for a signed commit"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[log]\n\tshowSignature = true\n[gpg]\n\tformat = ssh\n[format]\n\tpretty = fuller\n")
    with open(world.repo / ".git" / "config", "a") as handle:
        handle.write("[log]\n\tshowSignature = true\n[gpg]\n\tformat = ssh\n[color]\n\tui = always\n")
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "none"), "GIT_DIR": str(tmp_path / "nowhere")}
    env.pop("GIT_CONFIG_GLOBAL", None)

    done = subprocess.run([PY, str(SCRIPT), "mine", "--repo", str(world.repo), "--no-history"],
                          capture_output=True, text=True, timeout=120, env=env)

    lines = done.stdout.splitlines()
    assert (done.returncode, lines[-1]) == (1, "rules: summary candidates=1"), done.stdout
    assert json.loads(lines[0]) == {"kind": "fix-cluster", "path": "a.py", "count": 2,
                                    "commits": json.loads(lines[0])["commits"]}


def test_mine_changes_nothing_in_the_repo_and_runs_no_program_from_its_config(world, tmp_path):
    world.commit("first")
    marker = tmp_path / "ran"
    prog = tmp_path / "prog.sh"
    prog.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    prog.chmod(0o755)
    with open(world.repo / ".git" / "config", "a") as handle:
        handle.write(f"[core]\n\tfsmonitor = {prog}\n\tpager = {prog}\n[diff]\n\texternal = {prog}\n")
    before = world.git("-c", "core.fsmonitor=false", "status", "--porcelain")

    world.mine()

    assert not marker.exists()
    assert world.git("-c", "core.fsmonitor=false", "status", "--porcelain") == before


# ── the rule table: what the script refuses ──────────────────────────────────

@pytest.mark.parametrize("text, message", [
    ("not json", "rules.json cannot be read as JSON"),
    ("[]", 'the file is {"rules": [...]} and nothing else'),
    ('{"rules": [], "more": 1}', 'the file is {"rules": [...]} and nothing else'),
    ('{"rules": [7]}', "rule 1 is not an object"),
])
def test_a_table_file_of_an_unknown_shape_is_an_error(world, text, message):
    (world.repo / ".claude").mkdir()
    (world.repo / ".claude" / "rules.json").write_text(text)

    for command in ("check", "table"):
        code, _, last, _ = world.run(command)
        assert code == 2 and message in last
    assert not (world.repo / ".claude" / "rules.md").exists()


@pytest.mark.parametrize("change, message", [
    ({"extra": 1}, "has a key this script does not know: 'extra'"),
    ({"id": "No Emojis"}, "an id is lower-case letters, digits and -"),
    ({"id": "x" * 61}, "an id is lower-case letters, digits and -"),
    ({"rule": ""}, "'rule' is one line of text"),
    ({"rule": "a\nb"}, "'rule' is one line of text"),
    ({"rule": "x" * 301}, "'rule' is one line of text"),
    ({"layer": "vibes"}, "'layer' is one of architecture, types, config, lint, test, hook, docs"),
    ({"enforcer": None}, "'enforcer' is {\"path\": ..., \"runs_in\": ...}"),
    ({"enforcer": {"path": "tests/a.py"}}, "'enforcer' is {\"path\": ..., \"runs_in\": ...}"),
    ({"enforcer": {"path": "tests/a.py", "runs_in": "ci", "x": 1}}, "'enforcer' is {\"path\": ..., \"runs_in\": ...}"),
    ({"enforcer": {"path": "../outside.py", "runs_in": "ci"}}, "the enforcer path is a relative path inside the repo"),
    ({"enforcer": {"path": "/etc/passwd", "runs_in": "ci"}}, "the enforcer path is a relative path inside the repo"),
    ({"enforcer": {"path": "~/x.py", "runs_in": "ci"}}, "the enforcer path is a relative path inside the repo"),
    ({"enforcer": {"path": "a//b.py", "runs_in": "ci"}}, "the enforcer path is a relative path inside the repo"),
    ({"enforcer": {"path": "", "runs_in": "ci"}}, "the enforcer path is a relative path inside the repo"),
    ({"enforcer": {"path": 7, "runs_in": "ci"}}, "the enforcer path is a relative path inside the repo"),
    ({"enforcer": {"path": "tests/a.py", "runs_in": "laptop"}}, "'runs_in' is one of ci, hook, none"),
    ({"enforcer": {"path": "tests/a.py", "runs_in": "none"}}, "'runs_in' is \"none\" for a docs rule, and only for a docs rule"),
    ({"layer": "docs"}, "'runs_in' is \"none\" for a docs rule, and only for a docs rule"),
    ({"evidence": []}, "'evidence' is a list of one or more short lines"),
    ({"evidence": "commit abc"}, "'evidence' is a list of one or more short lines"),
    ({"evidence": ["a\nb"]}, "'evidence' is a list of one or more short lines"),
    ({"accepted": "judgment only"}, "'accepted' is for a docs rule only"),
])
def test_a_row_that_the_script_does_not_know_is_an_error(world, change, message):
    world.table({**row(), **change})

    code, lines, last, _ = world.run("check")

    assert code == 2 and lines == [] and last.startswith("rules: error rules.json: ") and message in last


@pytest.mark.parametrize("missing", ["id", "rule", "layer", "enforcer", "evidence"])
def test_a_row_with_a_key_missing_is_an_error(world, missing):
    world.table({key: value for key, value in row().items() if key != missing})

    assert world.run("check")[2] == "rules: error rules.json: rule 1 needs id, rule, layer, enforcer and evidence"


def test_two_rows_with_one_id_and_a_missing_table_are_errors(world):
    assert world.run("check")[2] == "rules: error no .claude/rules.json in this repo"
    world.table(row(), row())

    assert world.run("check")[2] == "rules: error rules.json: rule 'no-emojis' is there two times"


def test_an_enforcer_path_that_leaves_the_repo_through_a_link_is_an_error(world, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "t.py").write_text("x")
    (world.repo / "tests").symlink_to(outside)
    world.table(row(path="tests/t.py"))

    assert "the enforcer path is a relative path inside the repo" in world.run("check")[2]


# ── the rule table: gaps ─────────────────────────────────────────────────────

def test_a_docs_rule_is_a_gap_until_someone_says_why_no_code_can_check_it(world):
    world.file("CLAUDE.md", "Lead with the main point.\n")
    docs = row("main-point-first", layer="docs", path="CLAUDE.md", runs_in="none")
    world.table(docs)
    world.run("table")

    assert world.run("check")[:3] == (1, ["docs-only main-point-first CLAUDE.md"], "rules: summary gaps=1")

    world.table({**docs, "accepted": "Only a reader can judge it."})
    world.run("table")

    assert world.run("check")[:3] == (0, [], "rules: clean")
    assert "`CLAUDE.md` (text only: Only a reader can judge it.)" in (world.repo / ".claude" / "rules.md").read_text()

    (world.repo / "CLAUDE.md").unlink()
    assert world.run("check")[1] == ["missing-enforcer main-point-first CLAUDE.md"]


def test_an_enforcer_that_is_a_folder_is_missing_and_a_hook_needs_no_workflow(world):
    (world.repo / "tests" / "test_no_emojis.py").mkdir(parents=True)
    world.file("hooks/gate.py")
    world.table(row(), row("gate", layer="hook", path="hooks/gate.py", runs_in="hook"))
    world.run("table")

    assert world.run("check")[1] == ["missing-enforcer no-emojis tests/test_no_emojis.py"]


@pytest.mark.parametrize("workflow, named", [
    ("run: pytest tests/test_no_emojis.py", True),
    ("run: python3 -m pytest tests/ -q", True),
    ("run: pytest tests/", True),
    ("run: pytest ./tests", True),
    ("run: pytest ./tests/", True),
    ("run: pytest ./tests/test_no_emojis.py", True),
    ("run: 'pytest tests/ -q'", True),
    ('run: "pytest tests/"', True),
    ("run: (cd tests/ && pytest)", True),
    ("run: pytest tests/; echo done", True),
    ("run: |\n          echo start\n          pytest tests/ -q", True),
    ("run: >-\n          python3 -m pytest\n          tests/test_no_emojis.py", True),
    # a folder as a bare word is not a path: the word can be anything
    ("run: python3 -m pytest tests -q", False),
    ("run: 'pytest tests -q'", False),
    ("run: make tests", False),
    ("run: npm run tests", False),
    ('run: echo "no tests yet"', False),
    ("run: |\n          echo start\n          pytest tests -q", False),
    # a path inside a longer word is not the path
    ("run: pytest --ignore=tests/", False),
    ("run: PYTHONPATH=tests/ pytest", False),
    ("run: pytest tests/*.py", False),
    ("run: pytest ../tests", False),
    ("run: pytest ../tests/", False),
    ("run: pytest mytests/", False),
    ("run: pytest tests_old/ -q", False),
    ("run: pytest tests/unit", False),
    ("run: pytest tests/unit/", False),
    ("run: pytest tests/test_no_emojis.py.bak", False),
    ("run: pytest other/tests/", False),
    ("run: pytest /tests/", False),
    ("run: pytest tests//", False),
    # what is not a command
    ("run: echo done  # pytest tests/", False),
    ("name: Lint shell scripts, no tests/ here", False),
    ("name: tests/\n        run: shellcheck x.sh", False),
    ("uses: some/action@v1\n        with:\n          args: tests/", False),
    ("run: |\n          echo start\n          # pytest tests/\n      - name: the tests/ of another day", False),
    ("run: |\n          echo start\n        env:\n          WHERE: tests/", False),
    ("# run: pytest tests/", False),
    # `run:` with nothing after it is a key with keys below it (the `defaults` of a workflow), not a command
    ("run:\n          working-directory: tests/", False),
    ("run:\n          pytest tests/", False),
    ("run: \n          working-directory: tests/", False),
    ("run: # the steps\n          pytest tests/", False),
    ("run: pytest", False),
    ("", False),
])
def test_ci_counts_when_a_command_names_the_enforcer_or_a_folder_above_it_as_a_path(world, workflow, named):
    world.file("tests/test_no_emojis.py")
    world.file(".github/workflows/ci.yml", f"jobs:\n  t:\n    steps:\n      - {workflow}\n")
    world.file(".github/workflows/notes.txt", "run: pytest tests/")
    world.table(row())
    world.run("table")

    code, lines, _, _ = world.run("check")

    assert (code, lines) == ((0, []) if named else (1, ["not-in-ci no-emojis tests/test_no_emojis.py"]))


@pytest.mark.parametrize("path, command, named", [
    # a folder named like a command word: the cases of the review
    ("test/unit.js", "npm test", False),
    ("test/unit.js", "make test", False),
    ("test/unit.js", "go test ./...", False),
    ("test/unit.js", "npx mocha test/", True),
    ("test/unit.js", "npx mocha test/unit.js", True),
    # a file at the top of the repo is a path only with ./ before it
    ("lint.sh", "./lint.sh", True),
    ("lint.sh", "bash ./lint.sh", True),
    ("lint.sh", "bash lint.sh", False),
    ("check", "make check", False),
    ("check", "./check", True),
    # a folder two levels down has a slash in its own name
    ("config/hooks/tests/test_gate.py", "pytest config/hooks/tests", True),
    ("config/hooks/tests/test_gate.py", "pytest config/hooks", True),
    ("config/hooks/tests/test_gate.py", "pytest ./config/hooks/tests/", True),
    ("config/hooks/tests/test_gate.py", "pytest config/", True),
    ("config/hooks/tests/test_gate.py", "pytest config", False),
    ("config/hooks/tests/test_gate.py", "pytest hooks/tests", False),
    ("config/hooks/tests/test_gate.py", "pytest tests/", False),
    ("config/hooks/tests/test_gate.py", "pytest config/hooks/tests/test_gate.py::test_one", False),
])
def test_a_name_counts_only_when_it_is_written_as_a_path_from_the_top_of_the_repo(world, path, command, named):
    world.file(path)
    world.file(".github/workflows/ci.yml", f"jobs:\n  t:\n    steps:\n      - run: {command}\n")
    world.table(row(path=path))
    world.run("table")

    code, lines, _, _ = world.run("check")

    assert (code, lines) == ((0, []) if named else (1, [f"not-in-ci no-emojis {path}"]))


def named_in_second_version(lines, rel):
    """One pattern for each spelling, where the script splits each line into words."""
    edge = "\\s\"'`;&|()<>"
    parts = rel.split("/")
    for depth in range(1, len(parts) + 1):
        name = re.escape("/".join(parts[:depth]))
        spellings = [f"\\./{name}/?", f"{name}/"] + ([f"{name}"] if depth > 1 else [])
        for spelling in spellings:
            if any(re.search(f"(?:^|[{edge}])(?:{spelling})(?=$|[{edge}])", line) for line in lines):
                return True
    return False


def test_on_random_commands_the_path_rule_agrees_with_a_second_version(module):
    rng = random.Random(20261118)
    words = ["tests", "tests/", "./tests", "./tests/", "test", "test/", "./test", "test/unit", "test/unit/x", "config",
             "config/", "config/hooks", "./config/hooks/", "config/hooks/tests", "hooks/tests", "tests/test_gate.py",
             "config/hooks/tests/test_gate.py", "lint.sh", "./lint.sh", "mytests/", "pytest", "-q", "x"]
    joins = [" ", " ", " ", " ", "  ", "\t", '"', "'", "`", ";", " && ", "|", "(", ")", "<", ">",
             "", "/", "=", "*", ".", "-", "../", "\\", ","]
    paths = ["tests/test_gate.py", "config/hooks/tests/test_gate.py", "lint.sh", "test/unit/x"]
    seen = {True: 0, False: 0}
    for _ in range(30000):
        line = "pytest"
        for _ in range(rng.randint(1, 5)):
            line += rng.choice(joins) + rng.choice(words)
        line += rng.choice(["", "", " ", ";", '"', "/", "."])
        rel = rng.choice(paths)
        want = named_in_second_version([line], rel)
        assert module.named_in([f"      - run: {line}\n"], rel) is want, (line, rel)
        seen[want] += 1
    assert min(seen.values()) > 5000, seen


def test_a_job_that_is_named_like_the_folder_does_not_name_the_enforcer(world):
    world.file("tests/test_no_emojis.py")
    world.file(".github/workflows/ci.yml", "name: tests\non: [push]\njobs:\n  tests:\n    name: Run the tests\n"
               "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - name: tests\n"
               "        run: shellcheck scripts/*.sh\n")
    world.table(row())
    world.run("table")

    assert world.run("check")[:3] == (1, ["not-in-ci no-emojis tests/test_no_emojis.py"], "rules: summary gaps=1")


def test_a_table_that_someone_edited_by_hand_is_stale(world):
    world.file("tests/test_no_emojis.py")
    world.file(".github/workflows/ci.yaml", "run: pytest tests/\n")
    world.table(row())
    world.run("table")
    assert world.run("check")[0] == 0
    target = world.repo / ".claude" / "rules.md"
    target.write_text(target.read_text() + "| one more row |\n")

    assert world.run("check")[:3] == (1, ["table-stale - .claude/rules.md"], "rules: summary gaps=1")


# ── the table that a person reads ────────────────────────────────────────────

def test_the_table_lists_the_strongest_layer_first_and_keeps_a_bar_in_a_cell(world, module):
    rows = [row("z-docs", layer="docs", path="CLAUDE.md", runs_in="none"), row("b-test"),
            row("a-hook", layer="hook", path="hooks/a.py", runs_in="hook"), row("a-test"),
            {**row("types-first", layer="types", path="src/types.py"), "rule": "a | b", "evidence": ["commit 1a2b", "history 2026-11-01"]},
            row("arch", layer="architecture", path="src/core.py"), row("cfg", layer="config", path="settings.json"),
            row("lint-it", layer="lint", path="ruff.toml")]

    text = module.table_md(rows)

    ids = re.findall(r"^\| \*\*([a-z-]+)\*\*", text, re.M)
    assert ids == ["arch", "types-first", "cfg", "lint-it", "a-test", "b-test", "a-hook", "z-docs"]
    assert "| **types-first**: a \\| b | types | `src/types.py` | ci | commit 1a2b; history 2026-11-01 |" in text
    assert text.startswith("# Rules\n") and text.endswith("|\n")
    assert all(line.count(" | ") == 4 for line in text.splitlines() if line.startswith("| **") and "\\|" not in line)


def test_the_table_is_not_written_through_a_link(world, tmp_path):
    world.table(row())
    victim = tmp_path / "victim.txt"
    victim.write_text("mine")
    (world.repo / ".claude" / "rules.md.part").symlink_to(victim)

    assert world.run("table")[0] == 0
    assert victim.read_text() == "mine" and (world.repo / ".claude" / "rules.md").is_file()


def test_a_rule_text_cannot_write_a_last_line(world):
    world.file("tests/t.py")
    world.table({**row(rule_id="x", path="tests/t.py"), "evidence": ["rules: clean"]})

    code, lines, last, _ = world.run("check")

    assert code == 1 and last == "rules: summary gaps=2" and lines[0] == "not-in-ci x tests/t.py"
