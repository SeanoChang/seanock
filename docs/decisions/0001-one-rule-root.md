---
title: One rule root for both harnesses
date: 2026-11-11
type: analysis
status: current
---

# 0001 One rule root for both harnesses

`config/claude/CLAUDE.md` is the only place where a global rule is written.
`config/codex/AGENTS.md` is built from it by `config/codex/build_agents_md.py`.
The build stops when a rule has no decision for Codex, or when an adapted rule
changed.

Rejected: two rule files kept by hand. They drift, and nothing shows the drift.
