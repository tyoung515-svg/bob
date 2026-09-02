---
id: gist-0013-capability-path
title: Violation fixture with a path-named capability
criticality: capability
verification_class: spec-conformance
transfer: shaped
distribution: public
intended: [bobclaw]
provenance:
  source_tree: bob
  commit: abc1234
requires:
  capabilities: [core/nodes/route.py]
status: shaped
---

## Intent

Violation fixture: `requires.capabilities` contains `core/nodes/route.py`, a path-named
token (contains '/' and '.py'). Capabilities are named, never path-named.

## Contract

- A contract sentence.

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
