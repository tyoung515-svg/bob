---
id: gist-0002-sample-capability
title: Sample capability used as a landing cross-check reference
criticality: capability
verification_class: spec-conformance
transfer: shaped
distribution: public
intended: [bobclaw, users]
provenance:
  source_tree: bob
  commit: deadbee
  release: v0.97.0
  results: "fixture — reference gist for test_gist_format.py"
requires:
  capabilities: [sample-capability, config-surface]
  gists: []
  baseline: ">=0.96"
status: shaped
---

## Intent

A minimal but schema-complete gist used only as the referenced gist for landing
cross-check fixtures. It declares exactly two invariants so a landing must carry two
invariant rows to satisfy the §7 cross-check.

## Contract

- When enabled, the sample capability is observable at the operator surface.
- A reference name flags renameable per tree.

## Invariants

1. Disabled produces byte-identical behavior to before the capability existed.
2. The capability is additive and never reorders existing behavior.

## Acceptance

1. Off produces byte-identical output.
2. On surfaces the capability at the first operator-observable point.

## Scope fence

- One template only; no per-face customization.
- Does not alter routing.
