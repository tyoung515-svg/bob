---
id: gist-0010-missing-field
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

Violation fixture: the mandatory `title` frontmatter field is absent. Everything else
is valid so only the missing-field rule trips.

## Contract

- A contract sentence.

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
