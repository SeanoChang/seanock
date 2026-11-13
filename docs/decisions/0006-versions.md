---
title: One semver number for the harness, by contract
date: 2026-11-13
type: analysis
status: current
---

# 0006 One semver number for the harness, by contract

The harness has one version in `VERSION` at the repo root, for the rules, the
workers, the hooks and the skills in this repo. The number follows a contract:
MAJOR when the architecture or a habit you rely on changes, MINOR when
something is added, PATCH for a fix. A release is a tag named `vX.Y.Z` on
`main`.

A skill that leaves the machine as a copy also carries `metadata.version` in
its frontmatter. Skills kept outside this repo are outside the version.

The first release is 0.1.0. While the major is 0, a change that would be MAJOR
goes into a MINOR release and the changelog lists it under "Changed".

Rejected: calendar versions, which do not say how large a change is. Rejected:
a version per skill, which gives one number for each skill and none for the
rules. Rejected: a first release named 1.0.0, which would promise that the
habits stay while the checks and the unattended mode are still to be built.
