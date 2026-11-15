---
title: Every pull request into dev is a version
date: 2026-11-15
type: analysis
status: current
---

# 0008 Every pull request into dev is a version

Each pull request into `dev` raises the middle number of `VERSION` by one and
sets the last number to 0. It adds its own entry at the top of the changelog,
with its date, and its title starts with the number: `0.4.0: One version per
pull request`. You chose this on 2026-11-15. A pull request that you name as
a major step takes the next first number in place of the next middle one
(1.0.0 after 0.9.0).

This replaces two parts of [record 0006](0006-versions.md). The middle number
no longer means "something was added": it counts pull requests, and the
changelog entry of a version says what it holds. And the last number no
longer means "a fix": a fix is a pull request like any other and takes the
next middle number. The rest of 0006 stands: one number for the whole
harness, what counts as a major step, the tag `vX.Y.Z` on `main`, and
`metadata.version` on a skill that leaves the machine as a copy.

A release pull request from `dev` to `main` changes no number. `main` gets the
number that `dev` holds, and that number gets the tag. The numbers between two
releases have a changelog entry and no tag.

The cost: every pull request changes `VERSION` and the top of the changelog.
So two open pull requests cannot both hold the next number: the one that
merges later is rebased onto `dev` and takes the number after it. The last
number has no use in this flow and stays 0.

Rejected: the number of the release at the start of each title, with one
number per release as before. Two pull requests of one release then carry the
same number. Rejected: the last number per pull request (0.3.1, 0.3.2) and the
middle number per release, which is closer to semantic versioning; you asked
for the middle number.
