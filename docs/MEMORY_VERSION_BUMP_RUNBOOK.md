---
owner: personal
---

# Memory Module — Version-Bump Runbook

## 1. When to bump

Bump one of the version constants (`_EXTRACTOR_VERSION` / `_PROMPT_VERSION`) in `bobclaw-core/core/memory/extractor.py:21-22` when any of these trigger events occurs:

- [ ] The `_EXTRACTION_PROMPT_TEMPLATE` string (line 32) changes by even one character, whitespace, or punctuation mark.
- [ ] The `_dedup_and_build_facts` method (lines 164-212) changes the set of inputs fed to `compute_input_hash`.
- [ ] The output JSON schema in the prompt instructions changes (e.g., a new field is added to the expected JSON shape).
- [ ] A new field is added to `Fact.body` that the extractor populates.
- [ ] The `_GENERATION_METHOD` string is repurposed or split into domain-specific methods (future rename; bump both).

## 2. What to bump

| Trigger | Bump |
|---|---|
| Template text change (even 1 char) | `_PROMPT_VERSION` |
| Dedup hash-input logic change | `_EXTRACTOR_VERSION` |
| Both template AND logic change | Both |
| JSON schema / Fact.body field change | Both |
| `_GENERATION_METHOD` change | `_EXTRACTOR_VERSION` (and `_PROMPT_VERSION` if template changed) |

Convention: bump by incrementing the suffix — `"v1"` → `"v2"` → `"v3"`. No semantic versioning needed; the constant is a dedup-salt trigger, not a release artifact.

## 3. How to bump

Edit `bobclaw-core/core/memory/extractor.py:21-22`. Current values (as of INT-3):

```python
_EXTRACTOR_VERSION = "v1"   # line 21
_PROMPT_VERSION = "v1"      # line 22
_GENERATION_METHOD = "extract_facts_from_event"  # line 23
```

Change the relevant constant(s). Example for a template-only change:

```python
_EXTRACTOR_VERSION = "v1"
_PROMPT_VERSION = "v2"       # bumped for prompt template update
_GENERATION_METHOD = "extract_facts_from_event"
```

## 4. What happens after bump

Event-level dedup hashes are computed from `compute_input_hash(_GENERATION_METHOD, inputs)` where `inputs` includes `_EXTRACTOR_VERSION` and `_PROMPT_VERSION`. A bump changes the hash, so the next agent_turn that triggers extraction will re-extract even if the event body matches a prior turn.

**Operational consequence:** one turn's worth of LLM extraction cost is paid for the re-extracted event. This is intentional and harmless — it keeps the fact set current.

**Historical L1 facts** in the FactStore remain tagged with the old version hash. They coexist with new version facts. No automatic re-hashing of old facts occurs. If a downstream query returns both, the older facts' `input_hash` will not match the new version's hash — this is fine; the hash is only used for dedup at extraction time, not for retrieval.

## 5. Pre-merge checklist

Before merging a PR that bumps a version constant:

- [ ] Confirm new or updated tests cover the behavior that motivated the bump (the changed template or logic).
- [ ] Note in the PR description **which** constant bumped and **why** (e.g., `_PROMPT_VERSION v1→v2: extraction prompt now asks for confidence_score as a float`).
- [ ] Flag for the reviewer that historical L1 facts tagged with the old version hash will NOT be re-extracted automatically — they remain durable in the FactStore.
- [ ] Run `pytest bobclaw-core/tests/memory -q` and confirm no regressions (INT-3 baseline: 382 passed).
- [ ] Run `pytest bobclaw-core -q` and confirm no regressions (INT-3 baseline: 713 passed, 6 skipped).
- [ ] If the bump is logic-only (no template change), verify the `_dedup_and_build_facts` method's hash inputs match the `_EXTRACTOR_VERSION` scope.

## 6. Migration of old facts (if needed)

**No automated migration script exists as of INT-3.** Old facts coexist with new facts in the FactStore.

If the new version produces meaningfully different facts (e.g., a new schema field that the old extraction didn't populate), and those old facts need to be re-extracted, the recommended manual process is:

1. Delete the stale facts from the FactStore (SQL: `DELETE FROM memory_facts WHERE generation_method = '<method>'` — careful: this also removes them from Qdrant; verify Qdrant scroll returns zero before proceeding).
2. Delete corresponding Qdrant points by scrolling the collection and deleting stale-source points.
3. Trigger re-extraction by running the affected agent turns through the graph again (or by a dedicated replay script if one is written in a future sprint).

**Until a replay script exists, the canonical stance is:** old facts coexist. If a fact's version is meaningfully out of date, a future dedup or rollup stage can filter by `input_hash` prefix or `generation_method` at query time.
