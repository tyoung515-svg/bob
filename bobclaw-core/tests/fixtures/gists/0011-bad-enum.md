---
id: gist-0011-bad-enum
title: Violation fixture with an invalid criticality enum
criticality: urgent
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

Violation fixture: `criticality: urgent` is not in the {security, fix, capability}
enum. Default-FAIL on an unknown enum value.

## Contract

- A contract sentence.

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
