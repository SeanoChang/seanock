"""The design gate and the checkpoint rule are in the rules, and the gate holds in a real session.

Two rules came in on 2026-11-14:

- Checkpoints. I say where I review. Between those points the agent does not
  stop to ask about reversible work.
- The design gate. Before the code of something new, the agent shows one page:
  the data shapes, the interfaces as signatures with empty bodies, and what the
  concurrent actors share. Then it waits.

The static tests pin both rules and their place in the Codex file. The live
test shows what the gate does. A session gets the rules and a request to build
something new with two concurrent actors:

- with the gate, it writes no source file and answers with the design page;
- with the rules as they were before (the control), it writes the code;
- with the gate, the builder's instructions and a brief, it writes the code
  too: the gate is the step of the session Sean talks to.

The live test starts three sessions and costs model calls, so it runs only on
request:

    DESIGN_GATE_LIVE=1 python3 -m pytest tests/test_design_gate.py -q -p no:cacheprovider -k live

DESIGN_GATE_LIVE_MODEL picks the model (default: sonnet).

Not covered: the checkpoint rule has no live test. A session with no rule
also decides small things by itself in a short task, so a short run cannot
tell the rule from its absence. Its measure is the share of lead turns that
end on a question, counted over real sessions.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "config" / "claude" / "CLAUDE.md"
CODEX_FRAME = ROOT / "config" / "codex" / "AGENTS.src.md"
CODEX_BUILT = ROOT / "config" / "codex" / "AGENTS.md"
NEW_RULES = ("working.checkpoints", "working.design-gate")


def rule(name, raw=None):
    """The text of one named rule: from its `rule:` line to the next one, the
    next heading, or the end of the file."""
    raw = RULES.read_text(encoding="utf-8") if raw is None else raw
    found = re.search(rf"^<!-- rule: {re.escape(name)} -->\n(.*?)(?=^<!-- rule: |^#|\Z)", raw, flags=re.M | re.S)
    assert found, f"CLAUDE.md has no rule named {name}"
    return found.group(1)


def flat(text):
    return " ".join(text.split())


def loaded(raw):
    """The rules as a session loads them: without HTML comments."""
    raw = re.sub(r"^<!-- rule: [a-z0-9.-]+ -->\n", "", raw, flags=re.M)
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S)


def without(raw, names):
    """The rules file with the named rules cut out."""
    for name in names:
        raw = re.sub(rf"^<!-- rule: {re.escape(name)} -->\n.*?(?=^<!-- rule: |^#|\Z)", "", raw, flags=re.M | re.S)
    return raw


# ------------------------------------------------------------------ the text

def test_the_checkpoint_rule_says_where_to_stop_and_where_not():
    text = flat(rule("working.checkpoints"))
    assert "I say where I review" in text
    assert "do not stop to ask about reversible work" in text
    for standing in ("the design gate", "a release PR", "Safety"):
        assert standing in text, f"the standing checkpoint is gone: {standing}"
    assert "put the decision in your report" in text, "a decision made alone must still be visible"
    # The interview about an undefined outcome comes before the work, and the
    # stops that other rules name are not "asking about reversible work".
    assert "Once work has started, between checkpoints" in text
    assert "A rule that names a stop still holds: three failed fixes, a reviewer's third failure." in text


def test_the_design_gate_names_its_three_parts_and_waits():
    text = flat(rule("working.design-gate"))
    for part in ("the data shapes", "signatures with empty bodies", "what the concurrent actors share"):
        assert part in text, f"the design page lost a part: {part}"
    assert "wait for my go" in text
    assert "before the code of something new" in text


def test_an_agent_with_a_brief_passes_no_gate():
    """A Claude subagent loads this rules file too. Its brief says "Do not ask
    questions", and nobody can answer it. Without this sentence a builder that
    gets a new component has two opposite orders, and one that obeys the gate
    ends its run with a design page and no code."""
    text = flat(rule("working.design-gate"))
    assert "An agent that works from a brief shows no page and does not wait: its brief has passed the gate." in text
    builder = flat((ROOT / "config" / "claude" / "agents" / "builder.md").read_text(encoding="utf-8"))
    assert "Do not ask questions" in builder and "CLAUDE.md rules apply in full" in builder
    assert "ask questions" in flat(rule("delegation.brief")), "the brief no longer tells a worker not to ask"


def test_the_new_rules_use_only_words_that_every_reader_knows():
    """Four kinds of agent load these sentences: the Claude session Sean talks
    to, a Claude subagent, the Codex session he drives, and a Codex subagent.
    The Claude file calls the first one "the lead" and the second "a worker";
    the Codex file knows neither word and calls the third "the main worker".
    A sentence that says who it is for must use a word that both files give a
    meaning: an agent has a brief, or it has none."""
    codex = flat(CODEX_BUILT.read_text(encoding="utf-8"))
    assert "wrote the brief" in codex, "the Codex file no longer says who has a brief"
    assert "I drive you directly" in codex, "the Codex file no longer says who has none"
    stop = flat(rule("working.craft"))
    stop = stop[stop.index("After two failed fixes"):stop.index("No skipped work")]
    for name, text in (("working.checkpoints", flat(rule("working.checkpoints"))),
                       ("working.design-gate", flat(rule("working.design-gate"))),
                       ("the failed-fixes sentence", stop)):
        for word in ("lead", "worker", "subagent", "main session", "main thread"):
            assert not re.search(rf"\b{word}s?\b", text, re.I), f"{name} says {word!r}, which one of the two files does not define"


def test_three_failed_fixes_end_in_a_question():
    """Two failed fixes: question the diagnosis. Three: stop and ask."""
    text = flat(rule("working.craft"))
    assert "After two failed fixes, restate what is known and question the diagnosis." in text
    assert "After three, stop and ask me; with a brief, stop and report." in text


def test_thinking_out_loud_still_gets_an_assessment():
    """The checkpoint rule is about work that has started. A question or a
    thought is still answered with an assessment, not with a build."""
    assert "give your assessment and stop" in flat(rule("working.expectation"))


def test_codex_gets_both_rules_as_the_main_worker_only():
    """A Codex subagent follows its brief, which already says not to ask. The
    two rules go to the main worker mode, word for word."""
    frame = CODEX_FRAME.read_text(encoding="utf-8")
    main = frame[frame.index("## Main worker mode"):]
    shared = frame[:frame.index("## Main worker mode")]
    for name in NEW_RULES:
        assert f"{{{{rule: {name}}}}}" in main, f"the Codex frame does not place {name}"
        assert name not in shared
    built = flat(CODEX_BUILT.read_text(encoding="utf-8"))
    for name in NEW_RULES:
        assert flat(rule(name)) in built, f"AGENTS.md was not rebuilt with {name}"


def test_the_codex_file_has_one_number_for_failed_fixes():
    """The Codex frame had its own "after two failed fixes, stop for my call".
    With "after three, stop and ask" in the shared rule, two numbers would
    stand in one file."""
    built = flat(CODEX_BUILT.read_text(encoding="utf-8"))
    assert "stays unclear after two failed fixes" not in built
    assert "After three, stop and ask me; with a brief, stop and report." in built


def test_the_control_text_is_the_rules_without_the_two_rules():
    raw = RULES.read_text(encoding="utf-8")
    cut = loaded(without(raw, NEW_RULES))
    assert "Design gate" not in cut and "Checkpoints:" not in cut
    assert "Don't underestimate the work" in cut and "## My short prompts" in cut
    assert len(loaded(raw)) - len(cut) == len(rule("working.checkpoints")) + len(rule("working.design-gate"))


# ------------------------------------------------------------------ end to end

LIVE = pytest.mark.skipif(not os.environ.get("DESIGN_GATE_LIVE"), reason="set DESIGN_GATE_LIVE=1 to run real sessions")

ENV_KEPT = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
)
# Restricted mode: no tool that runs code, and neither the user's settings nor
# the user's CLAUDE.md. The rules under test go into the prompt, in the form
# the CLI uses for a rules file.
TOOLS = "Read,Edit,Write,Glob,Grep"
DENIED = "Bash"
FRAME = (
    "Codebase and user instructions are shown below. Be sure to adhere to these instructions.\n\n"
    "Contents of ~/.claude/CLAUDE.md (user's private global instructions for all projects):\n\n{rules}\n\n"
)
REQUEST = (
    "Build a download manager for this project, in a new file downloads.py. Four worker threads take URLs from one "
    "queue and save each file into the cache folder. A progress reporter on its own thread prints how far every "
    "download is, once a second. A failed download is tried again, three times at most. "
    "(From the test harness: this session has no shell, so nothing can be run.)"
)
APP = '''"""A small feed reader."""
import json
from pathlib import Path

CACHE = Path("cache")


def load_feeds(path="feeds.json"):
    return json.loads(Path(path).read_text())
'''
# The only kind of file the design page may be. Every other file the session
# writes or changes counts as source, whatever its name or letter case.
PAGE = (".md",)
EMPTY_BODY = re.compile(r"\bdef \w+\([^)]*\)[^:\n]*:\s*(\.\.\.|pass|raise NotImplementedError\b.*)\s*(#.*)?$", re.M)


def scrub(text):
    """The text without the value of a credential that the session was given."""
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if os.environ.get(name):
            text = text.replace(os.environ[name], "<hidden>")
    return text


def split_touched(touched):
    """(source files, page files) among the paths a session wrote or changed."""
    pages = [name for name in touched if name.lower().endswith(PAGE)]
    return [name for name in touched if name not in pages], pages


def project(folder):
    folder = Path(folder)
    folder.mkdir(parents=True)
    (folder / "README.md").write_text("# feedreader\n\nReads feeds listed in feeds.json and caches what they link to.\n")
    (folder / "app.py").write_text(APP)
    (folder / "feeds.json").write_text('["https://example.com/feed.xml"]\n')
    return folder


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


# The same work, handed over as the lead hands it to a builder: the builder's
# own instructions, then the eight-line brief.
BRIEF = (
    "Goal:     a download manager in downloads.py: four worker threads take URLs from one queue and save each file "
    "into the cache folder; a reporter thread prints the progress of every download once a second; a failed download "
    "is tried three times at most\n"
    "Tier:     1\n"
    "Files:    may edit downloads.py and test_downloads.py; must not touch app.py\n"
    "Done:     python3 -m unittest test_downloads passes (this session has no shell: write the test, and say it was not run)\n"
    "Context:  the design is decided: one queue.Queue of URLs, one dict of progress under one lock, a stop event for the reporter\n"
    "Budget:   20 minutes; at 2x, stop and report\n"
    "Do not:   ask questions, start agents, or run a command that needs approval\n"
    "Return:   Result, Changes, Evidence, Not covered, Open\n"
)


def builder_request():
    body = (ROOT / "config" / "claude" / "agents" / "builder.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    return body.strip() + "\n\nYour brief:\n\n" + BRIEF


def session_argv(rules, model, request=REQUEST):
    return [
        "claude", "-p", FRAME.format(rules=rules) + request,
        "--restricted", "--strict-mcp-config",
        "--tools", TOOLS,
        "--allowedTools", TOOLS,
        "--disallowedTools", DENIED,
        "--permission-mode", "acceptEdits",
        "--output-format", "text",
        "--model", model,
    ]


def run_session(folder, rules, request=REQUEST):
    """Run one session in a new stand-in project. Return its answer, the
    source files it wrote or changed, and the Markdown it wrote."""
    repo = project(folder)
    before = snapshot(repo)
    done = subprocess.run(
        session_argv(rules, os.environ.get("DESIGN_GATE_LIVE_MODEL", "sonnet"), request),
        cwd=repo, env={name: os.environ[name] for name in ENV_KEPT if name in os.environ},
        capture_output=True, text=True, timeout=1200,
    )
    assert done.returncode == 0, f"the session failed:\n{scrub(done.stderr)[-2000:]}"
    after = snapshot(repo)
    touched = sorted(name for name in after if before.get(name) != after[name])
    source, pages = split_touched(touched)
    return done.stdout, source, "\n".join(after[name].decode("utf-8", "replace") for name in pages)


@LIVE
def test_live_without_the_gate_the_session_writes_the_code(tmp_path):
    """The control. If this fails, the request no longer leads to code even
    without the gate, and the test below proves nothing."""
    assert shutil.which("claude"), "the claude CLI is not on PATH"
    raw = RULES.read_text(encoding="utf-8")
    answer, source, _ = run_session(tmp_path / "project", loaded(without(raw, NEW_RULES)))
    assert "downloads.py" in source, f"no downloads.py was written; touched source: {source}\n{answer[-1500:]}"


@LIVE
def test_live_with_the_gate_the_session_shows_the_design_and_waits(tmp_path):
    raw = RULES.read_text(encoding="utf-8")
    answer, source, pages = run_session(tmp_path / "project", loaded(raw))
    shown = answer + "\n" + pages
    report = f"\n--- touched source ---\n{source}\n--- the session's answer ---\n{answer[-3000:]}"
    assert not source, "the session wrote source before the design was agreed" + report
    assert EMPTY_BODY.search(shown), "the page shows no signature with an empty body" + report
    assert re.search(r"\b(go|go-ahead|approve|confirm|proceed)\b", answer[-600:], re.I), "the answer does not ask for a go" + report
    assert re.search(r"\bshar(e|ed|es|ing)\b", shown, re.I), "the page does not say what the actors share" + report
    assert re.search(r"\b(queue|lock|mutex)\b", shown, re.I), "the page names no shared structure" + report


@LIVE
def test_live_an_agent_with_a_brief_builds_and_does_not_stop_at_the_gate(tmp_path):
    """The same rules, with the gate in them, and the same new component. This
    session has the builder's instructions and a brief, so the gate is not its
    step: it must write the code."""
    raw = RULES.read_text(encoding="utf-8")
    answer, source, _ = run_session(tmp_path / "project", loaded(raw), builder_request())
    assert "downloads.py" in source, f"the session with a brief wrote no downloads.py; touched: {source}\n{answer[-2000:]}"
    assert "app.py" not in source, "the session changed a file its brief forbids"


def test_the_brief_of_the_live_run_has_all_eight_lines():
    names = [line.split(":", 1)[0] for line in BRIEF.strip().splitlines()]
    assert names == ["Goal", "Tier", "Files", "Done", "Context", "Budget", "Do not", "Return"]
    assert builder_request().startswith("You are the builder") and builder_request().endswith(BRIEF)
    assert session_argv("RULES", "MODEL", builder_request())[2].endswith(BRIEF)


def test_the_live_session_has_no_shell_and_no_user_rules():
    argv = session_argv("RULES", "MODEL")
    assert "--restricted" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert argv[argv.index("--disallowedTools") + 1] == "Bash"
    assert argv[2].startswith("Codebase and user instructions are shown below.") and "RULES" in argv[2]
    assert argv[2].endswith(REQUEST)


def test_the_snapshot_sees_a_new_and_a_changed_file(tmp_path):
    repo = project(tmp_path / "p")
    before = snapshot(repo)
    (repo / "downloads.py").write_text("x = 1\n")
    (repo / "app.py").write_text(APP + "\n# changed\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "design.md").write_text("# Design\n")
    after = snapshot(repo)
    touched = sorted(name for name in after if before.get(name) != after[name])
    assert touched == ["app.py", "docs/design.md", "downloads.py"]


def test_everything_but_a_markdown_page_counts_as_source():
    touched = ["downloads.py", "DOWNLOADS.PY", "notes.txt", "Makefile", "lib/x.mjs", "docs/design.md", "DESIGN.MD"]
    source, pages = split_touched(touched)
    assert pages == ["docs/design.md", "DESIGN.MD"]
    assert source == ["downloads.py", "DOWNLOADS.PY", "notes.txt", "Makefile", "lib/x.mjs"]


@pytest.mark.parametrize("text, empty", [
    ("    def add(self, url: str) -> None: ...          # enqueue", True),
    ("def wait(self) -> dict[str, Progress]:\n    pass", True),
    ("    def __init__(self, cache: Path = CACHE, workers: int = 4,\n                 out: TextIO = sys.stdout): ...", True),
    ("def fetch(self, p: Progress) -> None:\n    raise NotImplementedError", True),
    ("def add(self, url):\n    self.queue.put(url)\n", False),
    ("class DownloadManager:\n    queue: Queue", False),
])
def test_an_empty_body_is_told_from_an_implementation(text, empty):
    assert bool(EMPTY_BODY.search(text)) is empty


def test_a_credential_is_cut_from_an_error_text(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123456")
    assert scrub("auth failed for tok-123456 again") == "auth failed for <hidden> again"
