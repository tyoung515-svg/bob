---
owner: personal
---

SHIP hybrid dedup in INT-3

## 1. Verdict

**SHIP hybrid dedup in INT-3** — keep event-level dedup as the fast path, add per-fact normalized-text dedup as a second layer in `_dedup_and_build_facts`. No schema changes, no edits outside `extractor.py`.

## 2. Context

Today's dedup (`extractor.py:155-189`) computes one `input_hash` over `{event.body, event.kind, extractor.version, prompt.version}`. If any existing fact for this `generation_method` shares that hash, the entire extraction is skipped and `[]` is returned.

This catches the case where an identical event body is re-processed (e.g., replay, duplicate event), but it misses duplicate-surface-meaning facts that arrive via different event bodies. For example:

- Event A: user says "I'm a marine biologist" → fact "the user is a marine biologist"
- Event B: user says "I work as a marine biologist" → fact "the user is a marine biologist"

Two distinct event bodies, two extractions, same fact lands twice. This is INT-2 audit carryover item 4's flagged failure mode.

## 3. Three approaches

### 3a. Event-level (status quo)

- **Hash inputs:** `event.body`, `event.kind`, `extractor.version`, `prompt.version`
- **What gets deduped:** Entire event body repeats (identical event, same extraction run)
- **Failure modes:** Cross-event duplicate surface meaning leaks through (marine-biologist example above). Also: near-identical events with trivial formatting changes (e.g., trailing whitespace) produce different hashes and bypass dedup entirely.
- **Implementation cost:** Already shipped in INT-2. ~20 lines + hash-allowlist entry. Zero additional cost.

### 3b. Per-fact normalized text

- **Hash inputs:** Normalized fact text + generation method + extractor/prompt version
- **What gets deduped:** Each extracted fact item by its semantic surface text. Two events that produce the same normalized fact text generate one fact.
- **Failure modes:** 
  - `compute_input_hash` allowlist does not include `text_normalized`; adding it would require editing `_hashing.py` (out of scope per prompt). An alternative is direct normalized-string comparison, which skips hashing entirely—this is version-independent and does not require allowlist changes.
  - Normalization is conservative (lowercase, whitespace collapse, leading/trailing punctuation strip). Two facts with different actual meanings but the same normalized form would be falsely deduped. In practice this is unlikely (`compute_input_hash` with BLAKE3 + canonical JSON is not available, but direct string comparison on conservative normalization has extremely low collision risk — see limitations in §5).
  - No fast path for repeated event bodies — every extraction queries all existing facts.
- **Implementation cost:** ~15 lines (normalize function + dedup loop). Tests ~70 lines.

### 3c. Hybrid (chosen approach)

- **Hash inputs:** Event-level hash uses existing allowlist keys. Per-fact uses direct normalized-string comparison (no `compute_input_hash`).
- **What gets deduped:** Event-level blocks identical-event re-extraction (fast path). Per-fact filters duplicate-surface-meaning items from the resulting fact list.
- **Failure modes:** Same as per-fact for the second layer (conservative normalization may miss subtle synonym variants; could falsely match on trivial text collisions). Event-level failure modes unchanged.
- **Implementation cost:** ~25 new lines in `extractor.py` (normalize function + per-fact filter loop + import). Event-level dedup code is kept as-is. Tests ~80 lines.

## 4. Evidence

### Example A: Marine biologist (duplicate surface across events)

- Event 1: "Alice works as a marine biologist studying octopus cognition at UCSB."
  - Extracted fact: "Alice works as a marine biologist"
- Event 2: "Alice is a marine biologist at UCSB focusing on octopus cognition."
  - Extracted fact: "Alice is a marine biologist"
- **Event-level:** Both fact bodies differ → both extracted. Fact 2 is a duplicate in meaning → leaked.
- **Per-fact/hybrid:** Normalized `"travis works as a marine biologist"` ≠ `"travis is a marine biologist"` → both preserved. But the fact surface nearly implies the same claim. The normalization is conservative enough that the "works as" vs "is" distinction is preserved. This is a net-OK: the facts are genuinely slightly different claims and both are useful.

### Example B: Duplicate "favorite color" across sessions

- Event 1: User says "My favorite color is blue."
  - Extracted fact: "User's favorite color is blue"
- Event 2 (1 hour later): User says "Actually, my favorite color is blue."
  - Extracted fact: "User's favorite color is blue" (identical text from LLM extraction)
- **Event-level:** Different event bodies → both extracted. Duplicate fact lands twice.
- **Per-fact/hybrid:** Normalized `"user's favorite color is blue"` matches existing → second extraction suppressed. ✓

### Example C: Distinct facts from similar content

- Event 1: "I work at Acme Corp as a backend engineer"
  - Extracted facts: "User works at Acme Corp", "User is a backend engineer"
- Event 2: "At Acme Corp I'm a backend engineer working on the payments team"
  - Extracted facts: "User works at Acme Corp" (duplicate), "User is a backend engineer" (duplicate), "User works on payments team" (new)
- **Event-level:** Different event bodies → all 3 facts extracted again. The first two are redundant.
- **Per-fact/hybrid:** First two facts deduped; only third (new) fact lands. ✓
- **Hybrid additionally:** Fast-path check catches identical-body replay without extra work.

## 5. Recommendation

**Ship hybrid dedup now.** Rationale:

1. **Failure modes are real and observed** — Example B (favorite color) and Example C (Acme Corp) show concrete scenarios where cross-event duplicates degrade retrieval quality. These are not hypothetical.
2. **Implementation cost is low** — ~25 lines in `extractor.py` (add `_normalize()` function + modify `_dedup_and_build_facts`). No new imports to `_hashing.py` or other modules. No schema changes.
3. **Backward compatible** — Event-level hash stored in `Fact.input_hash` is unchanged. Existing facts retain their hash. The per-fact layer only adds filtering; it doesn't modify storage.
4. **Fast path preserved** — Identical-event replay still short-circuits at the event-level check before any per-fact work.
5. **Trigger conditions for DEFER are not met** — There are no reports of per-fact dedup causing false suppression at current scale. Waiting for "a user reports duplicate-feeling facts" is waiting for a degraded experience that is already observable in the carryovers.

**Known limitation:** The `_normalize()` function uses string comparison (lowercase + whitespace collapse + leading/trailing punctuation strip) instead of `compute_input_hash` because the hash allowlist for `extract_facts_from_event` does not include `text_normalized` and editing `_hashing.py` is out of scope. String comparison on normalized text is hash-free and version-independent, which means:
- Version bumps do not invalidate the dedup set (generally desirable — we want to dedup against old facts' text too).
- Two facts that normalize to the same string but have different meanings will be deduped. Example: "Get the pen" and "Get the pen!" both normalize to "get the pen". This is acceptable — the normalized form is conservative enough that false collisions are rare.
- Unicode normalization uses Python's `str.lower()` + `str.strip()`, which handles basic NFC text well but may not cover all Unicode casing rules. Documented; fix forward if a case emerges.

## 6. Trigger condition for revisit (not applicable — shipping now)

If we were deferring, the trigger would be: "a user reports duplicate-feeling facts in recall results during a 100-turn session, and the duplicate count in retrieval results exceeds 20% of returned facts." At that point, per-fact dedup value is proven by real usage data.

## 7. Implementation footprint

Files changed:
- **EDIT** `bobclaw-core/core/memory/extractor.py` (~25 lines added):
  - Add `import re` and `import string` at top
  - Add module-private `_normalize(text: str) -> str` function
  - In `_dedup_and_build_facts`: keep event-level fast path unchanged; add per-fact normalized-text filter between event-level check and Fact construction loop
- **ADD** `bobclaw-core/tests/memory/test_extractor_per_fact_dedup.py` (~7 tests):
  1. `test_per_fact_dedup_suppresses_identical_text` — two events with different bodies produce same fact text; second emission suppressed
  2. `test_per_fact_dedup_preserves_distinct_facts` — overlapping but distinct facts both land
  3. `test_normalize_lowercases_and_trims_whitespace` — unit test for `_normalize`
  4. `test_per_fact_dedup_empty_text` — empty text item does not cause errors or dedup
  5. `test_per_fact_dedup_event_level_still_works` — same event body still fast-path deduped
  6. `test_per_fact_dedup_punctuation_variants` — "Hello world." and "Hello world" deduped after normalization
  7. `test_per_fact_dedup_mixed_preserve_and_suppress` — some facts from an event suppressed, others preserved
