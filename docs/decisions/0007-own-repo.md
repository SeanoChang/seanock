---
title: The harness lives in its own repo
date: 2026-11-13
type: analysis
status: current
---

# 0007 The harness lives in its own repo

The harness moved out of the terminal setup repo
([ghostty-tmux-nvim](https://github.com/SeanoChang/ghostty-tmux-nvim)) on
2026-11-13, at that repo's commit `ba0b418`. This repo is a clone with the full
history, so the commit ids in older notes and pull requests still resolve here.
The paths under `config/` did not change, and the installer keeps the names of
its render and backup folders, so a machine switches repos by running
`install.sh` from here.

Rejected: staying in the terminal setup repo. There a release tag needed a
prefix, and CI ran the harness tests for a change to an editor setting. A
person who wanted only the harness had to take the terminal setup too.
Rejected: a new repo with no history, which would break every commit id in
`docs/known-limits.md` and in the decision records.
