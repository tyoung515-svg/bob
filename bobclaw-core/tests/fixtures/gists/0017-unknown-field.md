---
id: gist-0017-unknown-field
title: Violation fixture with an unknown frontmatter field
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
priority: high
---

## Intent

Violation fixture: `priority` is not in the §3 schema. Default-FAIL posture rejects an
unknown frontmatter field rather than warning.

## Contract

- A contract sentence.

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
