# Changing fact extractor versions

This runbook concerns the optional fact extractor, not runtime image versions,
database migrations, embedding dimensions or the verbatim projection writer.
New runtime lanes leave fact extraction disabled.

The constants in `bobclaw-core/core/memory/extractor.py` are currently
`_EXTRACTOR_VERSION = "v2"`, `_PROMPT_VERSION = "v1"` and
`_GENERATION_METHOD = "extract_facts_from_event"`. Read the current code before
making a change rather than treating these values as permanent.

## Which constant to change

| Change | Version to increment |
| --- | --- |
| Extraction prompt text | `_PROMPT_VERSION` |
| Canonical extraction input or dedup identity logic | `_EXTRACTOR_VERSION` |
| Output schema or behavior involving both | Review and update both |
| Generation-method identity | Review the hash allowlist and all method-filtered readers as well as versions |

These strings participate in the hash computed after extraction from the
canonical user/assistant text, event kind and versions. Use the next version
suffix and update tests describing the changed behavior. If the hash inputs
change, also review `core/memory/_hashing.py` and its allowed input keys.

## What changes in storage

A version change allows a different input hash on a later extraction. It does
not schedule historical events for replay, rewrite old hashes or migrate stored
facts. The model call already precedes the hash check. Existing normalized fact
text can still suppress an otherwise new-version candidate. A bump therefore
does not guarantee a new stored copy of every old fact.

Do not delete rows from `memory_facts` with raw SQL to force a replay: SQLite
deletion alone does not clean up Qdrant points. Do not replay live agent turns
just to regenerate memory; turns can invoke tools and providers again. Leave old
facts in place unless a separate reviewed migration coordinates facts, vectors
and provenance using a tested backup.

## Review and verification

Describe which constant changed, the behavior it identifies and the impact on
existing facts in the change description. Add tests for the new identity/output
behavior and preserve the distinction between event-level and text-level dedup.

From `bobclaw-core`, using the development environment:

```bash
../.venv/bin/python -m pytest tests/memory -q
../.venv/bin/python -m pytest tests -q
```

Report the actual result and skips. The
[live-memory guide](MEMORY_LIVE_SMOKE_RUNBOOK.md) describes the limits of the
integration fixture; it is not a production migration procedure.
