# Fact extraction deduplication

This describes the optional fact extractor in
`bobclaw-core/core/memory/extractor.py`. It is separate from the verbatim turn
projection and recall used by new runtime lanes. Those lanes disable fact
extraction, and their `extract_small` slot is deferred.

## What the implementation does

For an `agent_turn`, the extractor resolves `extract_small`, calls the configured
OpenAI-compatible backend selected as `lmstudio`, parses fact JSON, then applies
two duplicate filters:

1. An input hash covers the canonical `user_message` and `assistant_response`,
   event kind, extractor version and prompt version. Volatile event metadata
   does not participate. If a stored fact from this generation method has the
   same hash, no new facts are returned.
2. Existing facts from `extract_facts_from_event` supply a normalized text set.
   Normalization lowercases, collapses whitespace and strips outer ASCII
   punctuation. A candidate matching that set is omitted.

The model call happens **before** the hash check. This deduplication prevents
some duplicate fact records; it does not avoid the model call on repeated input.
Existing texts are collected once, so two identical candidates in one newly
extracted batch are not added to that set as they are emitted.

## Limits

Normalized equality is not semantic equivalence. Rephrased claims can both
survive; punctuation/case distinctions can be lost. The implementation does not
perform Unicode normalization or reconcile contradictory claims. It queries the
fact store before building candidates; this function alone is not a transaction
or a cross-process uniqueness guarantee.

Version changes alter the event-input hash but leave historical facts and their
text in place. The normalized-text filter can still suppress a repeated fact
across versions. See [version changes](MEMORY_VERSION_BUMP_RUNBOOK.md).
The event log and verbatim writer have separate identity/completion behavior;
do not infer their guarantees from the fact extractor.

## Checks

From `bobclaw-core`, using the development environment in
[CONTRIBUTING.md](../CONTRIBUTING.md):

```bash
../.venv/bin/python -m pytest tests/memory/test_extractor_dedup_identity.py \
  tests/memory/test_extractor_per_fact_dedup.py -q
```

These tests use supplied responses. They do not establish the quality of a live
extraction model.
