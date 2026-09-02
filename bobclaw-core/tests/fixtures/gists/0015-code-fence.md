---
id: gist-0015-code-fence
title: Violation fixture with a language-tagged code fence
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

Violation fixture: the body carries a python-tagged code fence, which poisons the
clean room (D2 code-free rule). A yaml/json/text data fence would be allowed.

## Contract

The behavior is described by this snippet:

```python
def assemble(prompt):
    return "CARD\n" + prompt
```

## Invariants

1. An invariant.

## Acceptance

1. An acceptance criterion.

## Scope fence

- An anti-goal.
