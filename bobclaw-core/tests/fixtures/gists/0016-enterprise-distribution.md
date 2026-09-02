---
id: gist-0016-enterprise-distribution
title: Violation fixture — enterprise source without explicit distribution
criticality: capability
verification_class: spec-conformance
transfer: shaped
intended: [bobclaw]
provenance:
  source_tree: enterprise
  commit: abc1234
requires:
  capabilities: [some-capability]
status: shaped
---

## Intent

Violation fixture: `provenance.source_tree: enterprise` but `distribution` is not set.
D17 requires enterprise-sourced gists to declare distribution explicitly.

## Contract

- A contract sentence.

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
