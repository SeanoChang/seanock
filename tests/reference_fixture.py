"""A page that a live session built from the stand-in project of test_reference_rule.py.

The session had the builder's instructions, a brief ("the home page of the
app in site/index.html, exactly as prototype/home.html") and the rules as
they were before the reference rule. It wrote this page on 2026-11-17, and
its report said: "Dropped sections: 'On call now' and 'Plan this week' are
gone. No file in data/ backs them, so keeping them would mean showing
invented content."

The page is kept byte for byte. It is what a reviewer gets in the live tests
of test_reference_rule.py: five of the seven parts of the prototype, with
real data, and the two parts with no data left out.
"""

DROPPED_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Teamboard</title>
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
  footer { padding: 12px 24px; color: #666; border-top: 1px solid #ddd; }
  .empty, .error { color: #666; }
</style>
</head>
<body>
<header>
  <h1>Teamboard</h1>
  <input id="filter" type="search" placeholder="Filter members">
</header>
<main>
  <div>
    <section id="members">
      <h2>Members</h2>
      <div class="cards" id="member-cards"></div>
    </section>
    <section id="releases" style="margin-top:24px">
      <h2>Releases</h2>
      <table>
        <thead><tr><th>Version</th><th>Date</th><th>Notes</th></tr></thead>
        <tbody id="release-rows"></tbody>
      </table>
    </section>
  </div>
  <div>
    <section id="incidents">
      <h2>Open incidents</h2>
      <ul id="incident-list"></ul>
    </section>
  </div>
</main>
<footer>Teamboard reads its data from the data/ folder.</footer>
<script>
  // Page is served from the project root at /site/index.html; data lives at /data/.
  function load(name) {
    return fetch("../data/" + name + ".json").then(function (r) {
      if (!r.ok) throw new Error(name + ": HTTP " + r.status);
      return r.json();
    });
  }
  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }
  function note(parent, text, cls) {
    parent.appendChild(el("p", text, cls));
  }

  var members = [];
  var cards = document.getElementById("member-cards");
  function renderMembers() {
    var q = document.getElementById("filter").value.trim().toLowerCase();
    cards.replaceChildren();
    var shown = members.filter(function (m) {
      return (m.name + " " + m.role).toLowerCase().indexOf(q) !== -1;
    });
    shown.forEach(function (m) {
      var c = el("div", undefined, "card");
      c.appendChild(el("b", m.name));
      c.appendChild(document.createElement("br"));
      c.appendChild(el("small", m.role));
      cards.appendChild(c);
    });
    if (!shown.length) note(cards, "No members match.", "empty");
  }
  document.getElementById("filter").addEventListener("input", renderMembers);

  load("members").then(function (d) { members = d; renderMembers(); })
    .catch(function (e) { note(cards, "Could not load members (" + e.message + ").", "error"); });

  load("releases").then(function (d) {
    var body = document.getElementById("release-rows");
    d.forEach(function (r) {
      var tr = document.createElement("tr");
      [r.version, r.date, r.notes].forEach(function (v) { tr.appendChild(el("td", v)); });
      body.appendChild(tr);
    });
    if (!d.length) { var tr = document.createElement("tr"), td = el("td", "No releases.", "empty"); td.colSpan = 3; tr.appendChild(td); body.appendChild(tr); }
  }).catch(function (e) {
    var body = document.getElementById("release-rows");
    var tr = document.createElement("tr"), td = el("td", "Could not load releases (" + e.message + ").", "error");
    td.colSpan = 3; tr.appendChild(td); body.appendChild(tr);
  });

  load("incidents").then(function (d) {
    var list = document.getElementById("incident-list");
    d.forEach(function (i) { list.appendChild(el("li", i.title)); });
    if (!d.length) list.appendChild(el("li", "No open incidents.", "empty"));
  }).catch(function (e) {
    document.getElementById("incident-list").appendChild(el("li", "Could not load incidents (" + e.message + ").", "error"));
  });
</script>
</body>
</html>
'''
