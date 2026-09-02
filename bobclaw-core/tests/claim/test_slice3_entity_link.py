"""MS#4 · V1 claim-model — Slice 3 unit tests (subject entity-linking, gated).

PURE. Entity-linking recovers subject-drift recall misses, but ONLY when it passes its own
precision gate (must not merge known-distinct subjects). Pins the Slice-3 arc on the frozen
goldset: recall lifts past Slice 1 WHILE merge_rate stays 0.
"""
import pytest

from core.claim.v2.canonical import canonical_key
from core.claim.v2.entity_link import (
    entity_norm_version,
    link_precision_ok,
    link_subject,
    linked_key,
)
from core.ses import extraction_recall, load_goldset, merge_rate_breakdown


def test_link_subject_aliases_and_passthrough():
    assert link_subject("d3400") == "xs_power_d3400"
    assert link_subject("the_bms") == "bms"
    assert link_subject("amp") == "sundown_amp"
    assert link_subject("unknown_thing") == "unknown_thing"   # unknown → normalized passthrough


def test_linked_key_recovers_subject_drift_but_not_over_merge():
    g = {"subject": "sundown_amp", "predicate": "require", "object": "four_gauge_wire", "numeric_value": None}
    e = {"subject": "amp", "predicate": "require", "object": "four_gauge_wire", "numeric_value": None}
    assert linked_key(g) == linked_key(e)                     # amp → sundown_amp: recovered
    # distinct subjects with no alias must NOT collapse
    a = {"subject": "series_wiring", "predicate": "increase", "object": "voltage", "numeric_value": None}
    b = {"subject": "parallel_wiring", "predicate": "increase", "object": "voltage", "numeric_value": None}
    assert linked_key(a) != linked_key(b)


def test_link_precision_gate_holds_on_frozen_set():
    assert link_precision_ok(load_goldset()["distinct"]) is True
    assert entity_norm_version().startswith("ev1-")


def test_slice3_recall_arc_with_precision_held():
    gs = load_goldset()
    gold, extracted, distinct = gs["gold"], gs["extracted"], gs["distinct"]
    s1 = extraction_recall(gold, extracted, key=canonical_key)["recall"]
    s3 = extraction_recall(gold, extracted, key=linked_key)["recall"]
    m3 = merge_rate_breakdown(distinct, key=linked_key)

    assert s1 == pytest.approx(8 / 12)          # Slice 1 (predicate lemma)
    assert s3 == pytest.approx(10 / 12)         # Slice 3 adds subject-drift recovery → 0.833
    assert s3 > s1                              # entity-linking strictly recovers more
    assert m3["merge_rate"] <= 0.0             # precision gate holds throughout
