"""MS#4 · RS2 meta-lane — Step 0 tests (deterministic claim schema + JSONL ledger + measures).

PURE (ledger uses a tmp file). Corpus-anchor discipline, schema recall (fail-fast metric),
corpus-anchorable go/no-go fraction, and the JSONL ledger + hard-kill termination.
"""
from core.research.metalane import (
    MetaClaim,
    MetaLedger,
    corpus_anchorable_fraction,
    is_corpus_anchor,
    schema_recall,
)


def _c(subj, pred, obj, scope="mod", ev="ev:1"):
    return MetaClaim(subject=subj, predicate=pred, object=obj, scope=scope, evidence_anchor=ev)


# ── corpus-anchor discipline (§0) ────────────────────────────────────────────

def test_is_corpus_anchor():
    assert is_corpus_anchor("core/x.py:12")          # file:line
    assert is_corpus_anchor("core/x.py:12-40")       # file:line-range
    assert is_corpus_anchor("claim:abc123")          # claim-ID ref
    assert is_corpus_anchor("bounded")               # fixed vocab
    assert not is_corpus_anchor("some free text")    # free text is NOT anchorable
    assert not is_corpus_anchor("")


def test_schema_valid_requires_closed_predicate_and_anchor():
    assert _c("A", "depends_on", "core/x.py:5").schema_valid() is True
    assert _c("A", "vibes_with", "core/x.py:5").schema_valid() is False   # predicate not closed
    assert _c("A", "depends_on", "free text").schema_valid() is False     # object not anchored


def test_claim_id_deterministic():
    assert _c("A", "entails", "core/x.py:1").claim_id == _c("A", "entails", "core/x.py:1").claim_id
    assert _c("A", "entails", "core/x.py:1").claim_id != _c("A", "refutes", "core/x.py:1").claim_id


# ── Step-0 measures ──────────────────────────────────────────────────────────

def test_schema_recall_counts_only_valid():
    gold = [_c("A", "implements", "core/x.py:1"), _c("B", "depends_on", "core/y.py:2")]
    extracted = [
        _c("A", "implements", "core/x.py:1"),        # valid + matches
        _c("B", "depends_on", "free text"),          # matches identity fields but NOT schema-valid
    ]
    assert schema_recall(gold, extracted) == 0.5     # only the first counts
    assert schema_recall([], extracted) == 0.0


def test_corpus_anchorable_fraction_go_no_go():
    claims = [_c("A", "implements", "core/x.py:1"), _c("B", "depends_on", "prose"),
              _c("C", "entails", "claim:z")]
    assert corpus_anchorable_fraction(claims) == 2 / 3
    assert corpus_anchorable_fraction([]) == 0.0


# ── JSONL ledger + hard-kill termination (ARCH-05) ───────────────────────────

def test_ledger_append_and_verified(tmp_path):
    led = MetaLedger(tmp_path / "ledger.jsonl")
    led.append(1, _c("A", "implements", "core/x.py:1"), verified=True)
    led.append(1, _c("B", "depends_on", "core/y.py:2"), verified=True)
    led.append(1, _c("C", "entails", "core/z.py:3"), verified=False)
    assert len(led.records()) == 3
    assert len(led.verified_claims(1)) == 2          # the unverified one excluded


def test_hard_kill_on_thin_generation(tmp_path):
    led = MetaLedger(tmp_path / "ledger.jsonl")
    led.append(1, _c("A", "implements", "core/x.py:1"), verified=True)   # only 1 verified → halt
    assert led.should_halt(1) is True
    # a second distinct-anchor verified claim → compounding surface exists → don't halt
    led.append(1, _c("B", "depends_on", "core/y.py:2"), verified=True)
    assert led.should_halt(1) is False
    # two claims but the SAME anchor → still halt (≤1 distinct anchor)
    led2 = MetaLedger(tmp_path / "l2.jsonl")
    led2.append(1, _c("A", "implements", "core/x.py:1"), verified=True)
    led2.append(1, _c("B", "depends_on", "core/x.py:1"), verified=True)
    assert led2.should_halt(1) is True
