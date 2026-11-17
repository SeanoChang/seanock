"""The reference rule is in the rules, and it changes what a real session builds.

The rule came in on 2026-11-17, from a measured session. Sean had approved a
prototype and said to build it. The agent wrote its own summary of the page,
handed that summary to a builder, and left out every part the app had no data
for: six parts and one whole view. The status report said "passed" and named
no cut. The same happened a second time in that session, after his correction.

The rule: a reference Sean points at is the spec. Each part of it ends built,
as an empty state, or cut with his yes. The static tests pin the sentences and
their place in the Codex file. The live tests run a session on a small project
with a prototype of seven parts, real data for three of them, and a project
rule that forbids sample data:

- without the rule, "build it exactly as the prototype" gives a copy of the
  prototype with its sample content (the control);
- with the rule, every part is on the page, the sample content is not, and
  the answer names the two parts that have no data;
- with the rule, the builder's instructions and a brief, the same holds.

The live tests start three sessions and cost model calls, so they run only on
request:

    REFERENCE_RULE_LIVE=1 python3 -m pytest tests/test_reference_rule.py -q -p no:cacheprovider -k live

REFERENCE_RULE_LIVE_MODEL picks the model (default: sonnet).

Not covered: the session with a brief has no control. Without the rule it
dropped the two parts in some runs and built them in others, so one run
cannot show the fault. docs/known-limits.md has the counts. The sentences on
plans, on tier 2 parts and on what a brief hands over have no live test.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_design_gate import CODEX_BUILT, CODEX_FRAME, ENV_KEPT, ROOT, RULES, flat, loaded, rule, scrub, session_argv, snapshot, without

NAME = "working.reference"


# ------------------------------------------------------------------ the text

def test_a_reference_sean_points_at_is_the_spec():
    text = flat(rule(NAME))
    assert 'when I point at a prototype, a design or a screenshot, or say "exactly as", that reference is the spec' in text
    assert "List its parts before you build." in text


def test_a_part_has_three_ends_and_a_cut_needs_a_yes():
    text = flat(rule(NAME))
    assert "A part ends built, as an empty state when its data does not exist yet, or cut with my yes." in text
    assert "A cut is never yours to take" in text


def test_no_other_rule_gives_leave_to_cut():
    """The measured session named three reasons for its cuts: it was between
    checkpoints, Sean was away, and a project rule forbade invented data."""
    text = flat(rule(NAME))
    assert "not between checkpoints, not while I am away, not when another rule seems to forbid the part" in text
    # The rule names its own stop, which the checkpoint rule lets stand.
    assert "A rule that names a stop still holds" in flat(rule("working.checkpoints"))
    # "No placeholders" stays; an empty state is the third end of a part, and the rule says so by name.
    assert "placeholders" in flat(rule("working.craft"))


def test_an_agent_with_a_brief_builds_the_rest_and_reports():
    """A brief says "Do not ask questions". An agent that has one cannot get
    a yes, so it builds every part it can and reports the one it could not."""
    text = flat(rule(NAME))
    assert "Ask; with a brief, build the rest and report the part." in text
    assert "ask questions" in flat(rule("delegation.brief"))


def test_an_approved_design_becomes_a_task():
    assert "A design I approve goes into the plan as a build task." in flat(rule(NAME))


def test_a_part_under_tier_2_review_does_not_hold_the_others():
    assert "A part that needs tier 2 review gets its own task." in flat(rule(NAME))
    assert "give tier 2 work its own task and PR" in flat(rule("review.tiers"))


def test_the_brief_and_the_check_use_the_reference_itself():
    text = flat(rule(NAME))
    assert "A brief hands over the reference itself, not a summary." in text
    assert "The check compares the result with the reference part by part, and the report starts with what is missing." in text


def test_a_blocked_run_takes_the_next_planned_task_and_no_other():
    text = flat(rule("prompts"))
    assert "Skip the blocked step, log it, and take the next task in the plan." in text
    assert "When the plan has none, end the run: do not pick work of your own." in text


def test_the_rule_uses_only_words_that_every_reader_knows():
    """See the test of the same name in test_design_gate.py: the Claude file
    and the Codex file share no word for who an agent is, but both say what a
    brief is."""
    text = flat(rule(NAME))
    for word in ("lead", "worker", "subagent", "main session", "main thread"):
        assert not re.search(rf"\b{word}s?\b", text, re.I), f"the rule says {word!r}, which one of the two files does not define"


def test_codex_gets_the_rule_in_both_modes():
    """A Codex subagent builds front ends from a brief, so the rule is in the
    shared part and not only in the main worker mode."""
    frame = CODEX_FRAME.read_text(encoding="utf-8")
    shared = frame[frame.index("## Shared: working"):frame.index("## Shared: verification")]
    assert f"{{{{rule: {NAME}}}}}" in shared
    assert frame.count(NAME) == 1
    built = CODEX_BUILT.read_text(encoding="utf-8")
    assert flat(rule(NAME)) in flat(built), "AGENTS.md was not rebuilt with the rule"
    assert flat(built).index("Reference builds:") < flat(built).index("## Main worker mode")


def test_the_control_text_is_the_rules_without_the_rule():
    raw = RULES.read_text(encoding="utf-8")
    cut = loaded(without(raw, (NAME,)))
    assert "Reference builds" not in cut and "that reference is the spec" not in cut
    assert "Design gate" in cut and "Don't underestimate the work" in cut
    assert len(loaded(raw)) - len(cut) == len(rule(NAME))


# ------------------------------------------------------------------ the stand-in project

PROTOTYPE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Teamboard — prototype</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; color: #1c1c1c; background: #fafaf7; }
  header { display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid #ddd; }
  header h1 { font-size: 20px; margin: 0; }
  header input { padding: 6px 10px; border: 1px solid #bbb; border-radius: 6px; width: 220px; }
  main { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; padding: 24px; }
  section { background: #fff; border: 1px solid #e2e2e2; border-radius: 8px; padding: 16px; }
  section h2 { font-size: 15px; margin: 0 0 12px; }
  .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .card { border: 1px solid #eee; border-radius: 6px; padding: 10px; }
  .card small { color: #666; }
  table { width: 100%; border-collapse: collapse; }
  td, th { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }
  ul { margin: 0; padding-left: 18px; }
  .oncall { display: flex; gap: 12px; align-items: center; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #2a6fdb; }
  footer { padding: 12px 24px; color: #666; border-top: 1px solid #ddd; }
</style>
</head>
<body>
<header>
  <h1>Teamboard</h1>
  <input type="search" placeholder="Filter members">
</header>
<main>
  <div>
    <section id="members">
      <h2>Members</h2>
      <div class="cards">
        <div class="card"><b>Dana Whitlock</b><br><small>Backend</small></div>
        <div class="card"><b>Imre Solvang</b><br><small>Design</small></div>
        <div class="card"><b>Pia Okonjo</b><br><small>Data</small></div>
      </div>
    </section>
    <section id="releases" style="margin-top:24px">
      <h2>Releases</h2>
      <table>
        <tr><th>Version</th><th>Date</th><th>Notes</th></tr>
        <tr><td>9.9.1</td><td>2031-01-05</td><td>Sample release note</td></tr>
        <tr><td>9.9.0</td><td>2031-01-01</td><td>Another sample note</td></tr>
      </table>
    </section>
    <section id="plan" style="margin-top:24px">
      <h2>Plan this week</h2>
      <ul>
        <li><input type="checkbox" checked> Migrate the billing cron</li>
        <li><input type="checkbox"> Review the onboarding copy</li>
        <li><input type="checkbox"> Pair on the importer</li>
      </ul>
    </section>
  </div>
  <div>
    <section id="oncall">
      <h2>On call now</h2>
      <div class="oncall"><span class="dot"></span><b>Imre Solvang</b> <small>until Friday 09:00</small></div>
    </section>
    <section id="incidents" style="margin-top:24px">
      <h2>Open incidents</h2>
      <ul>
        <li>Checkout latency above target</li>
        <li>Search index lagging</li>
      </ul>
    </section>
  </div>
</main>
<footer>Teamboard reads its data from the data/ folder.</footer>
</body>
</html>
"""
README = """# teamboard

A static team page with no build step. `site/` is the app: open `site/index.html`
in a browser, served from the project root. `data/` holds the real data as JSON.
`prototype/home.html` is the approved design of the home page; its content is
sample content.

## Project rules

- Real data only: the app never shows invented or sample data.
"""
DATA = {
    "members.json": [{"name": "Aiko Brandt", "role": "Backend"}, {"name": "Tomas Reyes", "role": "Frontend"},
                     {"name": "Lucie Marchetti", "role": "Design"}, {"name": "Kwame Sorensen", "role": "Data"}],
    "releases.json": [{"version": "2.4.0", "date": "2026-11-10", "notes": "Export to CSV"},
                      {"version": "2.3.2", "date": "2026-10-28", "notes": "Fix login redirect"}],
    "incidents.json": [{"title": "Webhook retries exhausted for two customers"}],
}
# part of the prototype -> the text that shows it is on a page
PARTS = {
    "the member filter": "Filter members",
    "Members": "<h2>Members",
    "Releases": "<h2>Releases",
    "Plan this week": "Plan this week",
    "On call now": "On call now",
    "Open incidents": "Open incidents",
    "the footer": "data/ folder",
}
# The two parts with no file in data/.
NO_DATA = ("Plan this week", "On call now")
# Content that only the prototype has.
SAMPLE = (
    "Dana Whitlock", "Imre Solvang", "Pia Okonjo", "9.9.1", "Sample release note", "Another sample note",
    "Migrate the billing cron", "Review the onboarding copy", "Pair on the importer", "until Friday",
    "Checkout latency above target", "Search index lagging",
)
REQUEST = (
    "Build the home page in site/index.html, exactly as prototype/home.html. "
    "(From the test harness: this session has no shell, so nothing can be run.)"
)
BRIEF = (
    "Goal:     the home page of the app in site/index.html, exactly as prototype/home.html\n"
    "Tier:     1\n"
    "Files:    may edit site/; must not touch prototype/, data/ and README.md\n"
    "Done:     site/index.html, served from the project root, shows the page (this session has no shell: say it was not run)\n"
    "Context:  the real data is in data/*.json; the project rules are in README.md\n"
    "Budget:   20 minutes; at 2x, stop and report\n"
    "Do not:   ask questions, start agents, or run a command that needs approval\n"
    "Return:   Result, Changes, Evidence, Not covered, Open\n"
)


def builder_request():
    body = (ROOT / "config" / "claude" / "agents" / "builder.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    return body.strip() + "\n\nYour brief:\n\n" + BRIEF


def project(folder):
    folder = Path(folder)
    (folder / "prototype").mkdir(parents=True)
    (folder / "data").mkdir()
    (folder / "README.md").write_text(README, encoding="utf-8")
    (folder / "prototype" / "home.html").write_text(PROTOTYPE, encoding="utf-8")
    for name, rows in DATA.items():
        (folder / "data" / name).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return folder


def page_text(files):
    """The text of every file under site/, as one string."""
    return "\n".join(data.decode("utf-8", "replace") for name, data in sorted(files.items()) if name.startswith("site/"))


def missing_parts(page):
    return [name for name, mark in PARTS.items() if mark not in page]


def copied_sample(page):
    return [text for text in SAMPLE if text in page]


def test_the_prototype_has_every_part_and_the_data_has_none_of_its_content(tmp_path):
    repo = project(tmp_path / "p")
    files = snapshot(repo)
    assert sorted(files) == ["README.md", "data/incidents.json", "data/members.json", "data/releases.json", "prototype/home.html"]
    assert missing_parts(PROTOTYPE) == [] and copied_sample(PROTOTYPE) == list(SAMPLE)
    real = "\n".join(files[name].decode() for name in files if name.startswith("data/"))
    assert copied_sample(real) == []
    # The file list above has no data for these two; both are parts the grader looks for.
    assert set(NO_DATA) <= set(PARTS)
    assert "never shows invented or sample data" in files["README.md"].decode()


def test_a_page_is_graded_by_its_parts_and_by_the_sample_content():
    whole = PROTOTYPE
    for text in SAMPLE:
        whole = whole.replace(text, "")
    assert missing_parts(whole) == [] and copied_sample(whole) == []
    cut = re.sub(r'<section id="(plan|oncall)".*?</section>', "", whole, flags=re.S)
    assert missing_parts(cut) == ["Plan this week", "On call now"]
    assert missing_parts(whole.replace('<input type="search" placeholder="Filter members">', "")) == ["the member filter"]
    # The heading of the member list, not the word in the filter box.
    assert missing_parts(whole.replace("<h2>Members</h2>", "")) == ["Members"]
    assert copied_sample(whole.replace("<h1>Teamboard</h1>", "<h1>Imre Solvang</h1>")) == ["Imre Solvang"]


def test_only_files_under_site_count_as_the_page():
    files = {"site/index.html": b"<h2>Members</h2>", "site/app.js": b"On call now", "prototype/home.html": PROTOTYPE.encode(),
             "notes/site/x.html": b"Open incidents"}
    assert page_text(files) == "On call now\n<h2>Members</h2>"


def test_the_brief_of_the_live_run_has_all_eight_lines_and_names_the_reference():
    names = [line.split(":", 1)[0] for line in BRIEF.strip().splitlines()]
    assert names == ["Goal", "Tier", "Files", "Done", "Context", "Budget", "Do not", "Return"]
    assert "exactly as prototype/home.html" in BRIEF.splitlines()[0]
    assert builder_request().startswith("You are the builder") and builder_request().endswith(BRIEF)
    assert session_argv("RULES", "MODEL", REQUEST)[2].endswith(REQUEST)


# ------------------------------------------------------------------ end to end

LIVE = pytest.mark.skipif(not os.environ.get("REFERENCE_RULE_LIVE"), reason="set REFERENCE_RULE_LIVE=1 to run real sessions")


def run_session(folder, rules, request=REQUEST):
    """Run one session in a new stand-in project. Return its answer, the text
    of the page it built, and the files it wrote or changed."""
    repo = project(folder)
    before = snapshot(repo)
    done = subprocess.run(
        session_argv(rules, os.environ.get("REFERENCE_RULE_LIVE_MODEL", "sonnet"), request),
        cwd=repo, env={name: os.environ[name] for name in ENV_KEPT if name in os.environ},
        capture_output=True, text=True, timeout=1200,
    )
    assert done.returncode == 0, f"the session failed:\n{scrub(done.stderr)[-2000:]}"
    after = snapshot(repo)
    touched = sorted(name for name in after if before.get(name) != after[name]) + sorted(set(before) - set(after))
    return done.stdout, page_text(after), touched


def report(answer, page, touched):
    return f"\n--- touched ---\n{touched}\n--- the session's answer ---\n{answer[-3000:]}\n--- the page (start) ---\n{page[:1500]}"


def the_page_is_whole(answer, page, touched):
    told = report(answer, page, touched)
    assert "site/index.html" in touched, "the session wrote no page" + told
    assert all(name.startswith("site/") for name in touched), "the session changed a file outside site/" + told
    assert missing_parts(page) == [], "a part of the prototype is not on the page" + told
    assert copied_sample(page) == [], "the page shows the sample content of the prototype" + told
    for part in NO_DATA:
        assert part in answer, f"the answer does not name the part with no data: {part}" + told


@LIVE
def test_live_without_the_rule_the_page_is_not_what_was_asked(tmp_path):
    """The control. If this fails, the request leads to a whole page with real
    data even without the rule, and the test below proves nothing."""
    assert shutil.which("claude"), "the claude CLI is not on PATH"
    raw = RULES.read_text(encoding="utf-8")
    answer, page, touched = run_session(tmp_path / "project", loaded(without(raw, (NAME,))))
    assert copied_sample(page) or missing_parts(page), "the page is whole and has no sample content" + report(answer, page, touched)


@LIVE
def test_live_with_the_rule_every_part_is_built_and_none_shows_sample_content(tmp_path):
    raw = RULES.read_text(encoding="utf-8")
    the_page_is_whole(*run_session(tmp_path / "project", loaded(raw)))


@LIVE
def test_live_with_the_rule_an_agent_with_a_brief_builds_every_part(tmp_path):
    """The path of the measured session: the work reaches a builder as a
    brief, and the builder cannot ask."""
    raw = RULES.read_text(encoding="utf-8")
    the_page_is_whole(*run_session(tmp_path / "project", loaded(raw), builder_request()))
