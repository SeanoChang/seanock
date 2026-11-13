---
title: Task work merges into an integration branch, never a release branch
date: 2026-11-10
type: analysis
status: current
---

# 0004 Task work merges into an integration branch, never a release branch

Task pull requests go to the first of `dev`, `develop`, `development` or
`integration`. The hook refuses `main`, `master` and `trunk` under any
setting. You merge the release pull request yourself.

Rejected: letting an agent merge into the release branch after a review. One
wrong merge there is hard to take back.
