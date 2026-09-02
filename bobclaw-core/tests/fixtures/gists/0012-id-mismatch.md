---
id: gist-0099-different-slug
title: Violation fixture where the id disagrees with the filename
criticality: capability
verification_class: spec-conformance
transfer: shaped
distribution: public
intended: [bobclaw]
provenance:
  source_tree: bob
  commit: abc1234
requires:
  capabilities: [some-capability]
status: shaped
---

## Intent

Violation fixture: the id says `gist-0099-different-slug` (canonical file
`0099-different-slug.md`) but this file is named `0012-id-mismatch.md`. The id↔filename
agreement rule trips.

## Contract

- A contract sentence.

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
