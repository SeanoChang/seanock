---
title: The lead session builds dependent work
date: 2026-11-10
type: analysis
status: current
---

# 0002 The lead session builds dependent work

The main session writes the design, the code and every fix after a review. A
worker gets a piece only when the piece has its own context: its own files and
its own check, a wide read, or a review that needs a fresh view.

Rejected: one worker per role for every task. Each handoff loses detail, and
dependent steps then wait on each other.
