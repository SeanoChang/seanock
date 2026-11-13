---
title: Review depth follows the diff, and a hook checks each merge
date: 2026-11-10
type: analysis
status: current
---

# 0003 Review depth follows the diff, and a hook checks each merge

A change gets tier 0, 1 or 2 from what it touches, and the diff can raise the
tier. Tier 2 needs a verifier pass and an adversary that reports held. The
`merge-gate.py` hook lets `gh pr merge` through only when CI is green and a
review line names the head commit.

Rejected: a hook that looks for a merge inside any shell command. It failed
review three times on one bug class, because it needs a full model of bash. The
hook is a check on the normal path. It is not a security boundary.
